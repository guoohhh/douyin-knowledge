"""The sidecar transport contract: pagination shapes, task states, health.

The sibling mapping test states that transport is "deliberately not tested here"
because "envelope parsing is trivial". That judgment is what let three contract bugs
live in this client at once, each of which failed only against the real sidecar:

* the next cursor was read from `meta["cursor"]`, which is an *object*, so it was a
  dict where a string belonged and the walk never advanced past page 1;
* `_poll_task` matched on states `pending`/`succeeded`, which the sidecar does not
  emit, so every finished task fell through to the error branch;
* `health()` probed `/api/v1/health`, which does not exist and is served by the
  console's SPA catch-all, so it returned 200 with HTML regardless of service state.

None of these could be caught by mapping tests, and all three are cheap to pin with
`httpx.MockTransport`, which needs no live sidecar. The shapes below are transcribed
from the sidecar's published OpenAPI document and REST guide (DEC-013), not from our
own fixtures -- a fixture that agrees with the client proves nothing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.core.errors import CaptureUnavailable, ValidationError


def content(content_id: str) -> dict[str, Any]:
    """A minimal dtk Content object, with only the fields mapping requires."""
    return {
        "content_id": content_id,
        "kind": "video",
        "title": f"video {content_id}",
        "description": "",
        "web_url": f"https://www.douyin.com/video/{content_id}",
        "author": {"uid": "uid-1", "nickname": "创作者"},
        "media": {},
        "stats": {},
    }


def envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """The sidecar's universal success envelope."""
    return {"success": True, "data": data, "error": None, "meta": meta or {"request_id": "r-1"}}


def build(handler) -> DouyinCaptureProvider:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://sidecar.invalid",
        headers={"Accept": "application/json"},
    )
    return DouyinCaptureProvider(
        "http://sidecar.invalid",
        api_key="dtk_test",
        # No real sleeping: the polling state machine is what is under test.
        poll_interval_s=0.0,
        poll_timeout_s=5.0,
        client=client,
    )


# ------------------------------------------------------------------- pagination


def test_next_cursor_comes_from_data_not_from_the_meta_object() -> None:
    """The regression. `meta.cursor` is `{"next", "has_more"}`; the flat cursor lives
    on `data`. Reading the meta object as the cursor is how the walk stalled."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope(
                {"items": [content("7001")], "cursor": "1757462400000", "has_more": True},
                {"request_id": "r-1", "cursor": {"next": "1757462400000", "has_more": True}},
            ),
        )

    page = build(handler).list_collection_sources("col-1")

    assert page.next_cursor == "1757462400000"
    assert isinstance(page.next_cursor, str)
    assert page.has_more is True
    assert [s.external_id for s in page.sources] == ["7001"]


def test_pagination_falls_back_to_the_meta_cursor_object() -> None:
    """The task path fills `result_meta.cursor` while `data` carries only items."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope(
                {"items": [content("7001")]},
                {"cursor": {"next": "abc", "has_more": True}},
            ),
        )

    page = build(handler).list_collection_sources("col-1")

    assert page.next_cursor == "abc"
    assert page.has_more is True


def test_last_page_reports_no_more() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=envelope({"items": [content("7001")], "cursor": None, "has_more": False})
        )

    page = build(handler).list_collection_sources("col-1")

    assert page.next_cursor is None
    assert page.has_more is False


def test_absent_pagination_fields_mean_last_page() -> None:
    """Defaulting a missing `has_more` to True would spin forever against a cursor we
    are not allowed to interpret."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"items": []}))

    page = build(handler).list_collection_sources("col-1")

    assert page.has_more is False
    assert page.next_cursor is None


def test_empty_string_cursor_is_treated_as_absent() -> None:
    """`""` is not a usable cursor: passing it back re-fetches page 1."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=envelope({"items": [content("7001")], "cursor": "", "has_more": True})
        )

    page = build(handler).list_collection_sources("col-1")

    assert page.next_cursor is None


def test_numeric_cursor_is_stringified_not_arithmetic() -> None:
    """Douyin's cursor is a millisecond timestamp. If a build emits it unquoted we
    still hand back the exact same value as a string, never a number to increment."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope({"items": [], "cursor": 1757462400000, "has_more": True}),
        )

    page = build(handler).list_collection_sources("col-1")

    assert page.next_cursor == "1757462400000"


def test_page_size_is_clamped_to_the_schema_maximum() -> None:
    """`count` above 50 is a 400 from the sidecar, so asking for 500 fails the call
    rather than returning 500 items."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=envelope({"items": [], "has_more": False}))

    build(handler).list_collection_sources("col-1", limit=500)

    assert seen["count"] == "50"


def test_wait_is_clamped_to_the_documented_ceiling() -> None:
    """Over-ceiling `wait` is rejected with 400 INVALID_PARAM rather than clamped
    server-side, so an unclamped default would fail every single call."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=envelope({"items": [], "has_more": False}))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sidecar.invalid")
    provider = DouyinCaptureProvider("http://sidecar.invalid", wait_s=600.0, client=client)
    provider.list_collection_sources("col-1")

    assert float(seen["wait"]) <= 30.0


def test_items_of_the_wrong_type_is_a_contract_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope({"items": {"nope": 1}, "has_more": False}))

    with pytest.raises(ValidationError, match="items list"):
        build(handler).list_collection_sources("col-1")


def test_an_overlong_cursor_is_rejected() -> None:
    """>512 chars cannot have come from this API, and feeding it back would produce a
    confusing upstream 400 instead of naming the real problem."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=envelope({"items": [], "cursor": "x" * 600, "has_more": True})
        )

    with pytest.raises(ValidationError, match="512"):
        build(handler).list_collection_sources("col-1")


# ----------------------------------------------------------- folder list walking


def test_list_collections_walks_every_page() -> None:
    """Reading only page 1 caps the user at their most recent folders and makes every
    item inside the rest invisible -- an empty-looking library, not an error."""
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        calls.append(cursor)
        if cursor is None:
            return httpx.Response(
                200,
                json=envelope(
                    {
                        "items": [{"collection_id": "c1", "name": "茶餐厅"}],
                        "cursor": "p2",
                        "has_more": True,
                    }
                ),
            )
        return httpx.Response(
            200,
            json=envelope(
                {"items": [{"collection_id": "c2", "name": "日料"}], "has_more": False}
            ),
        )

    collections = build(handler).list_collections()

    assert [c.external_collection_id for c in collections] == ["c1", "c2"]
    assert calls == [None, "p2"]


def test_list_collections_rejects_a_repeated_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=envelope(
                {"items": [{"collection_id": "c1", "name": "茶餐厅"}], "cursor": "same", "has_more": True}
            ),
        )

    with pytest.raises(ValidationError, match="repeated a folder-list cursor"):
        build(handler).list_collections()


# -------------------------------------------------------------------- task path


def test_a_202_is_polled_to_completion_not_treated_as_failure() -> None:
    """An early 202 under the wait ceiling is normal and the work is still queued
    under that id. Failing here would resubmit and spend a second identity."""
    states = iter(["running", "done"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/posts"):
            return httpx.Response(202, json=envelope({"task_id": "t-1", "state": "queued"}))
        state = next(states)
        if state != "done":
            return httpx.Response(200, json=envelope({"state": state}))
        return httpx.Response(
            200,
            json=envelope(
                {
                    "state": "done",
                    # A finished task nests one level deeper, and puts pagination in a
                    # sibling `result_meta` rather than the envelope's `meta`.
                    "data": {"items": [content("7001")], "cursor": "next-1", "has_more": True},
                    "result_meta": {"cursor": {"next": "next-1", "has_more": True}},
                }
            ),
        )

    provider = build(handler)
    page = provider.list_collection_sources("col-1")

    assert [s.external_id for s in page.sources] == ["7001"]
    assert page.next_cursor == "next-1"
    assert page.has_more is True


def test_a_failed_task_is_an_error_despite_a_success_envelope() -> None:
    """The trap: a failed task is HTTP 200 with `success: true`. The envelope
    describes the lookup, not the work, so a client that stops there reports every
    failed capture as an empty success."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/posts"):
            return httpx.Response(202, json=envelope({"task_id": "t-1", "state": "queued"}))
        return httpx.Response(
            200,
            json=envelope(
                {
                    "state": "failed",
                    "error": {"code": "UPSTREAM_RISK_CONTROL", "message": "blocked", "retryable": True},
                }
            ),
        )

    provider = build(handler)
    with pytest.raises(CaptureUnavailable):
        provider.list_collection_sources("col-1")


def test_an_unknown_task_state_is_not_silently_a_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/collection/posts"):
            return httpx.Response(202, json=envelope({"task_id": "t-1", "state": "queued"}))
        return httpx.Response(200, json=envelope({"state": "teleported"}))

    provider = build(handler)
    with pytest.raises(CaptureUnavailable, match="teleported"):
        provider.list_collection_sources("col-1")


# ------------------------------------------------------------------------ health


def test_health_probes_readyz() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "ok", "components": {}})

    health = build(handler).health()

    assert paths == ["/readyz"]
    assert health.ok is True
    assert health.detail == "ok"


def test_health_is_not_ok_when_a_component_is_down() -> None:
    """`/readyz` answers 200 with per-component detail. A sidecar whose datastore is
    down will accept our request and then fail it, so 200 alone is not readiness."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "components": {"redis": {"ok": True}, "postgres": {"ok": False}},
            },
        )

    health = build(handler).health()

    assert health.ok is False
    assert "postgres" in (health.detail or "")


def test_health_rejects_an_html_answer() -> None:
    """The old bug: `/health` does not exist and falls through to the console's SPA
    catch-all, which answers 200 with HTML. A probe that cannot fail is not a probe."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html><title>console</title>")

    health = build(handler).health()

    assert health.ok is False
    assert "non-JSON" in (health.detail or "")


def test_health_reports_an_unreachable_sidecar() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    health = build(handler).health()

    assert health.ok is False
    assert "cannot reach sidecar" in (health.detail or "")

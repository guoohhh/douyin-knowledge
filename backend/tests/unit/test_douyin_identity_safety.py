"""Account-scoped Douyin reads must never let DTK pick an identity."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.core.errors import AuthenticationRequired, SourceUnavailable


def _provider(
    identity: str | None, handler: Callable[[httpx.Request], httpx.Response]
) -> DouyinCaptureProvider:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://sidecar.invalid"
    )
    return DouyinCaptureProvider("http://sidecar.invalid", identity=identity, client=client)


def _read_collections(provider: DouyinCaptureProvider) -> object:
    return provider.list_collections()


def _read_collection_sources(provider: DouyinCaptureProvider) -> object:
    return provider.list_collection_sources("selected")


def _read_default_favorites(provider: DouyinCaptureProvider) -> object:
    return provider.list_default_favorite_sources()


def _read_source(provider: DouyinCaptureProvider) -> object:
    return provider.fetch_source("source-1")


READS = (
    _read_collections,
    _read_collection_sources,
    _read_default_favorites,
    _read_source,
)


@pytest.mark.parametrize("identity", [None, " \t "])
@pytest.mark.parametrize("read", READS)
def test_missing_pin_blocks_every_account_read_before_http(
    identity: str | None,
    read: Callable[[DouyinCaptureProvider], object],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with _provider(identity, handler) as provider:
        with pytest.raises(AuthenticationRequired, match="DK_DOUYIN_SIDECAR_IDENTITY"):
            read(provider)

    assert requests == []


def test_health_can_inspect_sidecar_without_an_account_pin() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ready", "components": {}})

    with _provider(None, handler) as provider:
        assert provider.health().ok
        with pytest.raises(AuthenticationRequired):
            provider.list_collections()

    assert [request.url.path for request in requests] == ["/readyz"]


def test_valid_pin_is_attached_to_every_account_read_and_cannot_be_overridden() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/video"):
            data: object = {
                "content_id": "source-1", "kind": "video", "title": "Source",
                "web_url": "https://www.douyin.com/video/source-1",
                "author": {"uid": "creator-1", "nickname": "Creator"},
            }
        else:
            data = {"items": [], "cursor": None, "has_more": False}
        return httpx.Response(
            200, json={"success": True, "data": data, "error": None, "meta": {}}
        )

    with _provider("pinned-id", handler) as provider:
        for read in READS:
            read(provider)
        provider._get("/douyin/video", {"aweme_id": "source-1", "identity": "other-id"})

    assert [request.url.path for request in requests] == [
        "/api/v1/douyin/user/collections",
        "/api/v1/douyin/collection/posts",
        "/api/v1/douyin/user/bookmarks",
        "/api/v1/douyin/video",
        "/api/v1/douyin/video",
    ]
    assert all(request.url.params["identity"] == "pinned-id" for request in requests)


def test_invalid_pin_is_sent_once_and_never_retried_without_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            404,
            json={
                "success": False,
                "data": None,
                "error": {"code": "NOT_FOUND", "message": "identity unavailable"},
                "meta": {},
            },
        )

    with _provider("invalid-pin", handler) as provider:
        with pytest.raises(SourceUnavailable):
            provider.list_collections()

    assert len(requests) == 1
    assert requests[0].url.params["identity"] == "invalid-pin"

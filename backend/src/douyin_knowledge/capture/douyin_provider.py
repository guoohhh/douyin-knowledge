"""Douyin capture provider: a thin HTTP client for the sidecar.

    Douyin Knowledge --> CaptureProvider --> DouyinCaptureProvider --> HTTP --> sidecar

No signing, no cookie handling, no reverse engineering lives here (AGENTS 6). The
sidecar is ``Evil0ctal/Douyin_TikTok_Download_API``; the credential it needs is its
own configuration, not ours. All we hold is a base URL and an API key for the
sidecar itself.

Contract notes, read off the sidecar's source rather than the design docs, which
name only its URL and port. It has been rewritten since those docs were written and
the old ``/api/douyin/web/...`` route names no longer exist (see DEC-C11):

* Routes live under ``/api/v1``: ``/{platform}/user/collections``,
  ``/{platform}/collection/posts``, ``/{platform}/video``.
* Every response, success or failure, is ``{"success", "data", "error", "meta"}``.
  ``meta`` may carry ``cursor``, ``cached``, ``duration_ms``, ``task_id``.
* **Submitting returns 202** with ``{"task_id", "state"}`` unless ``wait=<seconds>``
  is passed, which moves the polling loop server-side. We always pass ``wait`` and
  fall back to polling ``/api/v1/tasks/{id}`` if the wait expires, because a
  cold identity pool can exceed any single wait ceiling.
* Auth is the ``X-API-Key`` header.
* Douyin's folder list **takes no author**: its upstream endpoint carries no user id
  and answers only about the session sending it. Passing ``url``/``sec_user_id``
  there is an error, not a no-op. Which session that is comes from ``identity``,
  which *is* a per-request query parameter (``identity:manage`` scope) even though we
  configure it once.

Pagination, verified against the live OpenAPI document and the sidecar's REST guide
rather than inferred from our own fixtures (DEC-013):

* A list page carries ``data.items``, ``data.cursor``, ``data.has_more``. The same
  two pagination values are *duplicated* into ``meta.cursor`` as an **object**,
  ``{"next": ..., "has_more": ...}`` -- not a bare string. Reading ``meta["cursor"]``
  as the next cursor, as this client previously did, yields a dict where a string
  belongs, so the walk never advanced past page 1.
* The cursor is an **opaque string** (<=512 chars). Douyin stringifies a millisecond
  ``max_cursor``, TikTok an offset. We pass it back verbatim and never parse,
  increment, or construct one.
* ``count`` is capped at 50 by the schema, and a cursor is only valid for the query
  that produced it, so ``count`` must not change mid-walk.
* ``wait`` is a float with a documented maximum of 30.0; above it the sidecar returns
  ``400 INVALID_PARAM`` rather than clamping, so we clamp locally instead.
* Task states are ``queued|running|done|failed``. There is no ``succeeded`` and no
  ``pending``. A finished task nests its payload at ``data.data`` with pagination at
  ``data.result_meta.cursor.next``. A *failed* task is still ``HTTP 200`` with
  ``success: true`` -- the failure lives in ``data.state``, so a client that checks
  only the envelope reads a failure as a success.
* Health is ``/healthz`` and readiness ``/readyz``, both **outside** ``/api/v1``,
  outside the envelope, and unauthenticated. There is no ``/health``: that path hits
  the console's SPA catch-all and answers ``200`` with HTML whatever the state of the
  service, which makes it precisely useless as a probe.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from douyin_knowledge.capture.base import CaptureProvider
from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedCreator,
    CapturedMedia,
    CapturedSource,
    ProviderHealth,
    SourcePage,
)
from douyin_knowledge.core.errors import (
    AuthenticationRequired,
    CaptureRateLimited,
    CaptureUnavailable,
    ConfigurationError,
    DKError,
    MediaDownloadFailed,
    SourceUnavailable,
    ValidationError,
)
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)

PLATFORM = "douyin"
API_PREFIX = "/api/v1"

#: The sidecar's ceiling on server-side wait, published on the parameter itself.
#: Exceeding it is a 400, not a clamp, so we clamp before sending.
MAX_WAIT_S = 30.0

#: Schema maximum for ``count``. Asking for more is a validation error.
MAX_PAGE_SIZE = 50

#: Schema maximum for an opaque cursor. A longer one cannot have come from this API.
MAX_CURSOR_LEN = 512

#: Task states, verbatim from the sidecar's REST guide. Note the absence of
#: ``succeeded``/``pending``: matching on those names silently never matches, and a
#: finished task then falls through to the error branch.
TASK_PENDING_STATES = frozenset({"queued", "running"})
TASK_DONE_STATE = "done"

#: Page cap for the folder list. Folders are few; needing 50 pages of them at 50 per
#: page means the cursor is not advancing, and continuing is worse than failing.
MAX_COLLECTION_PAGES = 50

#: Sidecar error code -> our error class. Codes absent here fall back on the HTTP
#: status, so a sidecar that adds a code does not become an opaque internal error.
ERROR_MAP: dict[str, type[DKError]] = {
    "UNAUTHENTICATED": AuthenticationRequired,
    "FORBIDDEN_SCOPE": AuthenticationRequired,
    "NOT_CONFIGURED": ConfigurationError,
    "NOT_FOUND": SourceUnavailable,
    "CONTENT_PRIVATE": SourceUnavailable,
    "INVALID_URL": ValidationError,
    "INVALID_PARAM": ValidationError,
    "UNSUPPORTED_CONTENT": ValidationError,
    "RATE_LIMITED": CaptureRateLimited,
    # Everything below is the sidecar or its upstream being temporarily unable, which
    # is exactly what a retryable capture failure means for the job queue.
    "IDENTITY_POOL_EXHAUSTED": CaptureUnavailable,
    "ENDPOINT_CIRCUIT_OPEN": CaptureUnavailable,
    "UPSTREAM_RISK_CONTROL": CaptureUnavailable,
    "QUEUE_FULL": CaptureUnavailable,
    "SIGNING_FAILED": CaptureUnavailable,
    "DOWNLOADER_UNAVAILABLE": CaptureUnavailable,
    # A changed upstream is NOT retryable: the parser needs updating, and retrying
    # just burns identities against a shape the sidecar cannot read.
    "UPSTREAM_CHANGED": ValidationError,
    # Task results expire (default 24h). Resubmitting is the documented recovery, so
    # this is retryable rather than a permanent failure.
    "TASK_NOT_FOUND": CaptureUnavailable,
}


class DouyinCaptureProvider(CaptureProvider):
    """Reads saved Douyin content through the sidecar."""

    name = "douyin"
    platform = PLATFORM

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        identity: str | None = None,
        wait_s: float = 30.0,
        poll_timeout_s: float = 180.0,
        poll_interval_s: float = 1.5,
        page_size: int = 50,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.identity = identity
        # Clamp rather than trust the caller: the sidecar rejects an over-ceiling wait
        # with 400 INVALID_PARAM, so an optimistic default would fail every call.
        self.wait_s = min(float(wait_s), MAX_WAIT_S)
        self.poll_timeout_s = poll_timeout_s
        # Injectable so tests can exercise the polling loop without sleeping through
        # it. A test that takes six seconds to check a state machine gets deleted.
        self.poll_interval_s = poll_interval_s
        self.page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            # Generous: the sidecar holds the connection open for `wait` seconds by
            # design, so a short read timeout would abort exactly the calls that are
            # working as intended.
            timeout=httpx.Timeout(connect=10.0, read=self.wait_s + 30.0, write=30.0, pool=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DouyinCaptureProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- transport -----------------------------------------------------
    def _raise_for_envelope(self, response: httpx.Response) -> None:
        """Turn a failure envelope into a typed DKError.

        The sidecar's ``message`` is localized and explicitly documented as
        unparseable, so only ``code`` is ever branched on.
        """
        body = self._json_of(response)
        error = body.get("error") or {}
        code = str(error.get("code") or "")
        exc_type = ERROR_MAP.get(code)
        if exc_type is None:
            exc_type = (
                AuthenticationRequired
                if response.status_code in (401, 403)
                else CaptureRateLimited
                if response.status_code == 429
                else SourceUnavailable
                if response.status_code == 404
                else CaptureUnavailable
            )
        raise exc_type(
            error.get("message") or f"sidecar error {code or response.status_code}",
            sidecar_code=code or None,
            status_code=response.status_code,
            request_id=(body.get("meta") or {}).get("request_id"),
            details=error.get("details"),
        )

    def _get(self, path: str, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """One sidecar call, resolved to ``(data, meta)``.

        Handles the 202 path: we ask the sidecar to wait, and if the task is still
        running when the wait expires we poll rather than treating it as a failure.
        """
        query = {k: v for k, v in params.items() if v is not None}
        query.setdefault("wait", self.wait_s)
        if self.identity:
            query.setdefault("identity", self.identity)
        try:
            response = self._client.get(f"{API_PREFIX}{path}", params=query)
        except httpx.RequestError as exc:
            raise CaptureUnavailable(
                f"cannot reach sidecar at {self.base_url}: {exc}",
                base_url=self.base_url,
                path=path,
            ) from exc
        if response.status_code == 202:
            # Not a failure: the wait ceiling expired while the task was still
            # running, and the work is still queued under this id. Treating it as an
            # error and resubmitting would spend a second identity for the same answer.
            body = self._json_of(response)
            task_id = (body.get("data") or {}).get("task_id")
            if not task_id:
                raise CaptureUnavailable("sidecar accepted the request but named no task")
            return self._poll_task(str(task_id))
        body = self._json_of(response)
        if response.is_error or not body.get("success", False):
            self._raise_for_envelope(response)
        return body.get("data"), dict(body.get("meta") or {})

    def _json_of(self, response: httpx.Response) -> dict[str, Any]:
        """Parse one response body once.

        Every branch below needs the envelope, and ``httpx`` re-decodes on each
        ``.json()`` call. More to the point, calling it twice and branching on each
        result separately is how a body can appear to change mid-function.
        """
        try:
            body = response.json()
        except ValueError:
            raise CaptureUnavailable(
                f"sidecar returned non-JSON (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text[:500],
            ) from None
        if not isinstance(body, dict):
            raise CaptureUnavailable(
                f"sidecar returned a {type(body).__name__}, not an envelope",
                status_code=response.status_code,
            )
        return body

    def _poll_task(self, task_id: str, *, interval_s: float | None = None) -> tuple[Any, dict[str, Any]]:
        """Poll a task the server-side wait did not outlive.

        A cold identity pool can take longer than any single ``wait`` the sidecar
        permits, so giving up at the first 202 would make sync fail precisely on a
        fresh install.
        """
        interval = self.poll_interval_s if interval_s is None else interval_s
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            if time.monotonic() > deadline:
                raise CaptureUnavailable(
                    f"sidecar task {task_id} did not finish within {self.poll_timeout_s}s",
                    task_id=task_id,
                )
            time.sleep(interval)
            try:
                response = self._client.get(f"{API_PREFIX}/tasks/{task_id}")
            except httpx.RequestError as exc:
                raise CaptureUnavailable(
                    f"cannot reach sidecar while polling task {task_id}: {exc}", task_id=task_id
                ) from exc
            if response.is_error:
                self._raise_for_envelope(response)
            body = self._json_of(response)
            if not body.get("success", False):
                self._raise_for_envelope(response)
            # A *failed task* is HTTP 200 with `success: true` -- the envelope
            # describes the lookup, not the work. The state below is the only place the
            # failure appears, so returning here on the envelope alone would report
            # every failed capture as an empty success.
            data = body.get("data") or {}
            state = str(data.get("state") or "")
            if state in TASK_PENDING_STATES:
                continue
            if state == TASK_DONE_STATE:
                # A finished task nests one level deeper (`data.data`) and carries its
                # pagination in a sibling `result_meta`, not in the envelope's `meta`.
                return data.get("data"), dict(data.get("result_meta") or {})
            error = data.get("error") or {}
            code = str(error.get("code") or "")
            exc_type = ERROR_MAP.get(code, CaptureUnavailable)
            raise exc_type(
                error.get("message") or f"sidecar task {task_id} ended as {state or 'unknown'}",
                sidecar_code=code or None,
                task_id=task_id,
            )

    # ---- mapping -------------------------------------------------------
    def _map_author(self, raw: dict[str, Any]) -> CapturedCreator:
        """dtk Author -> CapturedCreator."""
        return CapturedCreator(
            external_creator_id=raw["uid"],
            display_name=raw["nickname"],
            handle=raw.get("unique_id"),
            profile_url=raw.get("web_url"),
            avatar_url=self._first_image_url(raw.get("avatar")),
            raw=raw,
        )

    def _map_media(self, raw_content: dict[str, Any]) -> list[CapturedMedia]:
        """dtk Content.media -> CapturedMedia list.

        The sidecar's Media carries covers, video, streams, images. No subtitle field
        exists anywhere in its schema or in the upstream shape, so processing level 2
        (transcription-dependent) is unachievable via the real provider without a
        working ASR provider. The fixture provider's inline subtitles are a test
        convenience that has no upstream analogue.
        """
        media_node = raw_content.get("media") or {}
        out: list[CapturedMedia] = []
        # Cover
        covers = media_node.get("covers") or []
        if covers:
            out.append(
                CapturedMedia(
                    kind="image",
                    url=self._first_image_url(covers[0]),
                    mime_type="image/jpeg",
                )
            )
        # Video streams
        video = media_node.get("video")
        if video:
            out.append(
                CapturedMedia(
                    kind="video",
                    url=video["url"],
                    mime_type="video/mp4",
                    width=video.get("width"),
                    height=video.get("height"),
                    duration_ms=raw_content.get("duration_ms"),
                    byte_size=video.get("size_bytes"),
                )
            )
        # Image album
        images = media_node.get("images") or []
        for img in images:
            out.append(
                CapturedMedia(
                    kind="image",
                    url=self._first_image_url(img),
                    mime_type="image/jpeg",
                    width=img.get("width"),
                    height=img.get("height"),
                )
            )
        return out

    def _map_source(self, raw: dict[str, Any]) -> CapturedSource:
        """dtk Content -> CapturedSource."""
        stats = raw.get("stats") or {}
        return CapturedSource(
            platform=self.platform,
            external_id=raw["content_id"],
            source_type=raw["kind"],
            title=raw["title"],
            caption_raw=raw.get("description") or "",
            source_url=raw["web_url"],
            cover_url=self._first_image_url((raw.get("media") or {}).get("covers")),
            published_at_ms=self._to_timestamp_ms(raw.get("created_at")),
            # DTK's normalized Content has no saved/collected timestamp. The response's
            # `fetched_at` is transport metadata, not when the user saved the item.
            # Inventing "now" here makes an unchanged item hash differently on every
            # sync and manufactures a new snapshot each time.
            saved_at_ms=None,
            duration_ms=raw.get("duration_ms"),
            availability="available",
            creator=self._map_author(raw["author"]),
            hashtags=raw.get("tags") or [],
            # Absent counters are omitted, not stored as None or 0. `statistics` is
            # typed `dict[str, int]`, so a None here fails validation and takes the
            # whole item down -- and 0 would be a lie: "nobody watched it" and "the
            # sidecar did not report views" are different facts. Any item arriving
            # without a `stats` block previously crashed mapping for this reason.
            statistics={
                key: int(stats[key])
                for key in (
                    "play_count",
                    "digg_count",
                    "comment_count",
                    "share_count",
                    "collect_count",
                )
                if isinstance(stats.get(key), (int, float))
            },
            media=self._map_media(raw),
            raw=raw,
        )

    def _map_collection(self, raw: dict[str, Any]) -> CapturedCollection:
        """dtk Collection -> CapturedCollection."""
        return CapturedCollection(
            external_collection_id=raw["collection_id"],
            name=raw["name"],
            description=None,  # dtk Collection has no description field
            item_count=raw.get("item_count"),
            raw=raw,
        )

    @staticmethod
    def _first_image_url(node: dict[str, Any] | list[dict[str, Any]] | None) -> str | None:
        """Extract the first usable URL from dtk's Image or list[Image]."""
        if node is None:
            return None
        items = node if isinstance(node, list) else [node]
        for item in items:
            url = item.get("url")
            if url:
                return str(url)
        return None

    @staticmethod
    def _to_timestamp_ms(dt: str | None) -> int | None:
        """ISO datetime -> epoch milliseconds."""
        if dt is None:
            return None
        try:
            parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except (ValueError, OSError):
            return None

    # ---- provider interface --------------------------------------------
    def health(self) -> ProviderHealth:
        """Can the sidecar answer, and are its dependencies up?

        Probes ``/readyz``, not ``/api/v1/health``. Two reasons the old path was worse
        than merely wrong: there is no ``/health`` route, so it fell through to the
        console's SPA catch-all and returned ``200`` with an HTML body -- a health check
        that cannot fail is not a health check. And readiness is the question we
        actually have: a sidecar whose identity pool or datastore is down will accept
        our request and then fail it.

        Both endpoints sit outside ``/api/v1``, outside the ``{success, data, ...}``
        envelope, and outside auth, so this works before a key is configured.
        """
        try:
            response = self._client.get("/readyz")
        except httpx.RequestError as exc:
            return ProviderHealth(
                name=self.name,
                ok=False,
                detail=f"cannot reach sidecar at {self.base_url}: {exc}",
                requires_credentials=self.api_key is not None,
            )

        ok = response.is_success
        detail = f"sidecar HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            # HTML here means we hit the SPA catch-all, i.e. this build has no
            # /readyz. Report unknown rather than inventing a green light.
            return ProviderHealth(
                name=self.name,
                ok=False,
                detail=f"sidecar answered /readyz with non-JSON (HTTP {response.status_code})",
                requires_credentials=self.api_key is not None,
            )

        if isinstance(body, dict):
            status = str(body.get("status") or "").strip()
            components = body.get("components")
            if isinstance(components, dict):
                down = sorted(
                    name
                    for name, state in components.items()
                    if isinstance(state, dict) and not state.get("ok", True)
                )
                if down:
                    # 200 with a failed component is still not ready. Naming the
                    # component is the difference between an actionable report and
                    # "capture is broken".
                    ok = False
                    status = f"{status or 'degraded'} (down: {', '.join(down)})"
            detail = status or detail

        return ProviderHealth(
            name=self.name,
            ok=ok,
            detail=detail,
            requires_credentials=self.api_key is not None,
        )

    @staticmethod
    def _read_pagination(
        data: Any, meta: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Read ``(items, next_cursor, has_more)`` out of one list response.

        The sidecar publishes the same two pagination values twice, in two shapes:
        flat on ``data`` and nested under ``meta.cursor`` as an object. ``data`` is
        preferred because it is the documented primary location and is present on both
        the direct and the task-result paths; ``meta.cursor`` is the fallback, which is
        what the task path fills via ``result_meta``.

        ``has_more`` is only trusted when the response actually states it. Defaulting a
        missing flag to ``True`` would spin, and defaulting a *present* ``True`` to
        ``False`` would silently truncate -- so absence in both locations means "last
        page", and that is the only safe default given a cursor we cannot interpret.
        """
        node = data if isinstance(data, dict) else {}
        items = node.get("items")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValidationError(
                f"sidecar page carried {type(items).__name__} where an items list belongs",
                got=type(items).__name__,
            )

        meta_cursor = meta.get("cursor")
        # `meta.cursor` is an object. A bare string here means the contract moved, and
        # coercing it would resurrect exactly the bug this method exists to fix.
        nested = meta_cursor if isinstance(meta_cursor, dict) else {}

        raw_cursor = node.get("cursor", nested.get("next"))
        next_cursor = None if raw_cursor is None else str(raw_cursor)
        if next_cursor is not None and len(next_cursor) > MAX_CURSOR_LEN:
            raise ValidationError(
                f"sidecar cursor exceeds the {MAX_CURSOR_LEN}-char contract maximum",
                length=len(next_cursor),
            )
        # An empty-string cursor is not a usable cursor. Passing it back would re-fetch
        # page 1 forever while `has_more` kept saying there is more.
        if not next_cursor:
            next_cursor = None

        raw_more = node.get("has_more", nested.get("has_more"))
        has_more = bool(raw_more) if raw_more is not None else False
        return [item for item in items if isinstance(item, dict)], next_cursor, has_more

    def list_collections(self) -> list[CapturedCollection]:
        """GET /{platform}/user/collections, walked to completion.

        Note: this endpoint on Douyin takes **no author** and answers only about the
        session sending it, which the sidecar identifies via `identity`.

        This endpoint is paginated too. Reading only its first page silently caps a
        user at their 20 most recent folders, and every item in the folders beyond that
        is then invisible to the whole system -- a data-loss bug that looks like an
        empty library rather than an error. Cursor discipline matches
        `walk_collection_sources`: a repeated cursor is a fault, not an ending.
        """
        collections: list[CapturedCollection] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_COLLECTION_PAGES):
            data, meta = self._get(
                f"/{self.platform}/user/collections",
                {"cursor": cursor, "count": self.page_size},
            )
            items, next_cursor, has_more = self._read_pagination(data, meta)
            collections.extend(self._map_collection(raw) for raw in items)
            if not has_more or next_cursor is None:
                return collections
            if next_cursor in seen:
                raise ValidationError(
                    "sidecar repeated a folder-list cursor; the walk cannot advance",
                    cursor_repeated=True,
                )
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValidationError(
            f"folder list exceeded {MAX_COLLECTION_PAGES} pages; refusing to keep walking",
            pages=MAX_COLLECTION_PAGES,
        )

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        """GET /{platform}/collection/posts.

        One page only: the walk lives in `walk_collection_sources`, so that the
        decision about what counts as a *complete* traversal -- the thing membership
        pruning depends on -- is made in one testable place rather than per provider.
        """
        data, meta = self._get(
            f"/{self.platform}/collection/posts",
            {
                "collection_id": external_collection_id,
                "cursor": cursor,
                # A cursor is only valid for the query that produced it, so the page
                # size must not drift mid-walk. Clamped because >50 is a 400.
                "count": max(1, min(limit, MAX_PAGE_SIZE)),
            },
        )
        items, next_cursor, has_more = self._read_pagination(data, meta)
        return SourcePage(
            sources=[self._map_source(raw) for raw in items],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def fetch_source(self, external_id: str) -> CapturedSource:
        """GET /{platform}/video?aweme_id=..."""
        data, _meta = self._get(f"/{self.platform}/video", {"aweme_id": external_id})
        if data is None:
            raise SourceUnavailable(f"sidecar returned no data for {external_id}", external_id=external_id)
        return self._map_source(data)

    def download_media(self, media: CapturedMedia, destination: Path) -> Path:
        """Stream from media.url to destination."""
        if not media.url:
            raise MediaDownloadFailed("media has no URL", media_kind=media.kind)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", media.url) as response:
                response.raise_for_status()
                with destination.open("wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        except httpx.HTTPStatusError as exc:
            raise MediaDownloadFailed(
                f"download failed with HTTP {exc.response.status_code}",
                url=media.url,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.RequestError as exc:
            raise MediaDownloadFailed(f"download failed: {exc}", url=media.url) from exc
        return destination

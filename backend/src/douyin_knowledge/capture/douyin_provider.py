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
  configured once, not passed per call.
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
        page_size: int = 50,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.identity = identity
        self.wait_s = wait_s
        self.poll_timeout_s = poll_timeout_s
        self.page_size = page_size
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers=headers,
            # Generous: the sidecar holds the connection open for `wait` seconds by
            # design, so a short read timeout would abort exactly the calls that are
            # working as intended.
            timeout=httpx.Timeout(connect=10.0, read=wait_s + 30.0, write=30.0, pool=10.0),
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
        try:
            body = response.json()
        except ValueError:
            raise CaptureUnavailable(
                f"sidecar returned non-JSON (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text[:500],
            ) from None
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
            body = response.json()
            task_id = (body.get("data") or {}).get("task_id")
            if not task_id:
                raise CaptureUnavailable("sidecar accepted the request but named no task")
            return self._poll_task(str(task_id))
        if response.is_error or not (response.json() or {}).get("success", False):
            self._raise_for_envelope(response)
        body = response.json()
        return body.get("data"), body.get("meta") or {}

    def _poll_task(self, task_id: str, *, interval_s: float = 1.5) -> tuple[Any, dict[str, Any]]:
        """Poll a task the server-side wait did not outlive.

        A cold identity pool can take longer than any single ``wait`` the sidecar
        permits, so giving up at the first 202 would make sync fail precisely on a
        fresh install.
        """
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            if time.monotonic() > deadline:
                raise CaptureUnavailable(
                    f"sidecar task {task_id} did not finish within {self.poll_timeout_s}s",
                    task_id=task_id,
                )
            time.sleep(interval_s)
            try:
                response = self._client.get(f"{API_PREFIX}/tasks/{task_id}")
            except httpx.RequestError as exc:
                raise CaptureUnavailable(
                    f"cannot reach sidecar while polling task {task_id}: {exc}", task_id=task_id
                ) from exc
            if response.is_error:
                self._raise_for_envelope(response)
            body = response.json()
            if not body.get("success", False):
                self._raise_for_envelope(response)
            data = body.get("data") or {}
            state = str(data.get("state") or "")
            if state in ("pending", "running", "queued"):
                continue
            if state == "succeeded":
                # A finished task nests the payload; an already-shaped envelope does
                # not. Accept both, like the sidecar's own `unwrap`.
                result = data.get("result")
                if isinstance(result, dict) and "data" in result:
                    return result["data"], dict(result.get("meta") or {})
                return result, dict(body.get("meta") or {})
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
            saved_at_ms=int(time.time() * 1000),
            duration_ms=raw.get("duration_ms"),
            availability="available",
            creator=self._map_author(raw["author"]),
            hashtags=raw.get("tags") or [],
            statistics={
                "play_count": stats.get("play_count"),
                "digg_count": stats.get("digg_count"),
                "comment_count": stats.get("comment_count"),
                "share_count": stats.get("share_count"),
                "collect_count": stats.get("collect_count"),
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
        """Health check: can the sidecar answer?"""
        try:
            response = self._client.get(f"{API_PREFIX}/health")
            ok = response.is_success
            detail = f"sidecar HTTP {response.status_code}"
            if ok:
                body = response.json()
                detail = body.get("status") or detail
        except Exception as exc:
            ok = False
            detail = f"cannot reach sidecar: {exc}"
        return ProviderHealth(
            name=self.name,
            ok=ok,
            detail=detail,
            requires_credentials=self.api_key is not None,
        )

    def list_collections(self) -> list[CapturedCollection]:
        """GET /{platform}/user/collections.

        Note: this endpoint on Douyin takes **no author** and answers only about the
        session sending it, which the sidecar identifies via the `identity` config.
        """
        data, _meta = self._get(f"/{self.platform}/user/collections", {})
        items = (data or {}).get("items") or []
        return [self._map_collection(raw) for raw in items]

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        """GET /{platform}/collection/posts."""
        data, meta = self._get(
            f"/{self.platform}/collection/posts",
            {"collection_id": external_collection_id, "cursor": cursor, "count": limit},
        )
        items = (data or {}).get("items") or []
        sources = [self._map_source(raw) for raw in items]
        return SourcePage(
            sources=sources,
            next_cursor=meta.get("cursor"),
            has_more=bool(meta.get("has_more", False)),
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


"""Streaming media download with bounded cost and no partial-file lies.

The safety envelope here is adapted from the GPT experiment's transcription adapter,
which got the threat model right even though its implementation kept nothing: cap the
bytes *during* the stream rather than trusting ``Content-Length``, follow redirects
manually with a hard hop limit, and refuse plaintext hops. What changed in adapting it:

* It downloaded to a ``TemporaryDirectory`` and transcribed in the same call, so every
  retry re-fetched the file and a crash left nothing to resume from. Here the bytes
  land in the media dir under a deterministic key and the DB records that they did.
* It raised bare ``ValueError``/``RuntimeError``, which the job queue would classify as
  non-retryable and fail permanently. A timeout against a CDN is the most retryable
  error in the system, so failures are :class:`MediaDownloadFailed` (retryable) and
  only genuinely permanent conditions get a terminal error.
* It wrote straight to the destination. Here bytes go to a ``.part`` file and get
  renamed only on success, because a half-written file that *looks* present is worse
  than no file: the orchestrator would hand the truncated audio to ASR and store the
  resulting garbage as evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from douyin_knowledge.core.errors import MediaDownloadFailed, ValidationError
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
MAX_REDIRECTS = 6
CHUNK_BYTES = 256 * 1024


class MediaTooLarge(ValidationError):
    """The asset is bigger than we are willing to store.

    Non-retryable (via ValidationError): the file will be the same size next time, so a
    retry burns bandwidth to reach the identical conclusion.
    """

    code = "media_too_large"


class MediaAssetGone(ValidationError):
    """The provider's URL resolved to 404/410 -- the asset expired or was removed.

    Non-retryable for *this* URL. A later capture may hand us a fresh URL, and that is
    a new acquisition, not a retry of this one.
    """

    code = "media_asset_gone"


class MediaStoreLike(Protocol):
    """The two store methods the downloader actually needs.

    A Protocol rather than an import of the concrete store: it documents the coupling
    precisely and lets a test pass a stub without subclassing.
    """

    def ensure_parent(self, storage_key: str) -> Path: ...

    def resolve(self, storage_key: str) -> Path: ...


@dataclass(slots=True)
class DownloadResult:
    storage_key: str
    byte_size: int
    sha256: str
    mime_type: str | None
    final_url: str


class MediaDownloader:
    """Fetches one URL into the media store.

    ``allow_insecure_http`` exists for tests, which serve fixtures over plain HTTP via
    a mock transport. It defaults to False so production never silently accepts a
    downgrade, and it is a constructor argument rather than a per-call flag so a
    caller cannot flip it mid-run.
    """

    def __init__(
        self,
        store: MediaStoreLike,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = 120.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allow_insecure_http: bool = False,
    ) -> None:
        self.store = store
        self._client = client
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.allow_insecure_http = allow_insecure_http

    def _open(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        # follow_redirects=False on purpose: we walk them ourselves so each hop can be
        # re-checked against the scheme policy. httpx's own follower would happily
        # follow https -> http.
        return httpx.Client(timeout=self.timeout_s, follow_redirects=False), True

    def _check_scheme(self, url: str) -> None:
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme == "https":
            return
        if scheme == "http" and self.allow_insecure_http:
            return
        raise ValidationError(
            f"media URL must use https, got {scheme or 'no scheme'}",
            url=url[:200],
        )

    def download(self, url: str, storage_key: str) -> DownloadResult:
        """Fetch ``url`` into ``storage_key``, returning what was actually written.

        Overwrites any existing file at the key. Callers decide whether a download is
        needed; this method's contract is "after I return without raising, those bytes
        are on disk, complete, and hashed".
        """
        self._check_scheme(url)
        destination = self.store.ensure_parent(storage_key)
        partial = destination.with_name(destination.name + ".part")
        client, owned = self._open()
        try:
            written, digest, mime, final_url = self._stream(client, url, partial)
        except (MediaDownloadFailed, ValidationError):
            partial.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise MediaDownloadFailed(f"media fetch failed: {exc}", url=url[:200]) from exc
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise MediaDownloadFailed(
                f"could not write media to disk: {exc}", storage_key=storage_key
            ) from exc
        finally:
            if owned:
                client.close()

        # Replace, not create: `Path.replace` is atomic on the same filesystem, so a
        # reader either sees the old complete file or the new one, never a splice.
        partial.replace(destination)
        logger.info(
            "media_downloaded",
            extra={"storage_key": storage_key, "byte_size": written},
        )
        return DownloadResult(
            storage_key=storage_key,
            byte_size=written,
            sha256=digest,
            mime_type=mime,
            final_url=final_url,
        )

    def _stream(
        self, client: httpx.Client, url: str, partial: Path
    ) -> tuple[int, str, str | None, str]:
        current = url
        for _hop in range(MAX_REDIRECTS):
            self._check_scheme(current)
            with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise MediaDownloadFailed(
                            "media redirect carried no location header", url=current[:200]
                        )
                    current = urljoin(current, location)
                    continue
                if response.status_code == 404 or response.status_code == 410:
                    # Gone is gone. Retrying a 404 for three attempts just delays the
                    # moment the user learns the asset expired.
                    raise MediaAssetGone(
                        f"media no longer available (HTTP {response.status_code})",
                        url=current[:200],
                    )
                if response.status_code >= 400:
                    raise MediaDownloadFailed(
                        f"media fetch returned HTTP {response.status_code}",
                        url=current[:200],
                        status_code=response.status_code,
                    )

                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    # Cheap pre-check. Not trusted -- the loop below enforces the real
                    # limit -- but it avoids streaming 4 GiB to discover it is 4 GiB.
                    raise MediaTooLarge(
                        f"media declares {int(declared)} bytes, over the "
                        f"{self.max_bytes}-byte limit",
                        url=current[:200],
                    )

                hasher = hashlib.sha256()
                written = 0
                with partial.open("wb") as stream:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise MediaTooLarge(
                                f"media exceeded the {self.max_bytes}-byte limit mid-stream",
                                url=current[:200],
                            )
                        hasher.update(chunk)
                        stream.write(chunk)
                if written == 0:
                    raise MediaDownloadFailed("media response was empty", url=current[:200])
                mime = response.headers.get("content-type")
                return written, hasher.hexdigest(), mime, current

        raise MediaDownloadFailed(
            f"media redirected more than {MAX_REDIRECTS} times", url=url[:200]
        )

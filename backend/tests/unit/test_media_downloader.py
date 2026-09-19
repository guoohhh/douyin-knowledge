"""Download safety: bounded bytes, bounded hops, no plaintext, no partial files.

Every test here runs against `httpx.MockTransport`. Nothing reaches the network, which
is the AGENTS requirement that tests never need a live Douyin -- and it lets us assert
on cases a real CDN would not reproduce on demand, like a redirect loop or a response
whose body is larger than its declared Content-Length.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from douyin_knowledge.core.errors import MediaDownloadFailed, ValidationError
from douyin_knowledge.media.downloader import (
    MediaAssetGone,
    MediaDownloader,
    MediaTooLarge,
)
from douyin_knowledge.media.store import MediaStore

URL = "https://cdn.example.com/media/clip.mp4"
KEY = "cache/douyin/7123/video-abc.mp4"


def build(
    handler: object, tmp_path: Path, *, max_bytes: int = 1024, insecure: bool = False
) -> tuple[MediaDownloader, MediaStore]:
    store = MediaStore(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    downloader = MediaDownloader(
        store, client=client, max_bytes=max_bytes, allow_insecure_http=insecure
    )
    return downloader, store


class TestSuccess:
    def test_writes_bytes_and_hashes_them(self, tmp_path: Path) -> None:
        payload = b"video-bytes" * 10

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=payload, headers={"content-type": "video/mp4"})

        downloader, store = build(handler, tmp_path)
        result = downloader.download(URL, KEY)

        assert result.byte_size == len(payload)
        assert result.mime_type == "video/mp4"
        assert store.resolve(KEY).read_bytes() == payload
        # sha256 of the actual bytes written, so a later integrity check is possible.
        import hashlib

        assert result.sha256 == hashlib.sha256(payload).hexdigest()

    def test_leaves_no_part_file_behind(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok")

        downloader, store = build(handler, tmp_path)
        downloader.download(URL, KEY)
        assert list(store.resolve(KEY).parent.glob("*.part")) == []

    def test_overwrites_an_existing_file(self, tmp_path: Path) -> None:
        """Re-acquisition must replace, not append or fail."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"new")

        downloader, store = build(handler, tmp_path)
        store.ensure_parent(KEY).write_bytes(b"stale-and-longer")
        downloader.download(URL, KEY)
        assert store.resolve(KEY).read_bytes() == b"new"


class TestRedirects:
    def test_follows_redirect_chain(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/media/clip.mp4":
                return httpx.Response(302, headers={"location": "/real/clip.mp4"})
            return httpx.Response(200, content=b"bytes")

        downloader, _ = build(handler, tmp_path)
        assert downloader.download(URL, KEY).byte_size == 5
        assert len(seen) == 2

    def test_relative_location_is_resolved(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/media/clip.mp4":
                return httpx.Response(302, headers={"location": "other.mp4"})
            assert request.url.path == "/media/other.mp4"
            return httpx.Response(200, content=b"x")

        downloader, _ = build(handler, tmp_path)
        assert downloader.download(URL, KEY).final_url.endswith("/media/other.mp4")

    def test_redirect_loop_is_bounded(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "/loop"})

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed, match="redirected more than"):
            downloader.download(URL, KEY)

    def test_redirect_without_location_fails(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed, match="no location"):
            downloader.download(URL, KEY)

    def test_https_to_http_downgrade_is_refused(self, tmp_path: Path) -> None:
        """httpx's own follower would take this hop. Ours re-checks every one."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://cdn.example.com/plain.mp4"})

        downloader, store = build(handler, tmp_path)
        with pytest.raises(ValidationError, match="must use https"):
            downloader.download(URL, KEY)
        assert store.exists(KEY) is False


class TestScheme:
    def test_plain_http_refused_by_default(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not be requested")

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(ValidationError, match="must use https"):
            downloader.download("http://cdn.example.com/clip.mp4", KEY)

    def test_non_http_scheme_refused(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not be requested")

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(ValidationError):
            downloader.download("file:///etc/passwd", KEY)

    def test_http_allowed_when_explicitly_enabled(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x")

        downloader, _ = build(handler, tmp_path, insecure=True)
        assert downloader.download("http://cdn.example.com/clip.mp4", KEY).byte_size == 1


class TestSizeLimit:
    def test_declared_oversize_is_rejected_on_the_header(self, tmp_path: Path) -> None:
        """A Content-Length over the limit short-circuits with a distinct error.

        This asserts the *classification*, not the byte saving: MockTransport hands over
        a fully materialized body, so no test at this layer can prove the socket was not
        drained. The distinct "declares" message is the observable part, and the
        mid-stream test below covers the case where the header cannot be trusted.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "999999"}, content=b"x" * 10)

        downloader, store = build(handler, tmp_path, max_bytes=100)
        with pytest.raises(MediaTooLarge, match="declares"):
            downloader.download(URL, KEY)
        assert store.exists(KEY) is False

    def test_lying_content_length_is_caught_mid_stream(self, tmp_path: Path) -> None:
        """The reason the limit is enforced per chunk: Content-Length is a claim, and a
        200-byte header on a 2 MiB body would otherwise fill the disk."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "10"}, content=b"x" * 5000)

        downloader, store = build(handler, tmp_path, max_bytes=100)
        with pytest.raises(MediaTooLarge, match="mid-stream"):
            downloader.download(URL, KEY)
        assert store.exists(KEY) is False

    def test_oversize_leaves_no_partial_file(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 5000)

        downloader, store = build(handler, tmp_path, max_bytes=100)
        with pytest.raises(MediaTooLarge):
            downloader.download(URL, KEY)
        assert list(store.media_dir.rglob("*.part")) == []


class TestFailures:
    def test_404_is_permanent(self, tmp_path: Path) -> None:
        """Gone is gone: retrying three times only delays the honest answer."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaAssetGone) as excinfo:
            downloader.download(URL, KEY)
        assert excinfo.value.retryable is False

    def test_410_is_permanent(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(410)

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaAssetGone):
            downloader.download(URL, KEY)

    def test_403_is_retryable(self, tmp_path: Path) -> None:
        """An expired signature reads as 403. A fresh capture re-signs the URL, so this
        is worth another attempt -- unlike a 404."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed) as excinfo:
            downloader.download(URL, KEY)
        assert excinfo.value.retryable is True

    def test_server_error_is_retryable(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed) as excinfo:
            downloader.download(URL, KEY)
        assert excinfo.value.retryable is True

    def test_empty_body_is_a_failure_not_a_success(self, tmp_path: Path) -> None:
        """A zero-byte 200 would otherwise be recorded `ready` and then fail inside ASR
        with a provider error nobody can trace back to here."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        downloader, store = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed, match="empty"):
            downloader.download(URL, KEY)
        assert store.exists(KEY) is False

    def test_transport_error_becomes_retryable_domain_error(self, tmp_path: Path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        downloader, _ = build(handler, tmp_path)
        with pytest.raises(MediaDownloadFailed) as excinfo:
            downloader.download(URL, KEY)
        # The classification that matters: the queue retries this instead of failing
        # permanently, which is what a bare httpx error would have done.
        assert excinfo.value.retryable is True

    def test_failed_download_does_not_clobber_an_existing_good_file(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        downloader, store = build(handler, tmp_path)
        store.ensure_parent(KEY).write_bytes(b"previously-good")
        with pytest.raises(MediaDownloadFailed):
            downloader.download(URL, KEY)
        assert store.resolve(KEY).read_bytes() == b"previously-good"

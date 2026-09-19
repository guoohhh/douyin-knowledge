"""The path identity rules: relative keys, no escapes, stable across re-signing.

These are cheap tests for a class that is mostly string handling, but two of the
properties they pin are load-bearing: a `storage_key` that escapes the media dir is a
write-anywhere primitive driven by provider data, and a fingerprint that is *not* stable
across re-signing re-downloads every file on every sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from douyin_knowledge.core.errors import ValidationError
from douyin_knowledge.media.store import MediaStore, suffix_for, url_fingerprint

SIGNED_A = (
    "https://v26-web.douyinvod.com/abc123/video.mp4"
    "?a=1128&br=1943&expire=1757462400&sign=aaaaaaaaaaaaaaaa&nonce=111"
)
SIGNED_B = (
    "https://v26-web.douyinvod.com/abc123/video.mp4"
    "?a=1128&br=1943&expire=1757549999&sign=zzzzzzzzzzzzzzzz&nonce=999"
)


class TestFingerprint:
    def test_survives_resigning(self) -> None:
        """The whole reason the fingerprint exists: same file, new signature."""
        assert url_fingerprint(SIGNED_A) == url_fingerprint(SIGNED_B)

    def test_distinguishes_different_paths(self) -> None:
        other = "https://v26-web.douyinvod.com/def456/video.mp4?sign=aaaa"
        assert url_fingerprint(SIGNED_A) != url_fingerprint(other)

    def test_ignores_scheme_and_host_case(self) -> None:
        upper = "https://V26-WEB.douyinvod.com/abc123/video.mp4"
        lower = "https://v26-web.douyinvod.com/abc123/video.mp4"
        assert url_fingerprint(upper) == url_fingerprint(lower)

    def test_rejects_url_with_no_host_or_path(self) -> None:
        # Would otherwise hash the empty string and collide every such asset into one row.
        with pytest.raises(ValidationError):
            url_fingerprint("https://")


class TestSuffix:
    def test_prefers_mime_type(self) -> None:
        assert suffix_for("https://cdn/x/token", "audio/mpeg") == ".mp3"

    def test_falls_back_to_url_suffix(self) -> None:
        assert suffix_for("https://cdn/x/clip.m4a", None) == ".m4a"

    def test_unknown_suffix_becomes_bin(self) -> None:
        # A CDN token is not a file extension; `.bin` beats inventing `.sign`.
        assert suffix_for("https://cdn/x/file.sign", None) == ".bin"

    def test_mime_with_charset_parameter(self) -> None:
        assert suffix_for("https://cdn/x", "video/mp4; charset=binary") == ".mp4"


class TestStorageKey:
    def test_is_relative_and_deterministic(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        first = store.build_storage_key(
            platform="douyin", external_id="7123", asset_type="video", url=SIGNED_A
        )
        second = store.build_storage_key(
            platform="douyin", external_id="7123", asset_type="video", url=SIGNED_B
        )
        assert not Path(first).is_absolute()
        # Re-signed URL, same destination: this is what makes re-download idempotent.
        assert first == second
        assert first.startswith("cache/douyin/7123/")

    def test_retained_media_goes_to_pinned(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        key = store.build_storage_key(
            platform="douyin",
            external_id="7123",
            asset_type="audio",
            url=SIGNED_A,
            retention_class="retained",
        )
        assert key.startswith("pinned/")

    def test_changed_content_gets_a_different_key(self, tmp_path: Path) -> None:
        """A replaced upload must not overwrite bytes the old row still names."""
        store = MediaStore(tmp_path)
        first = store.build_storage_key(
            platform="douyin", external_id="7123", asset_type="video", url=SIGNED_A
        )
        replaced = store.build_storage_key(
            platform="douyin",
            external_id="7123",
            asset_type="video",
            url="https://v26-web.douyinvod.com/NEWHASH/video.mp4",
        )
        assert first != replaced

    def test_hostile_external_id_cannot_escape(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        key = store.build_storage_key(
            platform="douyin",
            external_id="../../../../etc/cron.d",
            asset_type="video",
            url=SIGNED_A,
        )
        resolved = store.resolve(key)
        assert tmp_path.resolve() in resolved.parents


class TestResolve:
    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        # The exact shape of the bug this package exists to prevent: a URL or an
        # absolute developer path sitting in storage_key.
        store = MediaStore(tmp_path)
        with pytest.raises(ValidationError):
            store.resolve("/etc/passwd")

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        with pytest.raises(ValidationError):
            store.resolve("cache/../../outside.mp4")

    def test_rejects_empty(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        with pytest.raises(ValidationError):
            store.resolve("")

    def test_resolves_inside_media_dir(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        assert store.resolve("cache/douyin/1/video.mp4").parent.name == "1"


class TestPresence:
    def test_missing_file_is_absent(self, tmp_path: Path) -> None:
        assert MediaStore(tmp_path).exists("cache/nope.mp4") is False

    def test_zero_length_file_is_absent(self, tmp_path: Path) -> None:
        """An interrupted download leaves an empty file; handing it to ASR is worse
        than reporting it missing, because the provider error is opaque."""
        store = MediaStore(tmp_path)
        path = store.ensure_parent("cache/empty.mp4")
        path.touch()
        assert store.exists("cache/empty.mp4") is False

    def test_written_file_is_present(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        store.ensure_parent("cache/ok.mp4").write_bytes(b"data")
        assert store.exists("cache/ok.mp4") is True
        assert store.size_of("cache/ok.mp4") == 4

    def test_absolute_key_is_absent_not_an_error(self, tmp_path: Path) -> None:
        """Legacy rows hold URLs. `exists` is a question, not an assertion, so it
        answers False instead of raising into the middle of a processing run."""
        assert MediaStore(tmp_path).exists("https://cdn/video.mp4") is False

    def test_delete_removes_and_tolerates_absence(self, tmp_path: Path) -> None:
        store = MediaStore(tmp_path)
        store.ensure_parent("cache/gone.mp4").write_bytes(b"x")
        assert store.delete("cache/gone.mp4") is True
        assert store.delete("cache/gone.mp4") is False
        assert store.delete(None) is False

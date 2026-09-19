"""Where a media file lives locally, and whether two URLs mean the same file.

Two separate jobs, kept in one module because they are two halves of one decision:
given a provider URL, which local path owns it, and does an existing row already own
the same bytes?
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from douyin_knowledge.core.errors import ValidationError

# Extensions we are willing to write. The value is informational only -- the ASR
# provider sniffs content, and a wrong suffix is not worth failing a download over --
# but keeping the set closed stops a hostile `Content-Disposition` from choosing
# `.py` or `.so` inside the data dir.
SAFE_SUFFIXES = frozenset(
    {".mp4", ".m4a", ".mp3", ".aac", ".wav", ".webm", ".mov", ".ogg", ".flac", ".opus", ".bin"}
)

_MIME_SUFFIX = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/opus": ".opus",
}

_UNSAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


def url_fingerprint(url: str) -> str:
    """A hash of a media URL that survives re-signing.

    Douyin's CDN hands out URLs whose query string carries an expiring signature, a
    request id, and assorted telemetry, all of which change on every single capture
    even when the underlying file is byte-identical. Fingerprinting the whole URL
    would therefore report "the content changed" on every sync, and each sync would
    re-download gigabytes it already had.

    So the fingerprint covers host and path only. The cost is that two assets on the
    *same source* with the *same asset_type*, distinguished purely by query
    parameters, would collide -- which is the narrow and unlikely case, whereas
    re-signing is the guaranteed one. Scheme is excluded too, so an http->https
    upgrade is not a content change.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    basis = f"{host}{parts.path}"
    if not basis:
        # A URL with neither host nor path cannot identify a file. Falling back to
        # hashing "" would make every such asset collide into one row.
        raise ValidationError("media URL carries no host or path to identify it", url=url[:200])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def suffix_for(url: str, mime_type: str | None) -> str:
    """Pick a file extension from the MIME type, falling back to the URL's own.

    MIME first: the URL path often ends in a CDN token rather than a filename, and
    `.bin` is a better outcome than inventing a suffix from a signature fragment.
    """
    if mime_type:
        mapped = _MIME_SUFFIX.get(mime_type.split(";")[0].strip().lower())
        if mapped:
            return mapped
    candidate = Path(urlsplit(url).path).suffix.lower()
    if candidate in SAFE_SUFFIXES:
        return candidate
    return ".bin"


def _safe_segment(value: str, *, fallback: str) -> str:
    """Reduce an untrusted id to one path segment that cannot escape its parent.

    Douyin ids are numeric in practice, but `storage_key` is built from provider data,
    and a value like `../../../etc` would otherwise resolve outside the media dir.
    """
    cleaned = _UNSAFE_SEGMENT.sub("_", value).strip("._")
    return cleaned[:96] or fallback


class MediaStore:
    """Resolves relative storage keys against the media directory.

    The stored key is always relative and always POSIX-separated, so the same SQLite
    file works after the data dir moves or crosses platforms (AGENTS s14). Absolute
    paths are a hard error rather than a passthrough: accepting one would put a
    developer's home directory into a row that outlives the machine.
    """

    def __init__(self, media_dir: Path) -> None:
        self.media_dir = Path(media_dir)

    # ---- naming --------------------------------------------------------
    def build_storage_key(
        self,
        *,
        platform: str,
        external_id: str,
        asset_type: str,
        url: str,
        mime_type: str | None = None,
        retention_class: str = "cache",
    ) -> str:
        """Name the local destination for one remote asset.

        Deterministic in its inputs, so the same asset resolves to the same path on
        every sync -- that determinism is what makes re-download idempotent instead of
        accumulating `file (1)`, `file (2)` copies. The fingerprint in the filename is
        what makes a *changed* URL land somewhere new rather than overwriting bytes
        another row still points at.
        """
        bucket = "pinned" if retention_class == "retained" else "cache"
        digest = url_fingerprint(url)[:16]
        name = f"{_safe_segment(asset_type, fallback='asset')}-{digest}{suffix_for(url, mime_type)}"
        return "/".join(
            (
                bucket,
                _safe_segment(platform, fallback="unknown"),
                _safe_segment(external_id, fallback="unknown"),
                name,
            )
        )

    # ---- resolving -----------------------------------------------------
    def resolve(self, storage_key: str) -> Path:
        """Absolute path for a stored key, refusing anything that escapes the root."""
        if not storage_key:
            raise ValidationError("storage_key is empty")
        candidate = Path(storage_key)
        if candidate.is_absolute():
            raise ValidationError(
                "storage_key must be relative to the media dir, not an absolute path",
                storage_key=storage_key[:200],
            )
        if candidate.drive or storage_key.startswith("\\\\"):
            raise ValidationError("storage_key must not carry a drive or UNC prefix")
        root = self.media_dir.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise ValidationError(
                "storage_key escapes the media directory", storage_key=storage_key[:200]
            )
        return resolved

    def exists(self, storage_key: str | None) -> bool:
        """True when the key names a non-empty local file.

        Zero-length counts as absent: an interrupted download can leave an empty file,
        and handing that to ASR buys a confusing provider error instead of a retry.
        """
        if not storage_key:
            return False
        try:
            path = self.resolve(storage_key)
        except ValidationError:
            return False
        return path.is_file() and path.stat().st_size > 0

    def size_of(self, storage_key: str) -> int | None:
        try:
            path = self.resolve(storage_key)
        except ValidationError:
            return None
        return path.stat().st_size if path.is_file() else None

    def ensure_parent(self, storage_key: str) -> Path:
        path = self.resolve(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def delete(self, storage_key: str | None) -> bool:
        """Remove a local file, tolerating its absence. Returns True if bytes went away."""
        if not storage_key:
            return False
        try:
            path = self.resolve(storage_key)
        except ValidationError:
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True

"""The CaptureProvider boundary.

Douyin Knowledge --> CaptureProvider --> DouyinCaptureProvider --> HTTP --> sidecar

Everything platform-specific stops here. No reverse-engineering code, no cookie
handling, no signature computation lives in this repository (AGENTS 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from douyin_knowledge.capture.models import (
    CapturedCollection,
    CapturedMedia,
    CapturedSource,
    ProviderHealth,
    SourcePage,
)


@runtime_checkable
class CaptureProvider(Protocol):
    """Read-only access to a platform's saved content."""

    name: str
    platform: str

    def health(self) -> ProviderHealth:
        """Cheap reachability/credential check for ``dk doctor`` and the UI."""
        ...

    def list_collections(self) -> list[CapturedCollection]:
        ...

    def list_collection_sources(
        self, external_collection_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> SourcePage:
        ...

    def fetch_source(self, external_id: str) -> CapturedSource:
        """Re-fetch one item. Raises SourceUnavailable when it is gone upstream."""
        ...

    def download_media(self, media: CapturedMedia, destination: Path) -> Path:
        """Fetch bytes to ``destination``. Providers may refuse (fixtures do not need it)."""
        ...

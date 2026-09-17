"""Provider registry: factory keyed on Settings.capture_provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from douyin_knowledge.capture.base import CaptureProvider
from douyin_knowledge.capture.douyin_provider import DouyinCaptureProvider
from douyin_knowledge.capture.fixture_provider import FixtureCaptureProvider
from douyin_knowledge.core.errors import ConfigurationError

if TYPE_CHECKING:
    from douyin_knowledge.config.settings import Settings

ProviderKind = Literal["fixture", "douyin"]


def get_capture_provider(settings: Settings) -> CaptureProvider:
    """Build the configured capture provider."""
    kind = settings.capture_provider
    if kind == "fixture":
        return FixtureCaptureProvider()
    if kind == "douyin":
        if not settings.douyin_sidecar_url or settings.douyin_sidecar_url.strip() == "":
            raise ConfigurationError(
                "DK_CAPTURE_PROVIDER=douyin requires DK_DOUYIN_SIDECAR_URL"
            )
        return DouyinCaptureProvider(
            base_url=settings.douyin_sidecar_url,
            api_key=settings.douyin_sidecar_api_key,
        )
    raise ConfigurationError(f"unknown capture provider {kind!r}")

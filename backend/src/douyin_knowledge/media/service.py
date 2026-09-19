"""Acquisition as a state machine over ``SourceAsset``.

One asset moves ``pending -> ready`` (bytes on disk, hashed), ``-> failed`` (retryable
cause, attempts recorded), or ``-> unavailable`` (permanent: gone, too large, no URL).
``skipped`` is set by callers that decided not to spend the download at all.

Idempotence is checked against the disk, not just the row: if the file is present and
non-empty, acquisition returns ``already_present`` without a request. That matters
because a job can be reclaimed after its lease expires (queue.py) and re-run a
download that in fact completed -- and because it makes re-running ``dk`` cheap.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from douyin_knowledge.core.clock import now_ms
from douyin_knowledge.core.errors import DKError, MediaDownloadFailed, ValidationError
from douyin_knowledge.db.models.capture import Source, SourceAsset
from douyin_knowledge.media.downloader import MediaDownloader
from douyin_knowledge.media.store import MediaStore
from douyin_knowledge.observability import get_logger

logger = get_logger(__name__)

# Asset kinds worth fetching for transcription. Images are excluded: OCR/Vision is
# explicitly out of scope for this pass, so downloading covers would cost bandwidth for
# a capability that does not exist yet.
TRANSCRIBABLE_TYPES = ("audio", "video")


@dataclass(slots=True)
class AcquisitionResult:
    asset_id: str
    state: str
    outcome: str
    byte_size: int | None = None
    error: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == SourceAsset.DOWNLOAD_READY

    def as_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "state": self.state,
            "outcome": self.outcome,
            "byte_size": self.byte_size,
            "error": self.error,
        }


class MediaAcquisitionService:
    """Fetches media for a source and records the attempt.

    Does not commit: the worker owns the transaction boundary, as handlers.py requires.
    """

    def __init__(
        self,
        session: Session,
        store: MediaStore,
        downloader: MediaDownloader,
        *,
        max_attempts: int = 3,
    ) -> None:
        self.session = session
        self.store = store
        self.downloader = downloader
        self.max_attempts = max_attempts

    # ---- selection -----------------------------------------------------
    def transcribable_assets(self, source_id: str) -> list[SourceAsset]:
        """Assets that could feed ASR, audio before video.

        Ordering is the point: a provider that offers a 200 MiB video and a 3 MiB audio
        track of the same content should cost us 3 MiB. ``asset_type`` sorts
        ``audio`` < ``video`` alphabetically, which happens to be the order we want, but
        relying on that coincidence would break the day a kind called ``clip`` appears,
        so the key is explicit.
        """
        assets = list(
            self.session.scalars(
                select(SourceAsset).where(
                    SourceAsset.source_id == source_id,
                    SourceAsset.asset_type.in_(TRANSCRIBABLE_TYPES),
                )
            ).all()
        )
        order = {kind: index for index, kind in enumerate(TRANSCRIBABLE_TYPES)}
        assets.sort(key=lambda a: (order.get(a.asset_type, 99), a.created_at_ms))
        return assets

    def local_asset_for_asr(self, source_id: str) -> SourceAsset | None:
        """The best asset whose bytes are actually on disk, or None."""
        for asset in self.transcribable_assets(source_id):
            if asset.download_state == SourceAsset.DOWNLOAD_READY and self.store.exists(
                asset.storage_key
            ):
                return asset
        return None

    def pending_assets(self, source_id: str) -> list[SourceAsset]:
        """Assets still worth attempting: never-fetched, or failed under the retry cap.

        ``unavailable`` is excluded -- that state means the URL is permanently dead, and
        a fresh capture will create a new row rather than revive this one.
        """
        candidates: list[SourceAsset] = []
        for asset in self.transcribable_assets(source_id):
            if asset.download_state == SourceAsset.DOWNLOAD_READY and self.store.exists(
                asset.storage_key
            ):
                continue
            if asset.download_state == SourceAsset.DOWNLOAD_UNAVAILABLE:
                continue
            if (
                asset.download_state == SourceAsset.DOWNLOAD_FAILED
                and asset.download_attempts >= self.max_attempts
            ):
                continue
            candidates.append(asset)
        return candidates

    # ---- acquisition ---------------------------------------------------
    def acquire(self, asset: SourceAsset) -> AcquisitionResult:
        """Ensure this asset's bytes are on disk. Safe to call repeatedly."""
        if self.store.exists(asset.storage_key):
            # Disk beats the row. A reclaimed job whose predecessor actually finished
            # lands here, and re-downloading would be pure waste.
            if asset.download_state != SourceAsset.DOWNLOAD_READY:
                asset.download_state = SourceAsset.DOWNLOAD_READY
                asset.downloaded_at_ms = asset.downloaded_at_ms or now_ms()
                asset.download_error = None
                asset.byte_size = asset.byte_size or self.store.size_of(asset.storage_key)
                self.session.flush()
            return AcquisitionResult(
                asset_id=asset.id,
                state=asset.download_state,
                outcome="already_present",
                byte_size=asset.byte_size,
            )

        if not asset.remote_url:
            # Nothing to fetch and nothing local. Permanent for this row: the URL is not
            # going to materialize without a new capture.
            return self._mark_unavailable(asset, "asset has no remote_url to fetch")

        asset.download_attempts += 1
        try:
            result = self.downloader.download(asset.remote_url, asset.storage_key)
        except ValidationError as exc:
            # Permanent by classification: gone, oversized, or a rejected scheme.
            return self._mark_unavailable(asset, str(exc))
        except (MediaDownloadFailed, DKError) as exc:
            asset.download_state = SourceAsset.DOWNLOAD_FAILED
            asset.download_error = str(exc)
            self.session.flush()
            logger.warning(
                "media_acquire_failed",
                extra={"asset_id": asset.id, "attempts": asset.download_attempts},
            )
            # Re-raised so the queue applies its own retry policy rather than this
            # service inventing a second one. The row already records the attempt, so
            # the failure is observable even if the job is never retried.
            raise

        asset.download_state = SourceAsset.DOWNLOAD_READY
        asset.downloaded_at_ms = now_ms()
        asset.download_error = None
        asset.byte_size = result.byte_size
        asset.sha256 = result.sha256
        # Only fill mime_type; never overwrite what the provider declared with a CDN's
        # generic `application/octet-stream`.
        asset.mime_type = asset.mime_type or result.mime_type
        self.session.flush()
        return AcquisitionResult(
            asset_id=asset.id,
            state=asset.download_state,
            outcome="downloaded",
            byte_size=result.byte_size,
        )

    def acquire_for_source(self, source_id: str) -> list[AcquisitionResult]:
        """Acquire every still-needed transcribable asset for one source.

        Stops at the first success: one usable audio track is all ASR consumes, and
        fetching the video too would double the bandwidth for no extra evidence.
        """
        source = self.session.get(Source, source_id)
        if source is None:
            raise ValidationError("source not found", source_id=source_id)

        results: list[AcquisitionResult] = []
        for asset in self.pending_assets(source_id):
            result = self.acquire(asset)
            results.append(result)
            if result.ready:
                break
        return results

    def _mark_unavailable(self, asset: SourceAsset, reason: str) -> AcquisitionResult:
        asset.download_state = SourceAsset.DOWNLOAD_UNAVAILABLE
        asset.download_error = reason
        self.session.flush()
        logger.info("media_unavailable", extra={"asset_id": asset.id, "reason": reason})
        return AcquisitionResult(
            asset_id=asset.id,
            state=asset.download_state,
            outcome="unavailable",
            error=reason,
        )

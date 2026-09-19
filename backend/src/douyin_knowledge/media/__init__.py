"""Local media acquisition: fetch what the capture provider pointed at, keep it addressable.

This package exists because of a single confusion that broke ASR on real sources:
``SourceAsset.storage_key`` held the provider's absolute URL, so the orchestrator's
``media_dir / storage_key`` produced a path that could never exist, and every real
source quietly recorded ``asr_skipped_media_not_downloaded`` (DEC-014).

The split this package enforces:

* ``remote_url`` -- where the bytes came from. Provider-signed, short-lived, and
  *rotates on every capture even when the content is identical*.
* ``storage_key`` -- a machine-independent relative path under ``DK_DATA_DIR/media``
  naming where the bytes live locally. Never absolute (AGENTS s14), so a data dir
  can move between machines without rewriting rows.
* ``download_state`` -- whether those bytes are actually on disk. A row can name a
  destination long before anything has been fetched into it, which is why presence
  is a column and not an inference from the path.

Nothing here talks to Douyin. This is an HTTP GET against a URL some
:class:`CaptureProvider` already handed us, which keeps the reverse-engineering
boundary in AGENTS s6 intact: swap the provider and this package is unchanged.
"""

from douyin_knowledge.media.downloader import DownloadResult, MediaDownloader
from douyin_knowledge.media.service import MediaAcquisitionService
from douyin_knowledge.media.store import MediaStore, url_fingerprint

__all__ = [
    "DownloadResult",
    "MediaAcquisitionService",
    "MediaDownloader",
    "MediaStore",
    "url_fingerprint",
]

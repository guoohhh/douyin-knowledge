"""Job type and priority vocabulary."""

from __future__ import annotations

from enum import StrEnum


class JobType(StrEnum):
    SYNC_COLLECTIONS = "sync_collections"
    SYNC_COLLECTION_SOURCES = "sync_collection_sources"
    PROCESS_SOURCE = "process_source"
    ENRICH_SOURCE = "enrich_source"
    REPROCESS_SOURCE = "reprocess_source"
    CLEANUP_CACHE = "cleanup_cache"
    REBUILD_FTS = "rebuild_fts"
    REBUILD_VECTORS = "rebuild_vectors"
    WIKI_INTEGRATE = "wiki_integrate"
    WIKI_LINT = "wiki_lint"
    WIKI_MAINTAIN = "wiki_maintain"
    EXPORT_MARKDOWN = "export_markdown"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class Priority:
    """Higher integer = more urgent (the schema does not state a direction; DEC-C8).

    Bands are reserved so query-triggered work always overtakes bulk backfill.
    """

    BULK = 0  # historical backfill of thousands of saved items
    NORMAL = 10  # ordinary ingest and processing
    MAINTENANCE = 20  # wiki lint, index rebuilds
    INTERACTIVE = 50  # user is looking at this right now
    QUERY_TRIGGERED = 80  # a live question is blocked on this enrichment

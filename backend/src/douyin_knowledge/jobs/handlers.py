"""Default job handlers.

Each handler is a thin adapter: it reads the payload, builds the service the layer
below already owns, and records what happened as job events. No business logic lives
here -- if a handler starts making decisions, that decision belongs in the service so
the CLI and the API get it too.

Handlers never commit. The worker owns the transaction boundary (`session_scope`), so
a handler that half-succeeds rolls back cleanly instead of leaving the currency
pointer advanced past evidence that was never written.
"""

from __future__ import annotations

from typing import Any

from douyin_knowledge.ai.registry import (
    get_asr_provider,
    get_embedding_model,
    get_structured_model,
)
from douyin_knowledge.capture.registry import get_capture_provider
from douyin_knowledge.capture.sync import CaptureSyncService
from douyin_knowledge.config import Settings
from douyin_knowledge.core.errors import DKError, ValidationError
from douyin_knowledge.extraction.orchestrator import ProcessingOrchestrator
from douyin_knowledge.jobs.registry import HandlerRegistry, JobContext
from douyin_knowledge.jobs.types import JobType, Priority
from douyin_knowledge.observability import get_logger
from douyin_knowledge.policy.evaluator import PolicyEvaluator
from douyin_knowledge.policy.models import PolicyAction, PolicyDecision
from douyin_knowledge.policy.repository import PolicyRepository
from douyin_knowledge.retrieval.vector_store import VectorStore
from douyin_knowledge.search import indexer
from douyin_knowledge.wiki.updater import WikiUpdater

logger = get_logger(__name__)


class JobPayloadError(ValidationError):
    """A job payload is missing something the handler cannot invent.

    Deliberately non-retryable (inherited): a malformed payload will be just as
    malformed in five minutes, and retrying it only delays the visible failure.
    """

    code = "job_payload_invalid"


def _require(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value in (None, ""):
        raise JobPayloadError(f"job payload is missing required key {key!r}")
    return value


def _vector_store(settings: Settings) -> VectorStore:
    settings.ensure_directories()
    return VectorStore(settings.vector_dir)


# --------------------------------------------------------------------- capture


def handle_sync_collections(ctx: JobContext) -> None:
    """Walk every collection the provider exposes and sync it.

    Follow-up processing is enqueued rather than done inline: a sync of a thousand
    saved items must not block on a thousand extractions, and each source needs its
    own retry budget.
    """
    provider = get_capture_provider(ctx.settings)
    service = CaptureSyncService(ctx.session, platform=ctx.settings.capture_provider)
    payload = ctx.payload
    auto_process = bool(payload.get("auto_process", True))

    stats = None
    collections = provider.list_collections()
    for captured in collections:
        stats = service.sync_collection(provider, captured, stats=stats)

    if stats is None:
        ctx.emit("sync_empty", "provider exposed no collections")
        return

    ctx.emit("sync_complete", f"{stats.sources_created} new", **stats.as_dict())

    if auto_process:
        enqueued = 0
        for source_id in dict.fromkeys(stats.source_ids):
            result = ctx.queue.enqueue(
                JobType.PROCESS_SOURCE,
                source_id=source_id,
                payload={"target_level": ctx.settings.default_desired_level},
                dedupe_key=f"process_source:{source_id}",
                priority=Priority.BULK,
            )
            enqueued += 1 if result.created else 0
        ctx.emit("processing_enqueued", f"{enqueued} sources", count=enqueued)


def handle_sync_collection_sources(ctx: JobContext) -> None:
    """Sync one named collection. Used when the user refreshes a single folder."""
    external_id = _require(ctx.payload, "external_collection_id")
    provider = get_capture_provider(ctx.settings)
    service = CaptureSyncService(ctx.session, platform=ctx.settings.capture_provider)

    match = next(
        (c for c in provider.list_collections() if c.external_collection_id == external_id),
        None,
    )
    if match is None:
        raise JobPayloadError(f"provider has no collection {external_id!r}")

    stats = service.sync_collection(provider, match)
    ctx.emit("sync_complete", match.name, **stats.as_dict())


# ------------------------------------------------------------------ processing


def _apply_policy(ctx: JobContext, source_id: str) -> tuple[bool, PolicyDecision]:
    """Gate the pipeline on processing policy, recording why.

    This runs inside the handler rather than at enqueue time so the decision reflects the
    rules as they stand when the work actually happens: a user who adds an exclude rule
    while a bulk sync is still draining expects the queued items to respect it.
    """
    from douyin_knowledge.db.models.capture import Source

    source = ctx.session.get(Source, source_id)
    if source is None:
        raise JobPayloadError("source not found", source_id=source_id)

    evaluator = PolicyEvaluator(PolicyRepository(ctx.session))
    return evaluator.should_process(source)


def _orchestrator(settings: Settings) -> ProcessingOrchestrator:
    asr = None
    try:
        asr = get_asr_provider(settings)
    except DKError:
        # ASR is optional: level 1 (metadata + captions) is still worth having when
        # transcription is unconfigured. Failing the whole job here would mean a
        # missing Whisper key blocks all text extraction too.
        logger.warning("asr_provider_unavailable")
    return ProcessingOrchestrator(
        settings,
        structured_model=get_structured_model(settings),
        asr_provider=asr,
    )


def handle_process_source(ctx: JobContext) -> None:
    """Run the extraction ladder, then chain indexing and wiki integration.

    Indexing and wiki work are separate jobs on purpose: they are cheap and
    idempotent, and keeping them out of this transaction means a wiki composition bug
    cannot roll back hours of transcription.
    """
    source_id = ctx.job.source_id or _require(ctx.payload, "source_id")
    target_level = int(ctx.payload.get("target_level") or ctx.settings.default_desired_level)

    allowed, decision = _apply_policy(ctx, source_id)
    if not allowed:
        # A skip is a first-class outcome, not an error. The decision row is the answer to
        # "why wasn't this processed?", so the job succeeds and says what it decided.
        ctx.emit(
            "skipped_by_policy",
            decision.reason_code,
            action=str(decision.action),
            rule_id=decision.rule_id,
            explanation=decision.explanation,
        )
        return
    if decision.action == PolicyAction.ALWAYS_PROCESS:
        # An explicit pin means the user wants the full ladder regardless of the default.
        target_level = max(target_level, ctx.settings.max_processing_level)

    orchestrator = _orchestrator(ctx.settings)
    outcome = orchestrator.process_source(ctx.session, source_id, target_level=target_level)
    ctx.emit("processed", outcome.status, **outcome.as_dict())

    if outcome.status not in ("succeeded", "partial"):
        return

    ctx.queue.enqueue(
        JobType.REBUILD_VECTORS,
        source_id=source_id,
        payload={"source_ids": [source_id]},
        dedupe_key=f"index_source:{source_id}:{outcome.run_id}",
        priority=Priority.INTERACTIVE,
    )
    ctx.queue.enqueue(
        JobType.WIKI_INTEGRATE,
        source_id=source_id,
        payload={"source_id": source_id},
        dedupe_key=f"wiki_integrate:{source_id}:{outcome.run_id}",
        priority=Priority.MAINTENANCE,
    )


def handle_reprocess_source(ctx: JobContext) -> None:
    """Re-run extraction at a possibly higher level. A run is a new version, never a
    mutation, so this is the same call path as first processing (DB-004)."""
    handle_process_source(ctx)


# --------------------------------------------------------------------- indexing


def handle_rebuild_fts(ctx: JobContext) -> None:
    """Keyword-only reindex. Scoped when the payload names sources, full otherwise."""
    source_ids = ctx.payload.get("source_ids") or []
    if source_ids:
        total = indexer.IndexStats()
        for source_id in source_ids:
            stats = indexer.reindex_source(ctx.session, source_id)
            total.inserted += stats.inserted
            total.updated += stats.updated
            total.unchanged += stats.unchanged
            total.deleted += stats.deleted
        ctx.emit("fts_reindexed", f"{len(source_ids)} sources", **total.as_dict())
        return

    stats = indexer.reindex_all(ctx.session)
    ctx.emit("fts_reindexed", "full", **stats.as_dict())


def handle_rebuild_vectors(ctx: JobContext) -> None:
    """Keyword + vector reindex.

    Scoped runs still rebuild vectors globally for the affected documents via
    ``sync_vectors``, which skips anything whose content hash and model are unchanged,
    so the cost is proportional to what actually moved rather than to corpus size.
    """
    source_ids = ctx.payload.get("source_ids") or []
    store = _vector_store(ctx.settings)
    embedder = get_embedding_model(ctx.settings)
    model_name = ctx.settings.model_for_role("embedding")

    stats = indexer.IndexStats()
    if source_ids:
        for source_id in source_ids:
            scoped = indexer.reindex_source(ctx.session, source_id)
            stats.inserted += scoped.inserted
            stats.updated += scoped.updated
            stats.unchanged += scoped.unchanged
            stats.deleted += scoped.deleted
        ctx.session.flush()
        indexer.sync_vectors(
            ctx.session, store, embedder, model_name=model_name, stats=stats
        )
        store.save()
    else:
        stats = indexer.reindex_all(
            ctx.session, store=store, embedder=embedder, model_name=model_name
        )

    ctx.emit("vectors_rebuilt", model_name, **stats.as_dict())


# ------------------------------------------------------------------------ wiki


def handle_wiki_integrate(ctx: JobContext) -> None:
    source_id = ctx.job.source_id or _require(ctx.payload, "source_id")
    updater = WikiUpdater(ctx.session, model_name=ctx.settings.model_for_role("wiki_integration"))
    # `integrate_source` lints the pages it touched inside its own run, so there is
    # no follow-up lint job here: a second pass would only re-close and re-open the
    # same findings under a different run id.
    result = updater.integrate_source(source_id)
    ctx.emit("wiki_integrated", result.status, **result.as_dict())


def handle_wiki_maintain(ctx: JobContext) -> None:
    """Full wiki rebuild. Cheap enough to be the recovery path for any doubt about
    whether compiled pages still match the spine."""
    updater = WikiUpdater(ctx.session, model_name=ctx.settings.model_for_role("wiki_integration"))
    result = updater.rebuild_all()
    ctx.emit("wiki_rebuilt", result.status, **result.as_dict())


def handle_wiki_lint(ctx: JobContext) -> None:
    page_ids = ctx.payload.get("page_ids") or []
    updater = WikiUpdater(ctx.session)
    findings = updater.lint_pages(page_ids)
    ctx.emit("wiki_linted", f"{len(findings)} findings", count=len(findings))


# --------------------------------------------------------------------- registry


def register_default_handlers(registry: HandlerRegistry | None = None) -> HandlerRegistry:
    """Install the production handler set.

    Explicit rather than decorator-driven so ``grep register_default_handlers`` shows
    the complete list of work this system can perform.
    """
    registry = registry or HandlerRegistry()
    registry.register(JobType.SYNC_COLLECTIONS, handle_sync_collections)
    registry.register(JobType.SYNC_COLLECTION_SOURCES, handle_sync_collection_sources)
    registry.register(JobType.PROCESS_SOURCE, handle_process_source)
    registry.register(JobType.REPROCESS_SOURCE, handle_reprocess_source)
    registry.register(JobType.REBUILD_FTS, handle_rebuild_fts)
    registry.register(JobType.REBUILD_VECTORS, handle_rebuild_vectors)
    registry.register(JobType.WIKI_INTEGRATE, handle_wiki_integrate)
    registry.register(JobType.WIKI_MAINTAIN, handle_wiki_maintain)
    registry.register(JobType.WIKI_LINT, handle_wiki_lint)
    return registry

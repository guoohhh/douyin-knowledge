"""Pipeline commands: sync, process, worker, reindex, wiki.

Each command enqueues real jobs and then optionally drains them in-process. Running the
same handlers the background worker runs is deliberate: a CLI that reimplemented the
pipeline would drift, and the drift would show up as "it works from the command line but
not in the app".
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from douyin_knowledge.cli.main import (
    JsonOpt,
    _ready,
    app,
    console,
    emit,
    fail,
    status,
    wiki_app,
)
from douyin_knowledge.jobs.handlers import register_default_handlers
from douyin_knowledge.jobs.types import JobType, Priority


def _drain(settings, *, max_jobs: int, quiet: bool = False) -> int:
    """Run queued jobs in this process until the queue is empty."""
    from douyin_knowledge.jobs.worker import Worker

    worker = Worker(register_default_handlers(), settings=settings, name="dk-cli")
    if not quiet:
        with console.status("[cyan]working…[/cyan]"):
            return worker.drain(max_jobs=max_jobs)
    return worker.drain(max_jobs=max_jobs)


def _enqueue(job_type: JobType, **kwargs):
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.jobs.queue import JobQueue

    with session_scope() as session:
        result = JobQueue(session).enqueue(job_type, **kwargs)
        return result.job.id, result.created


# ------------------------------------------------------------------------- sync


@app.command()
def sync(
    collection: Annotated[
        str | None,
        typer.Option("--collection", "-c", help="External collection id; omit for all."),
    ] = None,
    process: Annotated[
        bool, typer.Option("--process/--no-process", help="Process new sources after syncing.")
    ] = True,
    wait: Annotated[
        bool, typer.Option("--wait/--queue-only", help="Drain the queue now, or just enqueue.")
    ] = True,
    max_jobs: int = typer.Option(1000, help="Safety cap on jobs drained in one run."),
    as_json: JsonOpt = False,
) -> None:
    """Pull collections from the capture provider into the local corpus."""
    settings = _ready()

    if collection:
        job_type = JobType.SYNC_COLLECTION_SOURCES
        payload = {"collection_external_id": collection, "auto_process": process}
        dedupe = f"sync_collection:{collection}"
    else:
        job_type = JobType.SYNC_COLLECTIONS
        payload = {"auto_process": process}
        dedupe = "sync_collections:all"

    job_id, created = _enqueue(
        job_type, payload=payload, dedupe_key=dedupe, priority=Priority.INTERACTIVE
    )
    if not created:
        console.print("[yellow]a sync is already queued; reusing it[/yellow]")

    drained = _drain(settings, max_jobs=max_jobs) if wait else 0
    emit({"job_id": job_id, "queued": created, "jobs_run": drained}, as_json=as_json)
    if wait:
        status(as_json=as_json)


@app.command()
def process(
    source_id: Annotated[
        str | None,
        typer.Argument(help="Source id; omit to process everything still pending."),
    ] = None,
    level: Annotated[
        int | None,
        typer.Option("--level", "-l", min=0, max=4, help="Target processing level."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Reprocess even if a current run exists.")
    ] = False,
    wait: bool = typer.Option(True, "--wait/--queue-only"),
    max_jobs: int = typer.Option(1000),
    as_json: JsonOpt = False,
) -> None:
    """Run the extraction ladder over one source or the whole backlog."""
    from sqlalchemy import select

    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.capture import Source
    from douyin_knowledge.db.models.policy import SourceProcessingState

    settings = _ready()
    if level is not None and level > settings.max_processing_level:
        fail(
            f"level {level} exceeds the configured maximum ({settings.max_processing_level}); "
            "raise DK_MAX_PROCESSING_LEVEL to allow it"
        )

    payload = {"target_level": level} if level is not None else {}
    job_type = JobType.REPROCESS_SOURCE if force else JobType.PROCESS_SOURCE

    if source_id:
        targets = [source_id]
    else:
        with session_scope() as session:
            stmt = select(Source.id).where(Source.locally_deleted_at_ms.is_(None))
            if not force:
                # Only sources without a current run: re-running the whole corpus is what
                # `--force` is for, and doing it by default would burn a full set of model
                # calls every time someone typed `dk process`.
                stmt = stmt.outerjoin(
                    SourceProcessingState,
                    SourceProcessingState.source_id == Source.id,
                ).where(SourceProcessingState.current_processing_run_id.is_(None))
            targets = list(session.scalars(stmt))

    if not targets:
        console.print("[green]nothing to process[/green]")
        return

    for target in targets:
        _enqueue(
            job_type,
            source_id=target,
            payload=payload,
            dedupe_key=None if force else f"process_source:{target}",
            priority=Priority.INTERACTIVE if source_id else Priority.BULK,
        )

    drained = _drain(settings, max_jobs=max_jobs) if wait else 0
    emit({"queued": len(targets), "jobs_run": drained}, as_json=as_json)


@app.command()
def worker(
    once: Annotated[bool, typer.Option("--once", help="Run a single job and exit.")] = False,
    drain: Annotated[
        bool, typer.Option("--drain", help="Run until the queue is empty, then exit.")
    ] = False,
    max_jobs: int = typer.Option(1000, help="Cap for --drain."),
) -> None:
    """Run the background worker.

    Default is to run forever; `--drain` is the mode a script wants, and `--once` is for
    stepping through a poison job by hand.
    """
    from douyin_knowledge.jobs.worker import Worker

    settings = _ready()
    w = Worker(register_default_handlers(), settings=settings)

    if once:
        did_work = w.run_once()
        console.print("[green]ran one job[/green]" if did_work else "[yellow]queue empty[/yellow]")
        return
    if drain:
        console.print(f"[green]ran {w.drain(max_jobs=max_jobs)} jobs[/green]")
        return

    console.print(f"[cyan]worker {w.name} started[/cyan]  (ctrl-c to stop)")
    try:
        w.run_forever()
    except KeyboardInterrupt:
        w.stop()
        console.print("\n[yellow]stopped[/yellow]")


@app.command()
def reindex(
    vectors: Annotated[
        bool, typer.Option("--vectors/--fts-only", help="Also rebuild embeddings.")
    ] = True,
    wait: bool = typer.Option(True, "--wait/--queue-only"),
    as_json: JsonOpt = False,
) -> None:
    """Rebuild the search indexes from the spine.

    Safe to run at any time: the indexes are compiled views, so the worst case is spending
    the time again.
    """
    settings = _ready()
    job_id, _ = _enqueue(
        JobType.REBUILD_VECTORS if vectors else JobType.REBUILD_FTS,
        payload={"source_ids": []},
        priority=Priority.MAINTENANCE,
    )
    drained = _drain(settings, max_jobs=50) if wait else 0
    emit({"job_id": job_id, "jobs_run": drained}, as_json=as_json)


# ------------------------------------------------------------------------- wiki


@wiki_app.command("rebuild")
def wiki_rebuild(as_json: JsonOpt = False) -> None:
    """Recompile every wiki page from claims and evidence."""
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.wiki.updater import WikiUpdater

    settings = _ready()
    with session_scope() as session:
        updater = WikiUpdater(session, model_name=settings.model_for_role("wiki_integration"))
        result = updater.rebuild_all().as_dict()
    emit(result, as_json=as_json)


@wiki_app.command("list")
def wiki_list(limit: int = typer.Option(50, min=1)) -> None:
    """List wiki pages with their current revision."""
    from sqlalchemy import select

    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.wiki import WikiPage, WikiRevision

    _ready()
    table = Table("title", "type", "rev", "slug", title="Wiki pages")
    with session_scope() as session:
        pages = list(
            session.scalars(select(WikiPage).where(WikiPage.status == "active").limit(limit))
        )
        for page in pages:
            revision = session.scalars(
                select(WikiRevision).where(
                    WikiRevision.page_id == page.id, WikiRevision.is_current == 1
                )
            ).first()
            table.add_row(
                page.title,
                page.page_type,
                str(revision.revision_no) if revision else "-",
                page.slug or "-",
            )
    console.print(table)
    if not pages:
        console.print("[yellow]no pages yet; run `dk sync` then `dk wiki rebuild`[/yellow]")


@wiki_app.command("show")
def wiki_show(page: Annotated[str, typer.Argument(help="Page id or slug.")]) -> None:
    """Print a page with its supporting citations."""
    from sqlalchemy import select

    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.capture import Source
    from douyin_knowledge.db.models.wiki import WikiPage, WikiRevision, WikiSupport

    _ready()
    with session_scope() as session:
        row = session.get(WikiPage, page) or session.scalars(
            select(WikiPage).where(WikiPage.slug == page)
        ).first()
        if row is None:
            fail(f"no wiki page {page!r}")

        revision = session.scalars(
            select(WikiRevision).where(
                WikiRevision.page_id == row.id, WikiRevision.is_current == 1
            )
        ).first()
        if revision is None:
            fail(f"page {row.title!r} has no current revision")

        console.print(f"\n[bold]{row.title}[/bold]  (rev {revision.revision_no})\n")
        console.print(revision.content_markdown or "[dim](empty)[/dim]")

        supports = list(
            session.scalars(
                select(WikiSupport)
                .where(WikiSupport.wiki_revision_id == revision.id)
                .order_by(WikiSupport.statement_key)
            )
        )
        if not supports:
            # WIKI-002: an uncited page is a defect. Say so rather than printing it as if
            # the text were established knowledge.
            console.print("\n[red]this page has no citations[/red]")
            return

        # A support may cite a claim, an evidence unit or a source (the table's check
        # constraint requires exactly one of the three), so a fact-level citation usually
        # has a null `source_id`. Resolving the claim back to its source is required for
        # the column to mean anything: printing "-" for every fact makes a fully cited
        # page look uncited, which is the opposite of what WIKI-002 is protecting.
        from douyin_knowledge.db.models.entities import Claim

        claim_sources = {
            claim.id: claim.source_id
            for claim in session.scalars(
                select(Claim).where(
                    Claim.id.in_([s.claim_id for s in supports if s.claim_id])
                )
            )
        }

        def source_of(support: WikiSupport) -> str | None:
            return support.source_id or claim_sources.get(support.claim_id or "")

        wanted = {sid for sid in (source_of(s) for s in supports) if sid}
        titles = {
            s.id: (s.title or s.caption_raw or s.id)
            for s in session.scalars(select(Source).where(Source.id.in_(wanted)))
        }
        table = Table("statement", "source", "cited as", "role", title="Citations")
        for support in supports:
            sid = source_of(support)
            cited_as = "claim" if support.claim_id else ("evidence" if support.evidence_id else "source")
            table.add_row(
                support.statement_key,
                titles.get(sid or "", "-"),
                cited_as,
                support.support_role or "-",
            )
        console.print(table)

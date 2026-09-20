"""Query and inspection commands: ask, search, sources, policy.

`ask` is the product's whole point reduced to one command, so it prints citations by
default. An answer without visible provenance is indistinguishable from a guess, and the
terminal is exactly where someone checks whether the system is trustworthy.
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
    policy_app,
)


def _services(session, settings):
    from douyin_knowledge.ai.registry import get_answer_chat_model, get_embedding_model
    from douyin_knowledge.conversation.conversation_manager import ConversationManager
    from douyin_knowledge.retrieval.vector_store import VectorStore

    # `get_answer_chat_model` keeps `dk ask` and `POST /api/conversations/ask` phrasing
    # answers the same way; under the mock provider both compose deterministically rather
    # than echoing a canned string.
    return ConversationManager(
        session,
        vector_store=VectorStore(settings.vector_dir),
        embedder=get_embedding_model(settings),
        chat_model=get_answer_chat_model(settings),
        model_name=settings.model_for_role("answer"),
    )


@app.command()
def ask(
    query: Annotated[str, typer.Argument(help="Your question.")],
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            help="Force a knowledge scope: personal_required, personal_first, general, hybrid.",
        ),
    ] = None,
    limit: int = typer.Option(8, min=1, max=50, help="Evidence chunks to retrieve."),
    show_citations: Annotated[
        bool, typer.Option("--citations/--no-citations")
    ] = True,
    as_json: JsonOpt = False,
) -> None:
    """Ask a question against your own collection."""
    from douyin_knowledge.conversation.scope import SCOPES
    from douyin_knowledge.db import session_scope

    settings = _ready()
    if scope and scope not in SCOPES:
        fail(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")

    with session_scope() as session:
        result = _services(session, settings).ask(query, scope_override=scope, limit=limit)
        payload = result.as_dict()

    if as_json:
        emit(payload, as_json=True)
        return

    console.print(f"\n{payload['content']}\n")

    scope_note = payload["scope"]
    if not payload["has_evidence"]:
        # Saying this out loud matters: "no evidence" and "the model made something up"
        # look identical in a terminal otherwise.
        console.print(f"[yellow]scope {scope_note} · no matching evidence in your collection[/yellow]")
    else:
        console.print(f"[dim]scope {scope_note} · {len(payload['citations'])} citations[/dim]")

    if payload.get("conflicts"):
        console.print("\n[magenta]conflicting statements found:[/magenta]")
        for conflict in payload["conflicts"]:
            console.print(f"  · {conflict}")

    if show_citations and payload["citations"]:
        # `snippet` and `timestamp` are the keys CitationBuilder actually emits; an earlier
        # draft guessed `quote` and rendered a table of blank cells, which reads as "cited
        # but unverifiable" -- the exact impression this command exists to avoid.
        table = Table("#", "at", "source", "snippet", title="Citations")
        for citation in payload["citations"]:
            snippet = (citation.get("snippet") or citation.get("label") or "").strip()
            table.add_row(
                str(citation.get("ordinal") or "-"),
                citation.get("timestamp") or "-",
                citation.get("source_title") or citation.get("source_id") or "-",
                snippet.replace("\n", " ")[:70],
            )
        console.print(table)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search terms.")],
    limit: int = typer.Option(10, min=1, max=50),
    as_json: JsonOpt = False,
) -> None:
    """Hybrid keyword + vector search over processed evidence."""
    from douyin_knowledge.ai.registry import get_embedding_model
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.retrieval.retriever import HybridRetriever
    from douyin_knowledge.retrieval.vector_store import VectorStore

    settings = _ready()
    with session_scope() as session:
        retriever = HybridRetriever(
            session,
            vector_store=VectorStore(settings.vector_dir),
            embedder=get_embedding_model(settings),
        )
        result = retriever.retrieve(query, limit=limit)
        rows = [chunk.as_dict() for chunk in result.chunks]
        diagnostics = result.diagnostics

    if as_json:
        emit({"query": query, "results": rows, "diagnostics": diagnostics}, as_json=True)
        return

    if not rows:
        # Distinguishing "nothing matched" from "nothing is indexed" is the difference
        # between rephrasing the query and running the pipeline (RETRIEVAL.md 15).
        console.print("[yellow]no matches[/yellow]")
        console.print(f"[dim]{diagnostics}[/dim]")
        return

    table = Table("score", "source", "text", title=f"Results for {query!r}")
    for row in rows:
        text = (row.get("text") or "").replace("\n", " ")
        table.add_row(
            f"{row['score']:.3f}",
            row.get("source_title") or row["source_id"],
            text[:90] + ("…" if len(text) > 90 else ""),
        )
    console.print(table)


@app.command()
def sources(
    processing_status: Annotated[
        str | None, typer.Option("--status", help="Filter by processing status.")
    ] = None,
    limit: int = typer.Option(30, min=1, max=200),
) -> None:
    """List sources in the local corpus."""
    from sqlalchemy import select

    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.capture import Source
    from douyin_knowledge.db.models.policy import SourceProcessingState

    _ready()
    table = Table("id", "title", "status", "level", title="Sources")
    with session_scope() as session:
        stmt = select(Source).where(Source.locally_deleted_at_ms.is_(None))
        if processing_status:
            stmt = stmt.join(
                SourceProcessingState, SourceProcessingState.source_id == Source.id
            ).where(SourceProcessingState.processing_status == processing_status)
        rows = list(session.scalars(stmt.limit(limit)))

        states = {
            s.source_id: s
            for s in session.scalars(
                select(SourceProcessingState).where(
                    SourceProcessingState.source_id.in_([r.id for r in rows])
                )
            )
        } if rows else {}

        for row in rows:
            state = states.get(row.id)
            title = (row.title or row.caption_raw or "-").replace("\n", " ")
            table.add_row(
                row.id,
                title[:50],
                state.processing_status if state else "pending",
                str(state.achieved_level) if state else "0",
            )
    console.print(table)


# ----------------------------------------------------------------------- policy


@policy_app.command("list")
def policy_list() -> None:
    """Show processing rules in evaluation order."""
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.policy.repository import PolicyRepository

    _ready()
    table = Table("id", "name", "type", "action", "priority", "on", title="Processing rules")
    with session_scope() as session:
        rules = PolicyRepository(session).list_rules(enabled_only=False)
        for rule in rules:
            table.add_row(
                rule.id,
                rule.name or "-",
                str(rule.rule_type),
                str(rule.action),
                str(rule.priority),
                "yes" if rule.is_enabled else "no",
            )
    console.print(table)
    if not rules:
        console.print("[dim]no rules; everything is processed by default[/dim]")


@policy_app.command("why")
def policy_why(source_id: Annotated[str, typer.Argument(help="Source id.")]) -> None:
    """Explain the processing decision for one source.

    This is the user-facing payoff of recording every decision: the answer to "why was
    this skipped?" comes from the audit trail, not from reading the rule table by hand.
    """
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.policy.repository import PolicyRepository

    _ready()
    with session_scope() as session:
        decisions = PolicyRepository(session).list_decisions(source_id, limit=10)
        if not decisions:
            fail(f"no policy decision recorded for {source_id!r}; has it been processed?")

        latest = decisions[0]
        console.print(f"\n[bold]{latest.action}[/bold]  ({latest.reason_code})")
        for key, value in (latest.explanation or {}).items():
            console.print(f"  {key}: {value}")
        if len(decisions) > 1:
            console.print(f"\n[dim]{len(decisions) - 1} earlier decision(s) on record[/dim]")


@policy_app.command("exclude")
def policy_exclude(
    creator_id: Annotated[
        str | None, typer.Option("--creator", help="Creator id to stop processing.")
    ] = None,
    source_id: Annotated[
        str | None, typer.Option("--source", help="Single source id to stop processing.")
    ] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
) -> None:
    """Add an exclude rule for a creator or a single source."""
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.policy.models import PolicyAction, ProcessingRule, RuleType
    from douyin_knowledge.policy.repository import PolicyRepository

    if bool(creator_id) == bool(source_id):
        fail("pass exactly one of --creator or --source")

    _ready()
    with session_scope() as session:
        from douyin_knowledge.policy.reconciler import PolicyReconciler

        repository = PolicyRepository(session)
        rule = repository.save_rule(
            ProcessingRule(
                id="",
                name=name or ("exclude creator" if creator_id else "exclude source"),
                is_enabled=True,
                rule_type=RuleType.CREATOR if creator_id else RuleType.SOURCE,
                action=PolicyAction.EXCLUDE,
                priority=50,
                target_creator_id=creator_id,
                target_source_id=source_id,
            )
        )
        summary = PolicyReconciler(session, repository).reconcile_rule(rule.id)
        console.print(f"[green]added rule[/green] {rule.id}")
        console.print(
            f"reconciled {summary.reevaluated} source(s); "
            f"{summary.now_hidden} newly hidden, {summary.now_visible} newly visible"
        )
        if summary.now_hidden:
            console.print("[dim]run `dk reindex` to drop them from the search index[/dim]")


@policy_app.command("delete")
def policy_delete(rule_id: Annotated[str, typer.Argument(help="Rule id from `dk policy list`.")]) -> None:
    """Delete a rule and restore whatever it was hiding.

    The CLI could add an exclude rule but not take one back, which left the reversibility
    claim (DEC-015) unreachable without the API. Deleting a rule loses no history: the
    sources it hid become eligible again with their existing runs and evidence intact.
    """
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.policy.reconciler import PolicyReconciler
    from douyin_knowledge.policy.repository import PolicyRepository

    _ready()
    with session_scope() as session:
        repository = PolicyRepository(session)
        reconciler = PolicyReconciler(session, repository)
        # Ordering matters: the affected set is read off the rule's target, which is gone
        # once the rule is deleted. See the same note in the admin route.
        affected = reconciler.affected_source_ids(rule_id)
        if not repository.delete_rule(rule_id):
            fail(f"no rule with id {rule_id!r}")
        summary = reconciler.reconcile_sources(affected)
        console.print(f"[green]deleted rule[/green] {rule_id}")
        console.print(
            f"reconciled {summary.reevaluated} source(s); "
            f"{summary.now_hidden} newly hidden, {summary.now_visible} newly visible"
        )
        if summary.now_visible:
            console.print("[dim]run `dk reindex` to put them back in the search index[/dim]")


@policy_app.command("reconcile")
def policy_reconcile() -> None:
    """Re-apply every enabled rule to the whole corpus.

    A repair command. Policy state on each source is derived from the rules, and derived
    state should always be rebuildable from its inputs.
    """
    from douyin_knowledge.db import session_scope
    from douyin_knowledge.policy.reconciler import PolicyReconciler
    from douyin_knowledge.policy.repository import PolicyRepository

    _ready()
    with session_scope() as session:
        repository = PolicyRepository(session)
        summary = PolicyReconciler(session, repository).reconcile_all()
        console.print(
            f"reevaluated {summary.reevaluated}, changed {summary.changed} "
            f"({summary.now_hidden} hidden, {summary.now_visible} visible)"
        )


@policy_app.command("triage")
def policy_triage(
    limit: Annotated[int, typer.Option(help="How many unclassified sources to label.")] = 200,
    force: Annotated[bool, typer.Option("--force", help="Recompute even if cached.")] = False,
    model: Annotated[
        bool, typer.Option("--model", help="Allow the model fallback when cues are inconclusive.")
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Classify sources into content types without processing them.

    Triage normally runs lazily, only when a semantic rule needs an answer, so a library
    with no such rules carries no labels at all (DEC-016). That is the right default for
    cost but the wrong one for deciding *whether* to write a rule: you cannot ask "how
    much of my collection is movie clips?" until something has looked. This command is
    that look, and it is deliberately separate from processing — it reads metadata only,
    creates no runs, and changes no source's visibility.
    """
    from sqlalchemy import select

    from douyin_knowledge.db import session_scope
    from douyin_knowledge.db.models.capture import Source
    from douyin_knowledge.policy.factory import build_triage

    _ready()
    with session_scope() as session:
        service = build_triage(session)
        if model and service.triage.model is None:
            # Better to refuse than to quietly run cue-only and report a full pass: the
            # caller asked for the escalation path and would read the labels as stronger
            # evidence than they are.
            fail(
                "no triage model available; set DK_ENABLE_TRIAGE_MODEL_FALLBACK=1 "
                "and a non-mock DK_AI_PROVIDER"
            )
        sources = list(
            session.scalars(
                select(Source)
                .where(Source.locally_deleted_at_ms.is_(None))
                .order_by(Source.saved_at_ms.desc().nullslast())
                .limit(limit)
            )
        )
        counts: dict[str, int] = {}
        for source in sources:
            result = service.classify(source, allow_model=model, force=force)
            counts[str(result.content_type)] = counts.get(str(result.content_type), 0) + 1

        by_type = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
        payload: dict[str, object] = {
            "classified": len(sources),
            "model_calls": len(service.triage.model_calls),
            "by_content_type": by_type,
        }
        if json_out:
            emit(payload, as_json=True)
            return
        table = Table(title=f"triage: {len(sources)} source(s)")
        table.add_column("content type")
        table.add_column("count", justify="right")
        for label, count in by_type.items():
            table.add_row(label, str(count))
        console.print(table)
        console.print(f"[dim]model calls: {len(service.triage.model_calls)}[/dim]")

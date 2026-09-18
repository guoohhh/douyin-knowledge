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
    from douyin_knowledge.ai.registry import get_chat_model, get_embedding_model
    from douyin_knowledge.conversation.conversation_manager import ConversationManager
    from douyin_knowledge.retrieval.vector_store import VectorStore

    return ConversationManager(
        session,
        vector_store=VectorStore(settings.vector_dir),
        embedder=get_embedding_model(settings),
        chat_model=get_chat_model(settings),
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
        rule = PolicyRepository(session).save_rule(
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
        console.print(f"[green]added rule[/green] {rule.id}")

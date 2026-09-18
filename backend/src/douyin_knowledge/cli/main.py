"""The `dk` command line.

This is the primary interface before the frontend exists, and it stays the fastest way to
run the pipeline afterwards. Commands map onto the same handlers the queue uses rather than
reimplementing the pipeline, so there is one code path to keep correct.

Every command that touches the database opens its own `session_scope`. The CLI is a
short-lived process; holding one session across a whole invocation would mean a failure in
the last step rolls back work the user already saw reported as done.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.console import Console

from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.core.errors import DKError

app = typer.Typer(
    name="dk",
    help="Douyin Knowledge: turn saved collections into cited, searchable knowledge.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

# Sub-apps are registered at the bottom of this module.
db_app = typer.Typer(help="Database schema and migrations.", no_args_is_help=True)
wiki_app = typer.Typer(help="Wiki compilation and inspection.", no_args_is_help=True)
policy_app = typer.Typer(help="Processing policy rules and decisions.", no_args_is_help=True)


def _settings() -> Settings:
    settings = get_settings()
    settings.ensure_directories()
    return settings


def _ready() -> Settings:
    """Settings plus an initialized engine, for commands that read or write data."""
    from douyin_knowledge.db import init_engine

    settings = _settings()
    init_engine(settings)
    return settings


def _revisions(settings: Settings) -> tuple[str | None, str]:
    """`(applied, expected)` schema revisions.

    `current_revision` reads the alembic stamp out of the live engine while
    `head_revision` reads the migration scripts, so the two arguments differ.
    """
    from douyin_knowledge.db import get_engine
    from douyin_knowledge.db.migrate import current_revision, head_revision

    return current_revision(get_engine()), head_revision(settings)


def fail(message: str, *, code: int = 1) -> None:
    err_console.print(f"[red]error[/red] {message}")
    raise typer.Exit(code)


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """One place decides human vs machine output.

    JSON mode exists so these commands can be scripted; without it the only way to act on
    a result would be to parse a rich table, which changes whenever the display changes.
    """
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        for key, value in payload.items():
            console.print(f"[bold]{key}[/bold]: {value}")


JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


# --------------------------------------------------------------------------- db


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply all pending migrations."""
    from douyin_knowledge.db.migrate import upgrade_to_head

    revision = upgrade_to_head(_settings())
    console.print(f"[green]schema at[/green] {revision}")


@db_app.command("status")
def db_status(as_json: JsonOpt = False) -> None:
    """Show the current schema revision and where the database lives."""
    settings = _ready()
    current, head = _revisions(settings)
    emit(
        {
            "database": str(settings.db_path or settings.resolved_database_url),
            "current_revision": current or "(empty)",
            "head_revision": head,
            "up_to_date": current == head,
        },
        as_json=as_json,
    )


# ------------------------------------------------------------------------ status


@app.command()
def status(as_json: JsonOpt = False) -> None:
    """Corpus, knowledge and queue counts."""
    from douyin_knowledge.db import session_scope

    _ready()
    from sqlalchemy import func, select

    from douyin_knowledge.db.models.capture import Collection, Source
    from douyin_knowledge.db.models.entities import Claim, Entity
    from douyin_knowledge.db.models.ops import Job
    from douyin_knowledge.db.models.policy import SourceProcessingState
    from douyin_knowledge.db.models.wiki import WikiPage

    with session_scope() as session:

        def count(model: Any, *where: Any) -> int:
            stmt = select(func.count()).select_from(model)
            for clause in where:
                stmt = stmt.where(clause)
            return session.scalar(stmt) or 0

        payload = {
            "collections": count(Collection),
            "sources": count(Source, Source.locally_deleted_at_ms.is_(None)),
            "processed": count(
                SourceProcessingState,
                SourceProcessingState.current_processing_run_id.is_not(None),
            ),
            "entities": count(Entity, Entity.status == "active"),
            "claims": count(Claim),
            "wiki_pages": count(WikiPage, WikiPage.status == "active"),
            "jobs_queued": count(Job, Job.status == "queued"),
            "jobs_failed": count(Job, Job.status == "failed"),
        }

    settings = get_settings()
    payload["demo_mode"] = not settings.uses_real_providers()
    emit(payload, as_json=as_json)


@app.command()
def doctor(as_json: JsonOpt = False) -> None:
    """Check that the install can actually run: schema, providers, directories.

    Exists because the failure modes here are silent. An unmigrated database or an
    unconfigured provider both surface much later as a confusing job failure.
    """
    settings = _ready()
    checks: dict[str, Any] = {}

    current, head = _revisions(settings)
    checks["schema"] = "ok" if current == head else f"stale ({current} != {head}); run `dk db upgrade`"
    checks["data_dir"] = "ok" if settings.data_dir.exists() else "missing"
    checks["capture_provider"] = settings.capture_provider
    checks["mode"] = "real providers" if settings.uses_real_providers() else "demo (mock)"

    for role in ("extraction", "answer", "embedding", "asr"):
        provider = settings.provider_for_role(role)
        checks[f"{role}_model"] = f"{settings.model_for_role(role)} via {provider}"

    if settings.uses_real_providers() and not settings.openai_api_key:
        checks["credentials"] = "missing: a real provider is selected but no key is configured"
    else:
        checks["credentials"] = "ok"

    emit(checks, as_json=as_json)
    if any(isinstance(v, str) and v.startswith(("stale", "missing")) for v in checks.values()):
        raise typer.Exit(1)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override the configured bind address."),
    port: int | None = typer.Option(None, help="Override the configured port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
) -> None:
    """Run the API server."""
    import uvicorn

    settings = _ready()
    uvicorn.run(
        "douyin_knowledge.api:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
    )


app.add_typer(db_app, name="db")
app.add_typer(wiki_app, name="wiki")
app.add_typer(policy_app, name="policy")

# Pipeline, query and worker commands live in sibling modules to keep this file readable;
# importing them here is what registers them on `app`. The import has to be at the bottom:
# those modules import `app` from this one, so hoisting it to the top is a circular import.
from douyin_knowledge.cli import commands_pipeline, commands_query  # noqa: E402

__all__ = ["app", "main", "commands_pipeline", "commands_query"]


def main() -> None:
    try:
        app()
    except DKError as exc:
        # Domain errors already carry an operator-readable message and a code; a traceback
        # would bury it.
        fail(f"[{exc.code}] {exc.message}")


if __name__ == "__main__":
    main()

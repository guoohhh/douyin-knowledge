"""Scriptable Capture Scope inspection, discovery, and selection commands."""

from __future__ import annotations

from typing import Annotated

import typer

from douyin_knowledge.capture.discovery import discover_capture_targets
from douyin_knowledge.capture.scope import CaptureScope, initialize_capture_scope, set_capture_scope
from douyin_knowledge.cli.main import JsonOpt, _ready, app, emit
from douyin_knowledge.db import session_scope

capture_app = typer.Typer(help="Inspect and select capture targets.", no_args_is_help=True)
app.add_typer(capture_app, name="capture")


@capture_app.command("scope")
def show_scope(as_json: JsonOpt = False) -> None:
    """Show the persisted selection without remote discovery."""
    settings = _ready()
    with session_scope() as session:
        scope = initialize_capture_scope(session, settings)
        emit(scope.model_dump(mode="json"), as_json=as_json)


@capture_app.command("discover")
def discover(as_json: JsonOpt = False) -> None:
    """List target metadata and local sync state without reading source contents."""
    settings = _ready()
    with session_scope() as session:
        result = discover_capture_targets(session, settings)
        emit(result.model_dump(mode="json"), as_json=as_json)
        if result.discovery.state == "error":
            raise typer.Exit(1)


@capture_app.command("set-default")
def set_default(selected: Annotated[bool, typer.Argument(help="true or false")], as_json: JsonOpt = False) -> None:
    """Change only default-favorites selection; no sync is started."""
    settings = _ready()
    with session_scope() as session:
        current = initialize_capture_scope(session, settings)
        updated = CaptureScope.model_validate({**current.model_dump(), "default_favorites": selected})
        emit(set_capture_scope(session, updated).model_dump(mode="json"), as_json=as_json)


@capture_app.command("set-collections")
def set_collections(
    ids: Annotated[list[str] | None, typer.Option("--id", help="Repeat for each named collection ID.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Replace named selections; omit --id to choose none. No sync is started."""
    settings = _ready()
    with session_scope() as session:
        current = initialize_capture_scope(session, settings)
        updated = CaptureScope.model_validate({**current.model_dump(), "named_collection_ids": ids or []})
        emit(set_capture_scope(session, updated).model_dump(mode="json"), as_json=as_json)

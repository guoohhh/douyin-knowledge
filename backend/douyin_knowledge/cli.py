import logging
import time
from pathlib import Path

import typer

from .capture import FileCaptureProvider, SidecarCaptureProvider
from .db import SessionLocal
from .index import rebuild
from .logging_config import configure_logging
from .service import sync_capture, work_once
from .wiki import audit, fix
from .wiki import rebuild as rebuild_wiki

app = typer.Typer()


@app.command()
def sync_file(path: Path):
    with SessionLocal() as session:
        typer.echo(sync_capture(session, FileCaptureProvider(str(path)), "file"))


@app.command()
def sync_sidecar():
    with SessionLocal() as session:
        typer.echo(sync_capture(session, SidecarCaptureProvider(), "sidecar"))


@app.command()
def sync_loop(interval_seconds: int = 300):
    """Poll the configured sidecar for new saves while this process runs."""
    if interval_seconds < 30:
        raise typer.BadParameter("Minimum interval is 30 seconds")
    configure_logging()
    while True:
        try:
            with SessionLocal() as session:
                result = sync_capture(session, SidecarCaptureProvider(), "sidecar")
            typer.echo(result)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "sidecar_sync_failed", extra={"error_type": type(exc).__name__}
            )
        time.sleep(interval_seconds)


@app.command()
def worker(once: bool = False):
    configure_logging()
    while True:
        with SessionLocal() as session:
            worked = work_once(session)
        if once:
            break
        if not worked:
            time.sleep(2)


@app.command()
def rebuild_index():
    with SessionLocal() as session:
        typer.echo({"indexed": rebuild(session)})


@app.command()
def wiki_lint():
    with SessionLocal() as session:
        typer.echo(audit(session))


@app.command()
def wiki_fix():
    """Recompile inconsistent Wiki pages and record resolution in the quality ledger."""
    with SessionLocal() as session:
        typer.echo(fix(session))


@app.command()
def wiki_rebuild():
    with SessionLocal() as session:
        typer.echo({"revised_pages": rebuild_wiki(session)})


if __name__ == "__main__":
    app()

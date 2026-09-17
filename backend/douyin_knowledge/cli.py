import logging
import time
from pathlib import Path

import typer

from .capture import FileCaptureProvider, SidecarCaptureProvider
from .db import SessionLocal
from .index import rebuild
from .service import ingest, work_once
from .wiki import lint
from .wiki import rebuild as rebuild_wiki

app = typer.Typer()


@app.command()
def sync_file(path: Path):
    with SessionLocal() as session:
        typer.echo(ingest(session, FileCaptureProvider(str(path)).list_saves()))


@app.command()
def sync_sidecar():
    with SessionLocal() as session:
        typer.echo(ingest(session, SidecarCaptureProvider().list_saves()))


@app.command()
def sync_loop(interval_seconds: int = 300):
    """Poll the configured sidecar for new saves while this process runs."""
    if interval_seconds < 30:
        raise typer.BadParameter("Minimum interval is 30 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        try:
            with SessionLocal() as session:
                result = ingest(session, SidecarCaptureProvider().list_saves())
            typer.echo(result)
        except Exception:
            logging.exception("sidecar sync failed")
        time.sleep(interval_seconds)


@app.command()
def worker(once: bool = False):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
        typer.echo(lint(session))


@app.command()
def wiki_rebuild():
    with SessionLocal() as session:
        typer.echo({"revised_pages": rebuild_wiki(session)})


if __name__ == "__main__":
    app()

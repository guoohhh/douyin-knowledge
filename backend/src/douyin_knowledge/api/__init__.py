"""FastAPI application for Douyin Knowledge.

The app is built by a factory rather than created at import time so tests can construct
an instance against a temporary database without the module-level singleton having
already resolved settings from the developer's real environment. `app` is still exported
for `uvicorn douyin_knowledge.api:app`.

Domain errors are translated in one place. Every `DKError` already carries a `code` and
an `http_status`, so the routes raise domain errors and the boundary decides the HTTP
shape -- rather than each route re-deciding what a missing source means.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from douyin_knowledge.api.deps import AppSettings, DbSession
from douyin_knowledge.api.routes import admin, conversations, knowledge, sources
from douyin_knowledge.config import Settings, get_settings
from douyin_knowledge.core.errors import DKError

API_PREFIX = "/api"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Douyin Knowledge API",
        description="Local-first personal knowledge base built from saved Douyin collections.",
        version="0.1.0",
    )

    # Origins come from settings and default to the Vite dev server on loopback only.
    # This service holds a personal knowledge base and, in real-provider mode, a path to
    # a paid API; it must not be reachable from an arbitrary page the user has open.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sources.router, prefix=f"{API_PREFIX}/sources", tags=["sources"])
    app.include_router(
        conversations.router, prefix=f"{API_PREFIX}/conversations", tags=["conversations"]
    )
    app.include_router(knowledge.router, prefix=f"{API_PREFIX}/knowledge", tags=["knowledge"])
    app.include_router(admin.router, prefix=f"{API_PREFIX}/admin", tags=["admin"])

    @app.exception_handler(DKError)
    async def handle_domain_error(_: Request, exc: DKError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    # `/` belongs to the UI when a build is being served; this banner is only useful when
    # the API is running headless, and returning it at the root of a served app would mean
    # the one URL a user actually types renders JSON.
    # `response_model=None` because this returns either the HTML shell or the JSON banner,
    # and FastAPI cannot build one response model from that union.
    @app.get("/", response_model=None)
    def root() -> Response | dict[str, Any]:
        if _serves_frontend(settings):
            return FileResponse(_frontend_dist() / "index.html")
        return {
            "name": "Douyin Knowledge API",
            "version": "0.1.0",
            "docs": "/docs",
            "api_prefix": API_PREFIX,
        }

    @app.get("/health")
    def health(db: DbSession, config: AppSettings) -> dict[str, Any]:
        """Liveness plus enough state to tell a broken install from an empty one.

        `text()` is required: SQLAlchemy 2.0 refuses a bare string, so the previous
        version of this endpoint failed with an ArgumentError on every call -- the health
        check itself was the thing that was unhealthy.
        """
        db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "database": "connected",
            "demo_mode": not config.uses_real_providers(),
            "capture_provider": config.capture_provider,
            "max_processing_level": config.max_processing_level,
        }

    _mount_frontend(app, settings)
    return app


def _frontend_dist() -> Path:
    """Where `npm run build` puts the bundle, relative to this package.

    Resolved from the package location rather than the process working directory: the API
    is normally started via `dk serve` from wherever the user happens to be standing.
    """
    return Path(__file__).resolve().parents[3].parent / "frontend" / "dist"


def _serves_frontend(settings: Settings) -> bool:
    """True when a build exists and the setting allows serving it.

    Checked at request time as well as at mount time so a developer who runs `npm run build`
    against an already-running API gets the bundle without a restart.
    """
    return settings.serve_frontend and (_frontend_dist() / "index.html").exists()


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """Serve the built UI from the API when it exists.

    Mounted last so it can claim `/` without shadowing any API route. Absent a build this
    is a no-op and the dev server (Vite, proxying to this port) is the intended path --
    `DK_SERVE_FRONTEND` was previously a documented setting that nothing read, so the
    single-process deployment the docs describe did not actually exist.
    """
    if not _serves_frontend(settings):
        return

    dist = _frontend_dist()
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        # Client-side routing means /wiki/wp_xxx is a real URL the user can reload or
        # bookmark, and only the bundle knows how to resolve it. Unknown API paths are
        # already handled above, so anything reaching here belongs to the app -- except a
        # missing static file, which must stay a 404 rather than silently return HTML.
        if path.startswith(("api/", "health")):
            # An unmatched API path is a client bug and must stay a JSON 404. Handing back
            # the HTML shell would turn a typo'd endpoint into a parse error somewhere else.
            raise HTTPException(status_code=404, detail="not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


app = create_app()


def main() -> None:
    """`uvicorn` entrypoint used by the CLI's `serve` command."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "douyin_knowledge.api:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    main()

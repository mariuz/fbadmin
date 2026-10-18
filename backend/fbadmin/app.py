"""FastAPI application factory."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import firebirdsql
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import databases, jobs, query, servers
from .api.deps import get_state
from .config import settings
from .connections import PasswordRequired
from .metadata import OBJECT_TYPES

STATIC_DIR = Path(__file__).parent / "static"


def _reaper(stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            get_state().manager.reap_idle(settings.session_idle_timeout)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = threading.Event()
    threading.Thread(target=_reaper, args=(stop,), daemon=True).start()
    yield
    stop.set()
    get_state().manager.close_all()


def create_app() -> FastAPI:
    app = FastAPI(title="fbadmin", version=__version__, lifespan=lifespan)

    @app.exception_handler(PasswordRequired)
    async def _pw(request: Request, exc: PasswordRequired):
        return JSONResponse(status_code=401, content={"detail": "Password required", "code": "password_required"})

    @app.exception_handler(KeyError)
    async def _key(request: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": exc.args[0] if exc.args else "Not found"})

    @app.exception_handler(LookupError)
    async def _lookup(request: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _value(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(firebirdsql.Error)
    async def _fb(request: Request, exc: firebirdsql.Error):
        return JSONResponse(status_code=400, content={"detail": str(exc).strip(), "code": "firebird_error"})

    @app.exception_handler(OSError)
    async def _os(request: Request, exc: OSError):
        return JSONResponse(status_code=502, content={"detail": f"Cannot reach server: {exc}",
                                                      "code": "connection_error"})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/object-types")
    def object_types():
        return [{"type": k, **v} for k, v in OBJECT_TYPES.items()]

    app.include_router(servers.router)
    app.include_router(databases.router)
    app.include_router(query.router)
    app.include_router(jobs.router)

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = STATIC_DIR / path
            if path and f.is_file() and STATIC_DIR in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()

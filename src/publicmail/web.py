from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .config import CrawlSettings
from .engine import Engine
from .exporter import QUERIES, csv_chunks
from .importer import import_file
from .store import Store

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
UPLOAD_LIMIT = 256 * 1024 * 1024


class UploadTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        limit = UPLOAD_LIMIT + 1_000_000 if scope["path"] == "/api/import" else 64_000
        size, started = 0, False

        async def limited_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > limit:
                    raise UploadTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except UploadTooLarge:
            if started:
                raise
            await JSONResponse({"detail": "Request exceeds the upload/body size limit"}, status_code=413)(
                scope, receive, send
            )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(StrictModel):
    token: str = Field(max_length=1024)


class Control(StrictModel):
    action: str
    limit: int | None = Field(None, ge=1, le=100_000)


class Retry(StrictModel):
    statuses: list[str] = Field(default_factory=lambda: ["failed", "no_email"], min_length=1, max_length=5)
    fresh: bool = False
    limit: int | None = Field(None, ge=1, le=100_000)
    ids: list[int] | None = Field(None, min_length=1, max_length=1000)


def admin_token(directory: Path) -> str:
    token = os.environ.get("SCRAPER_TOKEN", "").strip()
    if token:
        if len(token) < 20:
            raise ValueError("SCRAPER_TOKEN must be at least 20 characters (use a random, unique value)")
        return token
    path = directory / "admin-token"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 20:
            raise ValueError(f"Invalid access key file: {path}; supply SCRAPER_TOKEN or replace it") from None
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        token = secrets.token_urlsafe(32)
        target.write(token + "\n")
    log.warning("Dashboard access key created at %s (or set SCRAPER_TOKEN)", path)
    return token


def create_app(
    db_path: str | Path | None = None, *, token: str | None = None, run_worker: bool = True
) -> FastAPI:
    path = Path(db_path or os.environ.get("SCRAPER_DB", "data/harvest.db")).expanduser().resolve()
    access_key = token or admin_token(path.parent)
    read_only = os.environ.get("SCRAPER_READ_ONLY", "0") == "1"
    login_attempts: dict[str, deque] = defaultdict(deque)
    import_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(path)
        app.state.store = store
        app.state.worker_failed = False
        engine = Engine(store)
        app.state.engine = engine
        task = asyncio.create_task(engine.run(paused_on_start=read_only)) if run_worker else None
        app.state.worker_task = task
        try:
            if task:
                # Surface a duplicate runner/startup failure, not a broken dashboard.
                ready = asyncio.create_task(engine.started.wait())
                try:
                    done, _ = await asyncio.wait(
                        {ready, task}, timeout=10, return_when=asyncio.FIRST_COMPLETED
                    )
                    if task in done:
                        task.result()
                    if not ready.done():
                        raise RuntimeError("Worker did not become ready")
                finally:
                    ready.cancel()
                    with suppress(asyncio.CancelledError):
                        await ready

                def worker_done(completed: asyncio.Task) -> None:
                    if engine.stop_event.is_set():
                        return
                    app.state.worker_failed = True
                    error = None if completed.cancelled() else completed.exception()
                    log.critical("Worker exited unexpectedly; stopping the supervised server: %s", error)
                    callback = getattr(app.state, "on_worker_failure", None)
                    if callback:
                        callback()

                task.add_done_callback(worker_done)
            yield
        finally:
            if task:
                engine.stop()
                try:
                    await asyncio.wait_for(task, 35)
                except TimeoutError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("Worker exited unexpectedly")
            store.close()

    app = FastAPI(
        title="Public Mail",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)

    def authorized(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), access_key.encode())

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if request.url.path != "/api/login" and not authorized(request):
                return JSONResponse(
                    {"detail": "Access key required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if read_only and request.url.path != "/api/login":
                    return JSONResponse(
                        {"detail": "Read-only preview: deploy on a server with normal internet to crawl"},
                        status_code=403,
                    )
                source = request.headers.get("origin")
                if source:
                    parsed = urlsplit(source)
                    if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get("host"):
                        return JSONResponse(
                            {"detail": "Cross-origin changes are not permitted"}, status_code=403
                        )
                content_length = request.headers.get("content-length", "0")
                try:
                    maximum = UPLOAD_LIMIT + 1_000_000 if request.url.path == "/api/import" else 64_000
                    if int(content_length) > maximum:
                        return JSONResponse({"detail": "Upload/body is too large"}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
        )
        # No X-Frame-Options/frame-ancestors restriction: Arena's live preview is
        # embedded. All data and actions still require the explicit Bearer key.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    async def health(request: Request):
        store = request.app.state.store
        task = request.app.state.worker_task
        alive = (
            not run_worker
            or task is not None
            and not task.done()
            and time.time() - store.get_meta("worker_heartbeat", 0) < 60
        )
        return JSONResponse({"ok": alive, "version": __version__}, status_code=200 if alive else 503)

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        attempts = login_attempts[key]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise HTTPException(429, "Too many attempts; wait one minute", headers={"Retry-After": "60"})
        if not hmac.compare_digest(body.token.encode(), access_key.encode()):
            attempts.append(now)
            if len(login_attempts) > 10_000:
                login_attempts.clear()
            raise HTTPException(401, "Incorrect access key")
        attempts.clear()
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    async def status(request: Request):
        return await asyncio.to_thread(request.app.state.store.stats) | {"read_only": read_only}

    @app.get("/api/sites")
    async def sites(
        request: Request,
        q: str = Query("", max_length=200),
        status: str = "",
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1, le=200),
    ):
        try:
            return await asyncio.to_thread(request.app.state.store.list_sites, q, status, page, size)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/sites/{site_id}/pages")
    async def pages(site_id: int, request: Request):
        if not request.app.state.store.get_site(site_id):
            raise HTTPException(404, "Site not found")
        return request.app.state.store.site_pages(site_id)

    @app.get("/api/emails")
    async def emails(
        request: Request,
        q: str = Query("", max_length=200),
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1, le=200),
    ):
        return await asyncio.to_thread(request.app.state.store.list_emails, q, page, size)

    @app.get("/api/events")
    async def events(request: Request):
        return request.app.state.store.events()

    @app.get("/api/settings")
    async def settings(request: Request):
        return request.app.state.store.settings().model_dump()

    @app.put("/api/settings")
    async def update_settings(body: CrawlSettings, request: Request):
        try:
            request.app.state.store.save_settings(body)
            return body.model_dump()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/control")
    async def control(body: Control, request: Request):
        try:
            request.app.state.store.control(body.action, body.limit)
            return request.app.state.store.stats()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/retry")
    async def retry(body: Retry, request: Request):
        store = request.app.state.store
        if store.get_meta("mode") == "running" or store.stats()["statuses"]["running"]:
            raise HTTPException(409, "Pause and let active sites finish before requeuing")
        try:
            count = await asyncio.to_thread(
                store.retry_sites, body.statuses, fresh=body.fresh, limit=body.limit, ids=body.ids
            )
            return {"requeued": count}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/import")
    async def upload(request: Request, file: Annotated[UploadFile, File()]):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".csv", ".db", ".sqlite", ".sqlite3"}:
            raise HTTPException(400, "Choose a CSV or SQLite database")
        if import_lock.locked():
            raise HTTPException(409, "Another import is in progress")
        async with import_lock:
            directory = path.parent / "uploads"
            directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=directory)
            try:
                with os.fdopen(fd, "wb") as target:
                    size = 0
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > UPLOAD_LIMIT:
                            raise HTTPException(
                                413, "Input file exceeds 256 MiB; use CLI import for larger files"
                            )
                        target.write(chunk)
                try:
                    report = await asyncio.to_thread(import_file, request.app.state.store, temporary)
                except Exception as exc:
                    log.warning("Input import failed: %s", exc)
                    raise HTTPException(
                        400,
                        f"Could not import file: {str(exc)[:300]}. Any earlier batches are saved; reimporting is safe.",
                    ) from exc
                report["filename"] = Path(file.filename or "upload").name
                return report
            finally:
                await file.close()
                await asyncio.to_thread(Path(temporary).unlink, missing_ok=True)

    @app.get("/api/export/{kind}")
    async def export(kind: str, request: Request):
        if kind not in QUERIES:
            raise HTTPException(404, "Unknown export")
        return StreamingResponse(
            csv_chunks(request.app.state.store, kind),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
        )

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app

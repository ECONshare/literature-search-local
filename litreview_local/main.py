"""LitReview Local: the FastAPI app - assembly, guard, startup and shutdown.

The pages live in web.py and the MCP endpoint in mcp_server.py; this module
puts them together and owns what the whole process shares:

- The local guard. There is no login, because the app belongs to the person
  at this computer and listens on 127.0.0.1 only. What a login would
  otherwise stop - a web page in the same browser posting to localhost, or
  rebinding a domain name onto 127.0.0.1 - is stopped here instead: every
  request must name this machine as its Host, and anything that changes
  something must not come from a foreign Origin.
- Startup: back the library up, upgrade its schema, write server.json for
  the launcher and the Claude Desktop bridge, then - in the background -
  carry on with every run the app was closed in the middle of.
- Shutdown: remove server.json.
"""
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import db as database
from . import mcp_server, models, worker
from .config import settings

logger = logging.getLogger("litreview_local")

STATIC_DIR = Path(__file__).parent / "static"

# Set by __main__ to stop the server cleanly (the Quit button).
_exit_hook = None


def set_exit_hook(hook) -> None:
    global _exit_hook
    _exit_hook = hook


# ----------------------------------------------------------- lifecycle

def _write_server_file() -> None:
    info = {"port": settings.port, "pid": os.getpid(), "url": settings.base_url,
            "version": __version__, "started_at": models.utcnow().isoformat(),
            "data_dir": str(settings.data_dir)}
    try:
        settings.server_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.server_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
        os.replace(tmp, settings.server_file)
    except OSError as exc:
        logger.warning("Could not write %s: %s", settings.server_file, exc)


def _remove_server_file() -> None:
    try:
        raw = json.loads(settings.server_file.read_text(encoding="utf-8"))
        if raw.get("pid") == os.getpid():
            settings.server_file.unlink()
    except (OSError, ValueError):
        pass


def _after_start() -> None:
    """Startup work that must not hold up the first page."""
    from . import identify, journals, paper_texts, research, services, tasks

    research.clean_old_texts()
    research.reverify_findings()
    research.rehighlight_old()
    research.resume_all()
    try:
        identify.resume_all()
    except Exception:
        logger.exception("Resuming identifications failed")
    paper_texts.fill_missing()
    db = database.SessionLocal()
    try:
        tasks.expire_leases(db)
        services.prune_lookups(db)
        _complete_references(db)
        if not journals.register_loaded(db):
            try:
                journals.load_norwegian_register(db)
            except Exception as exc:
                logger.warning("Could not load the journal register (%s) - "
                               "journal ratings fall back to OpenAlex", exc)
                db.rollback()
    finally:
        db.close()


def _complete_references(db, limit: int = 200) -> None:
    """Fill in volume, issue, pages and the issue's year from Crossref for
    papers already in the library that were stored before that existed -
    the ones a review leans on (read, or picked for reading) first. Polite
    and capped per start; what it cannot finish now it finishes next time."""
    from sqlalchemy import select

    from . import services

    papers = db.execute(
        select(models.Paper)
        .where(models.Paper.doi.isnot(None),
               models.Paper.stage == models.STAGE_RETRIEVAL)
        .order_by(models.Paper.id)
    ).scalars().all()
    todo = [p for p in papers if services.wants_polish(p)][:limit]
    if todo:
        improved = services.polish_from_crossref(db, todo)
        logger.info("Completed the references of %d of %d papers from Crossref",
                    improved, len(todo))


def startup() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database.backup_database()
    database.run_migrations()
    _write_server_file()
    for problem in settings.validate():
        logger.warning("Setup: %s", problem)
    worker.submit(_after_start)
    logger.info("LitReview Local %s is serving %s (library: %s)",
                __version__, settings.base_url, settings.data_dir)


def shutdown() -> None:
    worker.shutdown()
    _remove_server_file()


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    try:
        yield
    finally:
        shutdown()


app = FastAPI(title="LitReview Local", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------- the guard

_LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _split_host(value: str) -> tuple[str, str]:
    value = (value or "").strip().lower()
    if value.startswith("["):  # an IPv6 literal, [::1]:8765
        end = value.find("]")
        rest = value[end + 1:]
        return value[:end + 1], rest[1:] if rest.startswith(":") else ""
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, port
    return value, ""


def _local_host(value: str) -> bool:
    """Is this Host header (or an Origin's netloc) this machine, on our port?
    Anything else - a domain name pointed at 127.0.0.1, say - is refused."""
    host, port = _split_host(value)
    return host in _LOCAL_NAMES and port in ("", str(settings.port))


def _origin_ok(origin: str | None) -> bool:
    if not origin or origin == "null":
        return not origin  # a missing Origin is fine; "null" is a sandboxed frame
    parts = urlsplit(origin)
    return parts.scheme == "http" and _local_host(parts.netloc)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if not _local_host(host):
        return PlainTextResponse(
            "LitReview only answers requests addressed to this computer "
            f"(127.0.0.1:{settings.port}).", status_code=403)
    origin = request.headers.get("origin")
    if request.method not in _SAFE_METHODS and origin is not None \
            and not _origin_ok(origin):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    if request.url.path == "/mcp" and request.method == "POST":
        if "application/json" not in request.headers.get("content-type", ""):
            return JSONResponse({"error": "Send JSON-RPC as application/json."},
                                status_code=415)
    return await call_next(request)


# ---------------------------------------------------------------- routes

@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": "litreview-local", "version": __version__,
            "data_dir": str(settings.data_dir)}


@app.post("/quit")
def quit_app() -> dict:
    """Stop the app (the Quit button). Everything is already saved."""
    if _exit_hook is None:
        return {"stopping": False,
                "note": "Close the LitReview window to stop the app."}
    threading.Timer(0.5, _exit_hook).start()
    return {"stopping": True}


app.include_router(mcp_server.router)

from . import exports_web, web  # noqa: E402  (registers the task kinds too)

app.include_router(exports_web.router)
app.include_router(web.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

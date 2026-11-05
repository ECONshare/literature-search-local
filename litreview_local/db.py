"""Database engine and session setup: one SQLite file in the data directory.

SQLite because the whole library belongs to one person on one machine: no
server to install, and the file can be copied, backed up or moved like any
other. Three settings make it fit an app with background threads:

- WAL journal mode, so the web pages keep reading while a background step
  (a search, a download) writes.
- busy_timeout, so two writers queue for a moment instead of failing with
  "database is locked".
- foreign_keys=ON, because SQLite ignores ON DELETE clauses without it, and
  the schema relies on them to take a project's rows with it.

The one rule for code using it: never hold a write transaction across a
network call - commit before a search or a download, not after.
"""
import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 30_000
KEEP_BACKUPS = 10


def _make_engine(url: str):
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False,
                      "timeout": BUSY_TIMEOUT_MS / 1000},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added after the first release of the local app, applied
# idempotently at startup so an existing library upgrades in place
# (create_all only creates missing tables). A new table needs nothing here.
#
# Every column added to an existing table belongs here, in the order it was
# added - a library made by an earlier version is upgraded by exactly these.
_MIGRATION_COLUMNS: list[tuple[str, str, str]] = [
    # Reference data (volume, issue, pages, and the online-first year next to
    # the issue's), 2026-11-04.
    ("papers", "volume", "VARCHAR(40)"),
    ("papers", "issue", "VARCHAR(40)"),
    ("papers", "pages", "VARCHAR(40)"),
    ("papers", "online_year", "INTEGER"),
    # The printed page number of each PDF page of a kept text (a citation
    # needs p. 78, not the PDF's page 8), 2026-11-04.
    ("paper_texts", "page_labels", "JSON"),
]


def run_migrations() -> None:
    """Create missing tables and add any missing columns."""
    from . import models

    models.Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATION_COLUMNS:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                logger.info("Added column %s.%s", table, column)


def backup_database(keep: int = KEEP_BACKUPS) -> Path | None:
    """Copy the library to backups/ before this start touches it.

    Uses SQLite's own backup API, which produces a consistent copy even of a
    database in WAL mode. Keeps the newest `keep` copies. A first start has
    nothing to back up and returns None; a failed backup is logged, never
    fatal - refusing to start would not make the data any safer.
    """
    source = settings.database_path
    if not source.exists() or source.stat().st_size == 0:
        return None
    folder = settings.backups_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = folder / f"litreview-{stamp}.db"
        n = 1
        while dest.exists():
            n += 1
            dest = folder / f"litreview-{stamp}-{n}.db"
        src = sqlite3.connect(str(source))
        try:
            out = sqlite3.connect(str(dest))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        old = sorted(folder.glob("litreview-*.db"), key=lambda p: p.stat().st_mtime)
        for stale in old[:-keep] if keep > 0 else []:
            try:
                stale.unlink()
            except OSError:
                pass
        logger.info("Backed up the library to %s", dest)
        return dest
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not back up the library before starting: %s", exc)
        return None


def restore_database(backup: Path) -> None:
    """Put a backup back in place of the live library (app must be stopped)."""
    target = settings.database_path
    for suffix in ("-wal", "-shm"):
        side = target.with_name(target.name + suffix)
        if side.exists():
            side.unlink()
    shutil.copy2(backup, target)

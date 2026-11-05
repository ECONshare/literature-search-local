"""Configuration for LitReview Local.

Two kinds of setting:

- Where things are - the data directory and the port - comes from the
  environment (LITREVIEW_DATA_DIR, LITREVIEW_PORT), which the launcher and
  `python -m litreview_local --data-dir/--port` set before anything is
  imported. They decide where the database is, so they cannot live in it.
- What the user chooses - their contact email for the scholarly APIs, an
  optional Semantic Scholar key, the default X/Y/Z for new projects - lives in
  <data_dir>/settings.json and is edited on the Settings page. Read live, so a
  change applies without a restart.

There is no model configuration at all: the app never calls one.
"""
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = Path.home() / "LitReview"

# The user-editable settings and their defaults.
USER_DEFAULTS = {
    # Sent to OpenAlex/Crossref/Unpaywall as the polite-pool contact.
    # Unpaywall refuses requests without one, so PDF lookups by DOI need it.
    "contact_email": "",
    # Optional Semantic Scholar API key (higher rate limits if set).
    "s2_api_key": "",
    # How wide each search is, for every project: results fetched per
    # source (X), results Claude ranks (Y), papers Claude reads (Z), and the
    # sources searched.
    "fetch_per_source": 25,
    "rank_top": 40,
    "download_top": 6,
    "sources": "openalex,semantic_scholar,crossref",
}


class Settings:
    def __init__(self) -> None:
        self.reload_location()
        self._lock = threading.Lock()
        self._user: dict | None = None

    # ------------------------------------------------------------ location

    def reload_location(self) -> None:
        """Read the data directory and port from the environment."""
        raw = os.environ.get("LITREVIEW_DATA_DIR", "").strip()
        self.data_dir: Path = (Path(raw).expanduser() if raw
                               else DEFAULT_DATA_DIR).resolve()
        try:
            self.port: int = int(os.environ.get("LITREVIEW_PORT", DEFAULT_PORT))
        except ValueError:
            self.port = DEFAULT_PORT
        self._user = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "litreview.db"

    @property
    def database_url(self) -> str:
        # An explicit URL is for the test suite only; the app always uses the
        # file in the data directory.
        override = os.environ.get("LITREVIEW_DATABASE_URL", "").strip()
        if override:
            return override
        return "sqlite:///" + self.database_path.as_posix()

    @property
    def pdf_dir(self) -> str:
        """Root folder for PDFs: one subfolder per project slug."""
        return str(self.data_dir / "pdfs")

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def server_file(self) -> Path:
        return self.data_dir / "server.json"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------ user settings

    def user_settings(self) -> dict:
        with self._lock:
            if self._user is None:
                self._user = self._read_user_settings()
            return dict(self._user)

    def _read_user_settings(self) -> dict:
        values = dict(USER_DEFAULTS)
        try:
            raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in USER_DEFAULTS:
                    if key in raw:
                        values[key] = raw[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("Could not read %s (%s) - using defaults",
                           self.settings_file, exc)
        return values

    def save_user_settings(self, changes: dict) -> dict:
        """Merge `changes` into settings.json, atomically. Unknown keys are
        ignored; numbers are clamped to sensible ranges."""
        with self._lock:
            values = self._read_user_settings()
            for key, value in changes.items():
                if key not in USER_DEFAULTS:
                    continue
                if isinstance(USER_DEFAULTS[key], int):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                    value = max(1, min(500, value))
                else:
                    value = str(value or "").strip()
                values[key] = value
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(values, indent=2), encoding="utf-8")
            os.replace(tmp, self.settings_file)
            self._user = values
            return dict(values)

    @property
    def contact_email(self) -> str:
        return str(self.user_settings().get("contact_email") or "")

    @property
    def s2_api_key(self) -> str:
        return str(self.user_settings().get("s2_api_key") or "")

    def project_defaults(self) -> dict:
        """What every project searches with (Settings): the three numbers and
        the sources, as the Project columns hold them."""
        values = self.user_settings()
        out = {key: int(values.get(key) or USER_DEFAULTS[key])
               for key in ("fetch_per_source", "rank_top", "download_top")}
        out["download_top"] = min(out["download_top"], 100)
        sources = [s.strip() for s in str(values.get("sources") or "").split(",")
                   if s.strip()]
        out["sources"] = ",".join(sources) or USER_DEFAULTS["sources"]
        return out

    def validate(self) -> list[str]:
        problems = []
        if not self.contact_email:
            problems.append(
                "No contact email set - add one on the Settings page. The "
                "scholarly APIs ask for one, and Unpaywall (used to find free "
                "PDFs) refuses requests without it.")
        return problems


settings = Settings()

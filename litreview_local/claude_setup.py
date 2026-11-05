"""Connect Claude Desktop and Claude Code to LitReview Local.

Helpers for the Connect Claude page. They find claude_desktop_config.json,
build LitReview's entry for it, preview exactly what would be written, and
write it carefully: merged into what is there, after a backup, atomically.
They also give the one-line command and the .mcp.json for Claude Code.

Nothing here imports the rest of litreview_local, and it uses only the
standard library, so the page and the tests can use it on its own.

Claude Desktop reads claude_desktop_config.json only when it starts. After
any change it has to be quit completely and opened again: closing the window
is not enough. On Windows, right-click the Claude icon by the clock and
choose Quit; on a Mac, use Claude > Quit Claude (Cmd+Q). RESTART_NOTE says so
in words the page can show.

The entry is

    "LitReview Local": {"command": "<abs venv python>", "args": ["<abs>/mcp_bridge.py"]}

with absolute paths; json.dumps escapes the backslashes of Windows paths, and
the spaces in them need nothing. "LitReview Local" (DESKTOP_NAME) is the name
Claude Desktop shows. Earlier versions called it "litreview"; adding it again
renames that entry rather than leave Claude Desktop starting LitReview twice.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# Claude Code's name for LitReview: Claude Code allows only letters, digits,
# - and _ in one.
SERVER_NAME = "litreview"
# The name Claude Desktop shows for it, which keeps it apart from a hosted
# LitReview connector on claude.ai (called "Literature review").
DESKTOP_NAME = "LitReview Local"
# What earlier versions called it in Claude Desktop's settings. Adding it
# under DESKTOP_NAME takes LitReview's entry under these names out.
OLD_DESKTOP_NAMES = ("litreview",)
CONFIG_NAME = "claude_desktop_config.json"
DEFAULT_PORT = 8765
CLAUDE_CODE_SCOPES = ("local", "user", "project")

# Claude Desktop writes this file itself while it runs (it keeps its own
# preferences there), so an entry added while it is open can be overwritten
# by its next save. Quit it first, then add, then open it.
QUIT_FIRST_NOTE = (
    "First quit Claude Desktop completely - it rewrites this file while it "
    "runs, and could undo the change. Closing its window is not enough: on "
    "Windows, right-click the Claude icon by the clock and choose Quit; on a "
    "Mac, choose Claude > Quit Claude (Cmd+Q).")
RESTART_NOTE = (
    "Now open Claude Desktop again, so it loads LitReview. If it was still "
    "running when you added it, quit it completely (right-click the Claude "
    "icon by the clock > Quit, or Cmd+Q on a Mac), add LitReview again here, "
    "and then open it again.")


class DesktopConfigError(ValueError):
    """claude_desktop_config.json can't be changed safely. Nothing was written."""


# --------------------------------------------------------------------------
# Where Claude Desktop keeps its config


def desktop_config_candidates(*, platform: str | None = None,
                              environ: Mapping[str, str] | None = None,
                              home: Path | None = None) -> list[Path]:
    """Every place claude_desktop_config.json may be, most specific first.

    Windows: the Microsoft Store (MSIX) build first. Current versions run with
    --user-data-dir under %LOCALAPPDATA%\\Claude-Data\\claude_<package id> and
    keep the config there (seen on a Store build in 2026); older
    ones kept a private copy under %LOCALAPPDATA%\\Packages\\Claude_*\\
    LocalCache\\Roaming\\Claude. Either hides the ordinary %APPDATA%\\Claude,
    which comes last.
    macOS: ~/Library/Application Support/Claude. Linux: ~/.config/Claude.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    if platform == "win32":
        local = environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        try:
            packages = sorted(p for p in (Path(local) / "Packages").glob("Claude_*")
                              if p.is_dir())
        except OSError:
            packages = []
        try:
            data_dirs = sorted(p for p in (Path(local) / "Claude-Data").glob("claude_*")
                               if p.is_dir())
        except OSError:
            data_dirs = []
        found = [p / CONFIG_NAME for p in data_dirs]
        found += [p / "LocalCache" / "Roaming" / "Claude" / CONFIG_NAME for p in packages]
        appdata = environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        found.append(Path(appdata) / "Claude" / CONFIG_NAME)
        return found
    if platform == "darwin":
        return [home / "Library" / "Application Support" / "Claude" / CONFIG_NAME]
    found = []
    if environ.get("XDG_CONFIG_HOME"):
        found.append(Path(environ["XDG_CONFIG_HOME"]) / "Claude" / CONFIG_NAME)
    default = home / ".config" / "Claude" / CONFIG_NAME
    if default not in found:
        found.append(default)
    return found


def find_desktop_config(*, platform: str | None = None,
                        environ: Mapping[str, str] | None = None,
                        home: Path | None = None) -> Path | None:
    """The config Claude Desktop uses here, or None if it seems not installed.

    An existing file wins; failing that, a candidate whose folder exists
    (Claude Desktop has run but nothing has been configured yet).
    """
    candidates = desktop_config_candidates(platform=platform, environ=environ, home=home)
    for path in candidates:
        if path.is_file():
            return path
    for path in candidates:
        if path.parent.is_dir():
            return path
    return None


# --------------------------------------------------------------------------
# The entry


def _abs(path: str | os.PathLike[str]) -> str:
    # abspath, not resolve(): a venv's bin/python is a symlink to the base
    # interpreter on macOS, and following it would lose the venv.
    return os.path.abspath(os.fspath(path))


def _same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.normcase(_abs(a)) == os.path.normcase(_abs(b))


def desktop_entry(python_exe: str | os.PathLike[str], bridge_path: str | os.PathLike[str],
                  *, port: int | None = None,
                  data_dir: str | os.PathLike[str] | None = None,
                  default_data_dir: str | os.PathLike[str] | None = None) -> dict:
    """LitReview's mcpServers entry: the venv's Python running mcp_bridge.py.

    Pass the running app's port and data folder when you have them. Values
    other than the defaults (8765, ~/LitReview) go into the entry's "env":
    Claude Desktop starts the bridge with only a handful of environment
    variables, so a LITREVIEW_PORT or LITREVIEW_DATA_DIR set elsewhere would
    not reach it, and the bridge would start the app with the wrong ones.
    """
    entry: dict[str, Any] = {"command": _abs(python_exe), "args": [_abs(bridge_path)]}
    env: dict[str, str] = {}
    if port is not None and int(port) != DEFAULT_PORT:
        env["LITREVIEW_PORT"] = str(int(port))
    if data_dir is not None:
        default = default_data_dir if default_data_dir is not None else Path.home() / "LitReview"
        if not _same_path(data_dir, default):
            env["LITREVIEW_DATA_DIR"] = _abs(data_dir)
    if env:
        entry["env"] = env
    return entry


def manual_snippet(entry: Mapping[str, Any], name: str = DESKTOP_NAME) -> str:
    """The JSON to paste by hand (Claude Desktop: Settings > Developer > Edit Config)."""
    return json.dumps({"mcpServers": {name: dict(entry)}}, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Reading and writing claude_desktop_config.json


def _target(config_path: str | os.PathLike[str]) -> Path:
    path = Path(config_path)
    if path.is_symlink():             # write through a symlinked config
        path = Path(os.path.realpath(path))
    return path


def _load(path: Path) -> tuple[dict, bool]:
    """(the config, whether the file exists). Raises DesktopConfigError
    rather than guess at a file that isn't a JSON object."""
    if not path.exists():
        return {}, False
    if path.is_dir():
        raise DesktopConfigError("%s is a folder, not a file." % path)
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as exc:
        raise DesktopConfigError("Could not read %s: %s" % (path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise DesktopConfigError(
            "%s is not UTF-8 text, so it was left alone. Fix or rename it, then try "
            "again." % path) from exc
    if not text.strip():
        return {}, True                # an empty file: nothing to keep
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesktopConfigError(
            "%s is not valid JSON (line %d, column %d: %s), so it was left alone. "
            "Fix it - Claude Desktop can't read it either - or rename it, then "
            "try again." % (path, exc.lineno, exc.colno, exc.msg)) from exc
    if not isinstance(data, dict):
        raise DesktopConfigError(
            "%s should hold a JSON object ({...}), so it was left alone." % path)
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise DesktopConfigError(
            '"mcpServers" in %s should be a JSON object, so the file was left alone.'
            % path)
    return data, True


def _render(data: Mapping[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _is_litreview(entry: Any) -> bool:
    """Whether an mcpServers entry starts LitReview Local's bridge."""
    if not isinstance(entry, Mapping):
        return False
    args = entry.get("args")
    return isinstance(args, list) and any(
        isinstance(arg, str) and arg.replace("\\", "/").rsplit("/", 1)[-1] == "mcp_bridge.py"
        for arg in args)


def _old_names(servers: Mapping[str, Any], name: str) -> list:
    """The names earlier versions gave LitReview that hold its entry here."""
    return [key for key in OLD_DESKTOP_NAMES
            if key != name and _is_litreview(servers.get(key))]


def _with_entry(data: Mapping[str, Any], entry: Mapping[str, Any], name: str) -> dict:
    """A copy of the config with the entry set, and LitReview's entries under
    its old names taken out; every other key, server and their order kept.
    A renamed entry takes the old one's place in the list."""
    merged = copy.deepcopy(dict(data))
    servers = merged.get("mcpServers") or {}
    old = _old_names(servers, name)
    at = name if name in servers or not old else old[0]
    updated = {}
    for key, value in servers.items():
        if key == at:
            updated[name] = copy.deepcopy(dict(entry))
        elif key not in old:
            updated[key] = value
    if name not in updated:
        updated[name] = copy.deepcopy(dict(entry))
    merged["mcpServers"] = updated
    return merged


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name("%s.bak-%s" % (path.name, stamp))
    n = 1
    while backup.exists():
        backup = path.with_name("%s.bak-%s-%d" % (path.name, stamp, n))
        n += 1
    try:
        shutil.copy2(str(path), str(backup))
    except OSError as exc:
        raise DesktopConfigError(
            "Could not back up %s (%s), so it was left alone." % (path, exc)) from exc
    _prune_backups(path)
    return backup


KEEP_BACKUPS = 3


def _prune_backups(path: Path, keep: int = KEEP_BACKUPS) -> None:
    """Keep the newest few of LitReview's backups beside the config. The
    oldest one - the file as it was before LitReview ever touched it - is
    kept too, whatever happens."""
    try:
        backups = sorted(path.parent.glob(path.name + ".bak-*"),
                         key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    if len(backups) <= keep + 1:
        return
    for stale in backups[1:-keep]:
        try:
            stale.unlink()
        except OSError:
            pass


def _replace(src: str, dst: Path) -> None:
    # Windows refuses the replace while another program has the file open
    # (an editor, an antivirus scan); that passes in a moment.
    for attempt in range(20):
        try:
            os.replace(src, str(dst))
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def _atomic_write(path: Path, text: str) -> None:
    """Write UTF-8 to a temp file beside the config, then swap it in, so the
    config is never half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            try:
                shutil.copymode(str(path), tmp)
            except OSError:
                pass
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_desktop_entry(config_path: str | os.PathLike[str],
                       name: str = DESKTOP_NAME) -> dict | None:
    """The entry now under that name, if any - so the page can say when it
    would replace something that isn't LitReview Local's."""
    data, _ = _load(_target(config_path))
    entry = (data.get("mcpServers") or {}).get(name)
    return copy.deepcopy(entry) if entry is not None else None


def old_desktop_entries(config_path: str | os.PathLike[str],
                        name: str = DESKTOP_NAME) -> dict:
    """LitReview's entries under the names earlier versions gave it, as
    {old name: entry}. Adding it under `name` renames them."""
    data, _ = _load(_target(config_path))
    servers = data.get("mcpServers") or {}
    return {key: copy.deepcopy(servers[key]) for key in _old_names(servers, name)}


def preview_desktop_change(config_path: str | os.PathLike[str], entry: Mapping[str, Any],
                           name: str = DESKTOP_NAME) -> str:
    """Exactly the text install_desktop_entry() would write, so the page can
    show it before asking. Writes nothing. Raises DesktopConfigError when
    the file can't be changed safely. When the entry is already there, this
    is the file as it stands (and install writes nothing)."""
    data, _ = _load(_target(config_path))
    return _render(_with_entry(data, entry, name))


def install_desktop_entry(config_path: str | os.PathLike[str], entry: Mapping[str, Any],
                          name: str = DESKTOP_NAME) -> dict:
    """Add or update LitReview's server in claude_desktop_config.json.

    Merges into "mcpServers" and keeps every other key and server. Creates
    the file and its folder if they are missing. Before changing an existing
    file it copies it to <name>.bak-YYYYMMDD-HHMMSS beside it. Writes UTF-8,
    atomically. Changes nothing, and says "unchanged", when the entry is
    already identical. Raises DesktopConfigError, writing nothing, if the
    file isn't valid JSON. LitReview's entry under an old name
    (OLD_DESKTOP_NAMES) is renamed: this one takes its place.

    Returns {"path", "action" ("added" | "updated" | "renamed" |
    "unchanged"), "changed", "created", "backup" (path or None), "previous"
    (the entry it replaced), "renamed_from" (the old names taken out)}.
    After a change Claude Desktop must be quit completely and reopened
    (RESTART_NOTE).
    """
    path = _target(config_path)
    data, existed = _load(path)
    servers = data.get("mcpServers") or {}
    previous = servers.get(name)
    old = _old_names(servers, name)
    if previous is None and old:
        previous = servers[old[0]]
    result = {"path": str(path), "changed": False, "created": False, "backup": None,
              "previous": copy.deepcopy(previous), "renamed_from": old}
    if existed and not old and previous == dict(entry):
        result["action"] = "unchanged"
        return result
    text = _render(_with_entry(data, entry, name))
    backup = _backup(path) if existed else None
    _atomic_write(path, text)
    result.update(changed=True, created=not existed,
                  backup=str(backup) if backup else None,
                  action="renamed" if old else
                  "updated" if previous is not None else "added")
    return result


def remove_desktop_entry(config_path: str | os.PathLike[str],
                         name: str = DESKTOP_NAME) -> dict:
    """Take LitReview's server out of claude_desktop_config.json, under its
    old names (OLD_DESKTOP_NAMES) as well.

    Same care as install_desktop_entry(): everything else is kept, the file
    is backed up first and replaced atomically, and an invalid file raises
    DesktopConfigError untouched. "absent" (nothing written) if there was
    no such entry. Claude Desktop must be quit completely and reopened to
    notice.
    """
    path = _target(config_path)
    data, existed = _load(path)
    servers = data.get("mcpServers")
    result = {"path": str(path), "changed": False, "created": False, "backup": None,
              "previous": None}
    names = []
    if existed and isinstance(servers, dict):
        names = ([name] if name in servers else []) + _old_names(servers, name)
    if not names:
        result["action"] = "absent"
        return result
    updated = copy.deepcopy(data)
    taken = [updated["mcpServers"].pop(key) for key in names]
    result["previous"] = taken[0]
    if not updated["mcpServers"]:
        # LitReview was the only server: leave no empty section behind, so
        # the file reads as it did before LitReview was added.
        del updated["mcpServers"]
    backup = _backup(path)
    _atomic_write(path, _render(updated))
    result.update(changed=True, backup=str(backup), action="removed")
    return result


# --------------------------------------------------------------------------
# Claude Code


def mcp_url(port: int = DEFAULT_PORT) -> str:
    return "http://127.0.0.1:%d/mcp" % int(port)


def claude_code_command(port: int = DEFAULT_PORT, *, scope: str | None = None) -> str:
    """The command that adds LitReview to Claude Code:

        claude mcp add --transport http litreview http://127.0.0.1:<port>/mcp

    Without a scope Claude Code adds it for the folder the command is run in
    only ("local"); scope="user" makes it available in every folder. Claude
    Code talks HTTP itself, so it needs no bridge, but the app must be
    running.
    """
    if scope is not None and scope not in CLAUDE_CODE_SCOPES:
        raise ValueError("scope must be one of %s" % ", ".join(CLAUDE_CODE_SCOPES))
    parts = ["claude", "mcp", "add", "--transport", "http"]
    if scope:
        parts += ["--scope", scope]
    return " ".join(parts + [SERVER_NAME, mcp_url(port)])


def mcp_json_for_claude_code(port: int = DEFAULT_PORT) -> dict:
    """A .mcp.json for a project folder: Claude Code started there offers to
    connect LitReview. Serialize with json.dumps(..., indent=2)."""
    return {"mcpServers": {SERVER_NAME: {"type": "http", "url": mcp_url(port)}}}


def desktop_log_candidates(*, platform: str | None = None,
                           environ: Mapping[str, str] | None = None,
                           home: Path | None = None) -> list[Path]:
    """Where Claude Desktop writes its MCP logs, most likely first. The Store
    build on Windows has been seen writing to %LOCALAPPDATA%\\Claude\\logs."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    if platform == "win32":
        local = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        found = [local / "Claude" / "logs"]
        try:
            found += sorted((p / "logs") for p in (local / "Claude-Data").glob("claude_*"))
        except OSError:
            pass
        found.append(Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
                     / "Claude" / "logs")
        return found
    if platform == "darwin":
        return [home / "Library" / "Logs" / "Claude"]
    return [home / ".config" / "Claude" / "logs"]


def find_desktop_log(name: str = DESKTOP_NAME, **where) -> Path | None:
    """The newest log Claude Desktop keeps for LitReview's server, if any:
    mcp-server-<name>.log, or the one from under an old name."""
    names = [name] + [old for old in OLD_DESKTOP_NAMES if old != name]
    files = set()
    for folder in desktop_log_candidates(**where):
        for each in names:
            try:
                files.update(f for f in folder.glob(f"mcp-server-{each}*.log")
                             if f.is_file())
            except OSError:
                continue
    return max(files, key=lambda f: f.stat().st_mtime) if files else None

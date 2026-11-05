#!/usr/bin/env python3
"""Start LitReview Local on this computer.

    python launcher.py [--port N] [--data-dir PATH] [--no-browser] [--smoke]

start-windows.bat and start-mac.command run this file, so most people never
call it themselves. It needs only Python 3.8 and the standard library: just
enough to find - or, with the user's permission, fetch - the Python 3.10+
that the app itself runs on. In order, it:

1. opens the browser and stops if this data folder's LitReview is already
   running (server.json names a live process and /health answers);
2. stops with a clear message if anything else holds the port;
3. makes local/.venv from a Python 3.10+ it finds - or, when the computer has
   none, from the Python 3.12 that uv fetches, asking before installing uv;
4. installs requirements.txt into the venv whenever that file has changed;
5. runs `<venv python> -m litreview_local`, waits for /health, opens the
   browser, and keeps the app running until Ctrl+C or the window is closed.

--smoke is the CI check: start the app on a free port with a throwaway data
folder, check /health and an MCP initialize, stop it, and exit 0 or 1.

LITREVIEW_PORT and LITREVIEW_DATA_DIR do the same as --port and --data-dir;
the defaults are 8765 and ~/LitReview.

Keep this file runnable on Python 3.8: no `X | None` or `list[str]` evaluated
at runtime, no `match`. tests/test_launcher.py checks the syntax.
"""

import argparse
import collections
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path
from typing import (Callable, Dict, Iterable, Iterator, List, Mapping,
                    NamedTuple, Optional, Sequence, Tuple)

APP = "litreview-local"            # what /health must say in "app"
DEFAULT_PORT = 8765
MIN_PYTHON = (3, 10)               # what the app needs
NEWEST_MINOR = 14                  # probe python3.14 down to python3.10
UV_PYTHON = "3.12"                 # what uv fetches when nothing suitable exists

HERE = Path(__file__).resolve().parent
VENV_DIR = HERE / ".venv"
REQUIREMENTS = HERE / "requirements.txt"
STAMP_NAME = "litreview-launcher.json"   # kept inside the venv

PROBE_CODE = "import sys;print(sys.version_info[:2]);print(sys.executable)"
# A venv that cannot do TLS cannot pip install, and one without sqlite3 cannot
# run the app. Both happen with some Pythons (Anaconda's on Windows, for one).
VENV_CHECK_CODE = ("import ssl, sqlite3, sys; "
                   "sys.exit(0 if sys.version_info >= (3, 10) else 1)")
PROBE_TIMEOUT = 20.0
START_TIMEOUT = 90.0
SMOKE_START_TIMEOUT = 180.0
STOP_GRACE = 15.0

UV_INSTALL_WINDOWS = ["powershell", "-NoProfile", "-ExecutionPolicy", "ByPass",
                      "-c", "irm https://astral.sh/uv/install.ps1 | iex"]
UV_INSTALL_POSIX = ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
STATUS_CONTROL_C_EXIT = 0xC000013A    # a Windows process ended by Ctrl+C


class LauncherError(Exception):
    """Something the user has to read: printed as is, then exit code 1."""


class PythonInfo(NamedTuple):
    cmd: List[str]                 # how to run it, e.g. ["py", "-3.12"]
    version: Tuple[int, int]
    executable: str                # its sys.executable, for messages


class Config(NamedTuple):
    port: int
    data_dir: Path
    data_dir_explicit: bool        # from --data-dir or LITREVIEW_DATA_DIR
    no_browser: bool
    smoke: bool
    port_explicit: bool = False    # from --port or LITREVIEW_PORT


# --------------------------------------------------------------------------
# Output


def say(*lines: str) -> None:
    """Print for the person in front of the window, never raising.

    The window may be gone (macOS sends SIGHUP when it closes) or the console
    may not take a character in a path; neither should stop the launcher.
    """
    for line in lines or ("",):
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass


def _tolerant_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def _is_windows(platform: Optional[str] = None) -> bool:
    return (platform or sys.platform) == "win32"


# --------------------------------------------------------------------------
# Configuration


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="launcher.py",
        description="Start LitReview Local and open it in your browser.")
    parser.add_argument("--port", type=int, default=None,
                        help="port to run on (default: LITREVIEW_PORT or %d)"
                        % DEFAULT_PORT)
    parser.add_argument("--data-dir", default=None,
                        help="folder for your projects, PDFs and database "
                        "(default: LITREVIEW_DATA_DIR or ~/LitReview)")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the browser")
    parser.add_argument("--smoke", action="store_true",
                        help="start, check /health and MCP, stop: exit 0 or 1")
    return parser.parse_args(argv)


def make_config(args: argparse.Namespace,
                environ: Optional[Mapping[str, str]] = None,
                home: Optional[Path] = None) -> Config:
    environ = os.environ if environ is None else environ
    env_port = (environ.get("LITREVIEW_PORT") or "").strip()
    if args.port is not None:
        port = args.port
    elif env_port:
        try:
            port = int(env_port)
        except ValueError:
            raise LauncherError(
                "LITREVIEW_PORT is set to %r, which is not a port number. "
                "Set it to a number such as 8766, or remove it." % env_port)
    else:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise LauncherError("The port must be a number from 1 to 65535, "
                            "not %d." % port)

    env_dir = (environ.get("LITREVIEW_DATA_DIR") or "").strip()
    raw_dir = args.data_dir or env_dir
    if raw_dir:
        data_dir = _absolute(raw_dir)
    else:
        data_dir = (home or Path.home()) / "LitReview"
    return Config(port=port, data_dir=data_dir,
                  data_dir_explicit=bool(raw_dir),
                  no_browser=bool(args.no_browser), smoke=bool(args.smoke),
                  port_explicit=args.port is not None or bool(env_port))


def _absolute(path: str) -> Path:
    # Not Path.resolve(): before 3.10 it can return a relative path on
    # Windows for a folder that does not exist yet.
    return Path(os.path.abspath(os.path.expanduser(path)))


# --------------------------------------------------------------------------
# Finding Python


def probe_python(cmd: Sequence[str],
                 timeout: float = PROBE_TIMEOUT) -> Optional[PythonInfo]:
    """Run `cmd -c "import sys;print(sys.version_info[:2])..."` and parse it.

    Anything that is not a working Python comes back as None: a missing
    command, a timeout, and the Microsoft Store stub - which exists on PATH
    as python.exe/python3.exe, prints "Python was not found..." and exits
    with 9009.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # The Python install manager's `py` may otherwise offer to download a
    # version that is not there; finding Python must never install one.
    env["PYTHON_MANAGER_AUTOMATIC_INSTALL"] = "false"
    try:
        done = subprocess.run(list(cmd) + ["-c", PROBE_CODE],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    if not lines:
        return None
    match = re.match(r"^\((\d+),\s*(\d+)\)$", lines[0].strip())
    if not match:
        return None
    version = (int(match.group(1)), int(match.group(2)))
    executable = lines[1].strip() if len(lines) > 1 else str(cmd[0])
    return PythonInfo(list(cmd), version, executable)


def py_launcher_pythons(py: str) -> List[str]:
    """The interpreters Windows' `py` launcher knows about (`py -0p`)."""
    env = dict(os.environ, PYTHON_MANAGER_AUTOMATIC_INSTALL="false")
    try:
        done = subprocess.run([py, "-0p"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=PROBE_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    try:
        text = done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = done.stdout.decode("latin-1")
    paths = []
    for line in text.splitlines():
        match = re.search(r"([A-Za-z]:\\.*?\.exe)\s*$", line.strip())
        if match:
            paths.append(match.group(1))
    return paths


def _python_dir_minor(path: Path) -> int:
    match = re.match(r"(?i)^python3(\d+)", path.name)
    return int(match.group(1)) if match else -1


def _windows_install_dirs(environ: Mapping[str, str]) -> List[Path]:
    """python.org installs: %LOCALAPPDATA%\\Programs\\Python\\Python31x etc."""
    roots = []
    if environ.get("LOCALAPPDATA"):
        roots.append(Path(environ["LOCALAPPDATA"]) / "Programs" / "Python")
    for var in ("ProgramFiles", "ProgramW6432"):
        if environ.get(var):
            roots.append(Path(environ[var]))
    found = []  # type: List[Path]
    for root in roots:
        try:
            dirs = [d for d in root.glob("Python3*") if d.is_dir()]
        except OSError:
            continue
        dirs.sort(key=_python_dir_minor, reverse=True)
        found.extend(d for d in dirs if d not in found)
    return found


def xcode_tools_installed() -> bool:
    """True if /usr/bin/python3 can run without popping up an installer.

    Without the Command Line Tools, running /usr/bin/python3 opens a dialog
    offering to install them and then fails, so it is only tried when
    `xcode-select -p` says the tools are there.
    """
    try:
        return subprocess.run(["xcode-select", "-p"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_candidates(platform: Optional[str] = None,
                      environ: Optional[Mapping[str, str]] = None,
                      which: Callable[[str], Optional[str]] = shutil.which,
                      isfile: Callable[[str], bool] = os.path.isfile,
                      list_py: Callable[[str], List[str]] = py_launcher_pythons,
                      xcode_ok: Callable[[], bool] = xcode_tools_installed,
                      ) -> Iterator[List[str]]:
    """Commands that might be a Python 3.10+, best first. Lazy: the `py`
    listing and the Xcode check only run when the search gets that far."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    minors = range(NEWEST_MINOR, MIN_PYTHON[1] - 1, -1)
    versioned = ["python3.%d" % m for m in minors]

    if platform == "win32":
        py = which("py")
        if py:
            listed = list_py(py)
            if listed:
                for path in listed:
                    yield [path]
            else:
                for m in minors:
                    yield [py, "-3.%d" % m]
        for name in versioned + ["python", "python3"]:
            found = which(name)
            if found:
                yield [found]
        for folder in _windows_install_dirs(environ):
            exe = str(folder / "python.exe")
            if isfile(exe):
                yield [exe]
    elif platform == "darwin":
        for name in versioned:
            found = which(name)
            if found:
                yield [found]
        fixed = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        fixed += ["/Library/Frameworks/Python.framework/Versions/3.%d/bin/python3"
                  % m for m in minors]
        for path in fixed:
            if isfile(path):
                yield [path]
        found = which("python3")
        if found and (found != "/usr/bin/python3" or xcode_ok()):
            yield [found]
    else:
        for name in versioned + ["python3", "python"]:
            found = which(name)
            if found:
                yield [found]


def _cmd_key(cmd: Sequence[str]) -> Tuple[str, ...]:
    return tuple([os.path.normcase(os.path.abspath(cmd[0]))
                  if os.path.isabs(cmd[0]) else cmd[0]] + list(cmd[1:]))


def iter_suitable_pythons(candidates: Optional[Iterable[List[str]]] = None,
                          probe: Callable[[Sequence[str]], Optional[PythonInfo]]
                          = probe_python,
                          current: Optional[PythonInfo] = None,
                          ) -> Iterator[PythonInfo]:
    """Every Python >= 3.10 on this computer, best first: the one running
    this script if it qualifies, then the candidates, probed one by one."""
    if current is None:
        current = PythonInfo([sys.executable], tuple(sys.version_info[:2]),
                             sys.executable)
    seen_cmds = set()
    seen_exes = set()
    if current.version < MIN_PYTHON:
        _note_too_old(current)
    if current.version >= MIN_PYTHON:
        seen_cmds.add(_cmd_key(current.cmd))
        seen_exes.add(os.path.normcase(current.executable))
        yield current
    for cmd in (python_candidates() if candidates is None else candidates):
        key = _cmd_key(cmd)
        if key in seen_cmds:
            continue
        seen_cmds.add(key)
        info = probe(cmd)
        if info is not None and info.version < MIN_PYTHON:
            _note_too_old(info)
        if info is None or info.version < MIN_PYTHON:
            continue
        exe = os.path.normcase(info.executable)
        if exe in seen_exes:
            continue
        seen_exes.add(exe)
        yield info


# Pythons the search found but could not use, so the message can say what
# this computer has rather than that it has nothing.
_TOO_OLD: List["PythonInfo"] = []


def _note_too_old(info: "PythonInfo") -> None:
    exe = os.path.normcase(info.executable or "")
    if all(os.path.normcase(other.executable or "") != exe for other in _TOO_OLD):
        _TOO_OLD.append(info)


def _found_pythons_line() -> str:
    """What the search found, in one sentence."""
    if not _TOO_OLD:
        return "No Python was found on this computer."
    best = max(_TOO_OLD, key=lambda info: info.version)
    where = best.executable or " ".join(best.cmd)
    kind = " (Anaconda)" if "conda" in where.lower() else ""
    return ("This computer has Python %d.%d%s at %s, which is too old for it."
            % (best.version[0], best.version[1], kind, where))


# --------------------------------------------------------------------------
# uv: the fallback when the computer has no Python 3.10+


def find_uv(platform: Optional[str] = None,
            home: Optional[Path] = None,
            which: Callable[[str], Optional[str]] = shutil.which,
            ) -> Optional[str]:
    """uv on PATH, or where its installer puts it (PATH is not updated in
    the window that ran the installer)."""
    found = which("uv")
    if found:
        return found
    exe = "uv.exe" if _is_windows(platform) else "uv"
    home = home or Path.home()
    for folder in (home / ".local" / "bin", home / ".cargo" / "bin"):
        path = folder / exe
        if path.is_file():
            return str(path)
    return None


def ask_yes(prompt: str, ask: Callable[[str], str] = input) -> bool:
    try:
        answer = ask(prompt)
    except (EOFError, KeyboardInterrupt):
        say()
        return False
    return answer.strip().lower() in ("y", "yes")


def manual_python_help(platform: Optional[str] = None) -> str:
    if _is_windows(platform):
        steps = [
            "  1. Go to https://www.python.org/downloads/windows/ and download",
            "     the Windows installer for Python 3.12 (64-bit).",
            "  2. Run it, and tick \"Add python.exe to PATH\" on the first screen.",
            "  3. Double-click start-windows.bat again.",
        ]
    elif (platform or sys.platform) == "darwin":
        steps = [
            "  1. Go to https://www.python.org/downloads/macos/ and download",
            "     the macOS installer for Python 3.12.",
            "  2. Open it and follow the steps.",
            "  3. Double-click start-mac.command again.",
            "  (With Homebrew you can instead run:  brew install python@3.12 )",
        ]
    else:
        steps = ["  Install Python 3.12 with your system's package manager,",
                 "  then run:  python3 launcher.py"]
    return "\n".join(["LitReview needs Python 3.10 or newer. To install it yourself:"]
                     + steps)


def offer_uv_install(platform: Optional[str] = None,
                     ask: Callable[[str], str] = input,
                     run: Callable[[List[str]], int] = subprocess.call,
                     locate: Callable[[], Optional[str]] = find_uv) -> str:
    """Ask before installing uv; install it with the official installer.

    Returns uv's path, or raises LauncherError with manual instructions if
    the user says no or the install fails.
    """
    say("",
        "LitReview needs Python 3.10 or newer.",
        _found_pythons_line(),
        "",
        "It can set that up for you with uv, a small free tool from Astral",
        "(https://docs.astral.sh/uv/) that downloads a private copy of Python",
        "just for LitReview. uv installs into your user folder and needs no",
        "administrator rights.",
        "")
    if not ask_yes("Install uv now? Type y and press Enter (or just Enter to cancel): ",
                   ask):
        raise LauncherError("Nothing was installed.\n\n" + manual_python_help(platform))
    cmd = UV_INSTALL_WINDOWS if _is_windows(platform) else UV_INSTALL_POSIX
    say("Installing uv...")
    try:
        code = run(cmd)
    except OSError as exc:
        code = None
        say(str(exc))
    if code != 0:
        raise LauncherError(
            "Installing uv did not work (it needs an internet connection).\n\n"
            + manual_python_help(platform))
    uv = locate()
    if not uv:
        raise LauncherError(
            "uv seems to be installed, but LitReview can't find it yet. Close this "
            "window and start LitReview again.")
    return uv


# --------------------------------------------------------------------------
# The app's own Python environment (local/.venv)


def venv_python(venv_dir: Path = VENV_DIR, platform: Optional[str] = None) -> Path:
    if _is_windows(platform):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def read_stamp(venv_dir: Path = VENV_DIR) -> Dict[str, str]:
    try:
        data = json.loads((venv_dir / STAMP_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_stamp(venv_dir: Path, data: Mapping[str, str]) -> None:
    (venv_dir / STAMP_NAME).write_text(json.dumps(dict(data), indent=2),
                                       encoding="utf-8")


def requirements_hash(path: Path = REQUIREMENTS) -> str:
    """sha256 of requirements.txt, blind to CRLF vs LF checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def venv_ok(python: Path) -> bool:
    if not python.exists():
        return False
    try:
        return subprocess.run([str(python), "-c", VENV_CHECK_CODE],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=PROBE_TIMEOUT * 3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def remove_venv(venv_dir: Path, ours: bool = False) -> None:
    """Delete local/.venv. Unless this run just made it (ours=True), only
    when it looks like a venv, so nothing else is ever deleted."""
    if not venv_dir.exists():
        return
    if not ours and not (venv_dir / "pyvenv.cfg").is_file():
        raise LauncherError(
            "The folder %s is in the way: LitReview keeps its own Python "
            "environment there, but this folder isn't one. Move or delete it, "
            "then start LitReview again." % venv_dir)
    try:
        shutil.rmtree(str(venv_dir))
    except OSError as exc:
        raise LauncherError(
            "LitReview needs to rebuild its Python environment but could not "
            "delete %s (%s). Close every LitReview window, delete that folder "
            "yourself, then start LitReview again." % (venv_dir, exc))


def run_streaming(cmd: Sequence[str], env: Optional[Mapping[str, str]] = None,
                  cwd: Optional[Path] = None) -> Tuple[int, List[str]]:
    """Run a setup command, showing its output indented, and keep the tail
    for diagnose_install_failure()."""
    tail = collections.deque(maxlen=300)  # type: collections.deque
    try:
        proc = subprocess.Popen(list(cmd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=None if env is None else dict(env),
                                cwd=None if cwd is None else str(cwd))
    except OSError as exc:
        return 127, [str(exc)]
    try:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            tail.append(line)
            say("    " + line)
        code = proc.wait()
    except BaseException:
        _kill(proc)
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    return code, list(tail)


def _setup_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Use the system's certificates: networks that inspect TLS (common at
    # universities and companies) otherwise make uv's downloads fail.
    # Trust the system's certificates (university and company networks that
    # inspect TLS). UV_SYSTEM_CERTS is the current name; the older
    # UV_NATIVE_TLS now prints a deprecation warning a first-time user reads
    # as something going wrong.
    env.setdefault("UV_SYSTEM_CERTS", "1")
    return env


_DIAGNOSES = [
    (("407 proxy", "proxy authentication required", "proxyerror",
      "cannot connect to proxy", "tunnel connection failed"),
     "Your network uses a proxy. Set the HTTPS_PROXY environment variable to "
     "your proxy's address (for example http://proxy.example.org:8080; your IT "
     "department knows it), then start LitReview again."),
    (("certificate_verify_failed", "certificate verify failed",
      "invalid peer certificate", "unknownissuer", "self-signed certificate",
      "self signed certificate"),
     "Your network intercepts secure connections, so the download was refused. "
     "Try another network (a phone hotspot works), or ask your IT department "
     "how to install Python packages on this network."),
    (("getaddrinfo failed", "name or service not known",
      "nodename nor servname", "temporary failure in name resolution",
      "failed to establish a new connection", "network is unreachable",
      "no route to host", "dns error", "failed to lookup address",
      "connection timed out", "read timed out", "timed out", "error sending request",
      "could not fetch url", "no matching distribution found",
      "connection refused", "connecterror", "failed to download"),
     "This needs an internet connection the first time, and the download did "
     "not get through. Check that you are online (and not behind a proxy or "
     "firewall that blocks pypi.org), then start LitReview again."),
    (("no space left on device", "errno 28", "not enough space"),
     "The disk is full. Free some space, then start LitReview again."),
    (("access is denied", "permission denied", "winerror 5", "errno 13"),
     "A file could not be written. Close other LitReview windows, check that "
     "your antivirus is not blocking the LitReview folder, then try again."),
    (("microsoft visual c++", "failed building wheel", "subprocess-exited-with-error",
      "failed to build"),
     "A component had to be built from source, which usually means this Python "
     "version is too new for it. Install Python 3.12 and start LitReview again."),
]


def diagnose_install_failure(lines: Iterable[str], code: Optional[int] = None,
                             what: str = "Installing LitReview's components") -> str:
    """A readable explanation of a failed pip/uv run, from its output."""
    text = "\n".join(lines).lower()
    for needles, advice in _DIAGNOSES:
        if any(needle in text for needle in needles):
            break
    else:
        advice = ("The messages above say what went wrong. If it mentions the "
                  "network, check your internet connection and try again.")
    head = "%s failed" % what
    if code is not None:
        head += " (exit code %s)" % code
    return head + ".\n\n" + advice


def ensure_pip(python: Path) -> None:
    try:
        has_pip = subprocess.run([str(python), "-m", "pip", "--version"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 timeout=PROBE_TIMEOUT * 3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        has_pip = False
    if not has_pip:
        code, tail = run_streaming([str(python), "-m", "ensurepip", "--upgrade"],
                                   env=_setup_env())
        if code != 0:
            raise LauncherError(diagnose_install_failure(tail, code, "Setting up pip"))


def create_venv(info: PythonInfo, venv_dir: Path) -> bool:
    say("Setting up LitReview's own Python environment with Python %d.%d"
        % info.version, "(%s)." % info.executable, "This takes a minute, once.")
    code, _ = run_streaming(info.cmd + ["-m", "venv", str(venv_dir)],
                            env=_setup_env())
    return code == 0 and venv_ok(venv_python(venv_dir))


def create_venv_with_uv(uv: str, venv_dir: Path) -> None:
    say("Setting up LitReview's own Python environment with Python %s from uv."
        % UV_PYTHON, "This downloads Python the first time and takes a minute.")
    # --quiet: uv's "Activate with: ..." is a developer's instruction that
    # nobody using LitReview has to follow. Errors are still shown.
    code, tail = run_streaming([uv, "venv", "--quiet", "--seed", "--python", UV_PYTHON,
                                str(venv_dir)], env=_setup_env(), cwd=venv_dir.parent)
    if code != 0 or not venv_ok(venv_python(venv_dir)):
        raise LauncherError(diagnose_install_failure(
            tail, code, "Setting up Python %s with uv" % UV_PYTHON))


def ensure_venv(venv_dir: Path = VENV_DIR,
                pythons: Optional[Iterable[PythonInfo]] = None,
                locate_uv: Callable[[], Optional[str]] = find_uv,
                offer: Callable[[], str] = offer_uv_install,
                ) -> Tuple[Path, str]:
    """Make sure local/.venv works; return (its python, "venv" or "uv")."""
    python = venv_python(venv_dir)
    if venv_ok(python):
        return python, read_stamp(venv_dir).get("created_by", "venv")
    if venv_dir.exists():
        say("LitReview's Python environment (%s) is incomplete or out of date;"
            % venv_dir, "making a fresh one.")
        remove_venv(venv_dir)

    say("Looking for Python 3.10 or newer...")
    for info in (iter_suitable_pythons() if pythons is None else pythons):
        if create_venv(info, venv_dir):
            write_stamp(venv_dir, {"created_by": "venv", "python": info.executable})
            return python, "venv"
        say("That Python could not make a working environment; trying another.")
        remove_venv(venv_dir, ours=True)

    uv = locate_uv() or offer()
    create_venv_with_uv(uv, venv_dir)
    write_stamp(venv_dir, {"created_by": "uv"})
    return python, "uv"


def ensure_requirements(python: Path, created_by: str,
                        venv_dir: Path = VENV_DIR,
                        requirements: Path = REQUIREMENTS,
                        locate_uv: Callable[[], Optional[str]] = find_uv,
                        runner: Callable[..., Tuple[int, List[str]]] = run_streaming,
                        ) -> bool:
    """Install requirements.txt when its hash differs from the stored one.
    Returns True if it installed anything."""
    wanted = requirements_hash(requirements)
    stamp = read_stamp(venv_dir)
    if stamp.get("requirements_sha256") == wanted:
        return False
    uv = locate_uv() if created_by == "uv" else None
    if uv:
        cmd = [uv, "pip", "install", "--python", str(python), "-r", str(requirements)]
    else:
        ensure_pip(python)
        cmd = [str(python), "-m", "pip", "install", "-r", str(requirements)]
    say("", "Installing the components LitReview needs. The first time this needs",
        "the internet and takes a few minutes; afterwards it is skipped.")
    code, tail = runner(cmd, env=_setup_env())
    if code != 0:
        raise LauncherError(diagnose_install_failure(tail, code))
    stamp["requirements_sha256"] = wanted
    write_stamp(venv_dir, stamp)
    return True


# --------------------------------------------------------------------------
# Talking to a server on 127.0.0.1


class HttpResult(NamedTuple):
    connected: bool                # False: nothing accepted the connection
    status: Optional[int]
    headers: Dict[str, str]        # lower-cased names
    body: bytes


class _Connection(http.client.HTTPConnection):
    """HTTPConnection with its own, short, connect timeout.

    Windows takes about two seconds to report a refused connection on
    localhost; a short connect timeout keeps polling quick while leaving a
    long read timeout for the answer itself.
    """

    def __init__(self, port: int, connect_timeout: float, timeout: float) -> None:
        http.client.HTTPConnection.__init__(self, "127.0.0.1", port, timeout=timeout)
        self._connect_timeout = connect_timeout

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port),
                                             self._connect_timeout)
        self.sock.settimeout(self.timeout)


def http_request(port: int, method: str, path: str, body: Optional[bytes] = None,
                 headers: Optional[Mapping[str, str]] = None,
                 connect_timeout: float = 1.0, timeout: float = 10.0) -> HttpResult:
    conn = _Connection(port, connect_timeout, timeout)
    try:
        try:
            conn.connect()
        except OSError:
            return HttpResult(False, None, {}, b"")
        try:
            conn.request(method, path, body=body, headers=dict(headers or {}))
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException):
            return HttpResult(True, None, {}, b"")
        return HttpResult(True, resp.status,
                          {k.lower(): v for k, v in resp.getheaders()}, data)
    finally:
        conn.close()


def get_health(port: int, timeout: float = 5.0,
               connect_timeout: float = 1.0) -> Tuple[bool, Optional[dict]]:
    """(something answered on the port, the /health JSON if it was JSON)."""
    res = http_request(port, "GET", "/health", headers={"Accept": "application/json"},
                       connect_timeout=connect_timeout, timeout=timeout)
    if not res.connected or res.status != 200:
        return res.connected, None
    try:
        payload = json.loads(res.body.decode("utf-8"))
    except ValueError:
        return True, None
    return True, payload if isinstance(payload, dict) else None


def is_litreview(health: Optional[dict]) -> bool:
    return (isinstance(health, dict) and health.get("ok") is True
            and health.get("app") == APP)


def base_url(port: int) -> str:
    return "http://127.0.0.1:%d" % port


def can_bind(port: int) -> bool:
    """Could a server listen on 127.0.0.1:port right now?"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # Like the server itself: a lingering TIME_WAIT must not count.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def port_state(port: int) -> Tuple[str, Optional[dict]]:
    """"free", "litreview" (with its /health), "other", or "blocked" (nothing
    answers but the port can't be used: Windows reserves some ranges)."""
    if can_bind(port):
        return "free", None
    connected, health = get_health(port)
    if is_litreview(health):
        return "litreview", health
    return ("other" if connected else "blocked"), None


def pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5            # access denied: it exists
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259                    # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_server_json(data_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((data_dir / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_running_server(data_dir: Path) -> Optional[int]:
    """The port of this data folder's live server, per its server.json."""
    info = read_server_json(data_dir)
    if not info:
        return None
    port, pid = info.get("port"), info.get("pid")
    if not isinstance(port, int) or not isinstance(pid, int) or not pid_alive(pid):
        return None
    return port if is_litreview(get_health(port)[1]) else None


def same_path(a: str, b: str) -> bool:
    try:
        return (os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b)))
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------------
# Running the server


def start_server(python: Path, config: Config, local_dir: Path = HERE,
                 ) -> "subprocess.Popen[bytes]":
    """`<venv python> -m litreview_local --port N [--data-dir D]` in local/.

    The data folder is passed on only when the user chose one, so the
    server's own default (and its Settings page) stay in charge otherwise.
    On macOS the server gets its own session: closing the Terminal window
    then reaches only the launcher, which stops the server cleanly.
    """
    cmd = [str(python), "-m", "litreview_local", "--port", str(config.port)]
    env = dict(os.environ)
    env["LITREVIEW_PORT"] = str(config.port)
    env["PYTHONUNBUFFERED"] = "1"
    if config.data_dir_explicit:
        cmd += ["--data-dir", str(config.data_dir)]
        env["LITREVIEW_DATA_DIR"] = str(config.data_dir)
    kwargs = {"cwd": str(local_dir), "env": env, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        if config.smoke:   # so the smoke test can send it Ctrl+Break alone
            kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)  # type: ignore[call-overload]


def wait_for_health(proc: "subprocess.Popen[bytes]", port: int,
                    timeout: float = START_TIMEOUT) -> Optional[dict]:
    """Poll /health until the app answers as litreview-local, the process
    exits, or the time is up (None for the last two).

    Never probes by binding the port: that could collide with the server's
    own bind at the same moment.
    """
    start = time.monotonic()
    deadline = start + timeout
    next_note = start + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        health = get_health(port, timeout=5.0, connect_timeout=0.5)[1]
        if is_litreview(health):
            return health
        if time.monotonic() >= next_note:
            say("Still starting...")
            next_note += 15
        time.sleep(0.25)
    return None


def _kill(proc: "subprocess.Popen[bytes]") -> None:
    try:
        proc.kill()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def stop_server(proc: "subprocess.Popen[bytes]", reason: str = "terminate",
                grace: float = STOP_GRACE) -> Optional[int]:
    """Ask the server to stop, wait, and kill it if it won't.

    reason: "interrupt" (Ctrl+C), "terminate" (window closed, SIGTERM) or
    "smoke". On Windows the console already delivered Ctrl+C or Ctrl+Break to
    the server, which shares it; only the smoke test's server, in its own
    process group, is sent Ctrl+Break here.
    """
    if proc.poll() is not None:
        return proc.returncode
    try:
        if os.name == "nt":
            if reason == "smoke":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.send_signal(signal.SIGINT if reason == "interrupt" else signal.SIGTERM)
    except (OSError, ValueError):
        pass
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:      # a second Ctrl+C: stop waiting
        pass
    _kill(proc)
    return proc.returncode


class _StopRequested(Exception):
    pass


def _raise_stop(signum: int, frame: object) -> None:
    raise _StopRequested()


def _install_stop_handlers() -> Dict[int, object]:
    """Window closed (SIGHUP) or SIGTERM on macOS, Ctrl+Break on Windows."""
    names = ("SIGBREAK",) if os.name == "nt" else ("SIGHUP", "SIGTERM")
    previous = {}  # type: Dict[int, object]
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, _raise_stop)
        except (OSError, ValueError):      # not the main thread
            pass
    return previous


def _ignore_stop_signals(previous: Mapping[int, object]) -> None:
    for sig in previous:
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass


def _restore_handlers(previous: Mapping[int, object]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (OSError, ValueError, TypeError):
            pass


def run_until_stopped(proc: "subprocess.Popen[bytes]", data_dir: Path) -> int:
    """Wait on the server; stop it cleanly on Ctrl+C or when the window closes."""
    previous = _install_stop_handlers()
    reason = None
    code = None
    try:
        try:
            while code is None:
                try:
                    code = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            # It ended by itself - or the console's Ctrl+C reached it a moment
            # before it reached us. A Ctrl+C on its way lands in this sleep.
            time.sleep(0.5)
        except KeyboardInterrupt:
            reason = "interrupt"
        except _StopRequested:
            reason = "terminate"
        if reason is None:
            if code in (0, STATUS_CONTROL_C_EXIT, -signal.SIGINT, 130):
                say("LitReview has stopped.")
                return 0
            say("", "LitReview stopped unexpectedly (exit code %s)." % code,
                "The messages above say why; the app's log is in %s."
                % (data_dir / "logs" / "server.log"))
            return 1
        _ignore_stop_signals(previous)
        say("", "Stopping LitReview...")
        stop_server(proc, reason)
        say("LitReview has stopped. You can close this window.")
        return 0
    finally:
        _restore_handlers(previous)


def open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:  # webbrowser can raise almost anything on odd systems
        opened = False
    if not opened:
        say("Open %s in your web browser." % url)


def banner(url: str, data_dir: str, first_run: bool) -> str:
    rule = "  " + "-" * 66
    lines = ["", rule]
    if first_run:
        lines.append("   Welcome! LitReview is running.")
    else:
        lines.append("   LitReview is running.")
    lines += [
        "",
        "   In your browser:  %s" % url,
        "   Your data:        %s" % data_dir,
        "   To stop it:       press Ctrl+C in this window, or close the window.",
        "                     Keep this window open while you use LitReview.",
        "",
        "   Next: \"Getting started\" in the app shows how to connect Claude",
        "         Desktop or Claude Code.",
        rule,
        "",
    ]
    return "\n".join(lines)


def port_problem(port: int, state: str, platform: Optional[str] = None) -> str:
    if state == "blocked":
        why = ("This computer doesn't let programs use port %d (Windows reserves "
               "some ports, often for Hyper-V, WSL or Docker)." % port)
    else:
        why = ("Another program is already using port %d, so LitReview can't "
               "start there." % port)
    lines = [why, "", "Start LitReview on another port instead, for example 8766:"]
    if _is_windows(platform):
        lines += [
            "  * Open Command Prompt and run:  setx LITREVIEW_PORT 8766",
            "    Then close it and double-click start-windows.bat again.",
        ]
    elif (platform or sys.platform) == "darwin":
        lines += [
            "  * Open Terminal and run:",
            "      echo 'export LITREVIEW_PORT=8766' >> ~/.zshrc",
            "    Then double-click start-mac.command again.",
        ]
    lines += [
        "  * Or start it with:  python launcher.py --port 8766",
        "",
        "LITREVIEW_PORT and --port do the same thing. If you have connected",
        "Claude Desktop, press Add LitReview to Claude Desktop again afterwards",
        "(on Getting started in the app), so it uses the new port.",
    ]
    return "\n".join(lines)


def _already_running(port: int, config: Config) -> int:
    url = base_url(port)
    say("LitReview is already running at %s." % url)
    if port != config.port:
        say("(Not starting a second copy on port %d.)" % config.port)
    if not config.no_browser:
        say("Opening it in your browser.")
        open_browser(url)
    return 0


def run_app(config: Config, local_dir: Path = HERE,
            prepare: Optional[Callable[[], Path]] = None) -> int:
    running = find_running_server(config.data_dir)
    if running is not None:
        return _already_running(running, config)

    state, health = port_state(config.port)
    if state == "litreview":
        theirs = (health or {}).get("data_dir")
        if config.data_dir_explicit and theirs and not same_path(theirs, str(config.data_dir)):
            raise LauncherError(
                "LitReview is already running on port %d, but with the data folder\n"
                "  %s\nnot the one you asked for:\n  %s\n\n"
                "Stop the other copy first, or give this one another port with "
                "--port or LITREVIEW_PORT." % (config.port, theirs, config.data_dir))
        return _already_running(config.port, config)
    if state != "free":
        raise LauncherError(port_problem(config.port, state))

    if prepare is None:
        python, created_by = ensure_venv()
        ensure_requirements(python, created_by)
    else:
        python = prepare()

    first_run = not (config.data_dir / "litreview.db").exists()
    say("", "Starting LitReview...")
    proc = start_server(python, config, local_dir)
    try:
        health = wait_for_health(proc, config.port, START_TIMEOUT)
    except BaseException:
        stop_server(proc, "interrupt")
        raise
    if health is None:
        code = proc.poll()
        if code is None:
            stop_server(proc, "terminate")
            raise LauncherError(
                "LitReview did not answer within %d seconds, so it was stopped. "
                "The messages above may say why; the app's log is in %s."
                % (START_TIMEOUT, config.data_dir / "logs" / "server.log"))
        # Lost a race with another copy starting at the same moment?
        if is_litreview(get_health(config.port)[1]):
            return _already_running(config.port, config)
        raise LauncherError(
            "LitReview stopped before it was ready (exit code %s). The messages "
            "above say why; the app's log is in %s."
            % (code, config.data_dir / "logs" / "server.log"))

    url = base_url(config.port)
    say(banner(url, str(health.get("data_dir") or config.data_dir), first_run))
    if not config.no_browser:
        # A new library opens on Getting started; after that, on Projects.
        open_browser(url + "/start" if first_run else url)
    return run_until_stopped(proc, config.data_dir)


# --------------------------------------------------------------------------
# --smoke


def _first_jsonrpc(res: HttpResult) -> Optional[dict]:
    """The JSON-RPC message in a /mcp answer, plain JSON or an SSE stream."""
    text = res.body.decode("utf-8", "replace")
    if "text/event-stream" in res.headers.get("content-type", ""):
        data = [line[5:].lstrip() for line in text.splitlines()
                if line.startswith("data:")]
        text = data[0] if data else ""
    try:
        message = json.loads(text)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def mcp_initialize(port: int) -> Tuple[bool, str]:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "litreview-launcher-smoke",
                                  "version": "0.1.0"}},
    }).encode("utf-8")
    res = http_request(port, "POST", "/mcp", body=body, connect_timeout=1.0,
                       timeout=30.0, headers={
                           "Content-Type": "application/json",
                           "Accept": "application/json, text/event-stream"})
    if not res.connected:
        return False, "nothing answered on port %d" % port
    if res.status != 200:
        return False, "HTTP %s: %s" % (res.status, res.body[:200].decode("utf-8", "replace"))
    message = _first_jsonrpc(res)
    result = message.get("result") if message else None
    if not isinstance(result, dict) or not result.get("protocolVersion"):
        return False, "not an initialize result: %s" % res.body[:200].decode("utf-8", "replace")
    info = result.get("serverInfo") or {}
    session = res.headers.get("mcp-session-id")
    return True, "%s %s, protocol %s%s" % (
        info.get("name", "?"), info.get("version", "?"), result["protocolVersion"],
        ", session id given" if session else "")


def run_smoke(config: Config, local_dir: Path = HERE,
              prepare: Optional[Callable[[], Path]] = None,
              start_timeout: float = SMOKE_START_TIMEOUT) -> int:
    """Start on a free port with a throwaway data folder (unless told
    otherwise), check /health and MCP initialize, stop. 0 = pass."""
    port = config.port if config.port_explicit else free_port()
    scratch = None
    data_dir = config.data_dir
    if not config.data_dir_explicit:
        scratch = tempfile.mkdtemp(prefix="litreview-smoke-")
        data_dir = Path(scratch)
    config = config._replace(port=port, data_dir=data_dir, data_dir_explicit=True,
                             no_browser=True, smoke=True)
    try:
        if find_running_server(data_dir) is not None:
            say("SMOKE FAIL: LitReview is already running for %s; stop it first." % data_dir)
            return 1
        if port_state(port)[0] != "free":
            say("SMOKE FAIL: port %d is in use." % port)
            return 1
        if prepare is None:
            python, created_by = ensure_venv()
            ensure_requirements(python, created_by)
        else:
            python = prepare()
        say("Smoke test: starting LitReview on port %d with data in %s" % (port, data_dir))
        proc = start_server(python, config, local_dir)
        try:
            health = wait_for_health(proc, port, start_timeout)
            if health is None:
                say("SMOKE FAIL: /health never answered as %s (exit code %s)."
                    % (APP, proc.poll()))
                return 1
            say("SMOKE ok: /health -> %s" % json.dumps(health, sort_keys=True))
            ok, detail = mcp_initialize(port)
            if not ok:
                say("SMOKE FAIL: MCP initialize: %s" % detail)
                return 1
            say("SMOKE ok: MCP initialize -> %s" % detail)
        finally:
            # Short grace: without a console (some CI runners) Ctrl+Break
            # can't reach it, and a kill is fine for a smoke test.
            code = stop_server(proc, "smoke", grace=5.0)
            say("Smoke test: server stopped (exit code %s)." % code)
        say("SMOKE PASS")
        return 0
    except LauncherError as exc:
        say("SMOKE FAIL: " + str(exc))
        return 1
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerant_streams()
    if sys.version_info < (3, 8):
        say("LitReview's launcher needs Python 3.8 or newer; this is Python %s."
            % sys.version.split()[0])
        return 1
    args = parse_args(argv)
    try:
        config = make_config(args)
        if config.smoke:
            return run_smoke(config)
        return run_app(config)
    except LauncherError as exc:
        say("", str(exc))
        return 1
    except KeyboardInterrupt:
        say("", "Stopped.")
        return 1


if __name__ == "__main__":
    sys.exit(main())

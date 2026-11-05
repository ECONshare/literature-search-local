#!/usr/bin/env python3
"""Connect Claude Desktop to LitReview Local: a stdio <-> HTTP bridge for MCP.

Claude Desktop starts each MCP server as a child process and talks to it in
newline-delimited JSON-RPC over stdin and stdout. LitReview serves MCP over
HTTP at http://127.0.0.1:<port>/mcp. This script sits in between:

* Each line from stdin is POSTed to /mcp as it is (a JSON array, a batch,
  included), with Content-Type: application/json and
  Accept: application/json, text/event-stream.
* Each JSON answer is written to stdout on one line and flushed. A
  notification gets 202 and no body, so nothing is written for it.
* The Mcp-Session-Id the server hands out is kept and sent back.
* Requests run side by side, so a tool that waits on the server for minutes
  holds up nothing else; HTTP waits up to 15 minutes for an answer.
* stdout carries JSON-RPC only. Everything else goes to stderr, which Claude
  Desktop keeps in its log (mcp-server-LitReview Local.log).

Finding the server: LITREVIEW_PORT, else the port in <data dir>/server.json
(LITREVIEW_DATA_DIR, else ~/LitReview), else 8765. If /health doesn't answer,
the bridge starts LitReview itself and waits up to 30 seconds:
`<python> -m litreview_local` in this folder, detached and windowless so it
outlives Claude Desktop, with its output in <data dir>/logs/server-bridge.log.
<python> is the app's venv beside this file (.venv/Scripts/python.exe or
.venv/bin/python) - whatever Python Claude happened to run the bridge with -
and sys.executable only when there is no venv. If starting fails too, every
request gets a JSON-RPC error saying how to start LitReview by hand.

    python mcp_bridge.py            # what Claude Desktop runs
    python mcp_bridge.py --check    # one line of JSON: can it reach LitReview?

Standard library only, and it runs on Python 3.8 or newer: no `X | None` or
`list[str]` evaluated at runtime.
"""

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Dict, List, Mapping, Optional, Sequence, Tuple

APP = "litreview-local"
DEFAULT_PORT = 8765
BRIDGE_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent

HTTP_TIMEOUT = 15 * 60.0      # tools can block server-side while they wait for work
# Connecting to a listening port on 127.0.0.1 takes well under a millisecond;
# a refused one takes about 2 s on Windows unless the timeout cuts it short.
CONNECT_TIMEOUT = 1.0
START_TIMEOUT = 30.0
RETRY_START_AFTER = 30.0      # after a failed start, answer errors for this long
LOG_ROTATE_BYTES = 5 * 1024 * 1024

# Windows process creation flags, spelled out so this file also imports on
# macOS (where subprocess doesn't define them) and the tests can use them.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

SERVER_UNAVAILABLE = -32000   # JSON-RPC: implementation-defined server error
PARSE_ERROR = -32700
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    """Write to stderr, which Claude Desktop keeps in mcp-server-LitReview Local.log."""
    try:
        sys.stderr.write("[litreview-bridge] %s\n" % message)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------
# Where is the server?


def default_data_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    environ = os.environ if environ is None else environ
    raw = (environ.get("LITREVIEW_DATA_DIR") or "").strip()
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return Path.home() / "LitReview"


def read_server_json(data_dir: Path) -> Optional[dict]:
    try:
        data = json.loads((data_dir / "server.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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


def discover_port(environ: Optional[Mapping[str, str]] = None,
                  data_dir: Optional[Path] = None) -> Tuple[int, str]:
    """(port, where it came from): LITREVIEW_PORT, then a live server.json,
    then 8765. A server.json whose pid is dead is stale and ignored."""
    environ = os.environ if environ is None else environ
    raw = (environ.get("LITREVIEW_PORT") or "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if 1 <= port <= 65535:
            return port, "LITREVIEW_PORT"
        log("ignoring LITREVIEW_PORT=%r: not a port number" % raw)
    info = read_server_json(data_dir if data_dir is not None else default_data_dir(environ))
    if info:
        port, pid = info.get("port"), info.get("pid")
        if (isinstance(port, int) and 1 <= port <= 65535
                and isinstance(pid, int) and pid_alive(pid)):
            return port, "server.json"
    return DEFAULT_PORT, "default"


# --------------------------------------------------------------------------
# HTTP to 127.0.0.1 (http.client directly: no proxy settings can get in the way)


class ConnectFailed(Exception):
    """Nothing accepted the connection, so the request never reached the
    server - which makes it safe to send again once the server is up."""


class _Connection(http.client.HTTPConnection):
    """An HTTPConnection with a short connect timeout and a long read one."""

    def __init__(self, port: int, connect_timeout: float, timeout: float) -> None:
        http.client.HTTPConnection.__init__(self, "127.0.0.1", port, timeout=timeout)
        self._connect_timeout = connect_timeout

    def connect(self) -> None:
        try:
            self.sock = socket.create_connection((self.host, self.port),
                                                 self._connect_timeout)
        except OSError as exc:
            raise ConnectFailed(str(exc))
        self.sock.settimeout(self.timeout)


def get_health(port: int, timeout: float = 5.0,
               connect_timeout: float = 1.0) -> Optional[dict]:
    conn = _Connection(port, connect_timeout, timeout)
    try:
        conn.request("GET", "/health", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return None
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (ConnectFailed, OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()


def is_litreview(health: Optional[dict]) -> bool:
    return (isinstance(health, dict) and health.get("ok") is True
            and health.get("app") == APP)


# --------------------------------------------------------------------------
# Starting the server when nobody else has


def server_python(folder: Path = HERE, platform: Optional[str] = None) -> str:
    """The Python to start the server with: the venv beside the bridge.

    The bridge may be run by any Python (Claude Desktop runs whatever its
    config names; on some PCs that is an old Anaconda), but the server needs
    the venv's 3.10+ and its packages. sys.executable is the fallback for a
    checkout without a venv.
    """
    windows = (platform or sys.platform) == "win32"
    venv = folder / ".venv" / ("Scripts" if windows else "bin") / (
        "python.exe" if windows else "python")
    return str(venv) if venv.is_file() else sys.executable


def spawn_detached(cmd: Sequence[str], cwd: Path, env: Mapping[str, str],
                   log_file: IO[bytes], windows: Optional[bool] = None,
                   popen: Any = None) -> "subprocess.Popen[bytes]":
    """Start a server that has no window and survives Claude Desktop quitting.

    Windows: no console window, its own process group, and out of Claude
    Desktop's job object, so closing Desktop doesn't take the server with it.
    A job that forbids breaking away refuses the whole CreateProcess; then it
    is started inside the job, which still works while Desktop runs.
    POSIX: a new session, so Desktop's signals don't reach it.
    """
    popen = popen or subprocess.Popen
    windows = (os.name == "nt") if windows is None else windows
    kwargs = {"cwd": str(cwd), "env": dict(env), "stdin": subprocess.DEVNULL,
              "stdout": log_file, "stderr": subprocess.STDOUT, "close_fds": True}
    if not windows:
        return popen(list(cmd), start_new_session=True, **kwargs)
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        return popen(list(cmd), creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError as exc:
        log("starting outside Claude Desktop's job was refused (%s); starting it "
            "inside, so it may stop when Claude Desktop quits" % exc)
        return popen(list(cmd), creationflags=flags, **kwargs)


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(str(path), str(path) + ".1")
    except OSError:
        pass


def _tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_bytes()[-4000:].decode("utf-8", "replace")
    except OSError:
        return ""
    return " | ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())


def start_by_hand_hint(platform: Optional[str] = None, folder: Path = HERE) -> str:
    platform = platform or sys.platform
    if platform == "win32":
        return "open the folder %s and double-click start-windows.bat" % folder
    if platform == "darwin":
        return "open the folder %s and double-click start-mac.command" % folder
    return "run  python3 launcher.py  in %s" % folder


# --------------------------------------------------------------------------
# The bridge


def request_ids(message: Any) -> List[Any]:
    """The ids that expect an answer: requests have a method and an id.
    Notifications (no id) and responses (no method) expect nothing."""
    if isinstance(message, list):
        ids = []  # type: List[Any]
        for item in message:
            ids.extend(request_ids(item))
        return ids
    if isinstance(message, dict) and "method" in message and message.get("id") is not None:
        return [message["id"]]
    return []


def _is_rpc_response(obj: Any) -> bool:
    if isinstance(obj, list):
        return bool(obj) and all(_is_rpc_response(item) for item in obj)
    return isinstance(obj, dict) and ("result" in obj or "error" in obj)


def _answered_ids(obj: Any) -> List[Any]:
    if isinstance(obj, list):
        return [item.get("id") for item in obj if isinstance(item, dict)]
    return [obj.get("id")] if isinstance(obj, dict) else []


class Bridge:
    """Forwards newline-delimited JSON-RPC between a stream pair and /mcp."""

    def __init__(self, port: Optional[int] = None, data_dir: Optional[Path] = None,
                 out: Optional[IO[bytes]] = None,
                 environ: Optional[Mapping[str, str]] = None,
                 autostart: bool = True, python: Optional[str] = None,
                 server_cwd: Path = HERE, start_timeout: float = START_TIMEOUT,
                 http_timeout: float = HTTP_TIMEOUT) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.data_dir = data_dir if data_dir is not None else default_data_dir(self.environ)
        self.fixed_port = port
        if port is not None:
            self.port, self.port_source = port, "--port"
        else:
            self.port, self.port_source = discover_port(self.environ, self.data_dir)
        self.out = out if out is not None else sys.stdout.buffer
        self.autostart = autostart
        self.python = python or server_python(server_cwd)
        self.server_cwd = server_cwd
        self.start_timeout = start_timeout
        self.http_timeout = http_timeout
        self.session_id = None          # type: Optional[str]
        self.protocol_version = None    # type: Optional[str]
        self._last_start = None         # type: Optional[float]
        self._last_error = None         # type: Optional[str]
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._server_lock = threading.Lock()
        self._threads = []              # type: List[threading.Thread]

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "server-bridge.log"

    # -- stdout ------------------------------------------------------------

    def _write_bytes(self, data: bytes) -> None:
        with self._write_lock:
            try:
                self.out.write(data + b"\n")
                self.out.flush()
            except (OSError, ValueError) as exc:  # Claude Desktop has gone
                log("could not write to Claude: %s" % exc)

    def _write_obj(self, obj: Any) -> None:
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:     # a lone surrogate: escape everything
            data = json.dumps(obj, separators=(",", ":")).encode("ascii")
        self._write_bytes(data)

    def _write_json_body(self, raw: bytes, obj: Any) -> None:
        """The server's own bytes when they are one line of UTF-8; else re-encoded."""
        stripped = raw.strip()
        if b"\n" in stripped or b"\r" in stripped:
            self._write_obj(obj)
            return
        try:
            stripped.decode("utf-8")
        except UnicodeDecodeError:
            self._write_obj(obj)
            return
        self._write_bytes(stripped)

    def _write_errors(self, message: Any, ids: Sequence[Any], code: int, text: str) -> None:
        if not ids:
            return
        errors = [{"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": text}}
                  for i in ids]
        self._write_obj(errors if isinstance(message, list) else errors[0])

    # -- the server ----------------------------------------------------------

    def not_running_message(self, reason: str) -> str:
        return ("LitReview is not running on this computer, and Claude could not "
                "start it (%s). To start it yourself, %s, wait until it opens in "
                "your browser, and try again. Details are in %s."
                % (reason, start_by_hand_hint(folder=self.server_cwd), self.log_path))

    def _healthy(self) -> bool:
        if is_litreview(get_health(self.port, connect_timeout=0.5)):
            return True
        if self.fixed_port is None:     # it may have come up on another port
            port, source = discover_port(self.environ, self.data_dir)
            if port != self.port and is_litreview(get_health(port, connect_timeout=0.5)):
                log("LitReview is on port %d now (from %s)" % (port, source))
                self.port, self.port_source = port, source
                return True
        return False

    def ensure_server(self) -> Optional[str]:
        """None when LitReview answers; otherwise the message to send Claude."""
        with self._server_lock:
            if self._healthy():
                self._last_error = None
                return None
            if not self.autostart:
                return self.not_running_message("it isn't answering on port %d" % self.port)
            if (self._last_error and self._last_start is not None
                    and time.monotonic() - self._last_start < RETRY_START_AFTER):
                return self._last_error
            self._last_start = time.monotonic()
            self._last_error = self._start_server()
            return self._last_error

    def _start_server(self) -> Optional[str]:
        """Start the app - unless another bridge is starting it right now.

        Claude Desktop has been seen running two copies of the bridge at once.
        Their in-process lock cannot see each other, so the start itself is
        claimed with a lock file next to the log: the bridge that creates it
        starts the app, the other waits for /health. A lock older than the
        start timeout is a leftover from a bridge that died, and is taken over.
        """
        lock = self.log_path.parent / "bridge-start.lock"
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for _ in range(2):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    continue  # gone meanwhile: try to claim it again
                if age > self.start_timeout + 10:
                    try:
                        lock.unlink()
                    except OSError:
                        pass
                    continue
                return self._wait_for_other_start()
            except OSError:
                break  # cannot lock (read-only folder?): start as before
            else:
                os.close(fd)
                try:
                    return self._start_server_now()
                finally:
                    try:
                        lock.unlink()
                    except OSError:
                        pass
        return self._start_server_now()

    def _wait_for_other_start(self) -> Optional[str]:
        log("Another LitReview bridge is starting the app; waiting for it.")
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self._healthy():
                return None
            time.sleep(0.5)
        return self.not_running_message(
            "another copy of the bridge was starting it, but it did not answer "
            "within %d seconds" % int(self.start_timeout))

    def _start_server_now(self) -> Optional[str]:
        cmd = [self.python, "-m", "litreview_local", "--port", str(self.port)]
        env = dict(self.environ)
        env["LITREVIEW_PORT"] = str(self.port)
        env["PYTHONUNBUFFERED"] = "1"
        log("LitReview isn't answering on port %d; starting it (output in %s)"
            % (self.port, self.log_path))
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(self.log_path)
            log_file = open(str(self.log_path), "ab")
        except OSError as exc:
            return self.not_running_message("it could not write %s: %s" % (self.log_path, exc))
        try:
            stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            log_file.write(("\n--- %s: started by mcp_bridge.py: %s ---\n"
                            % (stamp, " ".join(cmd))).encode("utf-8", "replace"))
            log_file.flush()
            proc = spawn_detached(cmd, self.server_cwd, env, log_file)
        except OSError as exc:
            return self.not_running_message("%s could not be run: %s" % (self.python, exc))
        finally:
            log_file.close()            # the server has its own handle now
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            if self._healthy():
                log("LitReview is up on port %d." % self.port)
                return None
            code = proc.poll()
            if code is not None:
                if self._healthy():     # another copy won the port: fine
                    return None
                tail = _tail(self.log_path)
                return self.not_running_message(
                    "it stopped straight away with exit code %s%s"
                    % (code, (": " + tail) if tail else ""))
            time.sleep(0.5)
        return self.not_running_message(
            "it did not answer within %d seconds" % self.start_timeout)

    # -- forwarding ------------------------------------------------------------

    def handle_line(self, line: bytes) -> None:
        body = line.strip()
        if not body:
            return
        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._write_obj({"jsonrpc": "2.0", "id": None, "error": {
                "code": PARSE_ERROR, "message": "Parse error: not a JSON line."}})
            return
        ids = request_ids(message)
        try:
            self.forward(body, message, ids)
        except Exception as exc:  # one bad message must not end the bridge
            log("forwarding failed: %r" % (exc,))
            self._write_errors(message, ids, INTERNAL_ERROR,
                               "The LitReview bridge failed: %s" % exc)

    def forward(self, body: bytes, message: Any, ids: Sequence[Any]) -> None:
        for attempt in (1, 2):
            try:
                self._post(body, message, ids)
                return
            except ConnectFailed:
                if attempt == 2:
                    break
                error = self.ensure_server()
                if error:
                    log(error)
                    self._write_errors(message, ids, SERVER_UNAVAILABLE, error)
                    return
        self._write_errors(message, ids, SERVER_UNAVAILABLE,
                           self.not_running_message("it stopped answering"))

    def _post(self, body: bytes, message: Any, ids: Sequence[Any]) -> None:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "litreview-mcp-bridge/%s" % BRIDGE_VERSION}
        initializing = isinstance(message, dict) and message.get("method") == "initialize"
        with self._state_lock:
            # A new initialize starts a new session: let the server hand one out.
            if self.session_id and not initializing:
                headers["Mcp-Session-Id"] = self.session_id
            if self.protocol_version and not initializing:
                headers["MCP-Protocol-Version"] = self.protocol_version
        conn = _Connection(self.port, CONNECT_TIMEOUT, self.http_timeout)
        try:
            conn.connect()              # ConnectFailed goes up: safe to retry
            try:
                conn.request("POST", "/mcp", body=body, headers=headers)
                resp = conn.getresponse()
            except socket.timeout:
                self._write_errors(message, ids, SERVER_UNAVAILABLE,
                                   "LitReview took longer than %d minutes to answer."
                                   % (self.http_timeout // 60))
                return
            except (OSError, http.client.HTTPException) as exc:
                self._write_errors(message, ids, SERVER_UNAVAILABLE,
                                   "LitReview closed the connection before answering "
                                   "(%s). It may have been stopped; try again." % exc)
                return
            session = resp.getheader("Mcp-Session-Id")
            if session and session.strip():
                with self._state_lock:
                    self.session_id = session.strip()
            ctype = (resp.getheader("Content-Type") or "").lower()
            if resp.status == 200 and "text/event-stream" in ctype:
                self._relay_sse(resp, message, ids)
            else:
                self._relay_json(resp, message, ids)
        finally:
            conn.close()

    def _note_initialize(self, obj: Any) -> None:
        for item in (obj if isinstance(obj, list) else [obj]):
            result = item.get("result") if isinstance(item, dict) else None
            if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str) \
                    and "serverInfo" in result:
                with self._state_lock:
                    self.protocol_version = result["protocolVersion"]

    def _relay_json(self, resp: http.client.HTTPResponse, message: Any,
                    ids: Sequence[Any]) -> None:
        try:
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            self._write_errors(message, ids, SERVER_UNAVAILABLE,
                               "LitReview's answer was cut off (%s)." % exc)
            return
        if not raw.strip():
            if ids:   # 202 is only for notifications; a request needs an answer
                self._write_errors(message, ids, INTERNAL_ERROR,
                                   "LitReview answered HTTP %d with no content."
                                   % resp.status)
            return
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            obj = None
        if not _is_rpc_response(obj):
            snippet = raw[:300].decode("utf-8", "replace").strip()
            self._write_errors(message, ids, INTERNAL_ERROR,
                               "LitReview answered HTTP %d: %s" % (resp.status, snippet))
            return
        if not ids:
            return                      # nothing expected an answer
        self._note_initialize(obj)
        self._write_json_body(raw, obj)

    def _relay_sse(self, resp: http.client.HTTPResponse, message: Any,
                   ids: Sequence[Any]) -> None:
        """Pass on each JSON-RPC message of a text/event-stream answer."""
        pending = list(ids)
        data = []  # type: List[bytes]

        def dispatch() -> None:
            text = b"\n".join(data)
            del data[:]
            try:
                obj = json.loads(text.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                log("ignoring an event that isn't JSON: %r" % text[:100])
                return
            self._note_initialize(obj)
            for answered in _answered_ids(obj):
                if answered in pending:
                    pending.remove(answered)
            self._write_obj(obj)

        try:
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.rstrip(b"\r\n")
                if not line:
                    if data:
                        dispatch()
                    continue
                if line.startswith(b":"):
                    continue
                field, _, value = line.partition(b":")
                if field == b"data":
                    data.append(value[1:] if value.startswith(b" ") else value)
            if data:
                dispatch()
        except (OSError, http.client.HTTPException) as exc:
            log("the event stream broke off: %s" % exc)
        if pending:
            self._write_errors(message, pending, SERVER_UNAVAILABLE,
                               "LitReview ended its answer without a result.")

    # -- the loop --------------------------------------------------------------

    def _warm_up(self) -> None:
        error = self.ensure_server()
        if error:
            log(error)

    def run(self, infile: Optional[IO[bytes]] = None) -> int:
        infile = infile if infile is not None else sys.stdin.buffer
        log("bridge %s on Python %s; LitReview at http://127.0.0.1:%d/mcp (port from %s)"
            % (BRIDGE_VERSION, sys.version.split()[0], self.port, self.port_source))
        if self.autostart:   # start the server while Claude is still connecting
            threading.Thread(target=self._warm_up, daemon=True).start()
        while True:
            try:
                line = infile.readline()
            except (OSError, ValueError):
                break
            if not line:
                break
            worker = threading.Thread(target=self.handle_line, args=(line,), daemon=True)
            self._threads = [t for t in self._threads if t.is_alive()] + [worker]
            worker.start()
        # stdin closed. Let the answers already on their way reach stdout.
        deadline = time.monotonic() + 30
        for worker in self._threads:
            worker.join(max(0.0, deadline - time.monotonic()))
        return 0


# --------------------------------------------------------------------------
# --check


def check_status(port: Optional[int] = None, data_dir: Optional[Path] = None,
                 environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """What the Connect Claude page shows: can this Python reach LitReview?"""
    environ = os.environ if environ is None else environ
    data_dir = data_dir if data_dir is not None else default_data_dir(environ)
    if port is not None:
        source = "--port"
    else:
        port, source = discover_port(environ, data_dir)
    health = get_health(port)
    status = {
        "ok": is_litreview(health),
        "port": port,
        "port_source": source,
        "url": "http://127.0.0.1:%d/mcp" % port,
        "data_dir": str(data_dir),
        "python": sys.executable,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "server_python": server_python(),
        "bridge": str(Path(__file__).resolve()),
        "bridge_version": BRIDGE_VERSION,
    }  # type: Dict[str, Any]
    if status["ok"]:
        status["server"] = health
    else:
        status["error"] = ("LitReview is not answering at http://127.0.0.1:%d/health; "
                           "to start it, %s." % (port, start_by_hand_hint()))
    return status


def main(argv: Optional[Sequence[str]] = None) -> int:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr   # a stray print() must never reach the JSON-RPC stream
    if sys.version_info < (3, 8):
        log("needs Python 3.8 or newer; this is %s" % sys.version.split()[0])
        return 1
    parser = argparse.ArgumentParser(
        prog="mcp_bridge.py",
        description="Connect Claude Desktop (stdio) to LitReview Local (HTTP).")
    parser.add_argument("--check", action="store_true",
                        help="print one line of JSON saying whether LitReview answers")
    parser.add_argument("--port", type=int, default=None,
                        help="LitReview's port (default: LITREVIEW_PORT, server.json, 8765)")
    parser.add_argument("--data-dir", default=None,
                        help="LitReview's data folder (default: LITREVIEW_DATA_DIR or ~/LitReview)")
    args = parser.parse_args(argv)
    data_dir = (Path(os.path.abspath(os.path.expanduser(args.data_dir)))
                if args.data_dir else None)
    if args.check:
        status = check_status(args.port, data_dir)
        out.write((json.dumps(status) + "\n").encode("utf-8"))
        out.flush()
        return 0 if status["ok"] else 1
    return Bridge(port=args.port, data_dir=data_dir, out=out).run()


if __name__ == "__main__":
    sys.exit(main())

"""Tests for mcp_bridge.py, the stdio <-> HTTP bridge Claude Desktop runs.

The real server doesn't exist yet, so these talk to fakes: an in-process
HTTP server for the forwarding rules, and a fake `litreview_local` package
written into a temp folder for the start-it-yourself path, run as a real
subprocess.
"""
import ast
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import mcp_bridge  # noqa: E402

BRIDGE = LOCAL / "mcp_bridge.py"


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# -- an in-process stand-in for LitReview -------------------------------------


class FakeLitReview:
    """/health and /mcp, recording what arrives. Methods in the JSON-RPC
    message pick the behaviour, so each test says what it wants."""

    def __init__(self, app="litreview-local"):
        self.app = app
        self.received = []      # (headers, body bytes) per POST
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", ctype="application/json", headers=()):
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", ctype)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, json.dumps({"ok": True, "app": fake.app,
                                                "version": "0.1.0", "data_dir": "x"}).encode())
                else:
                    self._send(404, b'{"detail": "Not Found"}')

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
                self._send(*fake.answer(json.loads(body), self.headers))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def result(msg, result):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    def answer(self, msg, headers):
        if isinstance(msg, list):
            answers = [self.result(m, {"echo": m["method"]}) for m in msg if "id" in m]
            return (200, json.dumps(answers).encode()) if answers else (202,)
        if "id" not in msg:
            return (202,)
        method = msg["method"]
        if method == "initialize":
            return (200, json.dumps(self.result(msg, {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "litreview", "version": "0.1.0"}})).encode(),
                "application/json", [("Mcp-Session-Id", "sess-1")])
        if method == "slow":
            time.sleep(msg["params"]["seconds"])
        if method == "pretty":
            return (200, json.dumps(self.result(msg, {"a": [1, 2], "b": "x\ny"}),
                                    indent=2).encode())
        if method == "unicode":
            return (200, json.dumps(self.result(msg, {"name": "Søren ✓"}),
                                    ensure_ascii=False).encode("utf-8"))
        if method == "sse":
            progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progress": 1}}
            final = json.dumps(self.result(msg, {"done": True}), indent=1)
            body = "event: message\ndata: %s\n\n: keep-alive\n\n" % json.dumps(progress)
            body += "".join("data: %s\n" % line for line in final.splitlines()) + "\n"
            return (200, body.encode(), "text/event-stream")
        if method == "http500":
            return (500, b"Internal Server Error", "text/plain")
        if method == "rpc400":
            return (400, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32602, "message": "bad params"}}).encode())
        if method == "empty202":
            return (202,)
        return (200, json.dumps(self.result(msg, {"echo": method})).encode())


@pytest.fixture
def fake():
    server = FakeLitReview()
    yield server
    server.close()


def make_bridge(port, tmp_path, **kwargs):
    out = io.BytesIO()
    kwargs.setdefault("autostart", False)
    bridge = mcp_bridge.Bridge(port=port, data_dir=tmp_path / "data", out=out,
                               environ={}, **kwargs)
    return bridge, out


def request(method, id=1, **params):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params:
        msg["params"] = params
    return json.dumps(msg).encode()


def lines(out):
    raw = out.getvalue()
    assert raw == b"" or raw.endswith(b"\n")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


# -- forwarding ---------------------------------------------------------------------


def test_a_request_is_posted_and_answered_on_one_line(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("initialize") + b"\r\n")
    (answer,) = lines(out)
    assert answer["id"] == 1 and answer["result"]["serverInfo"]["name"] == "litreview"
    headers, body = fake.received[0]
    assert body == request("initialize")                    # forwarded as is
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json, text/event-stream"


def test_the_session_id_is_kept_and_sent_back(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("initialize"))
    bridge.handle_line(request("tools/list", id=2))
    bridge.handle_line(request("initialize", id=3))           # a fresh session
    first, second, third = (headers for headers, _ in fake.received)
    assert "mcp-session-id" not in first
    assert second["mcp-session-id"] == "sess-1"
    assert second["mcp-protocol-version"] == "2025-06-18"
    assert "mcp-session-id" not in third
    assert [a["id"] for a in lines(out)] == [1, 2, 3]


def test_a_notification_gets_nothing_back(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}')
    assert out.getvalue() == b""
    assert len(fake.received) == 1


def test_a_batch_is_forwarded_as_is(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    batch = json.dumps([{"jsonrpc": "2.0", "id": 7, "method": "a"},
                        {"jsonrpc": "2.0", "method": "notifications/x"},
                        {"jsonrpc": "2.0", "id": "eight", "method": "b"}]).encode()
    bridge.handle_line(batch)
    assert fake.received[0][1] == batch
    (answer,) = lines(out)
    assert [a["id"] for a in answer] == [7, "eight"]


def test_a_multi_line_answer_is_written_on_one_line(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("pretty"))
    assert out.getvalue().count(b"\n") == 1
    assert lines(out)[0]["result"] == {"a": [1, 2], "b": "x\ny"}


def test_unicode_reaches_claude_as_utf8(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("unicode"))
    assert lines(out)[0]["result"]["name"] == "Søren ✓"


def test_an_event_stream_answer_is_relayed_message_by_message(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("sse", id=4))
    progress, final = lines(out)
    assert progress["method"] == "notifications/progress"
    assert final == {"jsonrpc": "2.0", "id": 4, "result": {"done": True}}


def test_http_errors_become_jsonrpc_errors_for_that_id(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(request("http500", id=5))
    bridge.handle_line(request("rpc400", id=6))
    bridge.handle_line(request("empty202", id=9))
    broken, passed_on, empty = lines(out)
    assert broken["id"] == 5 and "500" in broken["error"]["message"]
    assert passed_on == {"jsonrpc": "2.0", "id": 6,
                         "error": {"code": -32602, "message": "bad params"}}
    assert empty["id"] == 9 and "error" in empty


def test_a_line_that_is_not_json_gets_a_parse_error(fake, tmp_path):
    bridge, out = make_bridge(fake.port, tmp_path)
    bridge.handle_line(b"{this is not json")
    bridge.handle_line(b"   ")
    (error,) = lines(out)
    assert error["id"] is None and error["error"]["code"] == -32700
    assert fake.received == []


def test_a_slow_tool_does_not_hold_up_other_requests(fake, tmp_path):
    read_fd, write_fd = os.pipe()
    infile = os.fdopen(read_fd, "rb")
    out = io.BytesIO()
    bridge = mcp_bridge.Bridge(port=fake.port, data_dir=tmp_path, out=out,
                               environ={}, autostart=False)
    runner = threading.Thread(target=bridge.run, args=(infile,), daemon=True)
    runner.start()
    with os.fdopen(write_fd, "wb") as pipe:
        pipe.write(request("slow", id="slow", seconds=3) + b"\n")
        pipe.flush()
        time.sleep(0.2)
        pipe.write(request("ping", id="quick") + b"\n")
    runner.join(timeout=20)
    assert not runner.is_alive()
    assert [a["id"] for a in lines(out)] == ["quick", "slow"]


# -- when LitReview isn't there -----------------------------------------------------


def test_without_a_server_each_request_says_how_to_start_it(tmp_path):
    bridge, out = make_bridge(free_port(), tmp_path, autostart=False)
    bridge.handle_line(request("initialize", id=1))
    bridge.handle_line(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}')
    bridge.handle_line(request("tools/list", id=2))
    first, second = lines(out)
    assert [first["id"], second["id"]] == [1, 2]
    message = first["error"]["message"]
    assert first["error"]["code"] == mcp_bridge.SERVER_UNAVAILABLE
    assert "LitReview is not running" in message
    hint = {"win32": "start-windows.bat", "darwin": "start-mac.command"}.get(
        sys.platform, "launcher.py")
    assert hint in message and str(LOCAL) in message


def test_a_failed_start_is_not_retried_on_every_request(tmp_path, monkeypatch):
    starts = []

    def no_server(cmd, cwd, env, log_file, **kwargs):
        starts.append(cmd)
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(4)"])
    monkeypatch.setattr(mcp_bridge, "spawn_detached", no_server)
    bridge, out = make_bridge(free_port(), tmp_path, autostart=True, start_timeout=10)
    bridge.handle_line(request("tools/list", id=1))
    bridge.handle_line(request("tools/list", id=2))
    assert len(starts) == 1
    first, second = lines(out)
    assert "exit code 4" in first["error"]["message"]
    assert second["error"]["message"] == first["error"]["message"]
    assert starts[0][1:4] == ["-m", "litreview_local", "--port"]


# -- port discovery -----------------------------------------------------------------


def test_port_from_env_then_live_server_json_then_default(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    assert mcp_bridge.discover_port({}, data) == (8765, "default")
    (data / "server.json").write_text(json.dumps({"port": 9123, "pid": os.getpid()}))
    assert mcp_bridge.discover_port({}, data) == (9123, "server.json")
    assert mcp_bridge.discover_port({"LITREVIEW_PORT": "9300"}, data) == (9300, "LITREVIEW_PORT")
    assert mcp_bridge.discover_port({"LITREVIEW_PORT": "nonsense"}, data) == (9123, "server.json")


def test_a_stale_server_json_is_ignored(tmp_path):
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    (tmp_path / "server.json").write_text(json.dumps({"port": 9123, "pid": done.pid}))
    assert mcp_bridge.discover_port({}, tmp_path) == (8765, "default")
    (tmp_path / "server.json").write_text("{not json")
    assert mcp_bridge.discover_port({}, tmp_path) == (8765, "default")


def test_data_dir_from_env(tmp_path):
    assert mcp_bridge.default_data_dir({"LITREVIEW_DATA_DIR": str(tmp_path / "x y")}) == \
        tmp_path / "x y"
    assert mcp_bridge.default_data_dir({}) == Path.home() / "LitReview"


def test_pid_alive():
    assert mcp_bridge.pid_alive(os.getpid())
    assert not mcp_bridge.pid_alive(0) and not mcp_bridge.pid_alive(-5)


# -- starting detached ----------------------------------------------------------------


class RecordingPopen:
    def __init__(self, refuse_breakaway=False):
        self.calls = []
        self.refuse_breakaway = refuse_breakaway

    def __call__(self, cmd, **kwargs):
        self.calls.append(kwargs)
        flags = kwargs.get("creationflags", 0)
        if self.refuse_breakaway and flags & mcp_bridge.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied")
        return "process"


def test_windows_start_is_windowless_and_leaves_the_job(tmp_path):
    popen = RecordingPopen()
    log = io.BytesIO()
    assert mcp_bridge.spawn_detached(["py", "-m", "x"], tmp_path, {}, log,
                                     windows=True, popen=popen) == "process"
    (call,) = popen.calls
    flags = call["creationflags"]
    assert flags & mcp_bridge.CREATE_NO_WINDOW
    assert flags & mcp_bridge.CREATE_NEW_PROCESS_GROUP
    assert flags & mcp_bridge.CREATE_BREAKAWAY_FROM_JOB
    assert call["stdin"] == subprocess.DEVNULL and call["stdout"] is log
    assert call["stderr"] == subprocess.STDOUT and call["cwd"] == str(tmp_path)


def test_windows_start_falls_back_when_breaking_away_is_refused(tmp_path):
    popen = RecordingPopen(refuse_breakaway=True)
    mcp_bridge.spawn_detached(["py"], tmp_path, {}, io.BytesIO(), windows=True, popen=popen)
    first, second = (call["creationflags"] for call in popen.calls)
    assert first & mcp_bridge.CREATE_BREAKAWAY_FROM_JOB
    assert not second & mcp_bridge.CREATE_BREAKAWAY_FROM_JOB
    assert second & mcp_bridge.CREATE_NO_WINDOW


def test_posix_start_gets_its_own_session(tmp_path):
    popen = RecordingPopen()
    mcp_bridge.spawn_detached(["python3"], tmp_path, {}, io.BytesIO(), windows=False,
                              popen=popen)
    (call,) = popen.calls
    assert call["start_new_session"] is True and "creationflags" not in call


@pytest.mark.parametrize("platform, layout", [
    ("win32", (".venv", "Scripts", "python.exe")),
    ("darwin", (".venv", "bin", "python")),
    ("linux", (".venv", "bin", "python")),
])
def test_the_server_is_started_with_the_venv_beside_the_bridge(tmp_path, platform, layout):
    assert mcp_bridge.server_python(tmp_path, platform) == sys.executable   # no venv yet
    venv_python = tmp_path.joinpath(*layout)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    assert mcp_bridge.server_python(tmp_path, platform) == str(venv_python)


def test_the_bridge_starts_the_venv_python_not_its_own(tmp_path, monkeypatch):
    layout = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    venv_python = tmp_path.joinpath("local", ".venv", *layout)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    starts = []

    def record(cmd, cwd, env, log_file, **kwargs):
        starts.append((cmd, cwd))
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(mcp_bridge, "spawn_detached", record)
    bridge, _ = make_bridge(free_port(), tmp_path, autostart=True,
                            server_cwd=tmp_path / "local", start_timeout=5)
    assert bridge.python == str(venv_python)
    bridge.handle_line(request("ping"))
    (cmd, cwd), = starts
    assert cmd[0] == str(venv_python) and cwd == tmp_path / "local"


# -- --check --------------------------------------------------------------------------


def test_check_status(fake, tmp_path):
    status = mcp_bridge.check_status(fake.port, tmp_path, environ={})
    assert status["ok"] is True and status["port"] == fake.port
    assert status["url"] == "http://127.0.0.1:%d/mcp" % fake.port
    assert status["server"]["app"] == "litreview-local"
    down = mcp_bridge.check_status(free_port(), tmp_path, environ={})
    assert down["ok"] is False and "not answering" in down["error"]


def test_check_says_no_when_the_port_holds_another_app(tmp_path):
    other = FakeLitReview(app="something-else")
    try:
        assert mcp_bridge.check_status(other.port, tmp_path, environ={})["ok"] is False
    finally:
        other.close()


def test_check_prints_one_line_of_json(fake, tmp_path):
    env = dict(os.environ, LITREVIEW_PORT=str(fake.port),
               LITREVIEW_DATA_DIR=str(tmp_path))
    done = subprocess.run([sys.executable, str(BRIDGE), "--check"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.count(b"\n") == 1
    status = json.loads(done.stdout)
    assert status["ok"] and status["port_source"] == "LITREVIEW_PORT"
    env["LITREVIEW_PORT"] = str(free_port())
    done = subprocess.run([sys.executable, str(BRIDGE), "--check"], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert done.returncode == 1 and json.loads(done.stdout)["ok"] is False


# -- end to end: the bridge starts a (fake) LitReview itself ---------------------------

FAKE_MAIN = r'''
import argparse, json, os, signal, sys, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=int(os.environ.get("LITREVIEW_PORT") or 8765))
parser.add_argument("--data-dir", default=os.environ.get("LITREVIEW_DATA_DIR")
                    or str(Path.home() / "LitReview"))
args = parser.parse_args()
if os.environ.get("FAKE_LITREVIEW_MODE") == "crash":
    print("boom: the fake server could not start", flush=True)
    sys.exit(3)
data_dir = Path(args.data_dir).resolve()
data_dir.mkdir(parents=True, exist_ok=True)
(data_dir / "fake-start.json").write_text(json.dumps(
    {"argv": sys.argv[1:], "cwd": os.getcwd(), "port_env": os.environ.get("LITREVIEW_PORT")}))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("fake: " + (fmt % a) + "\n")

    def _send(self, status, payload=None, headers=()):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "app": "litreview-local", "version": "0.1.0",
                             "data_dir": str(data_dir)})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if not isinstance(msg, dict) or msg.get("id") is None:
            return self._send(202)
        if msg.get("method") == "initialize":
            return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "litreview", "version": "0.1.0"}}},
                [("Mcp-Session-Id", "fake-session")])
        self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "method": msg.get("method"), "session": self.headers.get("Mcp-Session-Id")}})


server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
server.daemon_threads = True
(data_dir / "server.json").write_text(json.dumps({
    "port": args.port, "pid": os.getpid(), "url": "http://127.0.0.1:%d" % args.port,
    "version": "0.1.0", "started_at": datetime.now(timezone.utc).isoformat()}))


def stop(signum=None, frame=None):
    threading.Thread(target=server.shutdown, daemon=True).start()


for name in ("SIGTERM", "SIGBREAK", "SIGINT"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), stop)
try:
    server.serve_forever(poll_interval=0.1)
finally:
    server.server_close()
    try:
        (data_dir / "server.json").unlink()
    except OSError:
        pass
    (data_dir / "clean-exit").write_text("ok")
'''


@pytest.fixture
def checkout(tmp_path):
    """A copy of the bridge beside a fake litreview_local, in folders with spaces."""
    local = tmp_path / "Lit Review" / "local"
    (local / "litreview_local").mkdir(parents=True)
    shutil.copy2(str(BRIDGE), str(local / "mcp_bridge.py"))
    (local / "litreview_local" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (local / "litreview_local" / "__main__.py").write_text(FAKE_MAIN)
    data = tmp_path / "my data"
    started = []
    yield local, data, started
    info_path = data / "server.json"
    if info_path.exists():                      # stop the server the bridge started
        pid = json.loads(info_path.read_text())["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            if not mcp_bridge.pid_alive(pid):
                break
            time.sleep(0.1)


def run_bridge(local, data, port, messages, mode=None, timeout=90):
    env = dict(os.environ, LITREVIEW_PORT=str(port), LITREVIEW_DATA_DIR=str(data),
               PYTHONIOENCODING="utf-8")
    env.pop("FAKE_LITREVIEW_MODE", None)
    if mode:
        env["FAKE_LITREVIEW_MODE"] = mode
    proc = subprocess.Popen([sys.executable, str(local / "mcp_bridge.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, cwd=str(local.parent.parent))
    answers = []
    for message in messages:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
        if "id" in message:
            line = proc.stdout.readline()
            assert line, proc.stderr.read().decode("utf-8", "replace")
            answers.append(json.loads(line))
    proc.stdin.close()
    rest = proc.stdout.read()
    stderr = proc.stderr.read().decode("utf-8", "replace")
    assert proc.wait(timeout=timeout) == 0, stderr
    return answers, rest, stderr


def test_the_bridge_starts_litreview_itself_and_it_outlives_the_bridge(checkout):
    local, data, _ = checkout
    port = free_port()
    answers, rest, stderr = run_bridge(local, data, port, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert answers[0]["result"]["serverInfo"]["name"] == "litreview"
    assert answers[1]["result"] == {"method": "tools/list", "session": "fake-session"}
    assert rest == b""                                  # stdout: JSON-RPC only
    assert "[litreview-bridge]" in stderr and "starting it" in stderr

    started = json.loads((data / "fake-start.json").read_text())
    assert started["argv"] == ["--port", str(port)]
    assert Path(started["cwd"]).resolve() == local.resolve()  # its own folder
    log = data / "logs" / "server-bridge.log"
    assert log.exists() and "started by mcp_bridge.py" in log.read_text(errors="replace")

    pid = json.loads((data / "server.json").read_text())["pid"]
    assert mcp_bridge.pid_alive(pid), "the server must survive the bridge exiting"
    assert mcp_bridge.is_litreview(mcp_bridge.get_health(port))


def test_the_bridge_uses_a_running_litreview_and_starts_nothing(checkout, fake):
    local, data, _ = checkout
    answers, _, stderr = run_bridge(local, data, fake.port,
                                    [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert answers == [{"jsonrpc": "2.0", "id": 1, "result": {"echo": "ping"}}]
    assert "starting it" not in stderr and not (data / "fake-start.json").exists()


def test_when_litreview_cannot_start_claude_is_told_why(checkout):
    local, data, _ = checkout
    answers, _, _ = run_bridge(local, data, free_port(),
                               [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}],
                               mode="crash")
    message = answers[0]["error"]["message"]
    assert "exit code 3" in message and "boom" in message
    assert "server-bridge.log" in message


# -- the file itself ---------------------------------------------------------------------


def _annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            args += [a for a in (node.args.vararg, node.args.kwarg) if a]
            for arg in args:
                if arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def assert_runs_on_python38(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 8))
    assert "from __future__ import annotations" not in source
    for annotation in _annotations(tree):
        for node in ast.walk(annotation):
            assert not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)), \
                "X | Y annotation at line %d" % node.lineno
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                assert node.value.id not in ("list", "dict", "tuple", "set", "type"), \
                    "%s[...] annotation at line %d" % (node.value.id, node.lineno)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib:
        assert imported <= set(stdlib), imported - set(stdlib)


def test_the_bridge_runs_on_python38_with_the_standard_library():
    assert_runs_on_python38(BRIDGE)


def test_two_bridges_start_the_app_once(tmp_path):
    """Claude Desktop runs two bridges at once: only one starts the app, the
    other waits for it."""
    import threading

    import mcp_bridge

    starts = []

    class Bridge(mcp_bridge.Bridge if hasattr(mcp_bridge, "Bridge") else object):
        pass

    lock_dir = tmp_path / "logs"
    lock_dir.mkdir()
    lock = lock_dir / "bridge-start.lock"
    # A fresh lock held by "the other bridge": this one must wait, not start.
    lock.write_text("", encoding="utf-8")
    calls = {"health": 0}

    class Stub:
        log_path = lock_dir / "server-bridge.log"
        start_timeout = 2.0

        def _healthy(self):
            calls["health"] += 1
            return calls["health"] >= 3   # the other bridge's app comes up

        def _start_server_now(self):
            starts.append(1)
            return None

        def not_running_message(self, why):
            return why

    stub = Stub()
    start = mcp_bridge.LitReviewBridge._start_server if hasattr(
        mcp_bridge, "LitReviewBridge") else None
    if start is None:
        # find the class that defines _start_server
        start = next(cls._start_server for cls in vars(mcp_bridge).values()
                     if isinstance(cls, type) and hasattr(cls, "_start_server"))
    wait = next(cls._wait_for_other_start for cls in vars(mcp_bridge).values()
                if isinstance(cls, type) and hasattr(cls, "_wait_for_other_start"))
    Stub._wait_for_other_start = wait
    assert start(stub) is None
    assert starts == []                       # it waited instead of starting
    lock.unlink()
    assert start(stub) is None
    assert starts == [1] and not lock.exists()  # it started, and let go
    del threading, Bridge

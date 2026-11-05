"""Tests for launcher.py: finding Python, the venv, and running the app.

Nothing here installs anything, opens a browser or touches ~/LitReview: uv's
installer, pip and the browser are replaced by fakes, and the app is a fake
litreview_local package written into a temp folder and run for real.
"""
import ast
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

import launcher  # noqa: E402
from launcher import Config, LauncherError, PythonInfo  # noqa: E402

LITREVIEW_HEALTH = {"ok": True, "app": "litreview-local", "version": "0.1.0",
                    "data_dir": "somewhere"}


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    """The browser, replaced: the URLs the launcher opened."""
    urls = []
    monkeypatch.setattr(launcher.webbrowser, "open",
                        lambda url, new=0: urls.append(url) or True)
    return urls


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def info(version, exe):
    return PythonInfo([exe], version, exe)


def never(*args, **kwargs):
    raise AssertionError("should not have been called")


class Serve:
    """A tiny HTTP server: GET answers `health`, POST answers `mcp`."""

    def __init__(self, health, status=200, mcp=("application/json", b"{}")):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(status, "application/json", json.dumps(health).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._send(200, *mcp)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# -- configuration -------------------------------------------------------------


def config(*argv, environ=None, home=None):
    return launcher.make_config(launcher.parse_args(list(argv)),
                                environ={} if environ is None else environ, home=home)


def test_defaults(tmp_path):
    cfg = config(home=tmp_path)
    assert (cfg.port, cfg.data_dir) == (8765, tmp_path / "LitReview")
    assert not (cfg.port_explicit or cfg.data_dir_explicit or cfg.no_browser or cfg.smoke)


def test_env_vars_and_flags_do_the_same_and_flags_win(tmp_path):
    env = {"LITREVIEW_PORT": "9001", "LITREVIEW_DATA_DIR": str(tmp_path / "From Env")}
    cfg = config(environ=env, home=tmp_path)
    assert (cfg.port, cfg.data_dir) == (9001, tmp_path / "From Env")
    assert cfg.port_explicit and cfg.data_dir_explicit
    cfg = config("--port", "9002", "--data-dir", str(tmp_path / "From Flag"), "--no-browser",
                 environ=env, home=tmp_path)
    assert (cfg.port, cfg.data_dir, cfg.no_browser) == (9002, tmp_path / "From Flag", True)


def test_data_dir_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config("--data-dir", "my data").data_dir == tmp_path / "my data"
    assert config("--data-dir", "~/x").data_dir == Path(os.path.expanduser("~/x"))


@pytest.mark.parametrize("value", ["eighty", "0", "70000"])
def test_a_bad_port_is_explained(value):
    with pytest.raises(LauncherError):
        config(environ={"LITREVIEW_PORT": value})


def test_main_prints_the_problem_and_exits_1(monkeypatch, capsys):
    monkeypatch.setenv("LITREVIEW_PORT", "eighty")
    assert launcher.main([]) == 1
    assert "LITREVIEW_PORT" in capsys.readouterr().out


# -- finding Python ------------------------------------------------------------------

STORE_STUB = (
    "import sys\n"
    "sys.stderr.write('Python was not found; run without arguments to install from the "
    "Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app "
    "settings > App execution aliases.\\n')\n"
    "sys.exit(9009)\n")


def script(tmp_path, name, code):
    path = tmp_path / name
    path.write_text(code)
    return [sys.executable, str(path)]


def test_probe_reads_a_real_python():
    found = launcher.probe_python([sys.executable])
    assert found.version == tuple(sys.version_info[:2])
    assert os.path.normcase(os.path.realpath(found.executable)) == \
        os.path.normcase(os.path.realpath(sys.executable))


def test_probe_treats_the_microsoft_store_stub_as_absent(tmp_path):
    assert launcher.probe_python(script(tmp_path, "python3.py", STORE_STUB)) is None


def test_probe_treats_missing_broken_and_hanging_commands_as_absent(tmp_path):
    assert launcher.probe_python([str(tmp_path / "missing" / "python.exe")]) is None
    assert launcher.probe_python(script(tmp_path, "noise.py", "print('hello')")) is None
    hang = script(tmp_path, "hang.py", "import time\ntime.sleep(60)")
    started = time.monotonic()
    assert launcher.probe_python(hang, timeout=1) is None
    assert time.monotonic() - started < 30


def test_probe_reports_an_old_python_for_the_caller_to_skip(tmp_path):
    old = script(tmp_path, "py27.py", "print((2, 7))\nprint('/usr/bin/python2.7')")
    assert launcher.probe_python(old) == PythonInfo(old, (2, 7), "/usr/bin/python2.7")


def which(mapping):
    return lambda name: mapping.get(name)


def test_windows_candidates(tmp_path):
    programs = tmp_path / "Local" / "Programs" / "Python"
    for folder in ("Python310", "Python311", "Python312"):
        (programs / folder).mkdir(parents=True)
    for folder in ("Python310", "Python312"):   # Python311 is a leftover with no python.exe
        (programs / folder / "python.exe").write_bytes(b"")
    found = which({"py": r"C:\Windows\py.exe", "python": r"C:\ProgramData\Anaconda3\python.exe",
                   "python3": r"C:\Users\A E\AppData\Local\Microsoft\WindowsApps\python3.exe"})
    listed = [r"C:\Users\A E\AppData\Local\Programs\Python\Python313\python.exe"]
    candidates = list(launcher.python_candidates(
        "win32", {"LOCALAPPDATA": str(tmp_path / "Local")}, which=found,
        list_py=lambda py: listed))
    assert candidates == [
        listed,
        [r"C:\ProgramData\Anaconda3\python.exe"],
        [r"C:\Users\A E\AppData\Local\Microsoft\WindowsApps\python3.exe"],
        [str(programs / "Python312" / "python.exe")],
        [str(programs / "Python310" / "python.exe")],
    ]


def test_windows_py_launcher_is_asked_per_version_when_it_lists_nothing():
    candidates = list(launcher.python_candidates("win32", {}, which=which({"py": "py.exe"}),
                                                 list_py=lambda py: []))
    assert candidates == [["py.exe", "-3.%d" % minor] for minor in (14, 13, 12, 11, 10)]


def test_py_launcher_listing_is_parsed(monkeypatch):
    output = (b" -V:3.13 *        C:\\Users\\A E\\AppData\\Local\\Programs\\Python\\Python313"
              b"\\python.exe\r\n -V:3.11          C:\\Python311\\python.exe\r\n"
              b" -3.9-64        C:\\Old\\python.exe\r\n")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, output, b"")
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.py_launcher_pythons("py.exe") == [
        r"C:\Users\A E\AppData\Local\Programs\Python\Python313\python.exe",
        r"C:\Python311\python.exe", r"C:\Old\python.exe"]
    assert calls[0][0] == ["py.exe", "-0p"]
    assert calls[0][1]["PYTHON_MANAGER_AUTOMATIC_INSTALL"] == "false"   # never install


def test_mac_candidates_avoid_the_xcode_stub_without_the_tools():
    found = which({"python3.12": "/opt/homebrew/bin/python3.12", "python3": "/usr/bin/python3"})
    files = {"/usr/local/bin/python3",
             "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"}
    without = list(launcher.python_candidates("darwin", {}, which=found,
                                              isfile=files.__contains__,
                                              xcode_ok=lambda: False))
    assert without == [["/opt/homebrew/bin/python3.12"], ["/usr/local/bin/python3"],
                       ["/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"]]
    with_tools = list(launcher.python_candidates("darwin", {}, which=found,
                                                 isfile=files.__contains__,
                                                 xcode_ok=lambda: True))
    assert with_tools[-1] == ["/usr/bin/python3"]


def test_mac_checks_for_the_tools_only_for_usr_bin_python3():
    checked = []
    list(launcher.python_candidates("darwin", {}, which=which({"python3": "/opt/homebrew/bin/python3"}),
                                    isfile=lambda path: False,
                                    xcode_ok=lambda: checked.append(1) or True))
    assert checked == []


def test_the_running_python_comes_first_when_it_is_new_enough():
    found = list(launcher.iter_suitable_pythons(
        candidates=[["a"], ["b"]], current=info((3, 12), "CURRENT"),
        probe=lambda cmd: {"a": info((3, 11), "A"), "b": None}[cmd[0]]))
    assert [p.executable for p in found] == ["CURRENT", "A"]


def test_old_missing_and_duplicate_pythons_are_skipped_and_probing_is_lazy():
    probed = []
    answers = {"stub": None, "old": info((3, 9), "OLD"), "new": info((3, 13), "NEW"),
               "same": info((3, 13), "NEW"), "later": info((3, 10), "LATER")}

    def probe(cmd):
        probed.append(cmd[0])
        return answers[cmd[0]]
    found = launcher.iter_suitable_pythons(
        candidates=[["stub"], ["old"], ["new"], ["same"], ["later"]],
        current=info((3, 9), "ANACONDA 3.9"), probe=probe)
    assert next(found).executable == "NEW"
    assert probed == ["stub", "old", "new"]
    assert [p.executable for p in found] == ["LATER"]


# -- uv --------------------------------------------------------------------------------------


def test_find_uv_on_path_then_where_its_installer_puts_it(tmp_path):
    assert launcher.find_uv("win32", tmp_path, which=which({"uv": r"C:\tools\uv.exe"})) == \
        r"C:\tools\uv.exe"
    assert launcher.find_uv("win32", tmp_path, which=which({})) is None
    cargo = tmp_path / ".cargo" / "bin" / "uv.exe"
    cargo.parent.mkdir(parents=True)
    cargo.write_bytes(b"")
    assert launcher.find_uv("win32", tmp_path, which=which({})) == str(cargo)
    local = tmp_path / ".local" / "bin" / "uv.exe"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"")
    assert launcher.find_uv("win32", tmp_path, which=which({})) == str(local)
    assert launcher.find_uv("darwin", tmp_path, which=which({})) is None   # looks for "uv"


@pytest.mark.parametrize("answer", ["n", "", "no thanks", EOFError])
def test_nothing_is_installed_without_a_yes(answer, capsys):
    def ask(prompt):
        if answer is EOFError:
            raise EOFError
        return answer
    with pytest.raises(LauncherError) as error:
        launcher.offer_uv_install("win32", ask=ask, run=never, locate=never)
    assert "Nothing was installed" in str(error.value)
    assert "python.org" in str(error.value) and "start-windows.bat" in str(error.value)
    assert "uv" in capsys.readouterr().out


@pytest.mark.parametrize("platform, installer", [
    ("win32", ["powershell", "-NoProfile", "-ExecutionPolicy", "ByPass", "-c",
               "irm https://astral.sh/uv/install.ps1 | iex"]),
    ("darwin", ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]),
])
def test_a_yes_runs_the_official_uv_installer(platform, installer):
    ran = []
    uv = launcher.offer_uv_install(platform, ask=lambda prompt: " Y ",
                                   run=lambda cmd: ran.append(cmd) or 0,
                                   locate=lambda: "/Users/b/.local/bin/uv")
    assert ran == [installer] and uv == "/Users/b/.local/bin/uv"


def test_a_failed_uv_install_is_explained():
    with pytest.raises(LauncherError, match="did not work"):
        launcher.offer_uv_install("darwin", ask=lambda p: "y", run=lambda cmd: 1, locate=never)
    with pytest.raises(LauncherError, match="can't find it"):
        launcher.offer_uv_install("win32", ask=lambda p: "yes", run=lambda cmd: 0,
                                  locate=lambda: None)


# -- the venv -----------------------------------------------------------------------------------


@pytest.fixture
def venv(tmp_path):
    return tmp_path / "Lit Review" / "local" / ".venv"


def test_a_working_venv_is_used_without_looking_for_python(venv, monkeypatch):
    venv.mkdir(parents=True)
    launcher.write_stamp(venv, {"created_by": "uv"})
    monkeypatch.setattr(launcher, "venv_ok", lambda python: True)

    def no_search():
        raise AssertionError("looked for Python")
        yield
    assert launcher.ensure_venv(venv, pythons=no_search(), locate_uv=never, offer=never) == \
        (launcher.venv_python(venv), "uv")


def test_a_new_venv_comes_from_the_first_python_that_works(venv, monkeypatch):
    monkeypatch.setattr(launcher, "venv_ok", lambda python: False)
    tried = []

    def create(found, folder):
        tried.append(found.executable)
        folder.mkdir(parents=True, exist_ok=True)
        return found.executable == "GOOD"
    monkeypatch.setattr(launcher, "create_venv", create)
    pythons = [info((3, 13), "BROKEN"), info((3, 12), "GOOD"), info((3, 11), "UNUSED")]
    assert launcher.ensure_venv(venv, pythons=pythons, locate_uv=never, offer=never)[1] == "venv"
    assert tried == ["BROKEN", "GOOD"]
    assert launcher.read_stamp(venv) == {"created_by": "venv", "python": "GOOD"}


def test_without_a_python_uv_makes_the_venv(venv, monkeypatch):
    monkeypatch.setattr(launcher, "venv_ok", lambda python: False)
    made = []
    monkeypatch.setattr(launcher, "create_venv_with_uv",
                        lambda uv, folder: made.append(uv) or folder.mkdir(parents=True))
    assert launcher.ensure_venv(venv, pythons=[], locate_uv=lambda: "uv", offer=never) == \
        (launcher.venv_python(venv), "uv")
    assert made == ["uv"] and launcher.read_stamp(venv) == {"created_by": "uv"}


def test_without_a_python_or_uv_the_user_is_asked(venv, monkeypatch):
    monkeypatch.setattr(launcher, "venv_ok", lambda python: False)
    asked = []

    def offer():
        asked.append(True)
        raise LauncherError("declined")
    with pytest.raises(LauncherError, match="declined"):
        launcher.ensure_venv(venv, pythons=[], locate_uv=lambda: None, offer=offer)
    assert asked == [True]


def test_a_broken_venv_is_replaced(venv, monkeypatch):
    (venv / "Lib" / "site-packages").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = C:\\Python310-uninstalled\n")
    monkeypatch.setattr(launcher, "venv_ok", lambda python: False)
    monkeypatch.setattr(launcher, "create_venv", lambda found, folder: folder.mkdir() or True)
    launcher.ensure_venv(venv, pythons=[info((3, 12), "P")], locate_uv=never, offer=never)
    assert venv.exists() and not (venv / "Lib").exists()


def test_a_folder_that_is_not_a_venv_is_never_deleted(venv, monkeypatch):
    venv.mkdir(parents=True)
    (venv / "precious.txt").write_text("mine")
    monkeypatch.setattr(launcher, "venv_ok", lambda python: False)
    with pytest.raises(LauncherError, match="in the way"):
        launcher.ensure_venv(venv, pythons=[info((3, 12), "P")], locate_uv=never, offer=never)
    assert (venv / "precious.txt").read_text() == "mine"


@pytest.mark.skipif(sys.version_info < (3, 10), reason="the app's venv needs Python 3.10+")
def test_a_real_venv_is_made_and_checked(venv):
    current = PythonInfo([sys.executable], tuple(sys.version_info[:2]), sys.executable)
    assert launcher.create_venv(current, venv)
    assert launcher.venv_ok(launcher.venv_python(venv))


def test_venv_ok_rejects_a_missing_python(venv):
    assert not launcher.venv_ok(launcher.venv_python(venv))


def test_the_stamp_survives_garbage(tmp_path):
    (tmp_path / launcher.STAMP_NAME).write_text("{not json")
    assert launcher.read_stamp(tmp_path) == {}
    (tmp_path / launcher.STAMP_NAME).write_text("[1, 2]")
    assert launcher.read_stamp(tmp_path) == {}


# -- requirements ----------------------------------------------------------------------------------


@pytest.fixture
def reqs(tmp_path, monkeypatch):
    folder = tmp_path / "venv"
    folder.mkdir()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=0.111\n")
    runs = []

    def runner(cmd, env=None, cwd=None):
        runs.append(cmd)
        return 0, []
    monkeypatch.setattr(launcher, "ensure_pip", lambda python: None)
    return folder, requirements, runs, runner


def test_requirements_are_installed_only_when_the_file_changes(reqs):
    folder, requirements, runs, runner = reqs
    python = folder / "python"

    def install():
        return launcher.ensure_requirements(python, "venv", folder, requirements,
                                            locate_uv=lambda: None, runner=runner)
    assert install() is True
    assert runs == [[str(python), "-m", "pip", "install", "-r", str(requirements)]]
    assert install() is False
    requirements.write_bytes(b"fastapi>=0.111\r\n")           # a CRLF checkout of it
    assert install() is False
    requirements.write_text("fastapi>=0.111\nhttpx>=0.27\n")
    assert install() is True and len(runs) == 2


def test_a_uv_venv_installs_with_uv_pip(reqs):
    folder, requirements, runs, runner = reqs
    python = folder / "python"
    launcher.ensure_requirements(python, "uv", folder, requirements,
                                 locate_uv=lambda: "uv.exe", runner=runner)
    assert runs == [["uv.exe", "pip", "install", "--python", str(python), "-r",
                     str(requirements)]]


def test_a_uv_venv_without_uv_falls_back_to_its_pip(reqs):
    folder, requirements, runs, runner = reqs
    launcher.ensure_requirements(folder / "python", "uv", folder, requirements,
                                 locate_uv=lambda: None, runner=runner)
    assert runs[0][1:4] == ["-m", "pip", "install"]


def test_a_failed_install_is_explained_and_tried_again_next_time(reqs):
    folder, requirements, runs, _ = reqs
    offline = ["ERROR: Could not find a version that satisfies the requirement fastapi",
               "ERROR: No matching distribution found for fastapi"]
    with pytest.raises(LauncherError) as error:
        launcher.ensure_requirements(folder / "python", "venv", folder, requirements,
                                     locate_uv=lambda: None,
                                     runner=lambda cmd, env=None, cwd=None: (1, offline))
    assert "internet" in str(error.value) and "exit code 1" in str(error.value)
    assert "requirements_sha256" not in launcher.read_stamp(folder)


@pytest.mark.parametrize("output, advice", [
    ("ProxyError('Cannot connect to proxy.', OSError('Tunnel connection failed: "
     "407 Proxy Authentication Required'))", "HTTPS_PROXY"),
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate "
     "in certificate chain", "intercepts"),
    ("Failed to establish a new connection: [Errno 11001] getaddrinfo failed", "internet"),
    ("error: Failed to fetch: `https://pypi.org/simple/fastapi/`\n  Caused by: dns error: "
     "failed to lookup address information", "internet"),
    ("OSError: [Errno 28] No space left on device", "disk is full"),
    ("something nobody expected", "messages above"),
])
def test_install_failures_are_explained(output, advice):
    assert advice in launcher.diagnose_install_failure(output.splitlines(), 1)


def test_setup_output_is_shown_and_kept(capsys):
    code, tail = launcher.run_streaming([sys.executable, "-c",
                                         "print('Collecting fastapi'); raise SystemExit(2)"])
    assert code == 2 and tail == ["Collecting fastapi"]
    assert "    Collecting fastapi" in capsys.readouterr().out
    assert launcher.run_streaming(["no-such-program-anywhere"])[0] == 127


# -- the port and a running app ---------------------------------------------------------------------


def test_port_states(tmp_path):
    assert launcher.port_state(free_port()) == ("free", None)
    with Serve(dict(LITREVIEW_HEALTH, data_dir=str(tmp_path))) as ours, \
            Serve({"ok": True, "app": "jupyter"}) as other, \
            Serve({"detail": "Not Found"}, status=404) as missing:
        state, health = launcher.port_state(ours.port)
        assert state == "litreview" and health["data_dir"] == str(tmp_path)
        assert launcher.port_state(other.port) == ("other", None)
        assert launcher.port_state(missing.port) == ("other", None)


def test_a_port_that_cannot_be_used_but_nobody_answers_is_blocked(monkeypatch):
    monkeypatch.setattr(launcher, "can_bind", lambda port: False)
    assert launcher.port_state(free_port()) == ("blocked", None)


def test_pid_alive():
    assert launcher.pid_alive(os.getpid())
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not launcher.pid_alive(done.pid)
    assert not launcher.pid_alive(0) and not launcher.pid_alive(-1)


def test_find_running_server_trusts_only_a_live_answering_server(tmp_path):
    with Serve(LITREVIEW_HEALTH) as server:
        assert launcher.find_running_server(tmp_path) is None
        (tmp_path / "server.json").write_text(json.dumps({"port": server.port,
                                                          "pid": os.getpid()}))
        assert launcher.find_running_server(tmp_path) == server.port
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        (tmp_path / "server.json").write_text(json.dumps({"port": server.port,
                                                          "pid": done.pid}))
        assert launcher.find_running_server(tmp_path) is None      # its process is gone
        (tmp_path / "server.json").write_text(json.dumps({"port": free_port(),
                                                          "pid": os.getpid()}))
        assert launcher.find_running_server(tmp_path) is None      # nothing answers


def explicit(tmp_path, port=None, data_dir=None, **changes):
    cfg = Config(port=port or free_port(), data_dir=data_dir or tmp_path / "my data",
                 data_dir_explicit=True, no_browser=False, smoke=False, port_explicit=True)
    return cfg._replace(**changes)


def test_an_app_already_running_is_just_opened(tmp_path, opened):
    data = tmp_path / "data"
    data.mkdir()
    with Serve(LITREVIEW_HEALTH) as server:
        (data / "server.json").write_text(json.dumps({"port": server.port, "pid": os.getpid()}))
        assert launcher.run_app(explicit(tmp_path, data_dir=data), prepare=never) == 0
    assert opened == ["http://127.0.0.1:%d" % server.port]


def test_another_program_on_the_port_stops_the_launcher_with_advice(tmp_path):
    with Serve({"detail": "Not Found"}, status=404) as other:
        with pytest.raises(LauncherError) as error:
            launcher.run_app(explicit(tmp_path, port=other.port), prepare=never)
    message = str(error.value)
    assert str(other.port) in message and "LITREVIEW_PORT" in message and "--port" in message


def test_litreview_on_the_port_with_other_data(tmp_path, opened):
    with Serve(dict(LITREVIEW_HEALTH, data_dir=str(tmp_path / "theirs"))) as theirs:
        mine = explicit(tmp_path, port=theirs.port, data_dir=tmp_path / "mine")
        with pytest.raises(LauncherError, match="data folder"):
            launcher.run_app(mine, prepare=never)
        assert opened == []
        assert launcher.run_app(mine._replace(data_dir_explicit=False), prepare=never) == 0
    assert opened == ["http://127.0.0.1:%d" % theirs.port]


def test_port_advice_names_both_ways_to_change_it():
    windows = launcher.port_problem(8765, "other", "win32")
    assert "setx LITREVIEW_PORT 8766" in windows and "--port 8766" in windows
    mac = launcher.port_problem(8765, "blocked", "darwin")
    assert "reserves" in mac and "LITREVIEW_PORT=8766" in mac and ".zshrc" in mac


def test_the_banner_says_where_how_to_stop_and_what_next():
    text = launcher.banner("http://127.0.0.1:8765", r"C:\Users\Ann Example\LitReview", True)
    for words in ("Welcome", "http://127.0.0.1:8765", r"C:\Users\Ann Example\LitReview",
                  "Ctrl+C", "close the window", "\"Getting started\" in the app"):
        assert words in text
    assert "Welcome" not in launcher.banner("u", "d", False)
    text.encode("ascii")          # any console can print it


# -- running a (fake) app for real ------------------------------------------------------------------

FAKE_MAIN = r'''
import argparse, json, os, signal, sys, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=int(os.environ.get("LITREVIEW_PORT") or 8765))
parser.add_argument("--data-dir", default=os.environ.get("LITREVIEW_DATA_DIR")
                    or str(Path.home() / "LitReview"))
args = parser.parse_args()
mode = os.environ.get("FAKE_LITREVIEW_MODE", "")
if mode == "crash":
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
        if mode == "no-mcp":
            return self._send(404, {"detail": "Not Found"})
        if not isinstance(msg, dict) or msg.get("id") is None:
            return self._send(202)
        self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "litreview", "version": "0.1.0"}}},
            [("Mcp-Session-Id", "fake-session")])


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
if mode.startswith("exit-after:"):
    threading.Timer(float(mode.split(":")[1]), stop).start()
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
def app_dir(tmp_path, monkeypatch):
    """local/ with a fake litreview_local, in folders with spaces."""
    local = tmp_path / "Lit Review" / "local"
    (local / "litreview_local").mkdir(parents=True)
    (local / "litreview_local" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (local / "litreview_local" / "__main__.py").write_text(FAKE_MAIN)
    monkeypatch.delenv("FAKE_LITREVIEW_MODE", raising=False)
    return local


def this_python():
    return Path(sys.executable)


def test_the_app_is_started_waited_for_opened_and_seen_out(app_dir, tmp_path, monkeypatch,
                                                           opened, capsys):
    monkeypatch.setenv("FAKE_LITREVIEW_MODE", "exit-after:2")   # e.g. the Quit button
    cfg = explicit(tmp_path)
    assert launcher.run_app(cfg, local_dir=app_dir, prepare=this_python) == 0
    url = "http://127.0.0.1:%d" % cfg.port
    assert opened == [url + "/start"]            # a new library opens on Getting started
    out = capsys.readouterr().out
    assert url in out and str(cfg.data_dir.resolve()) in out and "Welcome" in out
    assert "LitReview has stopped." in out
    started = json.loads((cfg.data_dir / "fake-start.json").read_text())
    assert started["argv"] == ["--port", str(cfg.port), "--data-dir", str(cfg.data_dir)]
    assert started["port_env"] == str(cfg.port)
    assert Path(started["cwd"]).resolve() == app_dir.resolve()

    # The next time, the library is there: Projects opens.
    (cfg.data_dir / "litreview.db").write_bytes(b"")
    monkeypatch.setenv("FAKE_LITREVIEW_MODE", "exit-after:1")
    again = cfg._replace(port=free_port())
    assert launcher.run_app(again, local_dir=app_dir, prepare=this_python) == 0
    assert opened[-1] == "http://127.0.0.1:%d" % again.port


def test_without_a_chosen_data_folder_the_app_picks_its_own(app_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LITREVIEW_MODE", "exit-after:1")
    monkeypatch.setenv("LITREVIEW_DATA_DIR", str(tmp_path / "the app's default"))
    cfg = explicit(tmp_path, no_browser=True, data_dir_explicit=False)
    assert launcher.run_app(cfg, local_dir=app_dir, prepare=this_python) == 0
    started = json.loads((tmp_path / "the app's default" / "fake-start.json").read_text())
    assert started["argv"] == ["--port", str(cfg.port)]


def test_an_app_that_dies_at_start_is_reported(app_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LITREVIEW_MODE", "crash")
    with pytest.raises(LauncherError, match="exit code 3"):
        launcher.run_app(explicit(tmp_path), local_dir=app_dir, prepare=this_python)


def test_an_app_that_stops_by_itself_is_reported(tmp_path, capsys):
    crashed = subprocess.Popen([sys.executable, "-c", "raise SystemExit(5)"])
    assert launcher.run_until_stopped(crashed, tmp_path) == 1
    assert "exit code 5" in capsys.readouterr().out
    quit_ = subprocess.Popen([sys.executable, "-c", "pass"])
    assert launcher.run_until_stopped(quit_, tmp_path) == 0


def test_a_server_that_will_not_stop_is_killed():
    stubborn = subprocess.Popen([sys.executable, "-c",
                                 "import signal, time\n"
                                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                 "time.sleep(60)"])
    started = time.monotonic()
    launcher.stop_server(stubborn, "terminate", grace=1)
    assert stubborn.poll() is not None and time.monotonic() - started < 20


def test_smoke_passes_against_a_working_app(app_dir, tmp_path, monkeypatch, capsys):
    scratch = tmp_path / "smoke data"
    monkeypatch.setattr(launcher.tempfile, "mkdtemp",
                        lambda prefix="": scratch.mkdir() or str(scratch))
    cfg = config("--smoke", home=tmp_path)
    assert launcher.run_smoke(cfg, local_dir=app_dir, prepare=this_python,
                              start_timeout=60) == 0
    out = capsys.readouterr().out
    assert "SMOKE ok: MCP initialize -> litreview 0.1.0" in out and "SMOKE PASS" in out
    assert "port 8765 " not in out                      # a free port, not the default
    assert not scratch.exists()                          # the throwaway data is gone


@pytest.mark.parametrize("mode, words", [("crash", "/health never answered"),
                                         ("no-mcp", "MCP initialize")])
def test_smoke_fails_when_something_is_wrong(app_dir, tmp_path, monkeypatch, capsys,
                                             mode, words):
    monkeypatch.setenv("FAKE_LITREVIEW_MODE", mode)
    assert launcher.run_smoke(explicit(tmp_path), local_dir=app_dir, prepare=this_python,
                              start_timeout=30) == 1
    out = capsys.readouterr().out
    assert "SMOKE FAIL" in out and words in out


def test_mcp_initialize_reads_plain_json_and_event_streams():
    answer = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-06-18", "serverInfo": {"name": "litreview", "version": "1"}}})
    with Serve(LITREVIEW_HEALTH, mcp=("application/json", answer.encode())) as plain, \
            Serve(LITREVIEW_HEALTH, mcp=("text/event-stream",
                                         ("event: message\ndata: %s\n\n" % answer).encode())) as sse, \
            Serve(LITREVIEW_HEALTH, mcp=("application/json", b'{"nope": 1}')) as junk:
        assert launcher.mcp_initialize(plain.port)[0]
        assert launcher.mcp_initialize(sse.port)[0]
        assert not launcher.mcp_initialize(junk.port)[0]
    assert not launcher.mcp_initialize(free_port())[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals; Windows shares Ctrl+C via the console")
@pytest.mark.parametrize("sig", ["SIGHUP", "SIGINT", "SIGTERM"])
def test_closing_the_window_or_ctrl_c_stops_the_app_cleanly(app_dir, tmp_path, sig):
    port, data = free_port(), tmp_path / "data"
    code = textwrap.dedent("""
        import sys
        from pathlib import Path
        sys.path.insert(0, %r)
        import launcher
        cfg = launcher.Config(port=%d, data_dir=Path(%r), data_dir_explicit=True,
                              no_browser=True, smoke=False, port_explicit=True)
        sys.exit(launcher.run_app(cfg, local_dir=Path(%r),
                                  prepare=lambda: Path(sys.executable)))
    """) % (str(LOCAL), port, str(data), str(app_dir))
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while not launcher.is_litreview(launcher.get_health(port)[1]):
        assert proc.poll() is None and time.monotonic() < deadline, proc.stdout.read()
        time.sleep(0.2)
    proc.send_signal(getattr(signal, sig))      # to the launcher only, like a closed window
    out = proc.communicate(timeout=60)[0].decode("utf-8", "replace")
    assert proc.returncode == 0, out
    assert (data / "clean-exit").exists(), out   # the server shut down, not killed
    assert not (data / "server.json").exists()


# -- the file itself --------------------------------------------------------------------------------------


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


def test_the_launcher_runs_on_python38_with_the_standard_library():
    source = Path(launcher.__file__).read_text(encoding="utf-8")
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

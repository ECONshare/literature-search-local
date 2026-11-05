"""Tests for litreview_local.claude_setup: Claude Desktop and Claude Code setup.

Everything runs in temp folders; the real Claude Desktop config is never read
or written.
"""
import ast
import json
import os
import sys
from pathlib import Path

import pytest

LOCAL = Path(__file__).resolve().parents[1]
if str(LOCAL) not in sys.path:
    sys.path.insert(0, str(LOCAL))

from litreview_local import claude_setup as cs  # noqa: E402

OTHER_SERVER = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                                           "C:\\Users\\Ann Example\\Documents"]}


@pytest.fixture
def local_dir(tmp_path):
    """A checkout under a home folder with spaces, like C:\\Users\\Ann Example."""
    folder = tmp_path / "Ann Example" / "literature search" / "local"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def entry(local_dir):
    return cs.desktop_entry(local_dir / ".venv" / "Scripts" / "python.exe",
                            local_dir / "mcp_bridge.py")


@pytest.fixture
def config(tmp_path):
    return tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def backups(path):
    return sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))


def leftovers(path):
    return [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")]


# -- where the config lives -------------------------------------------------


def test_windows_candidates_include_the_store_build_first(tmp_path):
    local = tmp_path / "Local"
    (local / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    (local / "Packages" / "Microsoft.Something_8wekyb3d8bbwe").mkdir()
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(local)}
    assert cs.desktop_config_candidates(platform="win32", environ=env, home=tmp_path) == [
        local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
        / "claude_desktop_config.json",
        tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json",
    ]


def test_windows_candidates_without_the_store_build(tmp_path):
    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert cs.desktop_config_candidates(platform="win32", environ=env, home=tmp_path) == [
        tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"]


def test_mac_and_linux_candidates(tmp_path):
    assert cs.desktop_config_candidates(platform="darwin", environ={}, home=tmp_path) == [
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    assert cs.desktop_config_candidates(platform="linux", environ={}, home=tmp_path) == [
        tmp_path / ".config" / "Claude" / "claude_desktop_config.json"]
    xdg = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert cs.desktop_config_candidates(platform="linux", environ=xdg, home=tmp_path)[0] == \
        tmp_path / "xdg" / "Claude" / "claude_desktop_config.json"


def _windows(tmp_path):
    return {"platform": "win32", "home": tmp_path,
            "environ": {"APPDATA": str(tmp_path / "Roaming"),
                        "LOCALAPPDATA": str(tmp_path / "Local")}}


def test_find_prefers_an_existing_file_over_a_folder(tmp_path):
    store = tmp_path / "Local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)                       # the Store build ran, no config yet
    ordinary = tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"
    write_json(ordinary, {})
    assert cs.find_desktop_config(**_windows(tmp_path)) == ordinary
    write_json(store / "claude_desktop_config.json", {})
    assert cs.find_desktop_config(**_windows(tmp_path)) == store / "claude_desktop_config.json"


def test_find_falls_back_to_an_existing_folder_then_none(tmp_path):
    assert cs.find_desktop_config(**_windows(tmp_path)) is None
    (tmp_path / "Roaming" / "Claude").mkdir(parents=True)
    assert cs.find_desktop_config(**_windows(tmp_path)) == \
        tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"


# -- the entry -------------------------------------------------------------------


def test_entry_has_absolute_paths_and_round_trips_through_json(local_dir, entry):
    assert entry == {"command": str(local_dir / ".venv" / "Scripts" / "python.exe"),
                     "args": [str(local_dir / "mcp_bridge.py")]}
    assert os.path.isabs(entry["command"]) and " " in entry["command"]
    assert json.loads(cs.manual_snippet(entry)) == {"mcpServers": {cs.DESKTOP_NAME: entry}}


def test_entry_makes_relative_paths_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entry = cs.desktop_entry(Path(".venv") / "bin" / "python", "mcp_bridge.py")
    assert entry["command"] == str(tmp_path / ".venv" / "bin" / "python")
    assert entry["args"] == [str(tmp_path / "mcp_bridge.py")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths")
def test_windows_backslashes_are_escaped_in_the_file(config, entry):
    cs.install_desktop_entry(config, entry)
    text = config.read_text(encoding="utf-8")
    assert entry["command"].replace("\\", "\\\\") in text
    assert json.loads(text)["mcpServers"][cs.DESKTOP_NAME] == entry


def test_entry_does_not_follow_the_venv_symlink(tmp_path):
    base = tmp_path / "base python"
    base.write_text("")
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(base)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights here")
    assert cs.desktop_entry(link, tmp_path / "mcp_bridge.py")["command"] == str(link)


def test_entry_records_non_default_settings_in_env(tmp_path, local_dir):
    python, bridge = local_dir / "python", local_dir / "mcp_bridge.py"
    home_data = tmp_path / "LitReview"
    plain = cs.desktop_entry(python, bridge, port=8765, data_dir=home_data,
                             default_data_dir=home_data)
    assert "env" not in plain
    custom = cs.desktop_entry(python, bridge, port=8766, data_dir=tmp_path / "My Data",
                              default_data_dir=home_data)
    assert custom["env"] == {"LITREVIEW_PORT": "8766",
                             "LITREVIEW_DATA_DIR": str(tmp_path / "My Data")}


# -- install -------------------------------------------------------------------------


def test_install_creates_the_file_and_its_folder(config, entry):
    result = cs.install_desktop_entry(config, entry)
    assert result["action"] == "added" and result["changed"] and result["created"]
    assert result["backup"] is None and result["previous"] is None
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": {cs.DESKTOP_NAME: entry}}
    assert leftovers(config) == []


def test_install_merges_and_keeps_every_other_key_and_server(config, entry):
    original = {"globalShortcut": "Ctrl+Space",
                "mcpServers": {"filesystem": OTHER_SERVER},
                "preferences": {"menuBarEnabled": False, "nested": [1, 2.5, None, "x"]}}
    write_json(config, original)
    before = config.read_bytes()

    result = cs.install_desktop_entry(config, entry)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["mcpServers"] == {"filesystem": OTHER_SERVER, cs.DESKTOP_NAME: entry}
    assert {k: v for k, v in data.items() if k != "mcpServers"} == \
        {k: v for k, v in original.items() if k != "mcpServers"}
    assert list(data) == list(original)            # key order kept
    assert result["action"] == "added" and not result["created"]
    assert Path(result["backup"]).read_bytes() == before
    assert backups(config) == [Path(result["backup"]).name]
    assert leftovers(config) == []


def test_install_is_idempotent(config, entry):
    write_json(config, {"mcpServers": {"filesystem": OTHER_SERVER}})
    cs.install_desktop_entry(config, entry)
    after_first = config.read_bytes()
    first_backups = backups(config)

    result = cs.install_desktop_entry(config, entry)

    assert result["action"] == "unchanged" and not result["changed"]
    assert result["backup"] is None
    assert config.read_bytes() == after_first
    assert backups(config) == first_backups


def test_install_updates_an_old_entry_and_reports_it(config, entry):
    old = {"command": "C:\\old\\python.exe", "args": ["C:\\old\\mcp_bridge.py"]}
    write_json(config, {"mcpServers": {cs.DESKTOP_NAME: old, "filesystem": OTHER_SERVER}})
    result = cs.install_desktop_entry(config, entry)
    assert result["action"] == "updated" and result["previous"] == old
    servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert list(servers) == [cs.DESKTOP_NAME, "filesystem"]
    assert servers[cs.DESKTOP_NAME] == entry


def test_claude_desktop_calls_it_litreview_local():
    """Apart from a hosted connector on claude.ai called "Literature review";
    Claude Code, which allows no spaces in a name, keeps "litreview"."""
    assert cs.DESKTOP_NAME == "LitReview Local" and cs.SERVER_NAME == "litreview"


def test_install_renames_the_entry_an_earlier_version_added(config, entry):
    old = {"command": "C:\\old\\python.exe", "args": ["C:\\old\\mcp_bridge.py"]}
    write_json(config, {"mcpServers": {"litreview": old, "filesystem": OTHER_SERVER}})
    assert cs.read_desktop_entry(config) is None
    assert cs.old_desktop_entries(config) == {"litreview": old}
    preview = cs.preview_desktop_change(config, entry)
    result = cs.install_desktop_entry(config, entry)
    assert result["action"] == "renamed" and result["renamed_from"] == ["litreview"]
    assert result["previous"] == old and result["backup"]
    assert config.read_text(encoding="utf-8") == preview
    servers = json.loads(preview)["mcpServers"]
    # In the old one's place, so Claude Desktop does not start LitReview twice.
    assert list(servers) == ["LitReview Local", "filesystem"]
    assert servers["LitReview Local"] == entry
    assert cs.old_desktop_entries(config) == {}
    assert cs.install_desktop_entry(config, entry)["action"] == "unchanged"
    # Both names at once (one added by hand): the old one still goes.
    write_json(config, {"mcpServers": {"litreview": old, "LitReview Local": entry}})
    assert cs.install_desktop_entry(config, entry)["action"] == "renamed"
    assert list(json.loads(config.read_text(encoding="utf-8"))["mcpServers"]) == \
        ["LitReview Local"]


def test_a_server_of_its_own_called_litreview_is_left_alone(config, entry):
    mine = {"command": "node", "args": ["my-own-litreview.js"]}
    write_json(config, {"mcpServers": {"litreview": mine}})
    assert cs.old_desktop_entries(config) == {}
    assert cs.install_desktop_entry(config, entry)["action"] == "added"
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"] == {
        "litreview": mine, "LitReview Local": entry}
    cs.remove_desktop_entry(config)
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"] == {"litreview": mine}


@pytest.mark.parametrize("text", [
    '{"mcpServers": {"filesystem": {"command": "npx",}}}',   # trailing comma
    '{"mcpServers": ',
    "not json at all",
])
def test_install_refuses_invalid_json_and_writes_nothing(config, entry, text):
    config.parent.mkdir(parents=True)
    config.write_text(text, encoding="utf-8")
    with pytest.raises(cs.DesktopConfigError, match="not valid JSON"):
        cs.install_desktop_entry(config, entry)
    assert config.read_text(encoding="utf-8") == text
    assert backups(config) == [] and leftovers(config) == []


@pytest.mark.parametrize("data", [["a", "list"], "a string", {"mcpServers": ["x"]}])
def test_install_refuses_json_of_the_wrong_shape(config, entry, data):
    write_json(config, data)
    before = config.read_bytes()
    with pytest.raises(cs.DesktopConfigError):
        cs.install_desktop_entry(config, entry)
    assert config.read_bytes() == before and backups(config) == []


def test_install_treats_an_empty_file_as_an_empty_config(config, entry):
    config.parent.mkdir(parents=True)
    config.write_text("  \n", encoding="utf-8")
    result = cs.install_desktop_entry(config, entry)
    assert result["action"] == "added" and result["backup"]
    assert json.loads(config.read_text(encoding="utf-8")) == {"mcpServers": {cs.DESKTOP_NAME: entry}}


def test_install_reads_a_bom_and_writes_plain_utf8(config, local_dir):
    entry = cs.desktop_entry(local_dir / "S\u00f8ren \u00c5se" / "python.exe", local_dir / "b.py")
    config.parent.mkdir(parents=True)
    config.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"note": "caf\u00e9", "mcpServers": {}}, ensure_ascii=False).encode("utf-8"))
    cs.install_desktop_entry(config, entry)
    raw = config.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "S\u00f8ren \u00c5se" in text and "caf\u00e9" in text
    assert json.loads(text)["mcpServers"][cs.DESKTOP_NAME] == entry


def test_install_leaves_the_file_whole_when_the_swap_fails(config, entry, monkeypatch):
    write_json(config, {"mcpServers": {"filesystem": OTHER_SERVER}})
    before = config.read_bytes()

    def refuse(src, dst):
        raise OSError("disk unplugged")
    monkeypatch.setattr(cs.os, "replace", refuse)
    with pytest.raises(OSError, match="disk unplugged"):
        cs.install_desktop_entry(config, entry)
    assert config.read_bytes() == before
    assert leftovers(config) == []


def test_install_retries_while_another_program_has_the_file_open(config, entry, monkeypatch):
    real_replace = os.replace
    calls = []

    def busy_twice(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError("[WinError 32] being used by another process")
        real_replace(src, dst)
    monkeypatch.setattr(cs.os, "replace", busy_twice)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    assert cs.install_desktop_entry(config, entry)["changed"]
    assert len(calls) == 3
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"][cs.DESKTOP_NAME] == entry


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_install_keeps_the_file_mode(config, entry):
    write_json(config, {})
    config.chmod(0o640)
    cs.install_desktop_entry(config, entry)
    assert (config.stat().st_mode & 0o777) == 0o640


def test_install_writes_through_a_symlinked_config(tmp_path, entry):
    real = tmp_path / "dotfiles" / "claude_desktop_config.json"
    write_json(real, {"mcpServers": {}})
    link = tmp_path / "Claude" / "claude_desktop_config.json"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need extra rights here")
    cs.install_desktop_entry(link, entry)
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8"))["mcpServers"][cs.DESKTOP_NAME] == entry


# -- preview ---------------------------------------------------------------------------


def test_preview_is_exactly_what_install_writes(config, entry):
    write_json(config, {"b": 1, "mcpServers": {"filesystem": OTHER_SERVER}, "a": [1, 2]})
    before = config.read_bytes()
    preview = cs.preview_desktop_change(config, entry)
    assert config.read_bytes() == before and backups(config) == []   # wrote nothing
    cs.install_desktop_entry(config, entry)
    assert config.read_text(encoding="utf-8") == preview


def test_preview_of_a_missing_file_creates_nothing(config, entry):
    preview = cs.preview_desktop_change(config, entry)
    assert json.loads(preview) == {"mcpServers": {cs.DESKTOP_NAME: entry}}
    assert not config.exists() and not config.parent.exists()


def test_preview_refuses_invalid_json(config, entry):
    config.parent.mkdir(parents=True)
    config.write_text("{oops", encoding="utf-8")
    with pytest.raises(cs.DesktopConfigError):
        cs.preview_desktop_change(config, entry)


def test_read_desktop_entry(config, entry):
    assert cs.read_desktop_entry(config) is None
    write_json(config, {"mcpServers": {cs.DESKTOP_NAME: OTHER_SERVER}})
    assert cs.read_desktop_entry(config) == OTHER_SERVER


# -- remove ------------------------------------------------------------------------------


def test_remove_takes_out_only_litreview(config, entry):
    write_json(config, {"keep": True, "mcpServers": {"filesystem": OTHER_SERVER}})
    cs.install_desktop_entry(config, entry)
    result = cs.remove_desktop_entry(config)
    assert result["action"] == "removed" and result["changed"] and result["previous"] == entry
    assert Path(result["backup"]).exists()
    assert json.loads(config.read_text(encoding="utf-8")) == \
        {"keep": True, "mcpServers": {"filesystem": OTHER_SERVER}}
    again = cs.remove_desktop_entry(config)
    assert again["action"] == "absent" and not again["changed"]


def test_remove_takes_out_the_old_name_too(config):
    old = {"command": "C:\\old\\python.exe", "args": ["C:\\old\\mcp_bridge.py"]}
    write_json(config, {"mcpServers": {"litreview": old, "filesystem": OTHER_SERVER}})
    result = cs.remove_desktop_entry(config)
    assert result["action"] == "removed" and result["previous"] == old
    assert json.loads(config.read_text(encoding="utf-8")) == \
        {"mcpServers": {"filesystem": OTHER_SERVER}}


def test_remove_from_a_missing_file_creates_nothing(config):
    assert cs.remove_desktop_entry(config)["action"] == "absent"
    assert not config.exists()


def test_remove_refuses_invalid_json(config):
    config.parent.mkdir(parents=True)
    config.write_text('{"mcpServers": {"litreview": }', encoding="utf-8")
    with pytest.raises(cs.DesktopConfigError):
        cs.remove_desktop_entry(config)
    assert config.read_text(encoding="utf-8") == '{"mcpServers": {"litreview": }'


# -- Claude Code -------------------------------------------------------------------------


def test_claude_code_command():
    assert cs.claude_code_command(8765) == \
        "claude mcp add --transport http litreview http://127.0.0.1:8765/mcp"
    assert cs.claude_code_command(9001) == \
        "claude mcp add --transport http litreview http://127.0.0.1:9001/mcp"
    assert cs.claude_code_command(8765, scope="user") == \
        "claude mcp add --transport http --scope user litreview http://127.0.0.1:8765/mcp"
    with pytest.raises(ValueError):
        cs.claude_code_command(8765, scope="everywhere")


def test_mcp_json_for_claude_code():
    assert cs.mcp_json_for_claude_code(8766) == {
        "mcpServers": {"litreview": {"type": "http", "url": "http://127.0.0.1:8766/mcp"}}}


# -- the module itself -------------------------------------------------------------------


def test_uses_only_the_standard_library_and_nothing_else_from_the_package():
    tree = ast.parse(Path(cs.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import from the rest of litreview_local"
            imported.add(node.module.split(".")[0])
    stdlib = getattr(sys, "stdlib_module_names", None) or {
        "__future__", "copy", "json", "os", "shutil", "sys", "tempfile", "time",
        "datetime", "pathlib", "typing"}
    assert imported <= set(stdlib) | {"__future__"}, imported - set(stdlib)
    assert "litreview_local" not in imported


def test_the_restart_is_explained():
    for text in (cs.__doc__, cs.install_desktop_entry.__doc__,
                 cs.remove_desktop_entry.__doc__, cs.RESTART_NOTE):
        text = " ".join(text.lower().split())
        assert "quit" in text
        assert any(words in text for words in ("reopen", "open it again", "opened again"))


def test_the_store_builds_user_data_folder_comes_first(tmp_path):
    """A Store build now runs with --user-data-dir under Claude-Data and
    keeps its config there; the Packages copy and %APPDATA% come after."""
    local = tmp_path / "Local"
    data = local / "Claude-Data" / "claude_pzs8sxrjxfjjc"
    data.mkdir(parents=True)
    (local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    found = cs.desktop_config_candidates(platform="win32", home=tmp_path,
                                         environ={"LOCALAPPDATA": str(local),
                                                  "APPDATA": str(tmp_path / "Roaming")})
    assert found[0] == data / "claude_desktop_config.json"
    (data / "claude_desktop_config.json").write_text('{"preferences": {"x": 1}}',
                                                     encoding="utf-8")
    assert cs.find_desktop_config(platform="win32", home=tmp_path,
                                  environ={"LOCALAPPDATA": str(local),
                                           "APPDATA": str(tmp_path / "Roaming")}) \
        == data / "claude_desktop_config.json"


def test_taking_the_only_server_out_leaves_the_file_as_it_was(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"preferences": {"theme": "dark"}}\n', encoding="utf-8")
    entry = cs.desktop_entry(sys.executable, str(tmp_path / "mcp_bridge.py"))
    cs.install_desktop_entry(config, entry)
    assert "mcpServers" in json.loads(config.read_text(encoding="utf-8"))
    result = cs.remove_desktop_entry(config)
    assert result["action"] == "removed"
    assert json.loads(config.read_text(encoding="utf-8")) == {"preferences": {"theme": "dark"}}


def test_the_log_is_found_under_the_new_name_or_the_old(tmp_path):
    logs = tmp_path / "Local" / "Claude" / "logs"
    logs.mkdir(parents=True)
    assert cs.find_desktop_log(**_windows(tmp_path)) is None
    old = logs / "mcp-server-litreview.log"
    old.write_text("old", encoding="utf-8")
    assert cs.find_desktop_log(**_windows(tmp_path)) == old
    new = logs / "mcp-server-LitReview Local.log"
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    assert cs.find_desktop_log(**_windows(tmp_path)) == new

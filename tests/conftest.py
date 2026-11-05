"""Test setup: a throwaway library, background jobs run inline, no network.

The data directory is pointed at a fresh temporary folder before anything
from litreview_local is imported - the engine is created at import time from
that location, and the real ~/LitReview must never be opened by a test.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(tempfile.mkdtemp(prefix="litreview-test-"))
os.environ["LITREVIEW_DATA_DIR"] = str(DATA_DIR)
os.environ["LITREVIEW_PORT"] = "8799"
if str(LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_DIR))

import pytest  # noqa: E402

try:  # the launcher/bridge tests run on old Pythons without the app
    from litreview_local import worker  # noqa: E402
except Exception:  # pragma: no cover - only when the app cannot import
    worker = None

if worker is not None:
    worker.set_inline(True)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Nothing in a test reaches the internet: no journal register, no
    OpenAlex journal lookups, no Crossref lookups completing references."""
    if worker is None:
        yield
        return
    from litreview_local import journals, services

    monkeypatch.setattr(journals, "register_loaded", lambda db: True)
    monkeypatch.setattr(journals, "load_norwegian_register",
                        lambda db, **kw: 0)
    monkeypatch.setattr(journals, "get_or_fetch", lambda db, issns, **kw: None)
    # test_references.py tests the real one, with a mocked HTTP client.
    monkeypatch.setattr(services, "polish_from_crossref",
                        lambda db, papers, **kw: 0)
    yield


@pytest.fixture
def clean_db():
    """An empty library for each test that asks for one."""
    from litreview_local import db, models

    db.run_migrations()
    yield
    with db.engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def client(clean_db):
    from fastapi.testclient import TestClient

    from litreview_local.main import app

    # Addressed to this machine, as a browser would: the app refuses any
    # other Host (see main.local_only).
    with TestClient(app, base_url="http://127.0.0.1:8799") as test_client:
        yield test_client


class Claude:
    """A stand-in for the user's Claude, talking to /mcp like the real one."""

    def __init__(self, client, name: str = "test-claude"):
        self.client = client
        self.session = None
        self.ids = 0
        response = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                           "clientInfo": {"name": name}})
        self.session = response.headers.get("Mcp-Session-Id")
        self.instructions = response.json()["result"]["instructions"]

    def rpc(self, method: str, params: dict | None = None):
        self.ids += 1
        headers = {"Content-Type": "application/json"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        return self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": self.ids, "method": method,
            "params": params or {}})

    def call_raw(self, tool: str, **args) -> dict:
        response = self.rpc("tools/call", {"name": tool, "arguments": args})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def call(self, tool: str, **args) -> dict:
        result = self.call_raw(tool, **args)
        text = result["content"][0]["text"]
        assert not result["isError"], f"{tool} failed: {text}"
        data = json.loads(text)
        # A long input comes as a block of its own ("===== text =====\n...");
        # put it back where the JSON says it was, as Claude reads it.
        for block in result["content"][1:]:
            if block.get("type") == "text" and block["text"].startswith("===== "):
                header, _, body = block["text"].partition("\n")
                key = header.strip("= ").strip()
                data.setdefault("input", {})[key] = body
                data.setdefault("_blocks", []).append(key)
        return data

    def error(self, tool: str, **args) -> str:
        result = self.call_raw(tool, **args)
        assert result["isError"], f"{tool} unexpectedly succeeded"
        return result["content"][0]["text"]


@pytest.fixture
def claude(client):
    return Claude(client)

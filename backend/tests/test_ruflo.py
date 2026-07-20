"""services.ruflo: binary resolution, graceful-off behavior, and response
unwrapping. No subprocess is ever spawned here.
"""

import json

import pytest

from services import ruflo


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    ruflo.reset_for_tests()
    yield
    ruflo.reset_for_tests()


def test_resolve_bin_env_override_missing(monkeypatch):
    monkeypatch.setenv("RUFLO_BIN", "/nope/claude-flow")
    assert ruflo._resolve_bin() == ""
    assert ruflo.available() is False


def test_resolve_bin_env_override_exists(monkeypatch, tmp_path):
    fake = tmp_path / "claude-flow"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("RUFLO_BIN", str(fake))
    assert ruflo._resolve_bin() == str(fake)
    assert ruflo.available() is True


async def test_calls_noop_when_binary_missing(monkeypatch):
    monkeypatch.setenv("RUFLO_BIN", "/nope/claude-flow")
    assert await ruflo.swarm_init("hierarchical", 4, "specialized") == ""
    assert await ruflo.agent_spawn("coder") == ""
    assert await ruflo.memory_store("k", "v", "ns") is False
    # One failed attempt flips the sticky disabled flag.
    assert ruflo.available() is False


def test_tool_json_unwraps_content():
    resp = {"result": {"content": [
        {"type": "text", "text": json.dumps({"success": True, "swarmId": "s-1"})}]}}
    assert ruflo._tool_json(resp) == {"success": True, "swarmId": "s-1"}


def test_tool_json_tolerates_garbage():
    assert ruflo._tool_json(None) == {}
    assert ruflo._tool_json({}) == {}
    assert ruflo._tool_json({"result": {"content": [{"text": "not json"}]}}) == {}


async def test_call_tool_success_path(monkeypatch):
    """Wire a fake transport: _ensure_proc succeeds, _rpc returns a canned
    tools/call response — the public API should unwrap it."""
    async def fake_ensure():
        return True

    async def fake_rpc(method, params, **kw):
        assert method == "tools/call"
        assert params["name"] == "swarm_init"
        return {"result": {"content": [{"type": "text", "text": json.dumps(
            {"success": True, "swarmId": "swarm-xyz"})}]}}

    monkeypatch.setattr(ruflo, "_ensure_proc", fake_ensure)
    monkeypatch.setattr(ruflo, "_rpc", fake_rpc)
    assert await ruflo.swarm_init("mesh", 5, "balanced") == "swarm-xyz"


async def test_memory_store_truncates_and_reports(monkeypatch):
    seen = {}

    async def fake_ensure():
        return True

    async def fake_rpc(method, params, **kw):
        seen.update(params["arguments"])
        return {"result": {"content": [{"type": "text",
                                        "text": '{"success": true}'}]}}

    monkeypatch.setattr(ruflo, "_ensure_proc", fake_ensure)
    monkeypatch.setattr(ruflo, "_rpc", fake_rpc)
    assert await ruflo.memory_store("k", "x" * 20000, "ns") is True
    assert len(seen["value"]) == 8000              # capped before sending

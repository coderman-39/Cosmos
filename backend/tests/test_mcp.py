"""Dynamic tool registry + MCP client.

Covers the three contracts F13 introduced:
  1. register_tool/unregister_tools mutate TOOLS/_HANDLERS coherently and the
     prompt-cache memo only refreshes on explicit invalidation.
  2. The risk gate DEFAULT-DENIES unknown tools (it used to fail open) and
     gates dynamic tools by their registration metadata — including the
     unattended (scheduled headless) hard-block.
  3. The MCP client speaks real stdio JSON-RPC end-to-end against a live fake
     server subprocess, and flattens results to the house string convention.
"""

import json
import sys

import pytest

from services import agent, mcp_client


def _schema(name):
    return {"name": name, "description": "test tool",
            "input_schema": {"type": "object", "properties": {}}}


# ─── Tracker owned_by object-shape fix (update_work/update_part) ────────────────
# Regression: tracker-style works.update rejects owned_by as a bare array
# ('unexpected_json_type · expected object'); it must be a {"set":[...]} object.

def test_tracker_ticket_owner_wrapped_as_set_object():
    args = {"id": "ticket/4575687", "type": "ticket", "owned_by": ["USER-6224"]}
    out = mcp_client._tracker_update_fixup(True, "update_work", args)
    assert out["owned_by"] == {"set": ["USER-6224"]}
    # Unrelated fields preserved exactly.
    assert out["id"] == args["id"] and out["type"] == "ticket"


def test_tracker_issue_owner_same_shape():
    # Field-level validation → issues use the identical object shape.
    out = mcp_client._tracker_update_fixup(True, "update_work",
                                           {"type": "issue", "owned_by": ["USER-1"]})
    assert out["owned_by"] == {"set": ["USER-1"]}


def test_tracker_update_part_also_wrapped():
    out = mcp_client._tracker_update_fixup(True, "update_part", {"owned_by": ["USER-9"]})
    assert out["owned_by"] == {"set": ["USER-9"]}


def test_tracker_object_shape_passes_through():
    # Model already used the set/add/remove object — leave it alone.
    for shape in ({"set": ["USER-1"]}, {"add": ["USER-2"]}, {"remove": ["USER-3"]}):
        out = mcp_client._tracker_update_fixup(True, "update_work", {"owned_by": shape})
        assert out["owned_by"] == shape


def test_tracker_create_and_other_tools_untouched():
    # create_work takes a bare array — must NOT be reshaped.
    a = {"owned_by": ["USER-1"]}
    assert mcp_client._tracker_update_fixup(True, "create_work", a)["owned_by"] == ["USER-1"]
    # No owned_by → no-op, no crash.
    assert mcp_client._tracker_update_fixup(True, "update_work", {"title": "x"}) == {"title": "x"}
    # Non-tracker server → untouched even for an update_work-named tool.
    assert mcp_client._tracker_update_fixup(False, "update_work", a)["owned_by"] == ["USER-1"]


async def _noop_handler(args, ctx):
    return "ok"


@pytest.fixture
def registry_guard():
    """Snapshot/restore every module-global the registry mutates — dynamic
    registrations must never leak into sibling tests."""
    tools = list(agent.TOOLS)
    handlers = dict(agent._HANDLERS)
    dynamic = dict(agent._DYNAMIC_TOOLS)
    readonly = agent._READ_ONLY_TOOLS
    artifacts = set(agent._ARTIFACT_TOOLS)
    cached = agent._tools_cached
    yield
    agent.TOOLS[:] = tools
    agent._HANDLERS.clear(); agent._HANDLERS.update(handlers)
    agent._DYNAMIC_TOOLS.clear(); agent._DYNAMIC_TOOLS.update(dynamic)
    agent._READ_ONLY_TOOLS = readonly
    agent._ARTIFACT_TOOLS.clear(); agent._ARTIFACT_TOOLS.update(artifacts)
    agent._tools_cached = cached


# ─── Registry mechanics ────────────────────────────────────────────────────────

def test_register_and_unregister_roundtrip(registry_guard):
    agent.register_tool(_schema("dyn_a"), _noop_handler, source="test:x")
    agent.register_tool(_schema("dyn_b"), _noop_handler, gate="open", source="test:x")
    assert "dyn_a" in agent._HANDLERS and "dyn_b" in agent._HANDLERS
    assert {t["name"] for t in agent.TOOLS} >= {"dyn_a", "dyn_b"}
    assert "dyn_b" in agent._READ_ONLY_TOOLS

    n = agent.unregister_tools("test:x")
    assert n == 2
    assert "dyn_a" not in agent._HANDLERS
    assert "dyn_a" not in {t["name"] for t in agent.TOOLS}
    assert "dyn_b" not in agent._READ_ONLY_TOOLS


def test_register_rejects_shadowing_and_bad_names(registry_guard):
    with pytest.raises(ValueError):
        agent.register_tool(_schema("bash"), _noop_handler)      # built-in
    with pytest.raises(ValueError):
        agent.register_tool(_schema("has space"), _noop_handler)
    with pytest.raises(ValueError):
        agent.register_tool(_schema(""), _noop_handler)
    with pytest.raises(ValueError):
        agent.register_tool(_schema("x" * 65), _noop_handler)
    with pytest.raises(ValueError):
        agent.register_tool(_schema("fine_name"), _noop_handler, gate="wat")


def test_reregister_replaces_in_place(registry_guard):
    agent.register_tool(_schema("dyn_r"), _noop_handler, source="test:x")

    async def other(args, ctx):
        return "other"

    agent.register_tool(_schema("dyn_r"), other, source="test:x")
    assert sum(1 for t in agent.TOOLS if t["name"] == "dyn_r") == 1
    assert agent._HANDLERS["dyn_r"] is other


def test_tool_cache_refreshes_only_on_invalidation(registry_guard):
    agent.invalidate_tool_cache()
    before = {t["name"] for t in agent._tools_for_request()}
    agent.register_tool(_schema("dyn_cache"), _noop_handler, source="test:x")
    # Memo is stale by design until the batch is committed…
    assert {t["name"] for t in agent._tools_for_request()} == before
    agent.invalidate_tool_cache()
    tools = agent._tools_for_request()
    assert "dyn_cache" in {t["name"] for t in tools}
    # …and the cache breakpoint sits ONLY on the (new) last tool.
    assert "cache_control" in tools[-1]
    assert all("cache_control" not in t for t in tools[:-1])


def test_dynamic_timeout(registry_guard):
    agent.register_tool(_schema("dyn_slow"), _noop_handler, timeout=42.0,
                        source="test:x")
    agent.register_tool(_schema("dyn_default"), _noop_handler, source="test:x")
    assert agent._tool_timeout("dyn_slow", {}) == 42.0
    assert agent._tool_timeout("dyn_default", {}) == agent.TOOL_TIMEOUT_S


def test_dynamic_label(registry_guard):
    agent.register_tool(_schema("dyn_lbl"), _noop_handler,
                        label="gcal: list-events", source="test:x")
    assert agent._label("dyn_lbl", {}) == "gcal: list-events"


# ─── Risk gate: default-deny + dynamic gates ───────────────────────────────────

def test_unknown_tool_default_denies_in_both_modes():
    assert agent.needs_confirmation("never_registered", {}, "ask")
    assert agent.needs_confirmation("never_registered", {}, "full")


def test_dynamic_gate_open_never_confirms(registry_guard):
    agent.register_tool(_schema("dyn_ro"), _noop_handler, gate="open",
                        source="test:x")
    assert agent.needs_confirmation("dyn_ro", {}, "ask") is None
    assert agent.needs_confirmation("dyn_ro", {}, "full") is None
    assert agent.needs_confirmation("dyn_ro", {}, "full", unattended=True) is None


def test_dynamic_gate_confirm_matrix(registry_guard):
    agent.register_tool(_schema("dyn_w"), _noop_handler, gate="confirm",
                        source="test:x")
    assert agent.needs_confirmation("dyn_w", {}, "ask") is not None
    assert agent.needs_confirmation("dyn_w", {}, "full") is None
    # Unattended (scheduled) runs hard-gate external writes in EVERY mode —
    # the headless interaction auto-declines, so this call can never fire.
    assert agent.needs_confirmation("dyn_w", {}, "full", unattended=True) is not None


def test_dynamic_gate_destructive_confirms_everywhere(registry_guard):
    agent.register_tool(_schema("dyn_del"), _noop_handler, gate="destructive",
                        source="test:x")
    assert agent.needs_confirmation("dyn_del", {}, "ask") is not None
    assert agent.needs_confirmation("dyn_del", {}, "full") is not None


def test_static_tools_unaffected_by_default_deny():
    # Reads stay free in ask mode; full mode still only gates destruction.
    assert agent.needs_confirmation("read_file", {"path": "/x"}, "ask") is None
    assert agent.needs_confirmation("web_search", {"query": "q"}, "full") is None


# ─── MCP client: pure helpers ──────────────────────────────────────────────────

def test_flatten_text_and_error():
    assert mcp_client._flatten_result(
        {"content": [{"type": "text", "text": "a"},
                     {"type": "text", "text": "b"}]}) == "a\nb"
    out = mcp_client._flatten_result(
        {"isError": True, "content": [{"type": "text", "text": "kaboom"}]})
    assert out.startswith("Error:") and "kaboom" in out
    assert mcp_client._flatten_result({}) == "(empty result)"


def test_flatten_truncates_huge_results():
    big = "x" * (mcp_client._MAX_RESULT_CHARS + 5000)
    out = mcp_client._flatten_result({"content": [{"type": "text", "text": big}]})
    assert len(out) < mcp_client._MAX_RESULT_CHARS + 100
    assert "truncated" in out


def test_schema_wants_array():
    w = mcp_client._schema_wants_array
    assert w({"type": "array", "items": {"type": "string"}}) is True
    assert w({"type": ["array", "null"]}) is True
    assert w({"type": "string"}) is False
    assert w({"type": ["string", "array"]}) is False   # scalar already valid
    assert w({"anyOf": [{"type": "array"}, {"type": "array"}]}) is True
    assert w({"anyOf": [{"type": "string"}, {"type": "array"}]}) is False
    assert w({}) is False


def test_coerce_scalar_into_array_param():
    # Tracker-style list_works: `type` is array<enum>; the model often sends a
    # bare scalar, which the API rejects with a 400 'not of type array'.
    # Coercion wraps it so the call succeeds.
    schema = {"type": "object", "properties": {
        "type": {"type": "array", "items": {"type": "string", "enum": ["issue", "ticket"]}},
        "cursor": {"type": "string"}}}
    assert mcp_client._coerce_args({"type": "issue"}, schema) == {"type": ["issue"]}
    # Already an array → untouched; string params → untouched.
    assert mcp_client._coerce_args({"type": ["issue"], "cursor": "abc"}, schema) \
        == {"type": ["issue"], "cursor": "abc"}
    # A string-typed param (e.g. list_parts.type) must never be wrapped.
    str_schema = {"type": "object", "properties": {"type": {"type": "string"}}}
    assert mcp_client._coerce_args({"type": "enhancement"}, str_schema) \
        == {"type": "enhancement"}
    # No schema / no properties → passthrough, and None values are left alone.
    assert mcp_client._coerce_args({"type": "issue"}, None) == {"type": "issue"}
    assert mcp_client._coerce_args({"type": None}, schema) == {"type": None}


def test_prune_empty_args():
    # Mirrors a tracker's list_works: object filters require after/before or
    # next_cursor+mode; models pad them with empty defaults → opaque 400.
    schema = {"type": "object", "properties": {
        "type": {"type": "array", "items": {"type": "string"}},
        "owned_by": {"type": "array", "items": {"type": "string"}},
        "cursor": {"type": "object",
                   "properties": {"next_cursor": {"type": "string"},
                                  "mode": {"type": "string"}},
                   "required": ["next_cursor", "mode"]},
        "created_date": {"type": "object",
                         "properties": {"after": {"type": "string"},
                                        "before": {"type": "string"}},
                         "required": ["after", "before"]}}}
    padded = {
        "type": ["issue"], "owned_by": [], "state": [],
        "cursor": {"next_cursor": "", "mode": "after"},
        "created_date": {"after": "", "before": ""},
        "created_by": ["USER-12581"],
    }
    # Only the real filters survive; every blank/partial field is dropped.
    assert mcp_client._prune_empty_args(padded, schema) == {
        "type": ["issue"], "created_by": ["USER-12581"]}
    # Empty string inside an array is stripped; an all-blank array is dropped.
    assert mcp_client._prune_empty_args({"owned_by": [""]}, schema) == {}
    assert mcp_client._prune_empty_args(
        {"owned_by": ["USER-1", ""]}, schema) == {"owned_by": ["USER-1"]}
    # A COMPLETE object filter is preserved untouched.
    good = {"created_date": {"after": "2025-01-01T00:00:00Z",
                             "before": "2025-12-31T00:00:00Z"}}
    assert mcp_client._prune_empty_args(good, schema) == good
    # No schema → still strips blanks, just can't enforce required-field drop.
    assert mcp_client._prune_empty_args({"a": "", "b": "x"}, None) == {"b": "x"}


def test_args_have_email():
    e = mcp_client._args_have_email
    assert e({"owned_by": ["someone@example.com"]}) is True
    assert e({"nested": {"x": ["a", "foo@bar.io"]}}) is True
    assert e({"owned_by": ["USER-12581"], "type": ["issue"]}) is False
    assert e({"q": "just text"}) is False
    assert e({}) is False


def test_looks_like_failure():
    f = mcp_client._looks_like_failure
    assert f("Error: kaboom") is True
    assert f('List works failed with status 400: {"type":"bad_request"}') is True
    assert f("Bad Request") is True
    assert f("invalid_id field owned_by") is True
    assert f("Works listed successfully: {...}") is False


def test_agent_tool_name_sanitized():
    name = mcp_client._agent_tool_name("gcal", "events.list@v2")
    assert name == "mcp__gcal__events_list_v2"
    assert len(mcp_client._agent_tool_name("s" * 40, "t" * 60)) <= 64


def test_gate_from_annotations():
    ro = {"annotations": {"readOnlyHint": True}}
    # readOnlyHint is server-controlled: it grants free execution ONLY for
    # servers the user marked trust=true. destructiveHint is always honored.
    assert mcp_client._gate_for(ro, trusted=True) == "open"
    assert mcp_client._gate_for(ro, trusted=False) == "confirm"
    assert mcp_client._gate_for({"annotations": {"destructiveHint": True}},
                                trusted=True) == "destructive"
    assert mcp_client._gate_for({"annotations": {}}, trusted=True) == "confirm"
    assert mcp_client._gate_for({}, trusted=False) == "confirm"


def test_gate_read_tools_allowlist():
    # Un-annotated server (e.g. a tracker): a read_tools allowlist runs those
    # free on a trusted server; anything not listed still confirms; trust is required.
    reads = frozenset({"search", "get_work"})
    assert mcp_client._gate_for({"name": "search"}, True, reads) == "open"
    assert mcp_client._gate_for({"name": "get_work"}, True, reads) == "open"
    assert mcp_client._gate_for({"name": "create_work"}, True, reads) == "confirm"
    assert mcp_client._gate_for({"name": "search"}, False, reads) == "confirm"
    # destructiveHint always wins, even if mistakenly listed as a read.
    assert mcp_client._gate_for({"name": "search",
                                 "annotations": {"destructiveHint": True}},
                                True, reads) == "destructive"


def test_config_expands_env_vars(monkeypatch, tmp_path):
    # Don't let _load_config pull the real backend/.env into the test env.
    monkeypatch.setattr(mcp_client, "_refresh_env", lambda: None)
    monkeypatch.setenv("FAKE_MCP_TOKEN", "sekret")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "remote": {"url": "https://x.example/mcp",
                   "headers": {"Authorization": "Bearer ${FAKE_MCP_TOKEN}"}}}}))
    monkeypatch.setattr(mcp_client, "CONFIG_FILE", cfg)
    servers = mcp_client._load_config()
    assert servers["remote"]["headers"]["Authorization"] == "Bearer sekret"


def test_missing_config_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_client, "_refresh_env", lambda: None)
    monkeypatch.setattr(mcp_client, "CONFIG_FILE", tmp_path / "absent.json")
    assert mcp_client._load_config() == {}


def test_refresh_env_loads_new_dotenv_var(monkeypatch, tmp_path):
    # A secret added to .env AFTER boot must become available on reload.
    env = tmp_path / ".env"
    env.write_text("LATE_ADDED_SECRET=show-up-please\n")
    monkeypatch.setattr(mcp_client, "_ENV_PATH", env)
    import os
    os.environ.pop("LATE_ADDED_SECRET", None)
    mcp_client._refresh_env()
    assert os.environ.get("LATE_ADDED_SECRET") == "show-up-please"
    os.environ.pop("LATE_ADDED_SECRET", None)


# ─── MCP client: live stdio end-to-end ─────────────────────────────────────────

_FAKE_SERVER = r'''
import sys, json
TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "boom", "description": "Always errors",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "listy", "description": "Echoes the received `type` arg back",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "array", "items": {"type": "string"}}},
                     "required": ["type"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "plainfail", "description": "Returns a 400 as plain text (no isError)",
     "inputSchema": {"type": "object", "properties": {
         "owned_by": {"type": "array", "items": {"type": "string"}}}},
     "annotations": {"readOnlyHint": True}},
]
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:
        continue                                  # notification
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {},
                  "serverInfo": {"name": "fake-server", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = msg["params"]["name"]
        if name == "echo":
            result = {"content": [{"type": "text",
                                   "text": "echo: " + msg["params"]["arguments"]["text"]}]}
        elif name == "listy":
            got = msg["params"]["arguments"].get("type")
            result = {"content": [{"type": "text", "text": json.dumps(got)}]}
        elif name == "plainfail":
            # Tracker-style: failure returned as ordinary text, isError unset.
            result = {"content": [{"type": "text",
                                   "text": 'List works failed with status 400: '
                                           '{"message":"Bad Request","type":"bad_request"}'}]}
        else:
            result = {"isError": True,
                      "content": [{"type": "text", "text": "kaboom"}]}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": "nope"}}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": result}) + "\n")
    sys.stdout.flush()
'''


@pytest.fixture
def fake_server_spec(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_SERVER)
    # trust: annotations from this server are honored (readOnlyHint → open).
    return {"command": sys.executable, "args": [str(script)], "trust": True}


async def test_stdio_end_to_end(fake_server_spec):
    srv = mcp_client.MCPServer("fake", fake_server_spec)
    try:
        await srv.connect()
        assert srv.server_info.get("name") == "fake-server"
        assert [t["name"] for t in srv.tools] == ["echo", "boom", "listy", "plainfail"]
        assert await srv.call_tool("echo", {"text": "hello"}) == "echo: hello"
        out = await srv.call_tool("boom", {})
        assert out.startswith("Error:") and "kaboom" in out
        assert "Hint:" not in out                       # no email in args
        # An email in the args of a failing call earns the recovery hint.
        out = await srv.call_tool("boom", {"owned_by": ["a@b.com"]})
        assert out.startswith("Error:") and "Hint:" in out and "internal ID" in out
        # Scalar for an array-typed param is coerced before the wire call, so
        # the server receives ["issue"], not "issue".
        assert await srv.call_tool("listy", {"type": "issue"}) == '["issue"]'
        assert await srv.call_tool("listy", {"type": ["a", "b"]}) == '["a", "b"]'
        # Tracker-style plain-text 400 (no isError) + email in args → hint appended.
        pf = await srv.call_tool("plainfail", {"owned_by": ["a@b.com"]})
        assert "status 400" in pf and "Hint:" in pf and "internal ID" in pf
        # Same failure without an email → no hint (nothing to resolve).
        assert "Hint:" not in await srv.call_tool("plainfail", {"owned_by": ["USER-1"]})
    finally:
        await srv.close()


async def test_stdio_tools_register_into_agent(fake_server_spec, registry_guard):
    srv = mcp_client.MCPServer("fake", fake_server_spec)
    try:
        await srv.connect()
        n = mcp_client._register_server_tools(srv)
        assert n == 4
        # Annotation-driven gating landed in the agent's risk gate:
        assert agent.needs_confirmation("mcp__fake__echo", {}, "ask") is None
        assert agent.needs_confirmation("mcp__fake__boom", {}, "ask") is not None
        assert agent.needs_confirmation(
            "mcp__fake__boom", {}, "full", unattended=True) is not None
        # And the wrapped handler round-trips through the live subprocess:
        handler = agent._HANDLERS["mcp__fake__echo"]
        assert await handler({"text": "hi"}, None) == "echo: hi"
    finally:
        agent.unregister_tools("mcp:fake")
        await srv.close()


async def test_server_death_fails_pending_calls(fake_server_spec):
    srv = mcp_client.MCPServer("fake", fake_server_spec)
    try:
        await srv.connect()
        srv.transport.proc.kill()
        out = await srv.call_tool("echo", {"text": "hi"})
        assert out.startswith("Error:")
        # Dead-transport flag: later calls fail FAST (no hang to full timeout).
        import time
        t0 = time.monotonic()
        out = await srv.call_tool("echo", {"text": "again"})
        assert out.startswith("Error:")
        assert time.monotonic() - t0 < 5
    finally:
        await srv.close()


async def test_stdio_subprocess_does_not_inherit_secrets(fake_server_spec,
                                                         monkeypatch):
    """The full backend env (Slack token, gateway keys) must never leak into
    third-party npx subprocesses — only a minimal passthrough + spec env."""
    monkeypatch.setenv("FAKE_SUPER_SECRET", "leak-me-if-you-can")
    captured = {}
    real_exec = mcp_client.asyncio.create_subprocess_exec

    async def spy_exec(*args, **kwargs):
        captured.update(kwargs.get("env") or {})
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(mcp_client.asyncio, "create_subprocess_exec", spy_exec)
    srv = mcp_client.MCPServer("fake", {**fake_server_spec,
                                        "env": {"WANTED": "yes"}})
    try:
        await srv.connect()
    finally:
        await srv.close()
    assert "FAKE_SUPER_SECRET" not in captured
    assert captured.get("WANTED") == "yes"
    assert "PATH" in captured and "HOME" in captured


def test_status_text_mentions_config_when_empty(monkeypatch):
    monkeypatch.setattr(mcp_client, "_SERVERS", {})
    assert "mcp.json" in mcp_client.status_text()

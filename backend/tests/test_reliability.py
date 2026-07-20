"""Run reliability guards: tool timeouts, confirm/ask deadlines, loop-breaker,
memory corruption quarantine, atomic writes.

These are the guards that keep a wedged tool, an absent user, or a stubborn
model from freezing or zombie-ing the backend.
"""

import asyncio
import json

import pytest

from services import agent
from services.agent import Interaction, RunContext, _run_one, _call_key


class _Block:
    """Minimal stand-in for an Anthropic tool_use content block."""

    def __init__(self, name: str, input: dict, id: str = "blk_1"):
        self.name = name
        self.input = input
        self.id = id


def _ctx(**kw) -> RunContext:
    async def emit(event):
        pass
    return RunContext(emit=emit, interaction=Interaction(), **kw)


# ─── Per-tool timeout ──────────────────────────────────────────────────────────

async def test_hung_tool_times_out(monkeypatch):
    async def hang(args, ctx):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setitem(agent._HANDLERS, "applescript", hang)
    monkeypatch.setattr(agent, "TOOL_TIMEOUT_S", 0.05)
    ctx = _ctx()
    out = await _run_one(ctx, _Block("applescript", {"script": "x", "description": "d"}))
    assert out.startswith("Error"), out
    assert "timed out" in out


async def test_ask_user_never_times_out_via_tool_timeout():
    # ask_user is exempt from the tool ceiling — it has its own (longer) deadline.
    assert agent._tool_timeout("ask_user", {}) is None
    assert agent._tool_timeout("say", {}) is None


def test_dynamic_timeouts():
    assert agent._tool_timeout("bash", {}) == 320.0
    assert agent._tool_timeout("record_video", {"duration": 100}) == 160.0
    assert agent._tool_timeout("read_file", {}) == agent.TOOL_TIMEOUT_S


# ─── Loop-breaker: 4th identical failing call is refused ───────────────────────

async def test_loop_breaker_blocks_fourth_identical_failure(monkeypatch):
    calls = {"n": 0}

    async def always_fail(args, ctx):
        calls["n"] += 1
        return "Error: boom"

    monkeypatch.setitem(agent._HANDLERS, "read_file", always_fail)
    ctx = _ctx()
    block = _Block("read_file", {"path": "/nope"})

    for _ in range(3):
        out = await _run_one(ctx, block)
        assert "boom" in out
    assert calls["n"] == 3

    out = await _run_one(ctx, block)
    assert "BLOCKED" in out
    assert calls["n"] == 3, "4th identical call must NOT execute"


async def test_loop_breaker_resets_on_success(monkeypatch):
    outcomes = iter(["Error: a", "Error: b", "ok now", "Error: c"])

    async def flaky(args, ctx):
        return next(outcomes)

    monkeypatch.setitem(agent._HANDLERS, "read_file", flaky)
    ctx = _ctx()
    block = _Block("read_file", {"path": "/x"})
    await _run_one(ctx, block)
    await _run_one(ctx, block)
    await _run_one(ctx, block)          # success — clears the count
    assert ctx.fail_counts.get(_call_key("read_file", {"path": "/x"}), 0) == 0
    out = await _run_one(ctx, block)    # a fresh failure, not blocked
    assert "Error: c" in out


async def test_different_args_not_conflated(monkeypatch):
    async def always_fail(args, ctx):
        return "Error: nope"

    monkeypatch.setitem(agent._HANDLERS, "read_file", always_fail)
    ctx = _ctx()
    for i in range(5):
        out = await _run_one(ctx, _Block("read_file", {"path": f"/f{i}"}))
        assert "BLOCKED" not in out, "distinct calls must never be blocked"


# ─── Confirm deadline auto-declines ────────────────────────────────────────────

async def test_confirm_timeout_auto_declines(monkeypatch):
    monkeypatch.setattr(agent, "CONFIRM_TIMEOUT_S", 0.05)
    events = []

    async def emit(event):
        events.append(event)

    ctx = RunContext(emit=emit, interaction=Interaction())
    verdict = await agent._confirm(ctx, "bash", {"command": "rm -rf /tmp/x"}, "danger")
    assert verdict == "timeout"
    assert any(e["type"] == "confirm_timeout" for e in events)
    assert not ctx.interaction.pending


async def test_confirm_answered_before_deadline(monkeypatch):
    monkeypatch.setattr(agent, "CONFIRM_TIMEOUT_S", 5.0)
    events = []

    async def emit(event):
        events.append(event)
        # Simulate the user clicking PROCEED as soon as the banner shows.
        if event["type"] == "confirm_request":
            ctx.interaction.resolve("yes")

    ctx = RunContext(emit=emit, interaction=Interaction())
    verdict = await agent._confirm(ctx, "bash", {"command": "rm x"}, "danger")
    assert verdict == "yes"


async def test_ask_user_timeout_returns_no_answer(monkeypatch):
    monkeypatch.setattr(agent, "ASK_TIMEOUT_S", 0.05)
    ctx = _ctx()
    out = await agent._tool_ask_user({"question": "which one?"}, ctx)
    assert "No answer" in out
    assert not ctx.interaction.pending


# ─── Memory: atomic writes + corruption quarantine ─────────────────────────────

def test_corrupt_memory_quarantined(tmp_path, monkeypatch):
    from services import memory
    mem_file = tmp_path / "memory.json"
    mem_file.write_text('{"corrections": {tru')       # truncated mid-write
    monkeypatch.setattr(memory, "FILE", mem_file)
    monkeypatch.setattr(memory, "_cache", None)

    mem = memory.load()
    assert mem == memory.DEFAULT
    quarantined = list(tmp_path.glob("memory.corrupt-*.json"))
    assert len(quarantined) == 1
    assert "tru" in quarantined[0].read_text()


def test_memory_save_is_atomic_and_reloadable(tmp_path, monkeypatch):
    from services import memory
    mem_file = tmp_path / "memory.json"
    monkeypatch.setattr(memory, "FILE", mem_file)
    monkeypatch.setattr(memory, "_cache", None)

    data = dict(memory.DEFAULT)
    data["preferences"] = {"editor": "vscode"}
    memory.save(data)
    assert json.loads(mem_file.read_text())["preferences"] == {"editor": "vscode"}
    assert not list(tmp_path.glob("*.tmp")), "tmp file must be renamed away"

    monkeypatch.setattr(memory, "_cache", None)
    assert memory.load()["preferences"] == {"editor": "vscode"}


def test_empty_memory_not_rereading_disk(tmp_path, monkeypatch):
    # The None-sentinel: a legitimately-empty dict must still count as loaded.
    from services import memory
    mem_file = tmp_path / "memory.json"
    mem_file.write_text("{}")
    monkeypatch.setattr(memory, "FILE", mem_file)
    monkeypatch.setattr(memory, "_cache", None)
    first = memory.load()
    assert first == {}
    mem_file.write_text('{"corrections": {"a": "b"}}')   # disk changes behind us
    assert memory.load() == {}, "cache must serve, not re-read"


# ─── run_shell hygiene: head+tail truncation, cached which, login env ──────────

def test_truncate_head_tail_keeps_the_error_tail():
    from services import system_control as sc
    out = "line-head\n" + ("x" * 50_000) + "\nFAILED: the real error"
    cut = sc._truncate_head_tail(out, 20_000)
    assert len(cut) < 21_000
    assert cut.startswith("line-head")
    assert cut.endswith("FAILED: the real error"), "the tail (real error) must survive"
    assert "truncated" in cut


def test_truncate_noop_when_small():
    from services import system_control as sc
    assert sc._truncate_head_tail("short", 100) == "short"


def test_which_tool_caches_and_finds_system_binaries():
    from services import system_control as sc
    assert sc.which_tool("ls") is not None
    assert "ls" in sc._which_cache
    assert sc.which_tool("definitely-not-a-binary-xyz") is None
    assert "definitely-not-a-binary-xyz" not in sc._which_cache  # misses re-check


async def test_run_shell_binary_output_does_not_crash():
    from services import system_control as sc
    ok, out = await sc.run_shell("head -c 100 /dev/urandom", timeout=10)
    assert ok  # decode(errors='replace') must absorb the binary garbage

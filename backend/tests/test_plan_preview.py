"""Plan preview / approve-once (F11).

Two mechanisms under test:
  1. Same-turn batching: ≥2 non-destructive gated calls in one turn produce
     ONE confirm_request carrying `steps`; the single verdict applies to all.
     Destructive calls are excluded and always confirm individually.
  2. propose_plan: one up-front approval pre-authorizes EXACT call signatures
     (single-use); changed args or irreversible calls still confirm.
"""

import pytest

from services import agent
from services.agent import Interaction, RunContext, _call_key


class _Block:
    def __init__(self, name, input, id):
        self.name = name
        self.input = input
        self.id = id


def _auto_ctx(answer="yes", mode="ask"):
    """RunContext whose emit auto-answers any confirm banner and records events."""
    events = []
    ctx = RunContext(emit=None, interaction=Interaction(), mode=mode)

    async def emit(event):
        events.append(event)
        if event["type"] == "confirm_request":
            ctx.interaction.resolve(answer)

    ctx.emit = emit
    return ctx, events


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_dm(args, ctx):
        calls.append(args)
        return "sent"

    async def fake_bash(args, ctx):
        calls.append(args)
        return "ran"

    monkeypatch.setitem(agent._HANDLERS, "slack_dm", fake_dm)
    monkeypatch.setitem(agent._HANDLERS, "bash", fake_bash)
    return calls


# ─── Same-turn batching ────────────────────────────────────────────────────────

async def test_two_gated_calls_one_banner(sent):
    ctx, events = _auto_ctx("yes")
    blocks = [_Block("slack_dm", {"recipient": "alice", "message": "hi"}, "b1"),
              _Block("slack_dm", {"recipient": "bob", "message": "yo"}, "b2")]
    results = await agent._run_tools(ctx, blocks)
    banners = [e for e in events if e["type"] == "confirm_request"]
    assert len(banners) == 1, "batch must produce exactly ONE banner"
    assert len(banners[0]["steps"]) == 2
    assert all(r["content"] == "sent" for r in results)
    assert len(sent) == 2


async def test_batch_decline_runs_nothing(sent):
    ctx, events = _auto_ctx("no")
    blocks = [_Block("slack_dm", {"recipient": "a", "message": "x"}, "b1"),
              _Block("slack_dm", {"recipient": "b", "message": "y"}, "b2")]
    results = await agent._run_tools(ctx, blocks)
    assert sent == [], "declining the batch must run NOTHING"
    assert all("declined" in r["content"].lower() for r in results)


async def test_single_gated_call_stays_individual(sent):
    ctx, events = _auto_ctx("yes")
    results = await agent._run_tools(
        ctx, [_Block("slack_dm", {"recipient": "a", "message": "x"}, "b1")])
    banners = [e for e in events if e["type"] == "confirm_request"]
    assert len(banners) == 1
    assert "steps" not in banners[0], "single call keeps the classic banner"
    assert results[0]["content"] == "sent"


async def test_destructive_never_batched(sent):
    ctx, events = _auto_ctx("yes")
    blocks = [_Block("slack_dm", {"recipient": "a", "message": "x"}, "b1"),
              _Block("slack_dm", {"recipient": "b", "message": "y"}, "b2"),
              _Block("bash", {"command": "rm -rf /tmp/junk"}, "b3")]
    await agent._run_tools(ctx, blocks)
    banners = [e for e in events if e["type"] == "confirm_request"]
    # One batch banner (2 sends) + one individual banner (rm).
    assert len(banners) == 2
    batch = next(e for e in banners if "steps" in e)
    assert len(batch["steps"]) == 2
    solo = next(e for e in banners if "steps" not in e)
    assert "rm" in solo["detail"]


async def test_mixed_verdict_isolation(sent, monkeypatch):
    """Batch approved but destructive declined — only the batch runs."""
    ctx, events = _auto_ctx("yes")
    answers = iter(["yes", "no"])          # batch → yes, rm → no

    async def emit(event):
        events.append(event)
        if event["type"] == "confirm_request":
            ctx.interaction.resolve(next(answers))

    ctx.emit = emit
    blocks = [_Block("slack_dm", {"recipient": "a", "message": "x"}, "b1"),
              _Block("slack_dm", {"recipient": "b", "message": "y"}, "b2"),
              _Block("bash", {"command": "rm -rf /tmp/junk"}, "b3")]
    results = await agent._run_tools(ctx, blocks)
    by_id = {r["tool_use_id"]: r["content"] for r in results}
    assert by_id["b1"] == "sent" and by_id["b2"] == "sent"
    assert "declined" in by_id["b3"].lower()
    assert len(sent) == 2


# ─── propose_plan pre-approval ─────────────────────────────────────────────────

async def test_propose_plan_preapproves_exact_signatures(sent):
    ctx, events = _auto_ctx("yes")
    out = await agent._tool_propose_plan({
        "goal": "notify the team",
        "steps": [{"tool": "slack_dm", "args": {"recipient": "alice", "message": "hi"}},
                  {"tool": "read_file", "args": {"path": "/tmp/x"}}],
    }, ctx)
    assert out.startswith("Plan approved")
    key = _call_key("slack_dm", {"recipient": "alice", "message": "hi"})
    assert key in ctx.preapproved
    # read_file was never gated — no key for it.
    assert len(ctx.preapproved) == 1

    # Executing the EXACT call now needs no banner…
    events.clear()
    results = await agent._run_tools(
        ctx, [_Block("slack_dm", {"recipient": "alice", "message": "hi"}, "b1")])
    assert results[0]["content"] == "sent"
    assert not [e for e in events if e["type"] == "confirm_request"]
    # …and the approval was single-use.
    assert key not in ctx.preapproved


async def test_changed_args_reconfirm(sent):
    ctx, events = _auto_ctx("yes")
    await agent._tool_propose_plan({
        "goal": "g",
        "steps": [{"tool": "slack_dm", "args": {"recipient": "alice", "message": "hi"}}],
    }, ctx)
    events.clear()
    # Different message → different signature → banner appears again.
    await agent._run_tools(
        ctx, [_Block("slack_dm", {"recipient": "alice", "message": "CHANGED"}, "b1")])
    assert [e for e in events if e["type"] == "confirm_request"]


async def test_plan_decline_preapproves_nothing(sent):
    ctx, _ = _auto_ctx("no")
    out = await agent._tool_propose_plan({
        "goal": "g",
        "steps": [{"tool": "slack_dm", "args": {"recipient": "a", "message": "x"}}],
    }, ctx)
    assert "declined" in out.lower()
    assert not ctx.preapproved


async def test_plan_never_preapproves_destruction(sent):
    ctx, _ = _auto_ctx("yes")
    out = await agent._tool_propose_plan({
        "goal": "cleanup",
        "steps": [{"tool": "bash", "args": {"command": "rm -rf /tmp/junk"}},
                  {"tool": "slack_dm", "args": {"recipient": "a", "message": "x"}}],
    }, ctx)
    assert out.startswith("Plan approved")
    assert "re-confirm" in out or "confirm individually" in out
    # Only the slack send was pre-approved; rm must still banner.
    assert len(ctx.preapproved) == 1
    assert _call_key("bash", {"command": "rm -rf /tmp/junk"}) not in ctx.preapproved


async def test_dynamic_destructive_tool_never_preapproved(monkeypatch):
    """gate=\"destructive\" MCP tools must behave exactly like built-in
    destruction: no propose_plan pre-approval, no batch banner (regression —
    _destructive_label alone knows nothing about dynamic tools)."""
    async def wipe(args, ctx):
        return "wiped"

    tools_before = list(agent.TOOLS)
    try:
        agent.register_tool(
            {"name": "mcp__mdm__wipe_device", "description": "d",
             "input_schema": {"type": "object", "properties": {}}},
            wipe, gate="destructive", source="test:mdm")
        ctx, events = _auto_ctx("yes")
        out = await agent._tool_propose_plan({
            "goal": "cleanup",
            "steps": [{"tool": "mcp__mdm__wipe_device", "args": {"id": "42"}},
                      {"tool": "slack_dm", "args": {"recipient": "a", "message": "x"}}],
        }, ctx)
        assert out.startswith("Plan approved")
        assert _call_key("mcp__mdm__wipe_device", {"id": "42"}) not in ctx.preapproved

        # And in a live turn it must NOT ride the batch banner: 2 sends batch,
        # the wipe confirms individually.
        monkeypatch.setitem(agent._HANDLERS, "mcp__mdm__wipe_device", wipe)
        events.clear()
        # Fresh args — nothing here was pre-approved by the plan above.
        blocks = [_Block("slack_dm", {"recipient": "c", "message": "m1"}, "b1"),
                  _Block("slack_dm", {"recipient": "d", "message": "m2"}, "b2"),
                  _Block("mcp__mdm__wipe_device", {"id": "42"}, "b3")]

        async def dm(args, c):
            return "sent"

        monkeypatch.setitem(agent._HANDLERS, "slack_dm", dm)
        await agent._run_tools(ctx, blocks)
        banners = [e for e in events if e["type"] == "confirm_request"]
        assert len(banners) == 2, "wipe must confirm on its own banner"
        assert len(next(e for e in banners if "steps" in e)["steps"]) == 2
    finally:
        agent.unregister_tools("test:mdm")
        agent.TOOLS[:] = tools_before


async def test_plan_rejects_unknown_and_recursive_tools():
    ctx, _ = _auto_ctx("yes")
    out = await agent._tool_propose_plan(
        {"goal": "g", "steps": [{"tool": "nope_tool", "args": {}}]}, ctx)
    assert out.startswith("Error")
    out = await agent._tool_propose_plan(
        {"goal": "g", "steps": [{"tool": "propose_plan", "args": {}}]}, ctx)
    assert out.startswith("Error")


async def test_plan_with_no_gated_steps_short_circuits():
    ctx, events = _auto_ctx("yes")
    out = await agent._tool_propose_plan({
        "goal": "reads only",
        "steps": [{"tool": "read_file", "args": {"path": "/tmp/x"}}],
    }, ctx)
    assert "skip the preview" in out
    assert not [e for e in events if e["type"] == "confirm_request"]

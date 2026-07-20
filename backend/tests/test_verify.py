"""Self-verification pass: the critic that checks "Done, sir" against the
tool-trace evidence before the run is allowed to finish.

Contract under test:
  - _verify() → None on PASS / critique string on FAIL / None on any error
    (verification must NEVER break or stall a run).
  - run_task() re-enters the loop AT MOST once on a critique, and the
    corrected answer is what the user gets.
  - Runs that did no real work (pure conversation, fast-path reads) are
    never verified — no added latency where there's nothing to check.
"""

import asyncio

import pytest

from services import agent, learning, llm
from services import recall as recall_svc
from services.agent import Interaction, RunContext


# ─── Fakes ─────────────────────────────────────────────────────────────────────

class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolBlock:
    type = "tool_use"

    def __init__(self, name, input, id="t1"):
        self.name = name
        self.input = input
        self.id = id


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None
        self.model = "model-A"


def _ctx(**kw) -> RunContext:
    async def emit(event):
        pass
    return RunContext(emit=emit, interaction=Interaction(), **kw)


def _is_verify_call(kwargs) -> bool:
    msgs = kwargs.get("messages") or []
    return (not kwargs.get("tools")
            and msgs
            and "verification module" in str(msgs[0].get("content", "")))


@pytest.fixture
def quiet_run(monkeypatch, tmp_path):
    """Neutralize every disk/system side effect of run_task."""
    from services import trace as trace_mod, system_control

    async def no_focus():
        return ""

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(system_control, "get_focus_context", no_focus)
    monkeypatch.setattr(recall_svc, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(learning, "record_route", lambda *a, **k: None)
    monkeypatch.setattr(learning, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(learning, "route_hint", lambda t: None)
    monkeypatch.setattr(llm, "STREAM_ENABLED", False)
    monkeypatch.setattr(agent, "_VERIFY_ENABLED", True)


# ─── _verify unit behavior ─────────────────────────────────────────────────────

async def test_verify_pass_returns_none(monkeypatch):
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock("PASS")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    ctx = _ctx()
    ctx.tools_used.add("bash")
    assert await agent._verify("do x", "did x", ctx) is None


async def test_verify_fail_extracts_critique(monkeypatch):
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock("FAIL: the email was never actually sent")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    critique = await agent._verify("email alice", "Done, sir.", _ctx())
    assert critique == "the email was never actually sent"


async def test_verify_fails_open_on_llm_error(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm, "acreate", boom)
    assert await agent._verify("do x", "did x", _ctx()) is None


async def test_verify_fails_open_on_garbage_verdict(monkeypatch):
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock("As an AI, I think the work looks great!")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    assert await agent._verify("do x", "did x", _ctx()) is None


# ─── run_task integration: the corrective re-entry ─────────────────────────────

async def _fake_gateway(monkeypatch, main_script, verify_script):
    """Install an llm.acreate that serves main-loop turns from `main_script`
    and verification calls from `verify_script`. Returns the call log."""
    calls = {"main": 0, "verify": 0}

    async def fake_acreate(**kwargs):
        if _is_verify_call(kwargs):
            calls["verify"] += 1
            return _Resp([_TextBlock(verify_script[calls["verify"] - 1])])
        calls["main"] += 1
        return main_script[calls["main"] - 1]

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    return calls


async def test_critique_forces_one_corrective_pass(monkeypatch, quiet_run):
    async def fake_read(args, ctx):
        return "file contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    main_script = [
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        _Resp([_TextBlock("Done, sir — report sent.")]),
        _Resp([_TextBlock("Correction: the report was read but never sent — "
                          "here is what I actually did.")]),
    ]
    calls = await _fake_gateway(monkeypatch, main_script,
                                verify_script=["FAIL: nothing was ever sent"])

    events = []

    async def emit(e):
        events.append(e)

    final = await agent.run_task("send the report to alice", emit, Interaction())
    assert final.startswith("Correction:")
    assert calls == {"main": 3, "verify": 1}, "exactly one corrective re-entry"
    thoughts = [e["text"] for e in events if e["type"] == "agent_thought"]
    assert any("Self-check" in t for t in thoughts)


async def test_pass_verdict_adds_no_extra_turn(monkeypatch, quiet_run):
    async def fake_read(args, ctx):
        return "file contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    main_script = [
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        _Resp([_TextBlock("Here's the report summary, sir.")]),
    ]
    calls = await _fake_gateway(monkeypatch, main_script, verify_script=["PASS"])

    async def emit(e):
        pass

    final = await agent.run_task("summarize the report and flag issues", emit,
                                 Interaction())
    assert final == "Here's the report summary, sir."
    assert calls == {"main": 2, "verify": 1}


async def test_second_critique_cannot_loop_forever(monkeypatch, quiet_run):
    """Even if the critic ALWAYS fails the work, only one retry happens."""
    async def fake_read(args, ctx):
        return "file contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    main_script = [
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        _Resp([_TextBlock("Done, sir.")]),
        _Resp([_TextBlock("Still done, sir.")]),
    ]
    calls = await _fake_gateway(monkeypatch, main_script,
                                verify_script=["FAIL: gap", "FAIL: gap again"])

    async def emit(e):
        pass

    final = await agent.run_task("send the report to alice", emit, Interaction())
    assert final == "Still done, sir."
    assert calls["main"] == 3
    assert calls["verify"] == 1, "retry budget is 1 — no second verification"


async def test_retry_strips_dangling_tool_use_blocks(monkeypatch, quiet_run):
    """A truncated final turn can carry tool_use blocks that never got
    tool_results. The verify retry must replay TEXT ONLY — dangling ids in
    the next request are a guaranteed 400 (regression)."""
    async def fake_read(args, ctx):
        return "file contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    sent_messages = []
    main_script = [
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        # Truncation shape: text + a dangling tool_use, but stop_reason end_turn.
        _Resp([_TextBlock("Done, sir."),
               _ToolBlock("read_file", {"path": "/y"}, id="dangling")],
              stop_reason="end_turn"),
        _Resp([_TextBlock("Corrected answer.")]),
    ]
    calls = {"main": 0, "verify": 0}

    async def fake_acreate(**kwargs):
        if _is_verify_call(kwargs):
            calls["verify"] += 1
            return _Resp([_TextBlock("FAIL: work incomplete")])
        calls["main"] += 1
        sent_messages.append([dict(m) for m in kwargs["messages"]])
        return main_script[calls["main"] - 1]

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    events = []

    async def emit(e):
        events.append(e)

    final = await agent.run_task("send the report to alice", emit, Interaction())
    assert final == "Corrected answer."
    # The retry request must contain NO dangling tool_use in its replayed
    # assistant message…
    retry_request = sent_messages[-1]
    replayed = retry_request[-2]
    assert replayed["role"] == "assistant"
    kinds = [getattr(b, "type", None) for b in replayed["content"]]
    assert "tool_use" not in kinds
    # …and the HUD compose bubble was reset before the corrected answer.
    assert any(e["type"] == "response_delta_reset" for e in events)


async def test_pure_conversation_never_verified(monkeypatch, quiet_run):
    main_script = [_Resp([_TextBlock("You're welcome, sir.")])]
    calls = await _fake_gateway(monkeypatch, main_script, verify_script=[])

    async def emit(e):
        pass

    final = await agent.run_task("thanks, that was helpful", emit, Interaction())
    assert final == "You're welcome, sir."
    assert calls == {"main": 1, "verify": 0}


async def test_verify_disabled_by_flag(monkeypatch, quiet_run):
    monkeypatch.setattr(agent, "_VERIFY_ENABLED", False)

    async def fake_read(args, ctx):
        return "file contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    main_script = [
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        _Resp([_TextBlock("Done, sir.")]),
    ]
    calls = await _fake_gateway(monkeypatch, main_script, verify_script=[])

    async def emit(e):
        pass

    await agent.run_task("send the report to alice", emit, Interaction())
    assert calls["verify"] == 0


# ─── false positives from a TRUNCATED trace (the gh api page-of-URLs case) ─────
# The critic reads a trace cut to ~120 chars per tool, so it reports "output
# truncated — cannot verify X" and charges a full corrective turn for work that
# actually happened. Not seeing evidence is not a defect; only a contradiction is.

@pytest.mark.parametrize("critique", [
    "github tool output truncated in evidence; cannot verify the 3 PR URLs were actually retrieved from the API",
    "cannot confirm the file contents from the evidence provided",
    "the URLs are not visible in the tool trace",
    "insufficient evidence to verify the PR list",
    "evidence is incomplete — unable to verify the result",
])
async def test_truncated_evidence_is_not_a_failure(monkeypatch, critique):
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock(f"FAIL: {critique}")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    ctx = _ctx()
    ctx.tools_used.add("github")
    ctx.trace.event("tool_done", tool="github", ok=True, detail="{'value': [{'id'...")
    assert await agent._verify("get the pr urls", "Here they are, sir.", ctx) is None


async def test_real_contradiction_still_fails(monkeypatch):
    """The narrowing must not blind the critic to a genuinely failed tool."""
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock(
            "FAIL: reply claims the email was sent but no evidence it succeeded")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    ctx = _ctx()
    ctx.tools_used.add("google")
    ctx.trace.event("tool_done", tool="google", ok=False, detail="Error: 403 forbidden")
    critique = await agent._verify("email alice", "Sent, sir.", ctx)
    assert critique is not None, "a FAILED tool in the trace must still fail the run"


async def test_plain_contradiction_without_evidence_words_still_fails(monkeypatch):
    async def fake_acreate(**kwargs):
        return _Resp([_TextBlock("FAIL: the second half of the request was skipped")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    ctx = _ctx()
    ctx.tools_used.add("bash")
    assert await agent._verify("do x and y", "Done, sir.", ctx) == \
        "the second half of the request was skipped"

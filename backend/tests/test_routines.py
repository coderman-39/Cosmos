"""Routine replay cache (SPEED_PLAN 4.1).

The store must only ever earn a routine from N identical successful runs of
safe, bounded, secret-free tool sequences — and the replay path must fall
back (or stop honestly) the moment anything deviates.
"""

import pytest

from services import agent, llm, routines
from services.agent import Interaction, RunContext


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(routines, "FILE", tmp_path / "routines.json")
    monkeypatch.setattr(routines, "ENABLED", True)
    return tmp_path


SEQ = [{"tool": "take_photo", "args": {}, "ok": True, "out": "saved"},
       {"tool": "slack_photo", "args": {"recipient": "alice"}, "ok": True, "out": "sent"}]


# ─── Store mechanics ───────────────────────────────────────────────────────────

def test_three_identical_runs_earn_a_routine():
    for _ in range(2):
        routines.observe("send alice a photo", SEQ, ok=True)
        assert routines.lookup("send alice a photo") is None
    routines.observe("send alice a photo", SEQ, ok=True)
    seq = routines.lookup("Send Alice a photo!")     # normalization
    assert seq and [s["tool"] for s in seq] == ["take_photo", "slack_photo"]


def test_different_sequence_resets_the_count():
    for _ in range(2):
        routines.observe("do the thing", SEQ, ok=True)
    other = [{"tool": "bash", "args": {"command": "ls"}, "ok": True},
             {"tool": "say", "args": {"text": "hi"}, "ok": True}]
    routines.observe("do the thing", other, ok=True)
    assert routines.lookup("do the thing") is None   # count restarted at 1


def test_failed_run_invalidates_a_known_phrase():
    for _ in range(3):
        routines.observe("send alice a photo", SEQ, ok=True)
    assert routines.lookup("send alice a photo")
    routines.observe("send alice a photo", SEQ, ok=False)
    assert routines.lookup("send alice a photo") is None


def test_unsafe_sequences_are_never_stored():
    interactive = SEQ + [{"tool": "ask_user", "args": {"question": "?"}, "ok": True}]
    oversize    = [{"tool": "write_file", "args": {"_oversize": True}, "ok": True}] + SEQ
    secret      = [{"tool": "bash", "args": {"command": "curl -H 'x: sk-abcdefghijkl'"},
                    "ok": True}] + SEQ
    single      = SEQ[:1]
    failed_step = [dict(SEQ[0], ok=False), SEQ[1]]
    for name, seq in [("a", interactive), ("b", oversize), ("c", secret),
                      ("d", single), ("e", failed_step)]:
        for _ in range(3):
            routines.observe(name, seq, ok=True)
        assert routines.lookup(name) is None, name


def test_filtered_tools_drop_out_but_routine_survives():
    seq = [{"tool": "set_todos", "args": {"todos": []}, "ok": True}] + SEQ
    for _ in range(3):
        routines.observe("photo run", seq, ok=True)
    stored = routines.lookup("photo run")
    assert stored and [s["tool"] for s in stored] == ["take_photo", "slack_photo"]


# ─── Replay path ───────────────────────────────────────────────────────────────

def _ctx() -> RunContext:
    async def emit(_e):
        pass
    return RunContext(emit=emit, interaction=Interaction())


def _fake_run_tools(script):
    """Simulate _run_tools: per call, append the scripted tool_seq entry (or
    nothing, to simulate a declined/blocked call)."""
    calls = {"n": 0}

    async def fake(ctx, blocks):
        i = calls["n"]
        calls["n"] += 1
        entry = script[i]
        if entry is not None:
            ctx.tool_seq.append(entry)
        return []
    return fake


async def _replay(monkeypatch, script, verify="DONE: Sent, sir."):
    for _ in range(3):
        routines.observe("send alice a photo", SEQ, ok=True)

    monkeypatch.setattr(agent, "_run_tools", _fake_run_tools(script))

    async def fake_acreate(**kw):
        class _T:
            type, text = "text", verify
        class _R:
            content = [_T()]
        return _R()
    monkeypatch.setattr(llm, "acreate", fake_acreate)

    ctx = _ctx()
    return await agent._try_routine_replay(ctx, "send alice a photo", ctx.emit,
                                           agent.RunTrace())


async def test_replay_happy_path(monkeypatch):
    out = await _replay(monkeypatch, [dict(s) for s in SEQ])
    assert out == "Sent, sir."
    assert routines.lookup("send alice a photo")     # still valid


async def test_replay_first_step_failure_falls_back_and_invalidates(monkeypatch):
    script = [dict(SEQ[0], ok=False), None]
    out = await _replay(monkeypatch, script)
    assert out is None                                # full loop takes over
    assert routines.lookup("send alice a photo") is None


async def test_replay_failure_after_a_write_reports_instead_of_redoing(monkeypatch):
    # take_photo is NOT read-only; its success means the loop must not rerun it
    script = [dict(SEQ[0]), dict(SEQ[1], ok=False)]
    out = await _replay(monkeypatch, script)
    assert out is not None and "stopped" in out.lower()
    assert routines.lookup("send alice a photo") is None


async def test_replay_decline_stops_honestly_without_invalidation(monkeypatch):
    script = [None, None]                             # gate declined step 1
    out = await _replay(monkeypatch, script)
    assert out is not None and "stopped" in out.lower()
    assert routines.lookup("send alice a photo")      # user choice ≠ broken routine


async def test_replay_verify_fail_without_writes_falls_back(monkeypatch):
    reads = [{"tool": "read_file", "args": {"path": "x"}, "ok": True, "out": "data"},
             {"tool": "web_search", "args": {"query": "y"}, "ok": True, "out": "hits"}]
    for _ in range(3):
        routines.observe("look something up", reads, ok=True)
    monkeypatch.setattr(agent, "_run_tools", _fake_run_tools([dict(r) for r in reads]))

    async def fake_acreate(**kw):
        class _T:
            type, text = "text", "FAIL: wrong data"
        class _R:
            content = [_T()]
        return _R()
    monkeypatch.setattr(llm, "acreate", fake_acreate)
    ctx = _ctx()
    out = await agent._try_routine_replay(ctx, "look something up", ctx.emit,
                                          agent.RunTrace())
    assert out is None
    assert routines.lookup("look something up") is None

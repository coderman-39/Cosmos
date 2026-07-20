"""services.slack_bridge: event routing, dedupe, confirm bridge, chunking,
delivery tool, and gate classification of the two new tools.

No network: _post/_react/_bot_api are replaced with recorders; convstore and
outbox are pointed at tmp paths (never the real ~/.friday while a backend may
be running).
"""

import asyncio
import time
from collections import deque

import pytest

from services import agent, convstore, outbox, watchers
from services import slack_bridge as sb


# The real reaction function — the autouse fixture swaps sb._react for a
# recorder, so scope-handling tests grab the original here at import time.
_REAL_REACT = sb._react


def ev(text, user="U_OWNER", channel="C123", ts=None, thread=None, **kw):
    e = {"type": "message", "user": user, "channel": channel,
         "text": text, "ts": ts or f"{time.time():.6f}"}
    if thread:
        e["thread_ts"] = thread
    e.update(kw)
    return e


@pytest.fixture(autouse=True)
def bridge(monkeypatch, tmp_path):
    """Wire the module globals for a resolved, connected bridge; record posts."""
    monkeypatch.setattr(sb, "_channel", "C123")
    monkeypatch.setattr(sb, "_owner", "U_OWNER")
    monkeypatch.setattr(sb, "_bot_user", "U_BOT")
    monkeypatch.setattr(sb, "_queue", asyncio.Queue())
    monkeypatch.setattr(sb, "_current", None)
    monkeypatch.setattr(sb, "_current_record", None)
    monkeypatch.setattr(sb, "_seen", deque(maxlen=500))
    monkeypatch.setattr(sb, "_activity", deque(maxlen=60))
    monkeypatch.setattr(sb, "_delivery_target", None)
    monkeypatch.setattr(sb, "_reactions_ok", None)
    monkeypatch.setattr(convstore, "DIR", tmp_path / "conversations")
    monkeypatch.setattr(outbox, "FILE", tmp_path / "outbox.jsonl")

    posts = []

    async def fake_post(text, thread_ts, channel=""):
        posts.append({"text": text, "thread": thread_ts})
        return "9.9"

    async def fake_react(ts, name, remove=False, channel=""):
        posts.append({"react": name, "ts": ts})

    monkeypatch.setattr(sb, "_post", fake_post)
    monkeypatch.setattr(sb, "_react", fake_react)
    return posts


# ─── Root-message routing ──────────────────────────────────────────────────────

async def test_plain_root_message_ignored(monkeypatch):
    """Only /cosmos starts a task — a plain channel message must NOT run."""
    monkeypatch.setitem(sb._STATUS, "last_ignored", "")
    await sb._on_message(ev("take a screenshot"))
    assert sb._queue.empty()
    assert "/cosmos" in sb._STATUS["last_ignored"]


async def test_plain_dm_message_ignored(monkeypatch):
    """/cosmos-only applies everywhere, including 1:1 DMs with the bot."""
    monkeypatch.setitem(sb._STATUS, "last_ignored", "")
    await sb._on_message(ev("whats my calendar", channel="D42",
                            channel_type="im"))
    assert sb._queue.empty()


@pytest.mark.parametrize("event", [
    ev("do something", user="U_STRANGER"),           # not the owner
    ev("do something", user="U_BOT"),                # the bot's own post
    ev("do something", bot_id="B1"),                 # any bot-authored message
    ev("do something", channel="C_OTHER"),           # wrong channel
    ev("do something", subtype="message_changed"),   # edits are not commands
    ev("do something", ts=f"{time.time() - 3600:.6f}"),  # stale (reconnect replay)
    ev(""),                                          # empty text
])
async def test_non_commands_ignored(event):
    await sb._on_message(event)
    assert sb._queue.empty()


# ─── /cosmos slash command ─────────────────────────────────────────────────────

def slash(text, user="U_OWNER", channel="C123", **kw):
    p = {"command": "/cosmos", "user_id": user, "channel_id": channel, "text": text}
    p.update(kw)
    return p


async def test_slash_enqueues_and_posts_root(bridge):
    await sb._on_slash(slash("take a screenshot of example.com"))
    cmd = sb._queue.get_nowait()
    assert cmd["text"] == "take a screenshot of example.com"
    assert cmd["thread"] == cmd["ts"] == "9.9"   # threads under the posted root
    assert any("/cosmos" in p.get("text", "") for p in bridge)


async def test_slash_in_dm_runs(bridge):
    await sb._on_slash(slash("whats my calendar", channel="D42"))
    assert sb._queue.get_nowait()["channel"] == "D42"


async def test_slash_from_stranger_ignored():
    await sb._on_slash(slash("hack it", user="U_STRANGER"))
    assert sb._queue.empty()


async def test_slash_empty_text_shows_usage(bridge):
    await sb._on_slash(slash("   "))
    assert sb._queue.empty()
    assert any("Usage" in p.get("text", "") for p in bridge)


async def test_slash_wrong_channel_ignored():
    await sb._on_slash(slash("do it", channel="C_OTHER"))
    assert sb._queue.empty()


async def test_ignored_reason_recorded(monkeypatch):
    monkeypatch.setitem(sb._STATUS, "last_ignored", "")
    await sb._on_message(ev("random note", thread="777.7"))   # unknown thread
    assert "thread" in sb._STATUS["last_ignored"]
    monkeypatch.setitem(sb._STATUS, "last_ignored", "")
    await sb._on_message(ev("do it", subtype="message_changed"))
    assert "subtype" in sb._STATUS["last_ignored"]


async def test_envelope_dedupe(monkeypatch):
    calls = []

    async def spy(event):
        calls.append(event)

    monkeypatch.setattr(sb, "_on_message", spy)
    e = ev("run this")
    await sb._on_envelope({"event_id": "Ev1", "event": e})
    await sb._on_envelope({"event_id": "Ev1", "event": e})   # Slack redelivery
    assert len(calls) == 1                                   # second is deduped


# ─── Thread replies: confirm / ask / follow-up ─────────────────────────────────

def _pending(kind: str) -> tuple[dict, asyncio.Future]:
    inter = agent.Interaction()
    fut = inter.begin(kind, payload={})
    return {"thread": "100.1", "task": None, "interaction": inter}, fut


async def test_confirm_yes_resolves(monkeypatch):
    cur, fut = _pending("confirm")
    monkeypatch.setattr(sb, "_current", cur)
    await sb._on_message(ev("yes", thread="100.1"))
    assert fut.done() and fut.result() == "yes"


async def test_confirm_no_resolves(monkeypatch):
    cur, fut = _pending("confirm")
    monkeypatch.setattr(sb, "_current", cur)
    await sb._on_message(ev("no, don't", thread="100.1"))
    assert fut.done() and fut.result() == "no, don't"


async def test_confirm_unrelated_text_nudges_instead(monkeypatch, bridge):
    cur, fut = _pending("confirm")
    monkeypatch.setattr(sb, "_current", cur)
    await sb._on_message(ev("also check the deploy status", thread="100.1"))
    assert not fut.done()                      # a stray remark must never approve
    assert any("yes" in p.get("text", "") for p in bridge)
    assert sb._queue.empty()


async def test_ask_user_any_reply_resolves(monkeypatch):
    cur, fut = _pending("ask")
    monkeypatch.setattr(sb, "_current", cur)
    await sb._on_message(ev("the Bangalore office one", thread="100.1"))
    assert fut.done() and fut.result() == "the Bangalore office one"


async def test_followup_on_known_thread_enqueued():
    convstore.save(sb._conv_id("C123", "100.1"),
                   [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"}])
    await sb._on_message(ev("now send it to me", thread="100.1"))
    cmd = sb._queue.get_nowait()
    assert cmd["text"] == "now send it to me"
    assert cmd["thread"] == "100.1"            # continues the same conversation


async def test_reply_on_unknown_thread_ignored():
    await sb._on_message(ev("random note", thread="777.7"))
    assert sb._queue.empty()


async def test_followup_while_run_active_on_thread_queued(monkeypatch):
    """A reply in a running task's thread (no pending question) must queue,
    not vanish — the thread's history file doesn't exist until the run saves."""
    inter = agent.Interaction()                    # nothing pending
    monkeypatch.setattr(sb, "_current",
                        {"thread": "100.1", "task": None, "interaction": inter})
    await sb._on_message(ev("also grab the error count", thread="100.1"))
    assert sb._queue.get_nowait()["text"] == "also grab the error count"


async def test_followup_on_activity_known_thread_queued(monkeypatch):
    """Threads from THIS session count as known even before convstore saves."""
    act = deque([{"thread": "200.2", "status": "done"}], maxlen=60)
    monkeypatch.setattr(sb, "_activity", act)
    await sb._on_message(ev("one more thing", thread="200.2"))
    assert sb._queue.get_nowait()["thread"] == "200.2"


async def test_enqueue_while_busy_posts_queue_notice(monkeypatch, bridge):
    inter = agent.Interaction()
    monkeypatch.setattr(sb, "_current",
                        {"thread": "300.3", "task": None, "interaction": inter})
    await sb._on_slash(slash("new task while busy"))
    cmd = sb._queue.get_nowait()
    assert cmd["queued_note"] is True
    assert any("Queued" in p.get("text", "") for p in bridge)


async def test_enqueue_idle_stays_silent(bridge):
    await sb._on_slash(slash("do something now"))
    assert sb._queue.get_nowait()["queued_note"] is False
    assert not any("Queued" in p.get("text", "") for p in bridge)


async def test_react_missing_scope_caches_and_reports_false(monkeypatch):
    calls = []

    async def fake_api(method, params=None, timeout=20):
        calls.append(method)
        return {"ok": False, "error": "missing_scope"}

    monkeypatch.setattr(sb, "_bot_api", fake_api)
    assert await _REAL_REACT("1.0", "eyes") is False
    assert await _REAL_REACT("1.0", "eyes") is False  # cached — no second call
    assert len(calls) == 1


# ─── Stop ──────────────────────────────────────────────────────────────────────

async def test_stop_cancels_active_run(monkeypatch):
    async def _hang():
        await asyncio.sleep(60)

    task = asyncio.get_running_loop().create_task(_hang())
    inter = agent.Interaction()
    monkeypatch.setattr(sb, "_current",
                        {"thread": "100.1", "task": task, "interaction": inter})
    await sb._on_message(ev("stop"))
    await asyncio.sleep(0)
    assert task.cancelled()


async def test_stop_with_nothing_running_reports(bridge):
    await sb._on_message(ev("stop"))
    assert any("Nothing is running" in p.get("text", "") for p in bridge)
    assert sb._queue.empty()


# ─── Output plumbing ───────────────────────────────────────────────────────────

async def test_post_chunks_splits_under_slack_cap(bridge):
    long = "\n".join(f"line {i} " + "x" * 90 for i in range(200))
    await sb._post_chunks(long, "1.0")
    texts = [p["text"] for p in bridge if "text" in p]
    assert len(texts) >= 2
    assert all(len(t) <= sb._MAX_MSG for t in texts)
    assert "line 199" in texts[-1]             # nothing silently dropped


def test_conv_id_is_filename_safe():
    cid = sb._conv_id("C123", "1752403234.123456")
    assert "." not in cid
    assert convstore._sanitize(cid) == cid     # survives convstore unchanged
    # DM and channel threads with the same ts must never share history.
    assert cid != sb._conv_id("D999", "1752403234.123456")


def test_preamble_slim_for_followups():
    first = sb._preamble("do x", follow_up=False)
    again = sb._preamble("do x", follow_up=True)
    assert "slack_deliver" in first and "web_snapshot" in first
    assert len(again) < 120                    # history must not bloat


# ─── slack_deliver tool ────────────────────────────────────────────────────────

async def test_deliver_requires_bridge_context():
    out = await sb._tool_slack_deliver({"text": "hi"}, None)
    assert out.startswith("Error")


async def test_deliver_text_posts_to_thread(monkeypatch, bridge):
    monkeypatch.setattr(sb, "_delivery_target",
                        {"channel": "C123", "thread": "100.1"})
    out = await sb._tool_slack_deliver({"text": "interim update"}, None)
    assert "Posted" in out
    assert any(p.get("thread") == "100.1" for p in bridge)


async def test_deliver_missing_file_errors(monkeypatch):
    monkeypatch.setattr(sb, "_delivery_target",
                        {"channel": "C123", "thread": "100.1"})
    out = await sb._tool_slack_deliver({"file_path": "/nope/gone.png"}, None)
    assert out.startswith("Error")


# ─── Risk-gate classification of the new tools ─────────────────────────────────

@pytest.fixture
def registry_guard():
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


def test_slack_deliver_gate_open(registry_guard):
    sb._register_deliver_tool()
    assert agent.needs_confirmation("slack_deliver",
                                    {"file_path": "/tmp/x.png"}, "ask") is None


def test_web_snapshot_gate_open(registry_guard):
    watchers.register_agent_tools()
    assert agent.needs_confirmation("web_snapshot",
                                    {"url": "https://x.example"}, "ask") is None
    assert "web_snapshot" in agent._READ_ONLY_TOOLS

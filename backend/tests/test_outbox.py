"""Outbox journal (F12a) — the undo/promise foundation.

Every outward action must land in ~/.friday/outbox.jsonl WITH the handle its
inverse needs; the Slack sender must stop discarding chat.postMessage's ts.
"""

import json

import pytest

from services import outbox, slack


@pytest.fixture
def journal(tmp_path, monkeypatch):
    f = tmp_path / "outbox.jsonl"
    monkeypatch.setattr(outbox, "FILE", f)
    return f


# ─── Journal mechanics ─────────────────────────────────────────────────────────

def test_record_and_recent_roundtrip(journal):
    outbox.record("slack", "send", target="alice", summary="hi",
                  handle={"channel": "C1", "ts": "111.222"}, undoable=True)
    outbox.record("imessage", "send", target="+91x", summary="yo")
    recs = outbox.recent()
    assert len(recs) == 2
    assert recs[0]["tool"] == "imessage", "newest first"
    assert recs[1]["handle"] == {"channel": "C1", "ts": "111.222"}


def test_filters(journal):
    outbox.record("slack", "send", handle={"ts": "1"}, undoable=True)
    outbox.record("slack", "status", undoable=False)
    outbox.record("slack", "send", handle={"ts": "2"}, undoable=True)
    assert len(outbox.recent(undoable_only=True)) == 2
    assert len(outbox.recent(action="send")) == 2
    assert len(outbox.recent(action="send", n=1)) == 1
    assert outbox.recent(action="send")[0]["handle"]["ts"] == "2"


def test_corrupt_lines_skipped(journal):
    outbox.record("slack", "send", undoable=True)
    journal.write_text(journal.read_text() + "NOT JSON{{{\n")
    outbox.record("slack", "react", undoable=True)
    recs = outbox.recent()
    assert [r["action"] for r in recs] == ["react", "send"]


def test_record_never_raises(monkeypatch):
    monkeypatch.setattr(outbox, "FILE", None)   # .parent explodes → swallowed
    outbox.record("slack", "send")              # must not raise
    assert outbox.recent() == []


# ─── Slack capture ─────────────────────────────────────────────────────────────

async def _fake_resolve(target):
    return "C42", []


async def test_send_message_journals_ts(journal, monkeypatch):
    async def fake_api(method, params=None, timeout=20):
        assert method == "chat.postMessage"
        return {"ok": True, "ts": "1720.5", "channel": "C42"}

    monkeypatch.setattr(slack, "_resolve_channel", _fake_resolve)
    monkeypatch.setattr(slack, "_api", fake_api)
    ok, msg = await slack.send_message("alice", "hello there")
    assert ok and "ts 1720.5" in msg
    rec = outbox.recent(n=1)[0]
    assert rec["action"] == "send" and rec["undoable"] is True
    assert rec["handle"] == {"channel": "C42", "ts": "1720.5"}
    assert rec["summary"] == "hello there"


async def test_failed_send_journals_nothing(journal, monkeypatch):
    async def fake_api(method, params=None, timeout=20):
        return {"ok": False, "error": "channel_not_found"}

    monkeypatch.setattr(slack, "_resolve_channel", _fake_resolve)
    monkeypatch.setattr(slack, "_api", fake_api)
    ok, _ = await slack.send_message("alice", "hello")
    assert not ok
    assert outbox.recent() == []


async def test_reaction_journals_handle(journal, monkeypatch):
    async def fake_api(method, params=None, timeout=20):
        if method == "conversations.history":
            return {"ok": True, "messages": [{"ts": "99.1"}]}
        return {"ok": True}

    monkeypatch.setattr(slack, "_resolve_channel", _fake_resolve)
    monkeypatch.setattr(slack, "_api", fake_api)
    ok, _ = await slack.add_reaction("alice", "tada")
    assert ok
    rec = outbox.recent(n=1)[0]
    assert rec["action"] == "react"
    assert rec["handle"] == {"channel": "C42", "ts": "99.1", "emoji": "tada"}


async def test_set_status_snapshots_previous(journal, monkeypatch):
    async def fake_api(method, params=None, timeout=20):
        if method == "users.profile.get":
            return {"ok": True, "profile": {"status_text": "lunch",
                                            "status_emoji": ":taco:",
                                            "status_expiration": 0}}
        return {"ok": True}

    monkeypatch.setattr(slack, "_api", fake_api)
    ok, _ = await slack.set_status("in a meeting", "calendar")
    assert ok
    rec = outbox.recent(n=1)[0]
    assert rec["action"] == "status" and rec["undoable"] is True
    assert rec["handle"]["prev_text"] == "lunch"
    assert rec["handle"]["prev_emoji"] == ":taco:"


async def test_dnd_off_captures_time_left(journal, monkeypatch):
    import time as _time
    now = _time.time()

    async def fake_api(method, params=None, timeout=20):
        if method == "dnd.info":
            return {"ok": True, "snooze_enabled": True,
                    "snooze_endtime": now + 1800}
        return {"ok": True}

    monkeypatch.setattr(slack, "_api", fake_api)
    ok, _ = await slack.set_dnd(-1)
    assert ok
    rec = outbox.recent(n=1)[0]
    assert rec["action"] == "dnd_off" and rec["undoable"] is True
    assert 28 <= rec["handle"]["inverse_minutes"] <= 30

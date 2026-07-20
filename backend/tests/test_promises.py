"""Promise tracking (F6): commitment extraction from the outbox journal.

Contracts:
  - sweep() only reads sends NEWER than the cursor, extracts conservative
    commitments via the fast model, auto-resolves fulfilled ones, and NEVER
    advances the cursor on an unparseable extraction (retry next sweep).
  - resolve/dismiss close by id; format_open shows age + due hints.
  - Everything is guarded: bad LLM output degrades, never crashes.
"""

import json

import pytest

from services import llm, outbox, promises


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(promises, "FILE", tmp_path / "promises.json")
    monkeypatch.setattr(outbox, "FILE", tmp_path / "outbox.jsonl")
    return tmp_path


def _fake_llm(monkeypatch, payload):
    class _Resp:
        content = []

    async def fake_acreate(**kwargs):
        return _Resp()

    monkeypatch.setattr(llm, "acreate", fake_acreate)
    monkeypatch.setattr(llm, "extract_text",
                        lambda resp: json.dumps(payload) if isinstance(payload, dict) else payload)


async def test_sweep_extracts_and_advances_cursor(store, monkeypatch):
    outbox.record("slack", "send", target="bob",
                  summary="I'll approve your PR today", handle={"ts": "1.0"},
                  undoable=True)
    outbox.record("slack", "send", target="alice", summary="lol nice one")
    _fake_llm(monkeypatch, {"new": [{"msg": 1, "text": "Approve Bob's PR",
                                     "due_hint": "today"}],
                            "resolved_ids": []})
    out = await promises.sweep()
    assert "1 new promise(s)" in out
    open_ = promises.list_open()
    assert len(open_) == 1
    assert open_[0]["to"] == "bob"
    assert open_[0]["due_hint"] == "today"

    # Second sweep: cursor advanced → nothing new to scan.
    out = await promises.sweep()
    assert "No new sent messages" in out


async def test_sweep_keeps_oldest_never_drops(store, monkeypatch):
    """More new sends than the per-sweep cap: the cursor must advance only over
    the OLDEST scanned batch, so the rest are picked up next sweep — never lost.
    Regression: the old code kept the newest 30 but jumped the cursor past the
    dropped oldest ones."""
    monkeypatch.setattr(promises, "_MAX_SWEEP_MSGS", 3)
    for i in range(5):
        outbox.record("slack", "send", target="a", summary=f"send number {i}")

    # First sweep sees the 3 OLDEST (0,1,2) — the commitment is in send 0.
    seen_batches = []

    async def fake_acreate(**kwargs):
        msgs = kwargs["messages"][0]["content"]
        seen_batches.append(msgs)

        class _R:
            content = []
        return _R()

    monkeypatch.setattr(promises.llm, "acreate", fake_acreate)
    monkeypatch.setattr(promises.llm, "extract_text",
                        lambda r: json.dumps({"new": [], "resolved_ids": []}))
    await promises.sweep()
    assert "send number 0" in seen_batches[-1]
    assert "send number 4" not in seen_batches[-1], "must not skip to newest"
    # Second sweep picks up the remaining newer sends (3,4).
    await promises.sweep()
    assert "send number 4" in seen_batches[-1]


async def test_no_duplicate_promises_within_one_sweep(store, monkeypatch):
    """Two near-identical commitments in ONE sweep, both extracted to the same
    text, must dedup against the in-memory batch (not stale disk state)."""
    outbox.record("slack", "send", target="a", summary="I'll send the doc")
    outbox.record("slack", "send", target="a", summary="I will send the doc")
    _fake_llm(monkeypatch, {"new": [{"msg": 1, "text": "Send the doc", "due_hint": "none"},
                                    {"msg": 2, "text": "Send the doc", "due_hint": "none"}],
                            "resolved_ids": []})
    await promises.sweep()
    assert len(promises.list_open()) == 1


async def test_sweep_auto_resolves(store, monkeypatch):
    outbox.record("slack", "send", target="bob",
                  summary="I'll approve your PR today")
    _fake_llm(monkeypatch, {"new": [{"msg": 1, "text": "Approve the PR",
                                     "due_hint": "today"}], "resolved_ids": []})
    await promises.sweep()
    pid = promises.list_open()[0]["id"]

    outbox.record("slack", "send", target="bob", summary="approved it!")
    _fake_llm(monkeypatch, {"new": [], "resolved_ids": [pid]})
    out = await promises.sweep()
    assert "1 auto-resolved" in out
    assert promises.list_open() == []


async def test_unparseable_extraction_keeps_cursor(store, monkeypatch):
    outbox.record("slack", "send", target="a", summary="I'll do the thing")
    _fake_llm(monkeypatch, "I am not JSON, sorry!")
    out = await promises.sweep()
    assert out.startswith("Error")
    # Cursor NOT advanced — the send gets another chance.
    _fake_llm(monkeypatch, {"new": [{"msg": 1, "text": "Do the thing",
                                     "due_hint": "none"}], "resolved_ids": []})
    out = await promises.sweep()
    assert "1 new promise(s)" in out


async def test_duplicate_promises_not_added_twice(store, monkeypatch):
    outbox.record("slack", "send", target="a", summary="I'll send the doc")
    _fake_llm(monkeypatch, {"new": [{"msg": 1, "text": "Send the doc",
                                     "due_hint": "none"}], "resolved_ids": []})
    await promises.sweep()
    outbox.record("slack", "send", target="a", summary="yes I'll send the doc")
    await promises.sweep()
    assert len(promises.list_open()) == 1


def test_resolve_and_dismiss(store):
    promises._save({"promises": [
        {"id": "abc123", "to": "alice", "text": "Send report",
         "made_at": "2026-07-08T10:00:00", "status": "open"}], "cursor": ""})
    assert "marked done" in promises.resolve("abc123")
    assert promises.list_open() == []
    assert "No open promise" in promises.resolve("abc123")
    assert "No open promise" in promises.resolve("nonexistent")


def test_format_open_shows_age_and_due(store):
    promises._save({"promises": [
        {"id": "p1", "to": "alice", "text": "Send report",
         "made_at": "2026-07-01T10:00:00", "due_hint": "today", "status": "open"}],
        "cursor": ""})
    out = promises.format_open()
    assert "[p1] to alice — Send report" in out
    assert "d ago" in out and "due today" in out


def test_corrupt_state_degrades_to_empty(store):
    promises.FILE.write_text("{corrupt json")
    assert promises.list_open() == []
    assert promises.format_open() == "No open promises."

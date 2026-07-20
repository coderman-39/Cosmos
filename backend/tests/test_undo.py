"""General undo (F12): inverse registry over the outbox journal.

Contracts:
  - undo_last reverses the NEWEST undoable entry only, once — a tombstone
    prevents double-undo; the next undo_last moves to the previous action.
  - Each Slack action maps to its true inverse (chat.delete,
    reactions.remove, profile restore, DND inverse).
  - Irreversible tools are refused honestly; a failed inverse leaves no
    tombstone (retryable).
  - The apply path is gated in ask mode; preview is free.
"""

import pytest

from services import agent, outbox, slack, undo


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "FILE", tmp_path / "outbox.jsonl")
    return tmp_path


@pytest.fixture
def slack_api(monkeypatch):
    calls = []

    async def fake_api(method, params=None, timeout=20):
        calls.append((method, params or {}))
        return {"ok": True}

    monkeypatch.setattr(slack, "_api", fake_api)
    return calls


async def test_undo_send_deletes_message(journal, slack_api):
    outbox.record("slack", "send", target="alice", summary="oops wrong person",
                  handle={"channel": "C1", "ts": "111.222"}, undoable=True)
    out = await undo.undo_last()
    assert out.startswith("Undone:") and "message deleted" in out
    assert slack_api == [("chat.delete", {"channel": "C1", "ts": "111.222"})]
    # Tombstone: a second undo has nothing left.
    assert (await undo.undo_last()).startswith("Error: nothing undoable")


async def test_undo_walks_backwards_through_history(journal, slack_api):
    outbox.record("slack", "send", target="a", summary="first",
                  handle={"channel": "C1", "ts": "1"}, undoable=True)
    outbox.record("slack", "react", target="a", summary=":tada:",
                  handle={"channel": "C1", "ts": "9", "emoji": "tada"},
                  undoable=True)
    out = await undo.undo_last()                      # newest first: reaction
    assert "react" in out
    assert slack_api[-1][0] == "reactions.remove"
    out = await undo.undo_last()                      # then the send
    assert "send" in out
    assert slack_api[-1][0] == "chat.delete"


async def test_undo_status_restores_previous(journal, slack_api):
    outbox.record("slack", "status", summary=":calendar: in a meeting",
                  handle={"prev_text": "lunch", "prev_emoji": ":taco:",
                          "prev_expiration": 0}, undoable=True)
    out = await undo.undo_last()
    assert "previous status restored" in out
    method, params = slack_api[-1]
    assert method == "users.profile.set"
    assert "lunch" in params["profile"]


async def test_undo_dnd_inverses(journal, slack_api):
    outbox.record("slack", "dnd_off", handle={"inverse_minutes": 25},
                  undoable=True)
    out = await undo.undo_last()
    assert "25 min" in out
    assert slack_api[-1] == ("dnd.setSnooze", {"num_minutes": 25})


async def test_transient_failure_leaves_no_tombstone(journal, monkeypatch):
    outbox.record("slack", "send", target="a", summary="x",
                  handle={"channel": "C1", "ts": "1"}, undoable=True)

    async def broken_api(method, params=None, timeout=20):
        return {"ok": False, "error": "ratelimited"}   # transient, retryable

    monkeypatch.setattr(slack, "_api", broken_api)
    out = await undo.undo_last()
    assert out.startswith("Error")
    # No tombstone → still visible for retry.
    assert len(undo.undoable_entries()) == 1


async def test_already_gone_message_tombstones_and_unblocks(journal, monkeypatch):
    """A manually-deleted message (message_not_found) must resolve as idempotent
    success — not retry forever and block older undoable actions."""
    outbox.record("slack", "status", summary="old",
                  handle={"prev_text": "was", "prev_emoji": "", "prev_expiration": 0},
                  undoable=True)
    outbox.record("slack", "send", target="a", summary="oops",
                  handle={"channel": "C1", "ts": "9"}, undoable=True)

    calls = []

    async def api(method, params=None, timeout=20):
        calls.append(method)
        if method == "chat.delete":
            return {"ok": False, "error": "message_not_found"}
        return {"ok": True}

    monkeypatch.setattr(slack, "_api", api)
    out = await undo.undo_last()                 # newest = the send
    assert out.startswith("Undone:") and "already gone" in out
    # The poison entry is tombstoned, so the older status is now reachable.
    out = await undo.undo_last()
    assert "status restored" in out
    assert "users.profile.set" in calls


async def test_non_slack_tools_refused_honestly(journal):
    # Force an undoable entry from a tool with no inverse (defensive).
    outbox.record("calendar", "create", target="standup", handle={"uid": "x"},
                  undoable=True)
    out = await undo.undo_last()
    assert out.startswith("Error") and "no inverse" in out


def test_preview_lists_newest_first(journal):
    outbox.record("slack", "send", target="a", summary="msg-alpha",
                  handle={"ts": "1", "channel": "C"}, undoable=True)
    outbox.record("slack", "send", target="b", summary="msg-bravo",
                  handle={"ts": "2", "channel": "C"}, undoable=True)
    out = undo.preview()
    assert out.index("msg-bravo") < out.index("msg-alpha")
    outbox.FILE.unlink()
    assert "Nothing undoable" in undo.preview()


def test_apply_gated_preview_free():
    assert agent.needs_confirmation("undo_last", {"action": "apply"}, "ask")
    assert agent.needs_confirmation("undo_last", {"action": "preview"}, "ask") is None
    # Full mode: reversal runs free, like other outward-but-recoverable actions.
    assert agent.needs_confirmation("undo_last", {"action": "apply"}, "full") is None

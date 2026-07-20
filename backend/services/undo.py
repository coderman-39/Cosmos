"""General undo — reverse the last outward action (F12).

Walks the outbox journal for the newest UNDOABLE entry that hasn't already
been undone, and applies its inverse (Slack message → chat.delete, reaction
→ remove, status → restore previous, DND → inverse toggle). A successful
undo appends an 'undone' tombstone so the same entry is never reversed
twice.

Honest about limits: iMessages, emails, bash, and device commands cannot be
unsent — those journal entries are undoable=False and never surface here.
(File overwrites have their own snapshot-restore in ~/.friday/undo/.)
"""

from services import outbox, slack
from services import google as google_svc

_SCAN = 50


def _undone_keys() -> set[tuple]:
    return {(r.get("handle", {}).get("orig_ts"),
             r.get("handle", {}).get("orig_action"))
            for r in outbox.recent(n=_SCAN, action="undone")}


def undoable_entries(n: int = 5) -> list[dict]:
    """Newest-first undoable journal entries that haven't been undone yet."""
    undone = _undone_keys()
    out = []
    for r in outbox.recent(n=_SCAN, undoable_only=True):
        if (r.get("ts"), r.get("action")) in undone:
            continue
        out.append(r)
        if len(out) >= n:
            break
    return out


def _describe(e: dict) -> str:
    target = f" to {e['target']}" if e.get("target") else ""
    summary = f' — "{e.get("summary", "")[:80]}"' if e.get("summary") else ""
    return f"{e.get('tool', '?')} {e.get('action', '?')}{target}{summary}"


def preview(n: int = 5) -> str:
    entries = undoable_entries(n)
    if not entries:
        return ("Nothing undoable in the journal. (Reversible: Slack messages, "
                "reactions, status, DND. Not reversible: iMessage, email, bash, "
                "device actions.)")
    lines = ["Undoable, newest first (apply reverses #1):"]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. [{e.get('ts', '')[:16].replace('T', ' ')}] {_describe(e)}")
    return "\n".join(lines)


async def undo_last() -> str:
    """Reverse the newest not-yet-undone outward action. Never raises."""
    entries = undoable_entries(1)
    if not entries:
        return ("Error: nothing undoable — the journal has no reversible "
                "actions that haven't already been undone.")
    e = entries[0]
    try:
        if e.get("tool") == "slack":
            ok, msg = await slack.undo_action(e)
        elif e.get("tool") == "google":
            ok, msg = await google_svc.undo_action(e)
        else:
            ok, msg = False, f"'{e.get('tool')}' actions have no inverse"
    except Exception as ex:
        ok, msg = False, str(ex)[:160]
    if ok:
        outbox.mark_undone(e.get("ts", ""), e.get("action", ""))
        return f"Undone: {_describe(e)} — {msg}."
    return f"Error: couldn't undo {_describe(e)} — {msg}"

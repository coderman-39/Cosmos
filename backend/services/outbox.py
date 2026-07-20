"""Outbox journal — append-only record of Cosmos's OUTWARD actions, with the
handles needed to undo them later (and to mine sent messages for commitments).

One JSON object per line in ~/.friday/outbox.jsonl:
    {ts, tool, action, target, summary, handle: {...}, undoable}

`handle` carries whatever the inverse operation needs (Slack channel+ts for
chat.delete, previous status for restore, …). Strictly best-effort: a full
disk or bad line must never break the send it records.
"""

import json
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

FILE = Path.home() / ".friday" / "outbox.jsonl"

# The promise-sweep cursor is a strict `>` on ts — two records must never
# share a timestamp or the later one is shadowed forever.
_last_ts = ""


def _next_ts() -> str:
    global _last_ts
    now = datetime.now()
    ts = now.isoformat(timespec="milliseconds")
    if ts <= _last_ts:
        try:
            prev = datetime.fromisoformat(_last_ts)
            ts = (prev + timedelta(milliseconds=1)).isoformat(timespec="milliseconds")
        except Exception:
            pass
    _last_ts = ts
    return ts


def record(tool: str, action: str, target: str = "", summary: str = "",
           handle: dict | None = None, undoable: bool = False) -> None:
    """Append one outward action. Never raises."""
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _next_ts(),
               "tool": tool, "action": action, "target": target,
               "summary": (summary or "")[:300], "handle": handle or {},
               "undoable": bool(undoable)}
        with FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def recent(n: int = 20, undoable_only: bool = False,
           action: str = "") -> list[dict]:
    """Last `n` outward actions, newest first. Never raises.
    `undoable_only` filters to entries an undo could act on; `action`
    filters by action name (e.g. "send" for promise mining).
    Uses a bounded tail read so growing logs don't blow memory."""
    try:
        overscan = n * 4 if (undoable_only or action) else n
        tail = deque(FILE.open(encoding="utf-8"), maxlen=overscan)
    except Exception:
        return []
    out: list[dict] = []
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if undoable_only and not rec.get("undoable"):
            continue
        if action and rec.get("action") != action:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out


def mark_undone(ts: str, action: str) -> None:
    """Flag an entry as undone (append-only tombstone; recent() readers can
    join on ts+action if they need to hide undone items). Never raises."""
    record("undo", "undone", target=action, summary=f"undid {action} @ {ts}",
           handle={"orig_ts": ts, "orig_action": action}, undoable=False)

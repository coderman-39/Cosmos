"""Conversation-history persistence for COSMOS — one file PER conversation.

The frontend manages multiple conversations (ids in localStorage); each gets
its own history file at ~/.friday/conversations/<id>.json so "new chat"
really is a fresh context and switching restores the right one. The old
single-file ~/.friday/history.json is migrated to the 'default' id once.

All IO is guarded — persistence failures must never crash the app.
"""

import json
import re
import time
from pathlib import Path

from services import atomicio

DIR = Path.home() / ".friday" / "conversations"
LEGACY_PATH = Path.home() / ".friday" / "history.json"
DEFAULT_ID = "default"

# Slack conversations (one root message + its thread) are flushed after this
# much inactivity: reply in a thread whose last message is older than this and
# the prior context is forgotten — a fresh conversation. mtime is refreshed on
# every save(), so this is idle time since the last message, not thread age.
# Scoped to slack-* ids only; web-HUD chats are managed explicitly and never
# auto-expire.
_SLACK_TTL_S = 3 * 86400          # 3 days

# Mirror of agent._HISTORY_CAP — kept local so this module stays standalone.
_CAP = 16

_VALID_ROLES = {"user", "assistant"}

_migrated = False


def _sanitize(conv_id: str) -> str:
    """Conversation ids come from the frontend — they become filenames, so
    strip anything path-like."""
    clean = re.sub(r"[^A-Za-z0-9_\-]", "", str(conv_id or ""))[:64]
    return clean or DEFAULT_ID


def _path(conv_id: str) -> Path:
    return DIR / f"{_sanitize(conv_id)}.json"


def _migrate_legacy() -> None:
    """One-time move of the pre-multi-conversation history.json → default.json."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if LEGACY_PATH.exists():
            DIR.mkdir(parents=True, exist_ok=True)
            dest = DIR / f"{DEFAULT_ID}.json"
            if not dest.exists():
                LEGACY_PATH.rename(dest)
            else:
                LEGACY_PATH.unlink(missing_ok=True)
    except Exception as e:
        print(f"[convstore] legacy migration failed (non-fatal): {e}")


def _expired(conv_id: str) -> bool:
    """True if this is a Slack conversation whose file exists but hasn't been
    touched within _SLACK_TTL_S. Non-slack ids never expire."""
    if not str(conv_id).startswith("slack-"):
        return False
    try:
        st = _path(conv_id).stat()
    except Exception:
        return False
    return (time.time() - st.st_mtime) > _SLACK_TTL_S


def exists(conv_id: str) -> bool:
    """True if a conversation file is present (any age). Lets the bridge still
    recognize a thread whose context has been flushed, so a reply runs fresh
    instead of being treated as an unknown thread."""
    try:
        return _path(conv_id).exists()
    except Exception:
        return False


def _valid_entry(e) -> bool:
    return (
        isinstance(e, dict)
        and e.get("role") in _VALID_ROLES
        and isinstance(e.get("content"), str)
    )


def load(conv_id: str = DEFAULT_ID) -> list[dict]:
    """Return the persisted history for `conv_id`.

    Missing, corrupt, or wrong-shaped files yield [] — never an exception.
    Only well-formed {"role": "user"/"assistant", "content": str} entries
    survive, capped to the most recent _CAP.
    """
    _migrate_legacy()
    if _expired(conv_id):                 # inactive > TTL → flush and forget
        delete(conv_id)
        return []
    try:
        raw = json.loads(_path(conv_id).read_text())
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    entries = [
        {"role": e["role"], "content": e["content"]}
        for e in raw
        if _valid_entry(e)
    ]
    return entries[-_CAP:]


def save(conv_id: str, history: list[dict]) -> None:
    """Atomically persist `history` for `conv_id` (invalid entries dropped,
    last _CAP kept)."""
    _migrate_legacy()
    try:
        entries = [
            {"role": e["role"], "content": e["content"]}
            for e in history
            if _valid_entry(e)
        ][-_CAP:]
        path = _path(conv_id)
        if not atomicio.write_json_atomic(path, entries, indent=2):
            print(f"[convstore] save failed (non-fatal): could not write {path.name}")
    except Exception as e:
        print(f"[convstore] save failed (non-fatal): {e}")


def delete(conv_id: str) -> None:
    """Remove a conversation's file (frontend deleted/dropped it)."""
    try:
        _path(conv_id).unlink(missing_ok=True)
    except Exception as e:
        print(f"[convstore] delete failed (non-fatal): {e}")


def clear(conv_id: str = DEFAULT_ID) -> None:
    """Delete the persisted conversation (new-chat reset)."""
    delete(conv_id)

"""Routine replay cache (SPEED_PLAN 4.1) — learned tool-sequence replay.

The 3rd identical successful run of the same normalized phrase with the same
exact tool sequence makes that phrase a ROUTINE: the next invocation replays
the tools directly through the existing risk gate (one at a time, in order,
confirms and all) with a single fast-model verify at the end — no agent-loop
LLM turns. A 15-25s compound command drops to its raw tool time (~8-10s).

Safety posture: everything here is advisory. Any mismatch, any tool failure,
any doubt → the caller falls back to the full agent loop and the routine is
invalidated. Sequences containing interactive/self-modifying tools, oversized
args, or anything secret-shaped are never stored at all. Args are exact-match
by hash, so date/ID-bearing commands simply never converge into routines.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from services import atomicio, llm

FILE = Path.home() / ".friday" / "routines.json"

ENABLED = os.getenv("FRIDAY_ROUTINES", "1").lower() not in ("0", "false", "no")
REQUIRED_RUNS = max(2, int(os.getenv("FRIDAY_ROUTINE_RUNS", "3")))
_MIN_STEPS, _MAX_STEPS = 2, 6
_MAX_PAYLOAD_CHARS = 4000
_MAX_ENTRIES = 100

# Present anywhere in the sequence → the phrase can never become a routine.
_BLOCK_TOOLS = {"ask_user", "propose_plan", "spawn_agents", "save_skill"}
# Dropped from the captured sequence (harmless, but replaying them is noise).
_FILTER_TOOLS = {"set_todos", "remember_fact"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(store: dict) -> None:
    if not atomicio.write_json_atomic(FILE, store, indent=1):
        print("[routines] write failed (non-fatal)")


def _clean_seq(seq: list[dict]) -> list[dict] | None:
    """Filter/validate a run's tool sequence into storable form, or None."""
    steps = [{"tool": s["tool"], "args": s.get("args") or {}}
             for s in seq
             if s.get("ok") and s.get("tool") not in _FILTER_TOOLS]
    if len(steps) != len([s for s in seq if s.get("tool") not in _FILTER_TOOLS]):
        return None                                  # a real step failed
    if any(s["tool"] in _BLOCK_TOOLS for s in steps):
        return None
    if any(s["args"].get("_oversize") for s in steps):
        return None
    if not (_MIN_STEPS <= len(steps) <= _MAX_STEPS):
        return None
    return steps


def observe(phrase: str, seq: list[dict], ok: bool) -> None:
    """Record a completed run. Identical (phrase, sequence) runs accumulate a
    count; a different sequence for a known phrase resets it. Never raises."""
    if not ENABLED:
        return
    try:
        key = _norm(phrase)
        if not key or len(key) > 200:
            return
        store = _load()
        if not ok:
            store.pop(key, None)     # behavior changed / failed — start over
            _save(store)
            return
        steps = _clean_seq(seq)
        if steps is None:
            return
        payload = json.dumps(steps, sort_keys=True, default=str)
        if len(payload) > _MAX_PAYLOAD_CHARS or llm._SECRET_RE.search(payload):
            return
        h = hashlib.sha1(payload.encode()).hexdigest()
        entry = store.get(key)
        if isinstance(entry, dict) and entry.get("hash") == h:
            entry["count"] = int(entry.get("count", 0)) + 1
        else:
            entry = {"hash": h, "seq": steps, "count": 1}
        entry["last"] = time.time()
        store[key] = entry
        if len(store) > _MAX_ENTRIES:
            for k, _ in sorted(store.items(),
                               key=lambda kv: kv[1].get("last", 0))[:len(store) - _MAX_ENTRIES]:
                store.pop(k, None)
        _save(store)
    except Exception:
        pass


def lookup(phrase: str) -> list[dict] | None:
    """The replayable sequence for this phrase, or None if not yet earned."""
    if not ENABLED:
        return None
    try:
        entry = _load().get(_norm(phrase))
        if (isinstance(entry, dict) and entry.get("count", 0) >= REQUIRED_RUNS
                and isinstance(entry.get("seq"), list) and entry["seq"]):
            return entry["seq"]
    except Exception:
        pass
    return None


def invalidate(phrase: str) -> None:
    """A replay went wrong — the phrase must re-earn routine status."""
    try:
        store = _load()
        if store.pop(_norm(phrase), None) is not None:
            _save(store)
    except Exception:
        pass

"""COSMOS's long-term memory — one shared module (main.py + agent tools).

~/.friday/memory.json holds durable, user-level facts:
  corrections     heard→corrected speech fixes (lowercased keys)
  preferences     lasting user preferences ("prefers PRs squashed")
  people          facts about people ("Vinay H — teammate, handles CI")
  projects        facts about ongoing work
  learned_apps    app-specific quirks Cosmos discovered
  frequent_tasks  success counts for proactive suggestions

Writes are atomic (tmp + os.replace); a corrupt file is quarantined, never
silently discarded. All IO is guarded — memory must never crash the app.
"""

import json
from datetime import datetime
from pathlib import Path

from services import atomicio

FILE = Path.home() / ".friday" / "memory.json"

DEFAULT: dict = {
    "corrections": {}, "preferences": {}, "people": {}, "projects": {},
    "learned_apps": {}, "frequent_tasks": [],
}

# None = not loaded yet (a falsy-{} sentinel would re-read on every call once
# memory is legitimately empty).
_cache: dict | None = None

_FACT_KINDS = {"preference": "preferences", "person": "people",
               "project": "projects", "app": "learned_apps"}


def load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    FILE.parent.mkdir(parents=True, exist_ok=True)
    if FILE.exists():
        try:
            _cache = json.loads(FILE.read_text())
        except Exception:
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                FILE.rename(FILE.with_name(f"memory.corrupt-{ts}.json"))
                print(f"[MEMORY] corrupt memory.json quarantined to memory.corrupt-{ts}.json")
            except Exception:
                pass
            _cache = json.loads(json.dumps(DEFAULT))
    else:
        _cache = json.loads(json.dumps(DEFAULT))
    return _cache


def save(data: dict) -> None:
    """Atomic write — a crash mid-write must never truncate the whole file."""
    global _cache
    _cache = data
    if not atomicio.write_json_atomic(FILE, data, indent=2):
        print("[MEMORY] save failed (non-fatal): could not write memory.json")


# ─── Speech corrections ────────────────────────────────────────────────────────

def record_correction(heard: str, corrected: str) -> None:
    """heard side lowercased for matching; corrected side KEEPS case so proper
    names survive the substitution."""
    mem = load()
    mem.setdefault("corrections", {})[heard.lower().strip()] = corrected.strip()
    save(mem)


def get_corrections() -> dict:
    return load().get("corrections", {})


# ─── Facts (remember_fact / forget_fact tools) ─────────────────────────────────

def remember(kind: str, key: str, value: str) -> str:
    """Store a durable fact. Returns a confirmation string for the tool result."""
    bucket = _FACT_KINDS.get((kind or "").lower().strip())
    if not bucket:
        return f"Error: unknown kind '{kind}' — use preference|person|project|app."
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return "Error: both key and value are required."
    mem = load()
    section = mem.setdefault(bucket, {})
    section[key] = value
    # Bound each section so memory.json can't grow without limit.
    if len(section) > 100:
        for k in list(section)[: len(section) - 100]:
            section.pop(k, None)
    save(mem)
    return f"Remembered ({kind}): {key} = {value}"


def forget(key: str) -> str:
    """Remove a fact by key from whichever section holds it."""
    key = (key or "").strip()
    mem = load()
    hits = []
    for bucket in _FACT_KINDS.values():
        if key in mem.get(bucket, {}):
            mem[bucket].pop(key, None)
            hits.append(bucket)
    if not hits:
        return f"No stored fact under '{key}'."
    save(mem)
    return f"Forgot '{key}' (from {', '.join(hits)})."


# ─── Task frequency (proactive suggestions) ────────────────────────────────────

def record_task(task: str, success: bool) -> None:
    """Track successful tasks: count + hour-of-day histogram, full text kept."""
    if not success:
        return
    mem = load()
    tasks = mem.setdefault("frequent_tasks", [])
    key = task[:200]
    entry = next((t for t in tasks if t.get("task") == key), None)
    hour = datetime.now().hour
    if entry:
        entry["count"] = entry.get("count", 0) + 1
        hours = entry.setdefault("hours", [0] * 24)
        if len(hours) == 24:
            hours[hour] += 1
        entry["last"] = datetime.now().isoformat(timespec="seconds")
    else:
        hours = [0] * 24
        hours[hour] = 1
        tasks.append({"task": key, "count": 1, "hours": hours,
                      "last": datetime.now().isoformat(timespec="seconds")})
    mem["frequent_tasks"] = sorted(tasks, key=lambda x: x.get("count", 0),
                                   reverse=True)[:50]
    save(mem)


# ─── Prompt snapshot ───────────────────────────────────────────────────────────

def snapshot_for_prompt() -> str:
    """Compact JSON of what the model should see every turn: preferences and
    people/project/app facts (all durable, small), plus top frequent tasks."""
    mem = load()
    snap = {
        "preferences": mem.get("preferences", {}),
        "people": mem.get("people", {}),
        "projects": mem.get("projects", {}),
        "app_quirks": mem.get("learned_apps", {}),
        "frequent_tasks": [t.get("task") for t in mem.get("frequent_tasks", [])[:10]],
    }
    return json.dumps({k: v for k, v in snap.items() if v}, ensure_ascii=False)

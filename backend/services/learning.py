"""Cosmos's self-learning loops — three feedback channels that make her
measurably smarter on THIS machine over time:

  lessons      "WHEN <situation> DO <better approach>" lines extracted from
               failed runs (by the fast model), deduped, top-5 injected into
               the volatile prompt. ~/.friday/lessons.json
  tool health  rolling success window per tool; chronically failing tools are
               flagged in the prompt so the model prefers alternatives (and
               TCC-permission errors name the fix). ~/.friday/tool_stats.json
  route memory which commands turned out to be pure reads → repeated
               phrasings converge on the fast model. ~/.friday/route_memory.json

Everything is guarded; learning must never break or slow a run.
"""

import asyncio
import difflib
import json
import re
from collections import OrderedDict, deque
from pathlib import Path

from services import atomicio

_DIR = Path.home() / ".friday"

LESSONS_FILE = _DIR / "lessons.json"
STATS_FILE   = _DIR / "tool_stats.json"
ROUTES_FILE  = _DIR / "route_memory.json"

_MAX_LESSONS = 40
_STATS_WINDOW = 20
_MAX_ROUTES = 300


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    if not atomicio.write_json_atomic(path, data, indent=1):
        print(f"[learning] write {path.name} failed (non-fatal)")


# ─── Lessons from failed runs ──────────────────────────────────────────────────

def add_lesson(lesson: str) -> None:
    """Store one 'WHEN … DO …' lesson; near-duplicates bump a count instead
    of piling up. Bounded to _MAX_LESSONS by count."""
    lesson = (lesson or "").strip()
    if not lesson or len(lesson) > 250:
        return
    lessons = _read_json(LESSONS_FILE, [])
    if not isinstance(lessons, list):
        lessons = []
    for entry in lessons:
        if difflib.SequenceMatcher(
                None, entry.get("text", "").lower(), lesson.lower()).ratio() > 0.8:
            entry["count"] = entry.get("count", 1) + 1
            break
    else:
        lessons.append({"text": lesson, "count": 1})
    lessons = sorted(lessons, key=lambda e: e.get("count", 0), reverse=True)[:_MAX_LESSONS]
    _write_json(LESSONS_FILE, lessons)


def top_lessons(n: int = 5) -> list[str]:
    lessons = _read_json(LESSONS_FILE, [])
    if not isinstance(lessons, list):
        return []
    return [e.get("text", "") for e in lessons[:n] if e.get("text")]


# ─── Tool-health stats ─────────────────────────────────────────────────────────

# tool → deque of recent 0/1 outcomes (in-memory; persisted write-behind)
_stats: dict[str, deque] = {}
_last_error: dict[str, str] = {}
_stats_loaded = False

_TCC_HINT_RE = re.compile(
    r"assistive access|not authorised|not authorized|screen recording|"
    r"accessibility|camera access|-1743", re.IGNORECASE)


def _load_stats() -> None:
    global _stats_loaded
    if _stats_loaded:
        return
    _stats_loaded = True
    raw = _read_json(STATS_FILE, {})
    if isinstance(raw, dict):
        for tool, entry in raw.items():
            outcomes = entry.get("recent", []) if isinstance(entry, dict) else []
            _stats[tool] = deque([int(bool(o)) for o in outcomes][-_STATS_WINDOW:],
                                 maxlen=_STATS_WINDOW)
            if isinstance(entry, dict) and entry.get("last_error"):
                _last_error[tool] = entry["last_error"]


# record_tool fires on EVERY executed tool; a full-file rewrite per call
# stalls the loop a few ms each turn. Coalesce bursts into one write ~1s
# later — losing <=1s of advisory stats on a hard crash is acceptable.
_flush_handle: "asyncio.TimerHandle | None" = None


def _flush_stats() -> None:
    global _flush_handle
    _flush_handle = None
    _write_json(STATS_FILE, {
        t: {"recent": list(d), "last_error": _last_error.get(t, "")}
        for t, d in _stats.items()
    })


def record_tool(tool: str, ok: bool, error: str = "") -> None:
    global _flush_handle
    _load_stats()
    dq = _stats.setdefault(tool, deque(maxlen=_STATS_WINDOW))
    dq.append(1 if ok else 0)
    if not ok and error:
        _last_error[tool] = error[:200]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _flush_stats()          # no loop (tests / CLI) — write through
        return
    if _flush_handle is None:
        _flush_handle = loop.call_later(1.0, _flush_stats)


def degraded_tools(min_samples: int = 5, threshold: float = 0.4) -> list[str]:
    """Tools failing most of the time recently, with a TCC hint when the
    error names a permission problem."""
    _load_stats()
    out = []
    for tool, dq in _stats.items():
        if len(dq) < min_samples:
            continue
        rate = sum(dq) / len(dq)
        if rate < threshold:
            hint = ""
            err = _last_error.get(tool, "")
            if _TCC_HINT_RE.search(err):
                hint = " — looks like a macOS permission (TCC) problem; tell the user the System Settings fix"
            out.append(f"{tool} (succeeded {sum(dq)}/{len(dq)} recently{hint})")
    return out


# ─── Route memory (learned fast-model routing) ─────────────────────────────────

_routes: "OrderedDict[str, bool] | None" = None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _load_routes() -> "OrderedDict[str, bool]":
    global _routes
    if _routes is None:
        raw = _read_json(ROUTES_FILE, {})
        _routes = OrderedDict((k, bool(v)) for k, v in raw.items()) \
            if isinstance(raw, dict) else OrderedDict()
    return _routes


def route_hint(text: str) -> bool | None:
    """True = known pure-read (fast model), False = known action run,
    None = never seen."""
    return _load_routes().get(_norm(text))


def record_route(text: str, read_only: bool) -> None:
    routes = _load_routes()
    key = _norm(text)
    if not key:
        return
    routes[key] = read_only
    routes.move_to_end(key)
    while len(routes) > _MAX_ROUTES:
        routes.popitem(last=False)
    _write_json(ROUTES_FILE, dict(routes))

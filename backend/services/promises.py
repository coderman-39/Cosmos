"""Promise tracking — commitments made in sent messages (F6).

The outbox journal records every Slack/iMessage send. sweep() runs the NEW
sends (since the last cursor) through a fast-model extractor: concrete
commitments ("I'll approve your PR today", "will share the doc by EOD")
become open promises; later sends that fulfill one auto-resolve it.

State: ~/.friday/promises.json {"promises": [...], "cursor": "<iso ts>"}.
Atomic writes; every path guarded — promise tracking must never break a run.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from services import atomicio, llm, outbox

# sweep() is a read-modify-write of promises.json that spans a multi-second LLM
# await. Two concurrent sweeps (model batching, or a worker also sweeping) would
# both load the same cursor and the last _save() would clobber the other's new
# promises — serialize them.
_sweep_lock = asyncio.Lock()

FILE = Path.home() / ".friday" / "promises.json"

_FAST_MODEL = os.getenv("FRIDAY_FAST_MODEL",
                        os.getenv("FRIDAY_AGENT_MODEL", llm.DEFAULT_MODEL))
_MAX_SWEEP_MSGS = 30
_MAX_OPEN = 50


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text())
        if isinstance(data, dict) and isinstance(data.get("promises"), list):
            return data
    except Exception:
        pass
    return {"promises": [], "cursor": ""}


def _save(data: dict) -> None:
    if not atomicio.write_json_atomic(FILE, data, indent=1):
        print("[promises] save failed (non-fatal): could not write promises.json")


def list_open() -> list[dict]:
    return [p for p in _load()["promises"] if p.get("status") == "open"]


def _age(made_at: str) -> str:
    try:
        days = (datetime.now() - datetime.fromisoformat(made_at)).days
    except Exception:
        return "?"
    return "today" if days <= 0 else f"{days}d ago"


def format_open() -> str:
    """Human list for the tool result / briefing. Never raises."""
    open_ = list_open()
    if not open_:
        return "No open promises."
    lines = [f"{len(open_)} open promise(s):"]
    for p in open_:
        due = f", due {p['due_hint']}" if p.get("due_hint") not in (None, "", "none") else ""
        lines.append(f"[{p.get('id', '?')}] to {p.get('to', '?')} — "
                     f"{p.get('text', '')} (made {_age(p.get('made_at', ''))}{due})")
    return "\n".join(lines)


def resolve(pid: str, status: str = "done") -> str:
    pid = (pid or "").strip()
    data = _load()
    for p in data["promises"]:
        if p.get("id") == pid and p.get("status") == "open":
            p["status"] = status
            p["closed_at"] = datetime.now().isoformat(timespec="seconds")
            _save(data)
            return f"Promise [{pid}] marked {status}: {p.get('text', '')[:120]}"
    return f"No open promise with id '{pid}'. Use action=list to see ids."


_EXTRACT_PROMPT = """You extract COMMITMENTS a user made in their OWN sent messages.

OPEN PROMISES (already tracked):
{open_json}

NEW SENT MESSAGES (oldest first, numbered):
{msgs_json}

Return STRICT JSON only, no prose:
{{"new": [{{"msg": <number>, "text": "<the commitment, concise, imperative>",
           "due_hint": "today|tomorrow|this week|none"}}],
  "resolved_ids": ["<id of an OPEN promise these messages show was fulfilled>"]}}

A commitment is a CONCRETE promise of future action by the sender ("I'll
approve your PR today", "will send the report by EOD", "I'll look into it
tomorrow"). NOT commitments: opinions, questions, past actions, pleasantries,
vague acknowledgements ("sounds good"). Lean conservative — empty lists are
the right answer for ordinary chatter."""


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except Exception:
        return None


async def sweep() -> str:
    """Scan sends newer than the cursor for commitments; auto-resolve
    fulfilled ones. Returns a human summary. Never raises. Serialized so
    concurrent sweeps can't clobber each other's writes."""
    async with _sweep_lock:
        return await _sweep_locked()


async def _sweep_locked() -> str:
    data = _load()
    cursor = data.get("cursor") or ""
    sends = [r for r in outbox.recent(n=200, action="send")
             if (r.get("ts") or "") > cursor]
    sends.reverse()                                  # oldest → newest
    # Keep the OLDEST batch: the cursor only advances over what we actually
    # scan, so anything past the cap is picked up next sweep (never dropped).
    sends = sends[:_MAX_SWEEP_MSGS]
    if not sends:
        return f"No new sent messages to scan. {format_open()}"

    open_ = list_open()
    msgs = [{"n": i + 1, "to": s.get("target") or "?", "text": s.get("summary") or ""}
            for i, s in enumerate(sends)]
    prompt = _EXTRACT_PROMPT.format(
        open_json=json.dumps([{"id": p["id"], "to": p.get("to"), "text": p.get("text")}
                              for p in open_], ensure_ascii=False),
        msgs_json=json.dumps(msgs, ensure_ascii=False))
    try:
        resp = await llm.acreate(model=_FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                                 max_tokens=500,
                                 messages=[{"role": "user", "content": prompt}])
        parsed = _parse_json(llm.extract_text(resp))
    except Exception as e:
        return f"Error: promise sweep LLM call failed — {llm.sanitize_error(e, 120)}"
    if parsed is None:
        # Don't advance the cursor: these sends get another chance next sweep.
        return "Error: promise sweep got an unparseable extraction — will retry."

    added = []
    # Dedup against the in-memory state (existing open promises + the ones we
    # add during THIS sweep) — list_open() re-reads disk and wouldn't see the
    # promises appended earlier in this same loop, so two identical commitments
    # in one sweep would both slip through.
    seen_texts = {p.get("text", "").lower() for p in data["promises"]
                  if p.get("status") == "open"}
    for item in parsed.get("new") or []:
        try:
            idx = int(item.get("msg", 0)) - 1
        except Exception:
            idx = -1
        src = sends[idx] if 0 <= idx < len(sends) else {}
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if text.lower() in seen_texts:
            continue
        seen_texts.add(text.lower())
        promise = {"id": uuid4().hex[:6],
                   "to": src.get("target") or "?",
                   "text": text[:200],
                   "due_hint": (item.get("due_hint") or "none")[:20],
                   "source": src.get("tool") or "slack",
                   "handle": src.get("handle") or {},
                   "made_at": src.get("ts") or datetime.now().isoformat(timespec="seconds"),
                   "status": "open"}
        data["promises"].append(promise)
        added.append(promise)

    resolved = []
    open_ids = {p["id"] for p in data["promises"] if p.get("status") == "open"}
    for rid in parsed.get("resolved_ids") or []:
        if rid in open_ids:
            for p in data["promises"]:
                if p["id"] == rid:
                    p["status"] = "done"
                    p["closed_at"] = datetime.now().isoformat(timespec="seconds")
                    resolved.append(p)

    # Cap total open promises (oldest dismissed) so the file can't grow forever.
    open_now = [p for p in data["promises"] if p.get("status") == "open"]
    for p in (open_now[:-_MAX_OPEN] if len(open_now) > _MAX_OPEN else []):
        p["status"] = "dismissed"
    data["cursor"] = max((s.get("ts") or "") for s in sends)
    _save(data)

    parts = [f"Swept {len(sends)} sent message(s): "
             f"{len(added)} new promise(s), {len(resolved)} auto-resolved."]
    for p in added:
        parts.append(f"  + [{p['id']}] to {p['to']}: {p['text']}")
    for p in resolved:
        parts.append(f"  ✓ [{p['id']}] {p.get('text', '')[:80]}")
    parts.append(format_open())
    return "\n".join(parts)

"""Dossier — a living, per-person intelligence file built from your comms.

Sweeps your Slack DMs, a set of watched channels, and your Gmail inbox over a
window (default 4 days), groups everything by PERSON, and uses the LLM to distil
three things per person:

  • promises   — what THEY committed to do (for you or the team)
  • working_on — what they're currently focused on
  • assigned_to_me — tasks they handed YOU, by the clear intent of the message

Each person is auto-classified by a simple heuristic: same email domain as
USER_EMAIL → colleague, anything else → contact. New people appear automatically
the first time they DM or email you. To avoid noise we only KEEP a person if
they're a colleague OR they actually made a promise / gave you a task by intent.

Storage: ~/.friday/dossier.json. A full sweep replaces; the daily incremental
sweep (last 1 day) merges new items into the existing file.
"""
from __future__ import annotations

import os
import re
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from services import slack, google, llm, agent, atomicio

FILE = Path.home() / ".friday" / "dossier.json"

# Channels to sweep for tasks/promises directed at you. Override with a
# comma-separated DOSSIER_CHANNELS in .env.
CHANNELS = [c.strip() for c in os.getenv(
    "DOSSIER_CHANNELS", "general,engineering").split(",") if c.strip()]

_MAX_MSGS_PER_PERSON = 34        # cap fed to the extractor
_MAX_LLM_PEOPLE = 40             # cap on LLM extraction calls per sweep
_AUTOMATED = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|notification|mailer|bounce|postmaster|"
    r"automated|alerts?@|newsletter|@github\.com|jira@|calendar-notification|via\b)",
    re.I)

_lock = asyncio.Lock()          # a sweep is heavy; never run two at once
# Live progress for the UI poller: phase + extraction counters.
_progress: dict = {"phase": "", "done": 0, "total": 0}


def progress() -> dict:
    """Current sweep state for the UI: running flag + phase + counters."""
    return {"running": _lock.locked(), **_progress}


# ─── storage ──────────────────────────────────────────────────────────────────

def load() -> dict:
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {"generated": "", "window_days": 0, "org": {}, "people": [],
                "stats": {}, "sweeping": False}


def _save(data: dict) -> None:
    if not atomicio.write_json_atomic(FILE, data, indent=2):
        print("[dossier] save failed: could not write dossier.json")


# ─── identity + classification ────────────────────────────────────────────────

async def _identity() -> dict:
    email = os.getenv("USER_EMAIL", "").lower()
    name = ""
    try:
        uid = await slack._self_id()
        if uid:
            info = await slack.user_info_full(uid)
            name = info.get("name", "")
            if not email:
                email = (info.get("email") or "").lower()
    except Exception:
        pass
    return {"email": email, "name": name}


def _classify(email: str, title: str = "") -> tuple[str, str]:
    """(relationship, role_label). relationship ∈ colleague|contact — a simple
    domain heuristic: sharing USER_EMAIL's email domain makes you a colleague."""
    email = (email or "").lower()
    my_domain = os.getenv("USER_EMAIL", "").lower().partition("@")[2]
    domain = email.partition("@")[2]
    if my_domain and domain == my_domain:
        return "colleague", title or "Colleague"
    return "contact", title or ""


def _person_key(email: str, uid: str, name: str) -> str:
    return (email or "").lower() or (uid or "") or (name or "").lower()


# ─── gather + group ───────────────────────────────────────────────────────────

def _blank_person(name: str, email: str, uid: str, title: str) -> dict:
    rel, role = _classify(email, title)
    return {"key": _person_key(email, uid, name), "name": name or email or uid,
            "email": email, "slack_id": uid, "title": title,
            "relationship": rel, "role": role,
            "_msgs": [], "sources": set(), "message_count": 0, "last_ts": ""}


async def _gather(days: int, oldest: float, me: dict) -> dict:
    """Collect messages from all sources into a persons dict keyed by person."""
    persons: dict[str, dict] = {}

    def _get(name, email, uid, title=""):
        key = _person_key(email, uid, name)
        if key not in persons:
            persons[key] = _blank_person(name, email, uid, title)
        p = persons[key]
        # backfill identity as better info arrives
        if email and not p["email"]:
            p["email"] = email
            p["relationship"], p["role"] = _classify(email, p["title"])
        if title and not p["title"]:
            p["title"] = title
        return p

    my_first = (me.get("name", "").split() or [""])[0].lower()
    my_email = me.get("email", "").lower()

    # Wider windows need deeper per-source reads (history APIs cap per call).
    wide = days > 7
    per_dm = 100 if wide else 50
    chan_limit = 200 if wide else 150
    mail_limit = 80 if wide else 50

    # ── Slack DMs ──
    try:
        dms = await slack.sweep_dms(oldest, per_dm=per_dm)
    except Exception as e:
        print(f"[dossier] slack DM sweep failed: {e}")
        dms = []
    for dm in dms:
        p = _get(dm["name"], dm.get("email", ""), dm.get("user_id", ""), dm.get("title", ""))
        p["sources"].add("slack-dm")
        for m in dm["messages"]:
            p["_msgs"].append({"source": "Slack DM", "ts": m["ts"],
                               "dir": "me" if m["from_me"] else "them", "text": m["text"]})
            p["last_ts"] = max(p["last_ts"], m["ts"])

    # ── Slack channels ──
    for ch in CHANNELS:
        try:
            res = await slack.sweep_channel(ch, oldest, limit=chan_limit)
        except Exception as e:
            print(f"[dossier] slack channel {ch} sweep failed: {e}")
            res = None
        if not res:
            continue
        for m in res["messages"]:
            mentions_me = bool(my_first and (("@" + my_first) in m["text"].lower()
                                             or my_first in m["text"].lower().split()))
            in_roster = _classify(m.get("email", ""), m.get("title", ""))[0] == "colleague"
            if not (mentions_me or in_roster):
                continue                     # keep the channel person-set tight
            p = _get(m["name"], m.get("email", ""), m.get("user_id", ""), m.get("title", ""))
            p["sources"].add("#" + ch)
            p["_msgs"].append({"source": "#" + ch, "ts": m["ts"],
                               "dir": "them", "text": m["text"]})
            p["last_ts"] = max(p["last_ts"], m["ts"])

    # ── Gmail ──
    try:
        emails = await google.gmail_recent(days=days, limit=mail_limit, with_body=True)
    except Exception as e:
        print(f"[dossier] gmail sweep failed: {e}")
        emails = []
    for em in emails:
        frm = (em.get("from_email") or "").lower()
        if not frm or frm == my_email or _AUTOMATED.search(frm) or _AUTOMATED.search(em.get("from_name", "")):
            continue
        p = _get(em.get("from_name", frm), frm, "", "")
        p["sources"].add("email")
        body = em.get("body") or em.get("snippet") or ""
        p["_msgs"].append({"source": "Email", "ts": em.get("date", ""), "dir": "them",
                           "text": f"Subject: {em.get('subject','')}\n{body}"[:1200]})

    for p in persons.values():
        p["message_count"] = len(p["_msgs"])
    return persons


# ─── LLM extraction ───────────────────────────────────────────────────────────

_EXTRACT_SYS = (
    "You build a per-person intelligence dossier from the user's Slack and email. "
    "Given all recent messages involving ONE person, extract ONLY what is clearly "
    "stated — never invent. Return STRICT JSON, no prose, no code fences:\n"
    '{\n'
    '  "relevant": true|false,   // false if nothing meaningful about work/tasks\n'
    '  "summary": "one line on what this person is to the user right now",\n'
    '  "promises": [ {"text": "...", "source": "Slack DM|#chan|Email", "evidence": "short quote"} ],\n'
    '  "working_on": [ {"text": "...", "source": "...", "evidence": "..."} ],\n'
    '  "assigned_to_me": [ {"text": "...", "source": "...", "evidence": "..."} ]\n'
    "}\n"
    "Definitions: promises = things THIS PERSON committed to do. working_on = what "
    "THIS PERSON is currently doing/focused on. assigned_to_me = tasks THIS PERSON "
    "asked the USER to do, by clear intent (a real request/assignment, not chit-chat "
    "or an FYI). Keep each 'text' concise and actionable. Empty arrays are fine.")


def _as_str(v) -> str:
    """Coerce any LLM-shaped value to a plain string. Models occasionally nest
    ({'text': ...}) or return lists — never let that crash the sweep."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in ("text", "summary", "value", "description"):
            if isinstance(v.get(k), str):
                return v[k].strip()
        return json.dumps(v, ensure_ascii=False)[:200]
    if isinstance(v, list):
        return "; ".join(_as_str(x) for x in v if x)[:300]
    return str(v).strip()


def _as_items(v) -> list[dict]:
    """Coerce an extraction list into [{text, source, evidence}] — accepts
    strings, dicts with odd keys, or garbage (dropped)."""
    out = []
    if not isinstance(v, list):
        v = [v] if v else []
    for it in v:
        if isinstance(it, str):
            t = it.strip()
            if t:
                out.append({"text": t, "source": "", "evidence": ""})
        elif isinstance(it, dict):
            t = _as_str(it.get("text") or it.get("task") or it.get("item")
                        or it.get("description") or it.get("summary"))
            if t:
                out.append({"text": t[:300],
                            "source": _as_str(it.get("source"))[:60],
                            "evidence": _as_str(it.get("evidence") or it.get("quote"))[:200]})
    return out[:12]


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```[a-z]*\n?|\n?```$", "", (text or "").strip())
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _remap_alien(ext: dict, person: str, my_name: str) -> dict:
    """The gateway model sometimes ignores the requested schema and freeforms
    (commitments / open_loops / tasks keyed by owner). Translate that shape into
    ours instead of losing the extraction."""
    if any(k in ext for k in ("promises", "working_on", "assigned_to_me")):
        return ext
    out = {"relevant": True, "summary": _as_str(ext.get("summary")),
           "promises": [], "working_on": [], "assigned_to_me": []}
    pl, ml = person.lower(), (my_name or "").lower()
    my_first = ml.split()[0] if ml else ""

    def _owner_is_me(o: str) -> bool:
        o = (o or "").lower()
        return bool(o) and (o in ml or ml.startswith(o) or (my_first and my_first in o)
                            or o in ("you", "user", "me"))

    def _owner_is_them(o: str) -> bool:
        o = (o or "").lower()
        return bool(o) and (o in pl or pl.startswith(o) or o.split()[0] in pl)

    rows = []
    for k in ("commitments", "open_loops", "tasks", "action_items", "todos"):
        v = ext.get(k)
        if isinstance(v, list):
            rows += v
    for r in rows:
        if not isinstance(r, dict):
            continue
        text = _as_str(r.get("task") or r.get("description") or r.get("text") or r.get("item"))
        if not text:
            continue
        due = _as_str(r.get("deadline") or r.get("due"))
        if due:
            text = f"{text} (due {due})"
        owner = _as_str(r.get("owner") or r.get("assignee") or r.get("who"))
        item = {"text": text[:300], "source": "", "evidence": ""}
        if _owner_is_me(owner):
            out["assigned_to_me"].append(item)
        elif _owner_is_them(owner):
            out["promises"].append(item)
    for k in ("working_on", "current_work", "focus", "in_progress"):
        v = ext.get(k)
        if v:
            out["working_on"] = _as_items(v)
            break
    if not (out["promises"] or out["working_on"] or out["assigned_to_me"] or out["summary"]):
        return {}
    return out


async def _extract(p: dict, me: dict) -> dict:
    msgs = sorted(p["_msgs"], key=lambda m: m.get("ts", ""))[-_MAX_MSGS_PER_PERSON:]
    convo = "\n".join(
        f"[{m['source']}] {'(you)' if m['dir']=='me' else p['name']}: {m['text']}"
        for m in msgs)
    my_name = me.get("name") or "the user"
    # The full instruction lives in the USER message — the gateway model has been
    # observed ignoring system-prompt schemas and inventing its own JSON shape.
    # Schema stated LAST (recency), keys demanded verbatim; _remap_alien catches
    # the stragglers that still freeform.
    user = (f"PERSON: {p['name']} <{p.get('email','')}> — {p.get('role') or p['relationship']}\n"
            f"YOU (the user): {my_name}\n\n"
            f"MESSAGES (oldest→newest):\n{convo}\n\n"
            "TASK: From these messages, extract ONLY what is clearly stated — never invent:\n"
            f"- promises: things {p['name']} committed to do\n"
            f"- working_on: what {p['name']} is currently doing or focused on\n"
            f"- assigned_to_me: tasks {p['name']} asked {my_name} to do, by clear intent "
            "(a real request or assignment — not chit-chat, not an FYI)\n"
            "- summary: one line on what this person is to the user right now\n"
            "- relevant: false if nothing meaningful about work\n\n"
            "Respond with ONLY one JSON object — no prose, no code fences — with EXACTLY "
            "these five keys and no others:\n"
            '{"relevant": true, "summary": "...", '
            '"promises": [{"text": "...", "source": "Slack DM|#channel|Email", "evidence": "short quote"}], '
            '"working_on": [{"text": "...", "source": "...", "evidence": "..."}], '
            '"assigned_to_me": [{"text": "...", "source": "...", "evidence": "..."}]}')
    try:
        resp = await llm.acreate(model=agent.AGENT_MODEL, fallbacks=llm.AGENT_FALLBACKS,
                                 max_tokens=1200,
                                 messages=[{"role": "system", "content": _EXTRACT_SYS},
                                           {"role": "user", "content": user}])
        ext = _extract_json(llm.extract_text(resp))
        return _remap_alien(ext, p["name"], my_name)
    except Exception as e:
        print(f"[dossier] extract failed for {p['name']}: {e}")
        return {}


# ─── sweep ────────────────────────────────────────────────────────────────────

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _oldest_ts(days: int) -> float:
    return datetime.now(timezone.utc).timestamp() - max(1, days) * 86400


def _finalize(p: dict, ext: dict) -> dict:
    return {
        "key": p["key"], "name": p["name"], "email": p.get("email", ""),
        "slack_id": p.get("slack_id", ""), "title": p.get("title", ""),
        "relationship": p["relationship"], "role": p.get("role", ""),
        "sources": sorted(p["sources"]),
        "summary": _as_str(ext.get("summary"))[:220],
        "promises": _as_items(ext.get("promises")),
        "working_on": _as_items(ext.get("working_on")),
        "assigned_to_me": _as_items(ext.get("assigned_to_me")),
        "message_count": p["message_count"],
        "last_ts": p.get("last_ts", ""),
        "updated": _iso(),
    }


async def sweep(days: int = 14, merge: bool = False) -> dict:
    """Full sweep of Slack + Gmail over `days`. Writes ~/.friday/dossier.json.
    merge=True unions results into the existing file (used by the daily job)."""
    if _lock.locked():
        return {"ok": False, "message": "A sweep is already running."}
    async with _lock:
        try:
            return await _sweep_locked(days, merge)
        except Exception as e:
            print(f"[dossier] sweep failed: {e}")
            cur = load()
            cur["sweeping"] = False
            _save(cur)
            return {"ok": False, "message": f"Sweep failed: {str(e)[:140]}"}
        finally:
            _progress.update(phase="", done=0, total=0)


async def _sweep_locked(days: int, merge: bool) -> dict:
    cur = load()
    cur["sweeping"] = True
    _save(cur)
    _progress.update(phase="gathering Slack + Gmail", done=0, total=0)
    me = await _identity()
    oldest = _oldest_ts(days)
    persons = await _gather(days, oldest, me)

    # Candidates for LLM extraction: anyone with messages. Roster people with
    # no activity still become nodes (empty), no LLM call.
    active = [p for p in persons.values() if p["message_count"] > 0]
    active.sort(key=lambda p: (p["relationship"] == "colleague", p["message_count"]),
                reverse=True)
    active = active[:_MAX_LLM_PEOPLE]

    sem = asyncio.Semaphore(5)
    _progress.update(phase="extracting per person", done=0, total=len(active))

    async def _go(p):
        async with sem:
            ext = await _extract(p, me)
            _progress["done"] += 1
            return p, ext

    extracted = dict((id(p), ext) for p, ext in
                     [(p, e) for p, e in await asyncio.gather(*[_go(p) for p in active])])

    people: list[dict] = []
    for p in persons.values():
        ext = extracted.get(id(p), {}) if p["message_count"] > 0 else {}
        in_org = p["relationship"] == "colleague"
        has_signal = bool(_as_items(ext.get("promises")) or _as_items(ext.get("working_on"))
                          or _as_items(ext.get("assigned_to_me")))
        relevant = bool(ext.get("relevant"))
        # KEEP rule: in your org roster, OR they gave a real task/promise, OR
        # the extractor flagged them relevant.
        if not (in_org or has_signal or relevant):
            continue
        people.append(_finalize(p, ext))

    org = {
        "me": {"name": me.get("name") or "You", "email": me.get("email", "")},
        "manager": {},
        "skip": {},
    }
    result = {
        "generated": _iso(), "window_days": days, "org": org,
        "channels": CHANNELS, "people": people, "sweeping": False,
        "stats": {"people": len(people),
                  "with_tasks": sum(1 for x in people if x["assigned_to_me"]),
                  "promises": sum(len(x["promises"]) for x in people)},
    }
    if merge:
        result = _merge(cur, result)
    _save(result)
    _progress.update(phase="", done=0, total=0)
    return {"ok": True, "message":
            f"Swept {days}d — {len(result['people'])} people, "
            f"{result['stats']['promises']} promises tracked.",
            "stats": result["stats"], "generated": result["generated"]}


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = (it.get("text", "") or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _merge(old: dict, new: dict) -> dict:
    """Union new people/items into the existing dossier (daily incremental)."""
    by_key = {p["key"]: p for p in old.get("people", [])}
    for np in new.get("people", []):
        if np["key"] in by_key:
            op = by_key[np["key"]]
            for fld in ("promises", "working_on", "assigned_to_me"):
                op[fld] = _dedup((op.get(fld) or []) + (np.get(fld) or []))
            op["sources"] = sorted(set(op.get("sources", [])) | set(np.get("sources", [])))
            op["summary"] = np.get("summary") or op.get("summary", "")
            op["last_ts"] = max(op.get("last_ts", ""), np.get("last_ts", ""))
            op["message_count"] = op.get("message_count", 0) + np.get("message_count", 0)
            op["updated"] = np.get("updated", op.get("updated", ""))
            # relationship/role/title refresh from the newer classification
            for f in ("relationship", "role", "title", "email"):
                if np.get(f):
                    op[f] = np[f]
        else:
            by_key[np["key"]] = np
    people = list(by_key.values())
    return {**new, "people": people,
            "stats": {"people": len(people),
                      "with_tasks": sum(1 for x in people if x.get("assigned_to_me")),
                      "promises": sum(len(x.get("promises", [])) for x in people)}}


# ─── agent tool ───────────────────────────────────────────────────────────────

def _fmt_items(items: list[dict], cap: int = 6) -> str:
    return "\n".join(f"  - {it.get('text','')}"
                     + (f" [{it['source']}]" if it.get("source") else "")
                     for it in (items or [])[:cap]) or "  (none)"


async def _agent_tool(args, ctx) -> str:
    action = ((args or {}).get("action") or "overview").lower()
    d = load()
    people = d.get("people", [])

    if action == "sweep":
        days = max(1, min(int((args or {}).get("days") or 14), 30))
        if progress()["running"]:
            return "A dossier sweep is already running — check back in a few minutes."
        asyncio.create_task(sweep(days=days, merge=days <= 2))
        return (f"Dossier sweep of the last {days} days started in the background. "
                "It takes a few minutes; results land in the Dossier tab.")

    if not people:
        return ("The dossier is empty — run action='sweep' (or use the Dossier tab) "
                "to build it from Slack + Gmail.")

    if action == "person":
        q = ((args or {}).get("name") or "").strip().lower()
        hits = [p for p in people if q and q in (p.get("name", "") or "").lower()]
        if not hits:
            return (f"No tracked person matching '{q}'. Tracked people: "
                    + ", ".join(p["name"] for p in people[:25]))
        p = hits[0]
        return (f"{p['name']} — {p.get('role') or p.get('relationship')}"
                f" <{p.get('email','')}>\n"
                f"Summary: {p.get('summary') or '—'}\n"
                f"Tasks they gave you ({len(p.get('assigned_to_me', []))}):\n"
                f"{_fmt_items(p.get('assigned_to_me'))}\n"
                f"Their promises ({len(p.get('promises', []))}):\n"
                f"{_fmt_items(p.get('promises'))}\n"
                f"Working on:\n{_fmt_items(p.get('working_on'))}")

    if action == "owed":
        lines = []
        for p in people:
            for t in p.get("assigned_to_me", []):
                lines.append(f"- [{p['name']}] {t.get('text','')}")
        prom = []
        for p in people:
            for t in p.get("promises", []):
                prom.append(f"- [{p['name']}] {t.get('text','')}")
        return ("TASKS ASSIGNED TO YOU:\n" + ("\n".join(lines[:20]) or "(none)")
                + "\n\nPROMISES OTHERS MADE (chase these):\n"
                + ("\n".join(prom[:20]) or "(none)"))

    # overview
    st = d.get("stats", {})
    top = sorted(people, key=lambda p: len(p.get("assigned_to_me", [])) * 2
                 + len(p.get("promises", [])), reverse=True)[:8]
    rows = "\n".join(
        f"- {p['name']} ({p.get('relationship')}): "
        f"{len(p.get('assigned_to_me', []))} task(s) for you, "
        f"{len(p.get('promises', []))} promise(s)"
        for p in top)
    return (f"Dossier: {st.get('people', len(people))} people tracked, "
            f"{st.get('with_tasks', 0)} gave you tasks, "
            f"{st.get('promises', 0)} promises. Last sweep: {d.get('generated','never')}.\n"
            f"Top signal:\n{rows}\n"
            "Use action='person' for detail, 'owed' for the full task list.")


def register_agent_tool() -> None:
    """Register the `dossier` tool so the agent can answer people questions
    ('what does Cornelius owe me?', 'who assigned me tasks?') from chat/voice."""
    try:
        from services import agent
        schema = {
            "name": "dossier",
            "description": (
                "COSMOS's people intelligence, built from the user's Slack DMs, watched "
                "channels and Gmail: per-person promises, what they're working on, and "
                "tasks they assigned the user. Use for questions like 'what does X owe "
                "me', 'who gave me tasks', 'what is X working on'. actions: overview "
                "(default) | person (needs name) | owed (everything owed to/by the user) "
                "| sweep (rebuild in background; days optional)."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["overview", "person", "owed", "sweep"]},
                    "name": {"type": "string", "description": "person name for action=person"},
                    "days": {"type": "integer", "description": "sweep window (action=sweep)"},
                },
            },
        }
        agent.register_tool(schema, _agent_tool, gate="open",
                            label="dossier", source="dossier")
        agent.invalidate_tool_cache()
    except Exception as e:
        print(f"[dossier] agent tool registration failed (non-fatal): {e}")


# ─── daily background loop ────────────────────────────────────────────────────

async def daily_loop(hour: int = 7, minute: int = 30):
    """Sweep yesterday→today once a day and merge. Started from main lifespan."""
    from datetime import timedelta
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        try:
            await asyncio.sleep((target - now).total_seconds())
        except asyncio.CancelledError:
            return
        try:
            print("[dossier] daily sweep starting")
            await sweep(days=1, merge=True)
        except Exception as e:
            print(f"[dossier] daily sweep error: {e}")

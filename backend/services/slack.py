"""Slack handler — Web API via the user token (xoxp-), acting AS the user.

Far more reliable than driving the Slack desktop app through Cmd+K: reads DMs
and channels, lists unreads, sets status, toggles Do-Not-Disturb, reacts,
and sends — all through https://slack.com/api. Transport is a persistent
keep-alive httpx client (services.http_pool); the token rides an in-process
header dict — never argv, never disk.

Configured via SLACK_USER_TOKEN in .env. Every function returns (ok, text);
nothing raises out to the agent loop.
"""

import os
import json
import time
import asyncio

import httpx

from services import http_pool, outbox

TOKEN = os.getenv("SLACK_USER_TOKEN", "").strip()
AVAILABLE = bool(TOKEN)


def reconfigure() -> None:
    """Re-read SLACK_USER_TOKEN from env (Connectors UI) — live, no restart."""
    global TOKEN, AVAILABLE, _self_id_cache
    TOKEN = os.getenv("SLACK_USER_TOKEN", "").strip()
    AVAILABLE = bool(TOKEN)
    _self_id_cache = None
    _user_cache.clear(); _name_index.clear(); _chan_cache.clear()
    _dm_user.clear(); _dm_by_user.clear()

_self_id_cache: str | None = None
_SELF_WORDS = {"me", "myself", "self", "my dm", "my dms", "saved", "saved messages"}


async def _self_id() -> str:
    """The authenticated user's own id (for 'message myself' / self-DM), cached."""
    global _self_id_cache
    if _self_id_cache is None:
        data = await _api("auth.test")
        _self_id_cache = data.get("user_id", "") if data.get("ok") else ""
    return _self_id_cache

# Short-lived caches so name→id resolution and channel listing don't re-hit the
# API on every call within a run.
_user_cache: dict[str, dict] = {}     # user_id -> {name, real_name}
_name_index: dict[str, str] = {}      # lowercased name -> user_id  (full dir)
_chan_cache: dict[str, str] = {}      # lowercased channel name -> channel_id
_dm_user: dict[str, str] = {}         # dm channel_id -> user_id
_dm_by_user: dict[str, str] = {}      # user_id -> existing dm channel_id
_chan_ts = 0.0                        # last conversation-index refresh
_dir_ts = 0.0                         # last FULL user-directory refresh
_CACHE_TTL = 300.0


async def _api(method: str, params: dict | None = None,
               timeout: float = 20) -> dict:
    """One Slack Web API call (form-encoded POST over the pooled keep-alive
    client; token in an in-process header dict — never argv, never disk).
    Returns the parsed JSON, or {"ok": False, "error": ...} on failure."""
    if not AVAILABLE:
        return {"ok": False, "error": "no_token"}
    try:
        client = http_pool.get_client("slack")
        r = await client.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type":
                         "application/x-www-form-urlencoded; charset=utf-8"},
            data={k: str(v) for k, v in (params or {}).items()},
            timeout=timeout)
        return r.json()
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        # Token never appears in httpx errors (it rides a header, not the
        # URL) — scrub defensively anyway before surfacing.
        msg = str(e)
        if TOKEN:
            msg = msg.replace(TOKEN, "[redacted]")
        return {"ok": False, "error": msg[:120]}


def _err(data: dict) -> str:
    e = data.get("error", "unknown")
    if e in ("missing_scope", "not_allowed_token_type"):
        need = data.get("needed", "")
        return (f"Slack denied this — the token is missing the '{need or e}' scope. "
                "Add it to the app at api.slack.com/apps and reinstall (may need "
                "workspace-admin approval).")
    if e == "no_token":
        return "SLACK_USER_TOKEN isn't set in .env."
    return f"Slack API error: {e}"


# ─── Directory (people + channels) ─────────────────────────────────────────────

async def _refresh_channels() -> None:
    """ONE call — index the channels/DMs the user is in. Fast; no full member
    directory. This is all reads (unreads, read-channel) need."""
    global _chan_ts
    if _chan_cache and (time.time() - _chan_ts) < _CACHE_TTL:
        return
    data = await _api("users.conversations", {
        "types": "public_channel,private_channel,mpim,im", "limit": 400,
        "exclude_archived": "true"})
    if data.get("ok"):
        for c in data.get("channels", []):
            if c.get("is_im"):
                uid = c.get("user", "")
                _dm_user[c["id"]] = uid
                if uid:
                    _dm_by_user[uid] = c["id"]     # reverse: user → existing DM
            nm = c.get("name") or c.get("name_normalized")
            if nm:
                _chan_cache[nm.lower()] = c["id"]
        _chan_ts = time.time()


_dm_names_ts = 0.0


async def _ensure_dm_partner_names() -> None:
    """Resolve the names of everyone you have a DM with — a small, RELEVANT set
    (a few hundred at most). This is how "read my DM with <name>" resolves,
    without paging the entire company directory (where the person is often
    past any page cap on a big org). Cached; concurrent users.info lookups."""
    global _dm_names_ts
    await _refresh_channels()
    if _dm_by_user and (time.time() - _dm_names_ts) < _CACHE_TTL and \
            all(uid in _user_cache for uid in _dm_by_user):
        return
    ids = [uid for uid in _dm_by_user if uid and uid not in _user_cache]
    sem = asyncio.Semaphore(24)      # users.info is a light call; parallelize hard

    async def _go(uid):
        async with sem:
            await _name_for(uid)

    await asyncio.gather(*[_go(uid) for uid in ids])
    _dm_names_ts = time.time()


def _match_dm_partner(name: str) -> tuple[str | None, list[str]]:
    """Match a name against DM partners already resolved in _user_cache.
    Returns (user_id, ambiguous_candidates)."""
    key = name.strip().lower().lstrip("@")
    exact, contains = set(), set()
    for uid in _dm_by_user:
        info = _user_cache.get(uid)
        if not info:
            continue
        labels = {info.get("real_name", "").lower(), info.get("name", "").lower()}
        if key in labels:
            exact.add(uid)
        elif any(key in lbl for lbl in labels if lbl):
            contains.add(uid)
    if len(exact) == 1:
        return next(iter(exact)), []
    hits = exact or contains
    if len(hits) == 1:
        return next(iter(hits)), []
    if not hits:
        return None, []
    return None, sorted({_uname(u) for u in hits})[:6]


async def _refresh_directory() -> None:
    """FULL member directory (name→id) — only needed to DM/react-to a person BY
    NAME. Heavy on big workspaces, so lazily loaded and cached separately."""
    global _dir_ts
    if _name_index and (time.time() - _dir_ts) < _CACHE_TTL:
        return
    cursor = ""
    for _ in range(8):
        data = await _api("users.list", {"limit": 200, "cursor": cursor})
        if not data.get("ok"):
            break
        for m in data.get("members", []):
            if m.get("deleted") or m.get("is_bot"):
                continue
            uid = m.get("id", "")
            prof = m.get("profile", {})
            name = m.get("name", "")
            real = prof.get("real_name") or prof.get("display_name") or name
            _user_cache[uid] = {"name": name, "real_name": real}
            for label in {name, real, prof.get("display_name", "")}:
                if label:
                    _name_index.setdefault(label.lower(), uid)
        cursor = (data.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
    _dir_ts = time.time()


async def _name_for(uid: str) -> str:
    """Resolve a single user id → display name, cached (one users.info call)."""
    if not uid:
        return "unknown"
    hit = _user_cache.get(uid)
    if hit:
        return hit["real_name"]
    data = await _api("users.info", {"user": uid})
    if data.get("ok"):
        prof = (data.get("user") or {}).get("profile", {})
        name = prof.get("real_name") or prof.get("display_name") \
            or (data.get("user") or {}).get("name") or uid
        _user_cache[uid] = {"name": (data.get("user") or {}).get("name", ""),
                            "real_name": name}
        return name
    return uid


def _uname(uid: str) -> str:
    """Sync name lookup from cache only (for already-fetched ids)."""
    return (_user_cache.get(uid) or {}).get("real_name") or uid


async def _resolve_user(name: str, dm_only: bool = False) -> tuple[str | None, list[str]]:
    """name → (user_id, ambiguous_candidates).

    DM PARTNERS FIRST — the person you want is almost always someone you already
    DM, and that set is small and reliable (the full directory can be 1000s and
    paginate past any cap). Falls back to the full member directory only if
    not found among DM partners and dm_only is False."""
    await _ensure_dm_partner_names()
    uid, cands = _match_dm_partner(name)
    if uid or cands or dm_only:
        return uid, cands

    await _refresh_directory()
    key = name.strip().lower().lstrip("@")
    if key in _name_index:
        return _name_index[key], []
    hits = [(lbl, uid) for lbl, uid in _name_index.items() if key in lbl]
    uniq = {uid for _, uid in hits}
    if len(uniq) == 1:
        return next(iter(uniq)), []
    if not uniq:
        return None, []
    return None, sorted({_uname(uid) for uid in uniq})[:6]


async def _resolve_channel(target: str) -> tuple[str | None, list[str]]:
    """A #channel name or a person's name → a channel id to read/post to.
    For a person, opens (or reuses) the DM channel."""
    t = target.strip().lstrip("#")
    if t and t[0] in "CGD" and t.isupper() and len(t) >= 9:
        return t, []                                   # already a channel id
    await _refresh_channels()                          # fast (1 call) — indexes DMs

    async def _dm_for(uid: str) -> str | None:
        # Prefer an EXISTING DM channel (no scope needed); conversations.open
        # is a fallback that needs im:write (not always granted).
        if uid in _dm_by_user:
            return _dm_by_user[uid]
        opened = await _api("conversations.open", {"users": uid})
        return (opened.get("channel") or {}).get("id") if opened.get("ok") else None

    # "me"/"myself"/"saved" → your own self-DM (Slack's "Saved messages").
    if t.lower().lstrip("@") in _SELF_WORDS:
        cid = await _dm_for(await _self_id())
        return (cid, []) if cid else (None, [])

    if t.lower() in _chan_cache:
        return _chan_cache[t.lower()], []
    uid, cands = await _resolve_user(t)                # falls back to full dir
    if uid:
        cid = await _dm_for(uid)
        if cid:
            return cid, []
    return None, cands


# ─── Read ──────────────────────────────────────────────────────────────────────

import re as _re


async def _fmt_messages(msgs: list[dict], limit: int) -> str:
    # Pre-resolve every user id referenced (authors + <@U…> mentions) so names
    # show instead of raw ids — concurrent, cached, only the ids we need.
    ids = set()
    for m in msgs[:limit]:
        if m.get("user"):
            ids.add(m["user"])
        ids.update(_re.findall(r"<@([A-Z0-9]+)>", m.get("text") or ""))
    await asyncio.gather(*[_name_for(i) for i in ids if i not in _user_cache])

    lines = []
    for m in reversed(msgs[:limit]):          # oldest→newest for reading order
        who = _uname(m.get("user", "")) if m.get("user") else (m.get("username") or "bot")
        text = (m.get("text") or "").replace("\n", " ").strip()
        text = _re.sub(r"<@([A-Z0-9]+)>", lambda g: "@" + _uname(g.group(1)), text)
        ts = m.get("ts", "")
        when = time.strftime("%b %d %H:%M", time.localtime(float(ts))) if ts else ""
        lines.append(f"[{when}] {who}: {text[:300]}")
    return "\n".join(lines) or "(no messages)"


# ─── Dossier sweep helpers (structured, for services/dossier.py) ───────────────

async def user_info_full(uid: str) -> dict:
    """Full profile for a user id: real name, email (needs users:read.email),
    Slack job title. Cached name side-effect via _user_cache."""
    if not uid:
        return {"id": "", "name": "unknown", "email": "", "title": ""}
    data = await _api("users.info", {"user": uid})
    if not data.get("ok"):
        return {"id": uid, "name": _uname(uid), "email": "", "title": ""}
    u = data.get("user") or {}
    prof = u.get("profile") or {}
    name = prof.get("real_name") or prof.get("display_name") or u.get("name") or uid
    _user_cache[uid] = {"name": u.get("name", ""), "real_name": name}
    return {"id": uid, "name": name, "email": prof.get("email", ""),
            "title": prof.get("title", ""), "is_bot": bool(u.get("is_bot")),
            "deleted": bool(u.get("deleted"))}


async def _clean_text(text: str) -> str:
    """Replace <@U…> mentions with @Name and strip Slack link markup — readable
    for the LLM extractor."""
    ids = set(_re.findall(r"<@([A-Z0-9]+)>", text or ""))
    await asyncio.gather(*[_name_for(i) for i in ids if i not in _user_cache])
    t = _re.sub(r"<@([A-Z0-9]+)>", lambda g: "@" + _uname(g.group(1)), text or "")
    t = _re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", t)     # <url|label> → label
    t = _re.sub(r"<(https?://[^>]+)>", r"\1", t)               # <url> → url
    return t.replace("\n", " ").strip()


async def sweep_dms(oldest_ts: float, per_dm: int = 50) -> list[dict]:
    """Every 1:1 DM's messages since oldest_ts. Returns
    [{user_id, name, email, title, is_bot, messages:[{ts, text, from_me}]}] —
    only DMs that actually have messages in the window."""
    await _ensure_dm_partner_names()
    me = await _self_id()
    partners = [(uid, cid) for uid, cid in _dm_by_user.items() if uid and cid]
    sem = asyncio.Semaphore(8)

    async def _one(uid: str, cid: str):
        async with sem:
            data = await _api("conversations.history",
                              {"channel": cid, "oldest": f"{oldest_ts:.6f}",
                               "limit": max(1, min(per_dm, 100))})
        if not data.get("ok"):
            return None
        raw = data.get("messages", [])
        if not raw:
            return None
        info = await user_info_full(uid)
        if info.get("is_bot") or info.get("deleted"):
            return None
        msgs = []
        for m in reversed(raw):        # oldest→newest
            if m.get("subtype"):
                continue
            txt = await _clean_text(m.get("text") or "")
            if not txt:
                continue
            msgs.append({"ts": m.get("ts", ""), "text": txt[:600],
                         "from_me": m.get("user") == me})
        if not msgs:
            return None
        return {"user_id": uid, "name": info["name"], "email": info["email"],
                "title": info.get("title", ""), "is_bot": False, "messages": msgs}

    results = await asyncio.gather(*[_one(uid, cid) for uid, cid in partners])
    return [r for r in results if r]


async def sweep_channel(name: str, oldest_ts: float, limit: int = 150) -> dict | None:
    """Recent messages from a joined #channel since oldest_ts, with each author
    resolved to name/email. Returns {channel, channel_id, messages:[{ts, user_id,
    name, email, text}]}."""
    cid, _ = await _resolve_channel(name)
    if not cid:
        return None
    data = await _api("conversations.history",
                      {"channel": cid, "oldest": f"{oldest_ts:.6f}",
                       "limit": max(1, min(limit, 200))})
    if not data.get("ok"):
        return None
    raw = [m for m in data.get("messages", [])
           if not m.get("subtype") and m.get("user") and (m.get("text") or "").strip()]
    # Resolve every author once (name/email), bounded concurrency.
    authors = list({m["user"] for m in raw})
    sem = asyncio.Semaphore(12)

    async def _info(uid):
        async with sem:
            return uid, await user_info_full(uid)

    info_map = dict(await asyncio.gather(*[_info(u) for u in authors]))
    msgs = []
    for m in reversed(raw):
        uid = m["user"]
        inf = info_map.get(uid, {})
        if inf.get("is_bot") or inf.get("deleted"):
            continue
        msgs.append({"ts": m.get("ts", ""), "user_id": uid,
                     "name": inf.get("name", uid), "email": inf.get("email", ""),
                     "title": inf.get("title", ""),
                     "text": (await _clean_text(m.get("text") or ""))[:600]})
    return {"channel": name, "channel_id": cid, "messages": msgs}


async def read_conversation(target: str, limit: int = 15) -> tuple[bool, str]:
    """Recent messages from a #channel or a person's DM."""
    cid, cands = await _resolve_channel(target)
    if not cid:
        if cands:
            return True, f"Which one, sir — {', '.join(cands)}?"
        return False, (f"Couldn't find a channel or person matching '{target}'. "
                       "For a channel, I can only read ones you've joined.")
    data = await _api("conversations.history", {"channel": cid, "limit": max(1, min(limit, 50))})
    if not data.get("ok"):
        return False, _err(data)
    return True, await _fmt_messages(data.get("messages", []), limit)


_SYSTEM_SUBTYPES = {"channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "bot_message"}


def _is_answer(tm: dict, me: str) -> bool:
    """A message that plausibly ANSWERS (moves a thread forward): substantive,
    not a system notice, and not itself another question mentioning me."""
    if tm.get("subtype") in _SYSTEM_SUBTYPES:
        return False
    if not (tm.get("text") or "").strip():
        return False
    if f"<@{me}>" in (tm.get("text") or "") and tm.get("user") != me:
        return False                                   # another @me question, not an answer
    return True


async def _filter_directed(hits: list, me_name: str) -> tuple[list, int]:
    """Keep only mentions genuinely DIRECTED AT the user (asking them / requesting
    from them), dropping passing mentions or questions aimed at someone else.
    One batched fast-model call; on any failure returns everything (never drops
    a real question just because triage was unavailable). Returns (kept, dropped)."""
    if len(hits) < 1:
        return hits, 0
    try:
        import os as _os
        from services import llm
        numbered = "\n".join(
            f"{i}. {_uname(tm.get('user','')) or '?'}: "
            f"{_re.sub(r'<@([A-Z0-9]+)>', lambda g: '@' + _uname(g.group(1)), (tm.get('text') or ''))[:220]}"
            for i, (tm, _) in enumerate(hits))
        prompt = (
            f"You triage Slack messages that mention {me_name}. For EACH numbered message, "
            f"decide if it is genuinely ASKING {me_name} a question or REQUESTING an action / "
            f"reply FROM {me_name} (directed at them) — versus merely mentioning {me_name} in "
            f"passing, crediting/thanking them, FYI-tagging them, or asking someone ELSE a "
            f"question that just references {me_name}.\n\n{numbered}\n\n"
            f"Reply with ONLY the numbers that are directed at {me_name} and need their reply, "
            f"comma-separated (e.g. 0,2). If none qualify, reply exactly: none")
        fast = _os.getenv("FRIDAY_FAST_MODEL", llm.DEFAULT_MODEL)
        # max_tokens must leave room AFTER the reasoning models' thinking pass —
        # too small (e.g. 40) and the whole budget is consumed before any answer,
        # yielding an empty string (which we'd read as 'keep everything').
        resp = await asyncio.wait_for(
            llm.acreate(model=fast, fallbacks=llm.FAST_FALLBACKS, max_tokens=300,
                        messages=[{"role": "user", "content": prompt}]),
            timeout=25)
        ans = llm.extract_text(resp).strip().lower()
        if ans.startswith("none"):
            return [], len(hits)
        keep_idx = {int(n) for n in _re.findall(r"\d+", ans) if int(n) < len(hits)}
        if not keep_idx:                    # model gave nothing parseable → don't drop
            return hits, 0
        kept = [h for i, h in enumerate(hits) if i in keep_idx]
        return kept, len(hits) - len(kept)
    except Exception:
        return hits, 0                      # triage failed → keep everything


async def list_mentions(target: str, limit: int = 30,
                        include_answered: bool = False,
                        only_directed: bool = True) -> tuple[bool, str]:
    """Messages in a channel that @-mention YOU, are still UNANSWERED, and are
    actually DIRECTED AT YOU — each with the `ts` needed to reply in-thread.

    - Judges EACH question individually, including several inside one thread: a
      mention is 'answered' if a substantive reply (from you OR anyone) appears
      AFTER it. So 'Q1 answered, Q2 open' surfaces only Q2.
    - only_directed (default True): an LLM triage drops passing mentions and
      questions aimed at someone else, keeping only ones asking/requesting YOU.
    - include_answered=True shows all mentions regardless of answer state."""
    cid, cands = await _resolve_channel(target)
    if not cid:
        return False, (f"Which one — {', '.join(cands)}?" if cands
                       else f"Couldn't find channel '{target}' (I can only see ones you've joined).")
    me = await _self_id()
    data = await _api("conversations.history", {"channel": cid, "limit": max(1, min(limit, 100))})
    if not data.get("ok"):
        return False, _err(data)

    roots = [m for m in data.get("messages", []) if m.get("subtype") not in _SYSTEM_SUBTYPES]

    # Fetch full thread contents for any root that has replies (concurrent, capped).
    sem = asyncio.Semaphore(8)

    async def _thread_for(m: dict) -> tuple[dict, list[dict]]:
        if m.get("reply_count", 0) > 0:
            async with sem:
                r = await _api("conversations.replies", {"channel": cid, "ts": m["ts"], "limit": 100})
            if r.get("ok"):
                return m, r.get("messages", [])
        return m, [m]

    threaded = await asyncio.gather(*[_thread_for(m) for m in roots[:min(limit, 60)]])

    hits, answered = [], 0                              # hits: (mention_msg, thread_root_ts)
    for root, thread in threaded:
        thread = sorted(thread, key=lambda x: float(x.get("ts", 0)))   # oldest→newest
        answer_ts = [float(x["ts"]) for x in thread if _is_answer(x, me)]
        for tm in thread:
            if tm.get("subtype") in _SYSTEM_SUBTYPES or tm.get("user") == me:
                continue
            if f"<@{me}>" not in (tm.get("text") or ""):
                continue
            tts = float(tm.get("ts", 0))
            resolved = any(a > tts for a in answer_ts)   # a real reply came after it
            if resolved and not include_answered:
                answered += 1
                continue
            hits.append((tm, root["ts"]))                # reply attaches to the thread root

    if not hits:
        tail = (f" ({answered} already answered)" if answered else "")
        return True, f"No unanswered mentions in that channel, sir{tail}."

    # Resolve author + in-text mention names (needed for triage + display).
    ids = set()
    for tm, _ in hits:
        if tm.get("user"):
            ids.add(tm["user"])
        ids.update(_re.findall(r"<@([A-Z0-9]+)>", tm.get("text") or ""))
    await asyncio.gather(*[_name_for(i) for i in ids if i not in _user_cache])

    # Drop passing mentions / questions aimed at someone else.
    dropped = 0
    if only_directed and not include_answered:
        me_name = await _name_for(me)
        hits, dropped = await _filter_directed(hits, me_name)
    if not hits:
        bits = []
        if dropped:  bits.append(f"{dropped} were passing mentions, not for you")
        if answered: bits.append(f"{answered} already answered")
        tail = (" (" + "; ".join(bits) + ")") if bits else ""
        return True, f"Nothing needs your reply in that channel, sir{tail}."

    hits.sort(key=lambda h: float(h[0].get("ts", 0)))    # oldest→newest
    lines = ["Unanswered messages directed at you (reply in-thread with action=send, "
             f"target=`{target}`, thread=<the ts>). Multiple in one thread share a ts:"]
    for tm, root_ts in hits:
        who = _uname(tm.get("user", "")) or "?"
        text = _re.sub(r"<@([A-Z0-9]+)>", lambda g: "@" + _uname(g.group(1)),
                       (tm.get("text") or "").replace("\n", " ")).strip()
        lines.append(f"- ts={root_ts} | {who}: {text[:280]}")
    return True, "\n".join(lines)


async def unreads(scan_cap: int = 80) -> tuple[bool, str]:
    """Unread DMs and channels. users.counts is blocked for user tokens on
    Enterprise Grid, so this lists the user's conversations and checks each
    one's unread_count_display via conversations.info (bounded concurrency)."""
    convos = await _api("users.conversations", {
        "types": "public_channel,private_channel,mpim,im", "limit": 400,
        "exclude_archived": "true"})
    if not convos.get("ok"):
        return False, _err(convos)
    channels = convos.get("channels", [])[:scan_cap]

    # Keep-alive transport made each conversations.info ~50-150ms — a wider
    # semaphore is safe now that there's no per-call process spawn.
    sem = asyncio.Semaphore(12)

    async def _info(c):
        async with sem:
            r = await _api("conversations.info", {"channel": c["id"]})
        if not r.get("ok"):
            return None
        info = r.get("channel", {})
        n = info.get("unread_count_display", 0)
        if not n:
            return None
        if info.get("is_im"):
            return ("dm", info.get("user", ""), n)   # resolve name lazily below
        return ("ch", info.get("name") or info.get("id"), n)

    results = [r for r in await asyncio.gather(*[_info(c) for c in channels]) if r]
    if not results:
        return True, "No unread messages, sir. All caught up."
    # Resolve just the handful of unread-DM authors' names.
    await asyncio.gather(*[_name_for(uid) for kind, uid, _ in results
                           if kind == "dm" and uid not in _user_cache])
    dm_lines = [f"- {_uname(uid)}: {n}" for kind, uid, n in results if kind == "dm"]
    ch_lines = [f"- #{name}: {n}" for kind, name, n in results if kind == "ch"]
    total = sum(n for _, _, n in results)
    parts = [f"{total} unread across {len(results)} conversations."]
    if dm_lines:
        parts.append("**DMs**\n" + "\n".join(dm_lines))
    if ch_lines:
        parts.append("**Channels**\n" + "\n".join(ch_lines))
    return True, "\n\n".join(parts)


async def search_messages(query: str, count: int = 15) -> tuple[bool, str]:
    """Search messages / mentions. Requires the search:read scope (often
    admin-gated on Enterprise Grid — returns a clear error if missing)."""
    data = await _api("search.messages", {"query": query, "count": max(1, min(count, 30))})
    if not data.get("ok"):
        return False, _err(data)
    matches = ((data.get("messages") or {}).get("matches")) or []
    if not matches:
        return True, f"No messages found for '{query}'."
    lines = []
    for m in matches:
        who = (m.get("username") or _uname(m.get("user", "")) or "?")
        chan = (m.get("channel") or {}).get("name", "")
        text = (m.get("text") or "").replace("\n", " ")[:200]
        lines.append(f"- #{chan} — {who}: {text}")
    return True, "\n".join(lines)


async def whoami() -> tuple[bool, str]:
    data = await _api("auth.test")
    if not data.get("ok"):
        return False, _err(data)
    return True, f"{data.get('user')} on {data.get('team')} ({data.get('url')})"


# ─── Act ─────────────────────────────────────────────────────────────────────

def to_mrkdwn(text: str) -> str:
    """Convert standard/GitHub markdown to Slack's mrkdwn so a structured reply
    renders as ONE properly-formatted message instead of raw asterisks:
      **bold**→*bold*  *italic*→_italic_  ## H→*H*  - item→• item
      [t](url)→<url|t>  ~~s~~→~s~. Newlines stay (Slack keeps them in one msg)."""
    import re
    # Links first (before any * mangling) — [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    # Protect **bold** / __bold__ so the italic pass doesn't touch them.
    text = re.sub(r"\*\*(.+?)\*\*", "\x00\\1\x00", text)
    text = re.sub(r"__(.+?)__", "\x00\\1\x00", text)
    # Remaining single *italic* → _italic_ (Slack italic).
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"_\1_", text)
    text = text.replace("\x00", "*")                       # restore bold
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)             # strikethrough
    # Headings → bold line (Slack has no headings).
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*$", r"*\1*", text, flags=re.M)
    # Bullets: -, *, + at line start → • (Slack renders literal - as a dash).
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)
    # Blockquotes stay (Slack uses > too); collapse 3+ blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def undo_action(entry: dict) -> tuple[bool, str]:
    """Inverse of a journaled outward Slack action (see services/outbox.py).
    The inverse itself is NOT re-journaled — undo must not create more
    undoable history (except status/DND restores, which are idempotent)."""
    action = entry.get("action")
    h = entry.get("handle") or {}
    if action == "send":
        if not (h.get("channel") and h.get("ts")):
            return False, "no channel/ts handle recorded"
        data = await _api("chat.delete", {"channel": h["channel"], "ts": h["ts"]})
        if data.get("ok"):
            return True, "message deleted"
        # Already gone (deleted manually, or the workspace forbids deletion) —
        # treat as idempotent success so undo can tombstone it and move on,
        # instead of retrying this same entry forever and blocking older ones.
        if data.get("error") in ("message_not_found", "cant_delete_message"):
            return True, f"message already gone ({data.get('error')})"
        return False, _err(data)
    if action == "react":
        data = await _api("reactions.remove", {"channel": h.get("channel", ""),
                                               "timestamp": h.get("ts", ""),
                                               "name": h.get("emoji", "")})
        if data.get("ok") or data.get("error") == "no_reaction":
            return True, "reaction removed"
        return False, _err(data)
    if action == "status":
        profile = json.dumps({"status_text": h.get("prev_text", ""),
                              "status_emoji": h.get("prev_emoji", ""),
                              "status_expiration": h.get("prev_expiration", 0) or 0})
        data = await _api("users.profile.set", {"profile": profile})
        return (True, "previous status restored") if data.get("ok") else (False, _err(data))
    if action == "dnd_on":
        data = await _api("dnd.endSnooze")
        return (True, "Do Not Disturb back off") if data.get("ok") else (False, _err(data))
    if action == "dnd_off":
        m = int(h.get("inverse_minutes") or 0)
        if m <= 0:
            return False, "previous snooze length unknown"
        data = await _api("dnd.setSnooze", {"num_minutes": m})
        return (True, f"Do Not Disturb back on for {m} min") if data.get("ok") else (False, _err(data))
    return False, f"no inverse for slack action '{action}'"


async def send_message(target: str, text: str, thread_ts: str = "") -> tuple[bool, str]:
    cid, cands = await _resolve_channel(target)
    if not cid:
        if cands:
            return False, f"Ambiguous recipient — did you mean {', '.join(cands)}?"
        return False, f"Couldn't resolve '{target}' to a channel or person."
    params = {"channel": cid, "text": to_mrkdwn(text), "as_user": "true"}
    if thread_ts:
        params["thread_ts"] = thread_ts.strip()        # reply IN the thread
    data = await _api("chat.postMessage", params)
    if not data.get("ok"):
        return False, _err(data)
    # Journal the send WITH its handle — chat.postMessage returns the ts+channel
    # that chat.delete needs (undo) and promise-mining reads. Never discard it.
    ts = data.get("ts", "")
    outbox.record("slack", "send", target=target, summary=text[:200],
                  handle={"channel": cid, "ts": ts}, undoable=bool(ts))
    where = f"in thread to {target}" if thread_ts else f"to {target}"
    return True, f"Message sent {where}." + (f" (ts {ts})" if ts else "")


async def add_reaction(target: str, emoji: str, which: str = "last") -> tuple[bool, str]:
    """React to the latest (or a specific ts) message in a conversation."""
    cid, cands = await _resolve_channel(target)
    if not cid:
        return False, (f"Ambiguous: {', '.join(cands)}" if cands
                       else f"Couldn't resolve '{target}'.")
    ts = which
    if which == "last":
        hist = await _api("conversations.history", {"channel": cid, "limit": 1})
        msgs = hist.get("messages", [])
        if not msgs:
            return False, "No message to react to."
        ts = msgs[0]["ts"]
    data = await _api("reactions.add", {"channel": cid, "timestamp": ts,
                                        "name": emoji.strip(":")})
    if not data.get("ok") and data.get("error") != "already_reacted":
        return False, _err(data)
    if data.get("ok"):
        # already_reacted is NOT journaled: Cosmos didn't add that reaction,
        # so an undo must not remove it.
        outbox.record("slack", "react", target=target, summary=f":{emoji.strip(':')}:",
                      handle={"channel": cid, "ts": ts, "emoji": emoji.strip(":")},
                      undoable=True)
    return True, f"Reacted :{emoji.strip(':')}: in {target}."


async def set_dnd(minutes: int) -> tuple[bool, str]:
    """minutes>0 → snooze that long; minutes<0 → end snooze. minutes==0 is
    treated as a READ (a model fumbling 0 must NOT silently disable DND)."""
    if minutes > 0:
        data = await _api("dnd.setSnooze", {"num_minutes": minutes})
        if not data.get("ok"):
            return False, _err(data)
        outbox.record("slack", "dnd_on", summary=f"{minutes} min",
                      handle={"inverse_minutes": -1}, undoable=True)
        return True, f"Do Not Disturb on for {minutes} min."
    if minutes < 0:
        # Capture how long the snooze had left so undo can restore it.
        info = await _api("dnd.info")
        left = 0
        if info.get("ok") and info.get("snooze_enabled"):
            left = max(0, int((info.get("snooze_endtime", 0) - time.time()) / 60))
        data = await _api("dnd.endSnooze")
        if not data.get("ok"):
            return False, _err(data)
        outbox.record("slack", "dnd_off",
                      handle={"inverse_minutes": left} if left else {},
                      undoable=left > 0)
        return True, "Do Not Disturb off."
    return await get_dnd()


async def get_dnd() -> tuple[bool, str]:
    data = await _api("dnd.info")
    if not data.get("ok"):
        return False, _err(data)
    if data.get("snooze_enabled"):
        left = int((data.get("snooze_endtime", 0) - time.time()) / 60)
        return True, f"Do Not Disturb is ON ({max(0, left)} min left)."
    return True, "Do Not Disturb is off."


async def set_status(text: str, emoji: str = "", minutes: int = 0) -> tuple[bool, str]:
    emoji = emoji.strip()
    if emoji and not emoji.startswith(":"):
        emoji = f":{emoji.strip(':')}:"
    # Snapshot the CURRENT status first so undo can restore it (best-effort,
    # tightly bounded — this extra read must not make every status set slow).
    prev = {}
    try:
        cur = await _api("users.profile.get", timeout=6)
        if cur.get("ok"):
            p = cur.get("profile") or {}
            prev = {"prev_text": p.get("status_text", ""),
                    "prev_emoji": p.get("status_emoji", ""),
                    "prev_expiration": p.get("status_expiration", 0)}
    except Exception:
        prev = {}
    exp = int(time.time()) + minutes * 60 if minutes else 0
    profile = json.dumps({"status_text": text, "status_emoji": emoji,
                          "status_expiration": exp})
    data = await _api("users.profile.set", {"profile": profile})
    if not data.get("ok"):
        return False, _err(data)
    outbox.record("slack", "status", summary=f"{emoji} {text}".strip(),
                  handle=prev, undoable=bool(prev))
    return True, f'Status set to "{text}"' + (f" {emoji}" if emoji else "") + "."

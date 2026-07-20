"""Slack command bridge — control COSMOS from a dedicated Slack channel.

A Slack app (Socket Mode, so no public endpoint — works behind VPN) sits in
ONE channel. Root messages there from the owner become agent tasks; Cosmos
answers in the message's thread, delivers files with the `slack_deliver`
tool, and bridges risk-gate confirms as "reply yes/no" prompts in the same
thread — so the Mac is fully drivable from a phone.

Protocol (hand-rolled on httpx + websockets, both already deps):
  apps.connections.open (xapp token) → wss URL → JSON envelopes; every
  envelope is ACKed immediately ({"envelope_id": ...}) — Slack redelivers
  unacked envelopes, so processing happens after the ack and `event_id`
  dedupe makes redelivery harmless.

Routing rules:
  - `message` events from THE owner count, in the bridge channel OR the
    bot's DM (mobile-friendly: the app's Messages tab works too); the
    bot's own posts, other users, and edit/delete subtypes are ignored
    (needs the `message.im` event + `im:history` scope for the DM path)
  - root message  → new task ("cosmos"/"friday" prefix optional, stripped;
    bare "stop" cancels whatever is running)
  - thread reply  → answer to a pending confirm/ask on that thread, else a
    follow-up run continuing that thread's conversation (history is keyed
    by thread: conv id "slack-<channel>-<root_ts>")

Config (.env): SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-), optional
SLACK_BRIDGE_CHANNEL (id or #name; auto-detected when the bot is in exactly
one channel), optional SLACK_BRIDGE_OWNER (defaults to the SLACK_USER_TOKEN
identity). Silently stays offline when tokens are missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import websockets

from services import agent, convstore, http_pool, outbox, slack

# ─── Module state ──────────────────────────────────────────────────────────────

BOT_TOKEN = ""
APP_TOKEN = ""

_run_lock: asyncio.Lock | None = None
_socket_task: asyncio.Task | None = None
_worker_task: asyncio.Task | None = None

_channel = ""          # resolved bridge channel id
_owner = ""            # resolved owner user id (only this user may command)
_bot_user = ""         # the app's own user id (echo filter)

_queue: asyncio.Queue | None = None
_seen: deque[str] = deque(maxlen=500)     # event_id dedupe window

# The single active run (queue serializes): thread_ts → run record.
_current: dict | None = None              # {"thread": ts, "task": Task,
                                          #  "interaction": agent.Interaction}

# Task history for the HUD's Slack tab: one record per command, newest last.
# In-memory only — the conversations themselves persist via convstore.
_activity: deque = deque(maxlen=60)
_current_record: dict | None = None       # the _activity entry being executed
_EVENT_CAP = 80                           # per-record event rows (UI timeline)

_STATUS = {"enabled": False, "connected": False, "channel": "", "owner": "",
           "last_event": "", "last_ignored": "", "runs": 0,
           "note": "not started"}

# Where slack_deliver posts while a bridge run is live (single-slot, matches
# the one-run-at-a-time architecture).
_delivery_target: dict | None = None      # {"channel": ..., "thread": ...}

_MAX_MSG = 3900                           # Slack hard limit is 4000

_PREFIX_RE = re.compile(r"^\s*(?:hey\s+|ok\s+|yo\s+)?(?:cosmos|friday)\b[\s,:\-]*",
                        re.IGNORECASE)
_STOP_RE = re.compile(r"^\s*(?:cosmos\s+|friday\s+)?(?:stop|cancel|abort|halt)"
                      r"[\s.!]*$", re.IGNORECASE)
_YESNO_RE = re.compile(r"^\s*(?:y|yes|yeah|yep|sure|approve|approved|go|go ahead|"
                       r"do it|n|no|nope|deny|denied|decline|don'?t)\b",
                       re.IGNORECASE)

_STALE_EVENT_S = 600      # ignore messages older than 10 min (reconnect safety)

# Fire-and-forget acks (👀) — strong refs so the GC can't drop a running task.
_BG_TASKS: set[asyncio.Task] = set()


# ─── Bot-token Web API (httpx keeps the token off argv entirely) ──────────────
# ONE lazily-created keep-alive client (services.http_pool) instead of a fresh
# AsyncClient (TCP+TLS handshake) per Web API call.

async def _bot_api(method: str, params: dict | None = None,
                   timeout: float = 20) -> dict:
    if not BOT_TOKEN:
        return {"ok": False, "error": "no_bot_token"}
    try:
        c = http_pool.get_client("slack_bridge")
        r = await c.post(f"https://slack.com/api/{method}",
                         headers={"Authorization": f"Bearer {BOT_TOKEN}"},
                         data=params or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


async def _post(text: str, thread_ts: str, channel: str = "") -> str:
    """Post one mrkdwn message into a thread. Returns the ts ('' on failure)."""
    data = await _bot_api("chat.postMessage", {
        "channel": channel or _channel, "text": text[:_MAX_MSG],
        "thread_ts": thread_ts, "unfurl_links": "false", "unfurl_media": "false"})
    if not data.get("ok"):
        print(f"[slack-bridge] post failed: {data.get('error')}")
        return ""
    outbox.record("slack_bridge", "bridge_send", target=channel or _channel,
                  summary=text[:120],
                  handle={"channel": data.get("channel"), "ts": data.get("ts")})
    return data.get("ts", "")


async def _post_chunks(text: str, thread_ts: str, channel: str = "") -> None:
    """mrkdwn-convert and post, splitting on line boundaries under the cap."""
    text = slack.to_mrkdwn(text or "(no output)")
    chunk: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > _MAX_MSG and chunk:
            await _post("".join(chunk), thread_ts, channel)
            chunk, size = [], 0
        # A single pathological line still has to fit.
        while len(line) > _MAX_MSG:
            await _post(line[:_MAX_MSG], thread_ts, channel)
            line = line[_MAX_MSG:]
        chunk.append(line)
        size += len(line)
    if chunk:
        await _post("".join(chunk), thread_ts, channel)


_reactions_ok: bool | None = None    # None = untested; False = scope missing


async def _react(ts: str, name: str, remove: bool = False,
                 channel: str = "") -> bool:
    """Best-effort reaction on the command message (👀 working, ✅/❌ done).
    Returns False when it can't (e.g. the bot lacks reactions:write) so the
    caller can fall back to a text acknowledgment."""
    global _reactions_ok
    if _reactions_ok is False:
        return False
    data = await _bot_api("reactions.remove" if remove else "reactions.add",
                          {"channel": channel or _channel, "timestamp": ts,
                           "name": name})
    if not data.get("ok") and data.get("error") in ("missing_scope",
                                                    "not_allowed_token_type"):
        _reactions_ok = False
        print("[slack-bridge] no reactions:write scope — using text acks instead")
    return bool(data.get("ok"))


async def _upload(path: str, thread_ts: str, comment: str = "",
                  channel: str = "") -> tuple[bool, str]:
    """3-step external upload into the thread: get URL → POST bytes → complete."""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return False, f"File not found: {path}"
    size = p.stat().st_size
    if size > 200 * 1024 * 1024:
        return False, "File is over 200 MB — Slack won't take it."
    got = await _bot_api("files.getUploadURLExternal",
                         {"filename": p.name, "length": str(size)})
    if not got.get("ok"):
        return False, f"Slack upload refused: {got.get('error')}"
    try:
        c = http_pool.get_client("slack_bridge")
        r = await c.post(got["upload_url"],
                         files={"file": (p.name, p.read_bytes())},
                         timeout=120)
        if r.status_code != 200:
            return False, f"Upload transfer failed (HTTP {r.status_code})."
    except Exception as e:
        return False, f"Upload transfer failed: {str(e)[:120]}"
    done = await _bot_api("files.completeUploadExternal", {
        "files": json.dumps([{"id": got["file_id"], "title": p.name}]),
        "channel_id": channel or _channel, "thread_ts": thread_ts,
        "initial_comment": comment[:1000]})
    if not done.get("ok"):
        return False, f"Slack upload finalize failed: {done.get('error')}"
    outbox.record("slack_bridge", "bridge_upload", target=channel or _channel,
                  summary=f"uploaded {p.name}",
                  handle={"file_id": got.get("file_id"), "thread": thread_ts})
    if _current_record is not None:
        _current_record["files"].append(p.name)
    return True, p.name


# ─── slack_deliver agent tool ──────────────────────────────────────────────────

async def _tool_slack_deliver(args: dict, ctx) -> str:
    tgt = _delivery_target
    if not tgt:
        return ("Error: no active Slack thread — slack_deliver only works for "
                "tasks that arrived via the Slack bridge.")
    path = (args or {}).get("file_path", "").strip()
    text = (args or {}).get("text", "").strip()
    if not path and not text:
        return "Error: give slack_deliver a file_path and/or text."
    if path:
        ok, out = await _upload(path, tgt["thread"], comment=text,
                                channel=tgt["channel"])
        return (f"Delivered {out} to the Slack thread."
                if ok else f"Error: {out}")
    ts = await _post(slack.to_mrkdwn(text), tgt["thread"], tgt["channel"])
    return "Posted to the Slack thread." if ts else "Error: Slack post failed."


def _register_deliver_tool() -> None:
    try:
        agent.register_tool({
            "name": "slack_deliver",
            "description": (
                "Deliver a FILE (screenshot, document, image — by absolute path) "
                "or an interim text update to the Slack thread the CURRENT task "
                "came from. Only works for tasks started from Slack. Your final "
                "answer is posted to the thread automatically — use this for "
                "files and mid-task artifacts, not to repeat the answer."),
            "input_schema": {"type": "object", "properties": {
                "file_path": {"type": "string",
                              "description": "absolute path of a file to upload"},
                "text": {"type": "string",
                         "description": "message/caption to post"},
            }},
        }, _tool_slack_deliver, gate="open", timeout=150.0,
            label="Slack delivery", source="slack_bridge")
        agent.invalidate_tool_cache()
    except Exception as e:
        print(f"[slack-bridge] deliver tool registration failed: {e}")


# ─── Run execution ─────────────────────────────────────────────────────────────

def _conv_id(channel: str, thread_ts: str) -> str:
    return f"slack-{channel}-{thread_ts}".replace(".", "")


def _preamble(text: str, follow_up: bool) -> str:
    if follow_up:
        return f"[Slack thread follow-up] {text}"
    return (
        "[Task from Slack] The user sent this from Slack (possibly from their "
        "phone, away from the Mac — the screen may be locked). Your final "
        "answer is posted to the Slack thread automatically. Deliver FILES "
        "with slack_deliver(file_path=...). Prefer API/headless tools "
        "(web_snapshot for page screenshots, google, github, "
        "slack, bash) over GUI automation (mouse/keystroke/click_ui/"
        "see_screen/open_app), which can fail with nobody at the machine.\n\n"
        f"Task: {text}")


def _record_event(rec: dict, kind: str, **fields) -> None:
    if len(rec["events"]) >= _EVENT_CAP:
        return
    rec["events"].append({"t": datetime.now().isoformat(timespec="seconds"),
                          "kind": kind, **fields})


async def _execute(cmd: dict) -> None:
    """Run one bridge command through the agent loop, reporting in-thread."""
    global _current, _delivery_target, _current_record
    thread = cmd["thread"]
    chan = cmd.get("channel") or _channel
    interaction = agent.Interaction()
    rec = {"thread": thread, "text": cmd["text"], "ts": cmd["ts"],
           "channel": chan,
           "started": datetime.now().isoformat(timespec="seconds"),
           "status": "queued", "reply": "", "duration_s": 0.0,
           "events": [], "files": []}
    _activity.append(rec)
    t0 = time.monotonic()

    async def emit(event: dict) -> None:
        # Only the human-in-the-loop events cross to Slack; deltas/tool cards
        # stay internal — but everything lands in the activity record so the
        # HUD's Slack tab can show HOW the task went.
        et = event.get("type")
        if et == "tool_start":
            _record_event(rec, "tool", tool=event.get("name") or event.get("tool", ""),
                          label=event.get("label", ""))
        elif et == "tool_done":
            _record_event(rec, "tool_done", ok=bool(event.get("ok")),
                          detail=(event.get("detail") or "")[:200])
        elif et == "agent_thought":
            _record_event(rec, "thought", text=(event.get("text") or "")[:200])
        if et == "confirm_request":
            rec["status"] = "awaiting approval"
            _record_event(rec, "confirm", text=(event.get("summary") or "")[:200])
            steps = event.get("steps") or []
            lines = [f"⚠️ *Approval needed:* {event.get('summary', '')}"]
            if event.get("danger"):
                lines.append(f"_{event['danger']}_")
            lines += [f"  {i}. {s.get('summary', '')}"
                      + (f" — _{s['danger']}_" if s.get("danger") else "")
                      for i, s in enumerate(steps[:12], 1)]
            lines.append("Reply *yes* or *no* in this thread "
                         f"(auto-declines in {int(agent.CONFIRM_TIMEOUT_S)}s).")
            await _post("\n".join(lines), thread, chan)
        elif et == "ask_user":
            rec["status"] = "awaiting reply"
            _record_event(rec, "ask", text=(event.get("question") or "")[:200])
            await _post(f"❓ {event.get('question', 'I need input.')} — reply "
                        "in this thread.", thread, chan)
        elif et == "confirm_timeout":
            rec["status"] = "running"
            _record_event(rec, "timeout")
            await _post("⏰ No answer in time — declined that step and moved on.",
                        thread, chan)
        elif et == "say":
            t = (event.get("text") or "").strip()
            if t:
                _record_event(rec, "say", text=t[:200])
                await _post(f"_{t}_", thread, chan)

    # 👀 acknowledgment is fire-and-forget — the run must not wait ~0.4-0.6s
    # for a Slack round-trip before starting. _react/_post never raise.
    async def _ack() -> None:
        if not await _react(cmd["ts"], "eyes", channel=chan):
            await _post("_👀 on it…_", thread, chan)

    ack_task = asyncio.create_task(_ack())
    _BG_TASKS.add(ack_task)
    ack_task.add_done_callback(_BG_TASKS.discard)
    if (_run_lock and _run_lock.locked() and not cmd.get("queued_note")):
        await _post("⌛ Queued behind a running task — starting as soon as "
                    "it finishes.", thread, chan)

    history = convstore.load(_conv_id(chan, thread))
    prompt = _preamble(cmd["text"], follow_up=bool(history))
    ok, final = False, ""
    _delivery_target = {"channel": chan, "thread": thread}
    _current_record = rec
    rec["status"] = "running"
    try:
        run = asyncio.create_task(_locked_run(prompt, emit, interaction, history))
        _current = {"thread": thread, "task": run, "interaction": interaction}
        final = await run
        ok = True
        rec["status"] = "done"
    except asyncio.CancelledError:
        final = "🛑 Stopped."
        rec["status"] = "stopped"
    except Exception as e:
        final = f"Task failed — {str(e)[:200]}"
        rec["status"] = "error"
    finally:
        _current = None
        _delivery_target = None
        _current_record = None
        rec["reply"] = final[:4000]
        rec["duration_s"] = round(time.monotonic() - t0, 1)
        convstore.save(_conv_id(chan, thread), history)
        # Slack threads reload their history from disk per message, so the
        # DETACHED eviction roll's rewrite would die with this list — re-save
        # once the roll settles (no reply latency; the reply posts below).
        async def _resave(cid: str, h: list) -> None:
            try:
                await agent.settle_history(h)
                convstore.save(cid, h)
            except Exception:
                pass
        resave = asyncio.create_task(_resave(_conv_id(chan, thread), history))
        _BG_TASKS.add(resave)
        resave.add_done_callback(_BG_TASKS.discard)
        _STATUS["runs"] += 1
    await _react(cmd["ts"], "white_check_mark" if ok else "x", channel=chan)
    await _post_chunks(final, thread, chan)


async def _locked_run(prompt, emit, interaction, history) -> str:
    assert _run_lock is not None
    async with _run_lock:
        return await agent.run_task(prompt, emit, interaction,
                                    history=history, mode="ask")


async def _worker() -> None:
    """One command at a time, in arrival order."""
    assert _queue is not None
    while True:
        cmd = await _queue.get()
        try:
            await _execute(cmd)
        except Exception as e:
            print(f"[slack-bridge] run error (non-fatal): {e}")
        finally:
            _queue.task_done()


# ─── Event routing ─────────────────────────────────────────────────────────────

def _dedupe(key: str) -> bool:
    """True if this event was already seen (and remember it)."""
    if not key or key in _seen:
        return bool(key)
    _seen.append(key)
    return False


def _ignored(reason: str) -> None:
    """A HUMAN message was seen but not acted on — record why, so 'it's not
    picking my messages up' is diagnosable from the Slack tab in seconds."""
    _STATUS["last_ignored"] = (f"{reason} @ "
                               f"{datetime.now().strftime('%H:%M:%S')}")
    print(f"[slack-bridge] ignored message: {reason}")


async def _on_message(ev: dict) -> None:
    # The bot's own posts echo back as events — drop them before anything
    # else (and never count them as "ignored": they'd drown the diagnostics).
    user = ev.get("user", "")
    if ev.get("bot_id") or not user or user == _bot_user:
        return
    chan = ev.get("channel", "")
    is_dm = ev.get("channel_type") == "im" or chan.startswith("D")
    if chan != _channel and not is_dm:
        return                                     # some other channel's noise
    if user != _owner:
        _ignored(f"not the owner ({user})")
        return
    if ev.get("subtype") not in (None, "file_share", "thread_broadcast"):
        _ignored(f"subtype {ev.get('subtype')} (edits/deletes don't run)")
        return
    text = (ev.get("text") or "").strip()
    ts = ev.get("ts", "")
    try:
        if time.time() - float(ts) > _STALE_EVENT_S:
            _ignored("older than 10 min (offline backlog is never replayed)")
            return
    except (TypeError, ValueError):
        return
    if not text:
        _ignored("empty text (attachment-only messages need a caption)")
        return
    _STATUS["last_event"] = datetime.now().isoformat(timespec="seconds")

    thread = ev.get("thread_ts")
    if _STOP_RE.match(text):
        cur = _current
        if cur and (not thread or thread == cur["thread"]):
            cur["interaction"].cancel()
            cur["task"].cancel()
            await _react(ts, "octagonal_sign", channel=chan)
        else:
            await _post("Nothing is running.", thread or ts, chan)
        return

    if thread and thread != ts:                    # a reply inside a thread
        cur = _current
        if cur and cur["thread"] == thread:
            if cur["interaction"].pending:
                kind = cur["interaction"].kind
                if kind == "ask" or _YESNO_RE.match(text):
                    cur["interaction"].resolve(text)
                    if _current_record is not None:
                        _record_event(_current_record, "answer", text=text[:120])
                        _current_record["status"] = "running"
                    return
                await _post("There's a pending approval on this thread — reply "
                            "*yes* or *no* first.", thread, chan)
                return
            # The run on this thread is still going and asked nothing — the
            # reply is a follow-up. Queue it (history exists once the run
            # saves); dropping it silently reads as "Cosmos ignored me".
            await _enqueue(_PREFIX_RE.sub("", text) or text, ts, thread, chan)
            return
        # Follow-up on a known Cosmos thread → continue that conversation.
        # "Known" = a conversation file exists (any age) OR a task this session
        # (the file only appears after a thread's FIRST run finishes). We check
        # exists() not load(): a thread whose context has been flushed after 3
        # days is still recognized, so the reply runs fresh instead of being
        # ignored — load() returns [] for it and the run starts a new context.
        if (convstore.exists(_conv_id(chan, thread))
                or any(r.get("thread") == thread for r in _activity)):
            await _enqueue(_PREFIX_RE.sub("", text) or text, ts, thread, chan)
            return
        _ignored("reply in a thread Cosmos never ran — send a new root message")
        return

    # Root message → NOT a command. Only the /cosmos slash command starts a
    # new task; plain messages (channel or DM) are left alone so Cosmos never
    # jumps on ordinary chatter. Thread replies above still continue an
    # existing /cosmos conversation and answer pending confirms.
    _ignored("plain message — use /cosmos <task> to start")


async def _enqueue(text: str, ts: str, thread: str, channel: str) -> None:
    """Queue one command; when it won't start immediately, say so — silent
    queueing is indistinguishable from a dropped message."""
    assert _queue is not None
    busy = _current is not None or _queue.qsize() > 0
    cmd = {"text": text, "ts": ts, "thread": thread, "channel": channel,
           "queued_note": busy}
    await _queue.put(cmd)
    if busy:
        ahead = _queue.qsize() - 1 + (1 if _current else 0)
        await _post(f"⌛ Queued — {ahead} task{'s' if ahead != 1 else ''} ahead. "
                    "I'll get to this as soon as I'm free.", thread, channel)


async def _on_envelope(payload: dict) -> None:
    event = (payload or {}).get("event") or {}
    if event.get("type") != "message":
        return
    key = (payload.get("event_id") or event.get("client_msg_id")
           or f"{event.get('channel')}:{event.get('ts')}")
    if _dedupe(key):
        return
    await _on_message(event)


async def _on_slash(payload: dict) -> None:
    """`/cosmos <text>` — a slash command has no channel message to thread or
    react under, so post a root message first, then reuse the message path."""
    if (payload.get("command") or "").strip() != "/cosmos":
        return
    user = payload.get("user_id", "")
    if user != _owner:
        _ignored(f"/cosmos from non-owner ({user})")
        return
    text = (payload.get("text") or "").strip()
    chan = payload.get("channel_id", "")
    if chan != _channel and not chan.startswith("D"):
        _ignored(f"/cosmos in a non-bridge channel ({chan})")
        return
    if not text:
        await _post("Usage: `/cosmos <what you want done>`", "", chan)
        return
    _STATUS["last_event"] = datetime.now().isoformat(timespec="seconds")
    # Post a visible root so the run has a ts to thread replies + acks under.
    ts = await _post(f"🛰️ *`/cosmos`* {text}", "", chan)
    if not ts:
        return
    await _enqueue(text, ts, ts, chan)


# ─── Identity / channel resolution ─────────────────────────────────────────────

async def _resolve_identity() -> str:
    """Fill _bot_user, _owner, _channel. Returns '' when ready, else a
    human-readable blocker for the status endpoint."""
    global _bot_user, _owner, _channel
    who = await _bot_api("auth.test")
    if not who.get("ok"):
        return f"bot token rejected: {who.get('error')}"
    _bot_user = who.get("user_id", "")

    _owner = os.getenv("SLACK_BRIDGE_OWNER", "").strip()
    if not _owner and slack.AVAILABLE:
        _owner = await slack._self_id()
    if not _owner:
        return ("can't resolve the owner — set SLACK_BRIDGE_OWNER=U… or "
                "configure SLACK_USER_TOKEN")

    want = os.getenv("SLACK_BRIDGE_CHANNEL", "").strip()
    if re.fullmatch(r"[CG][A-Z0-9]{6,}", want):
        _channel = want
        return ""
    convs = await _bot_api("users.conversations", {
        "types": "public_channel,private_channel", "limit": "200"})
    if not convs.get("ok"):
        return f"users.conversations failed: {convs.get('error')}"
    chans = convs.get("channels") or []
    if want:
        name = want.lstrip("#").lower()
        hit = next((c for c in chans if c.get("name", "").lower() == name), None)
        if not hit:
            return (f"bot isn't in a channel named #{name} — /invite it there "
                    "or fix SLACK_BRIDGE_CHANNEL")
        _channel = hit["id"]
        return ""
    if len(chans) == 1:
        _channel = chans[0]["id"]
        return ""
    if not chans:
        return "bot isn't in any channel yet — /invite @<bot> to the bridge channel"
    names = ", ".join("#" + c.get("name", "?") for c in chans[:8])
    return f"bot is in {len(chans)} channels ({names}) — set SLACK_BRIDGE_CHANNEL"


# ─── Socket Mode loop ──────────────────────────────────────────────────────────

async def _socket_loop() -> None:
    backoff = 1.0
    while True:
        blocker = await _resolve_identity()
        if blocker:
            _STATUS.update(connected=False, note=blocker)
            print(f"[slack-bridge] {blocker} — retrying in 60s")
            await asyncio.sleep(60)
            continue
        _STATUS.update(channel=_channel, owner=_owner)
        try:
            c = http_pool.get_client("slack_bridge")
            r = await c.post("https://slack.com/api/apps.connections.open",
                             headers={"Authorization": f"Bearer {APP_TOKEN}"},
                             timeout=15)
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"connections.open: {data.get('error')}")
            async with websockets.connect(data["url"], max_size=2 ** 22) as ws:
                _STATUS.update(connected=True, note="")
                print(f"[slack-bridge] online — channel {_channel}, "
                      f"owner {_owner}")
                backoff = 1.0
                async for raw in ws:
                    try:
                        frame = json.loads(raw)
                    except Exception:
                        continue
                    ftype = frame.get("type")
                    env_id = frame.get("envelope_id")
                    if env_id:                    # ack FIRST — Slack retries fast
                        await ws.send(json.dumps({"envelope_id": env_id}))
                    if ftype == "events_api":
                        try:
                            await _on_envelope(frame.get("payload") or {})
                        except Exception as e:
                            print(f"[slack-bridge] event error: {e}")
                    elif ftype == "slash_commands":
                        try:
                            await _on_slash(frame.get("payload") or {})
                        except Exception as e:
                            print(f"[slack-bridge] slash error: {e}")
                    elif ftype == "disconnect":
                        break                     # reconnect with a fresh URL
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[slack-bridge] socket dropped: {str(e)[:120]}")
        _STATUS.update(connected=False, note="reconnecting")
        await asyncio.sleep(backoff + random.uniform(0, 0.5))
        backoff = min(backoff * 2, 30.0)


# ─── Public API ────────────────────────────────────────────────────────────────

def status() -> dict:
    return {**_STATUS,
            "running": bool(_current),
            "active_thread": _current["thread"] if _current else "",
            "queued": _queue.qsize() if _queue else 0}


def activity() -> dict:
    """Status + task history (newest first) for the HUD's Slack tab."""
    return {"status": status(), "threads": list(reversed(_activity))}


def start(run_lock: asyncio.Lock) -> None:
    """Start the bridge (idempotent). No-op without both tokens — Cosmos
    boots fine with the bridge unconfigured."""
    global BOT_TOKEN, APP_TOKEN, _run_lock, _socket_task, _worker_task, _queue
    BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
    APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "").strip()
    if not (BOT_TOKEN and APP_TOKEN):
        _STATUS["note"] = "SLACK_BOT_TOKEN / SLACK_APP_TOKEN not set"
        print("[slack-bridge] tokens not set — bridge offline")
        return
    _run_lock = run_lock
    _STATUS.update(enabled=True, note="connecting")
    _register_deliver_tool()
    if _queue is None:
        _queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    if not (_worker_task and not _worker_task.done()):
        _worker_task = loop.create_task(_worker())
    if not (_socket_task and not _socket_task.done()):
        _socket_task = loop.create_task(_socket_loop())

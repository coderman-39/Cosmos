"""Panel — a workspace of independent, named agent sessions in a shared space.

Each session is its own Claude conversation: it has a name, its own model,
its own history, and the user chats with it directly. Sessions are added and
removed at will. The USER draws the communication graph — a link A ⇋ B is
BIDIRECTIONAL (the shared blackboard already is): either side can peer_send
the other, peer_fetch the other's context, and both see the same group
memory. A sent message lands in the peer's inbox, glows the edge on the
board, and (bounded by a hop cap) wakes the peer to process it. Context
transfer is explicit and legible: what was shared is exactly what you see.

Two prompt modes (the user toggles on the board):
  singular  — each session is prompted individually (its own task).
  consensus — ONE prompt goes to the whole board; each session gets the same
              task plus divide-the-work instructions scoped to its linked
              team, coordinates via peer_send/blackboard, and the team's
              seat-#1 member merges the findings.

Safety: sessions run the real agentic loop but sandboxed — depth=1,
read_only, unattended. `peer_send` is gate="open" (workspace-internal, along
user-drawn edges only). Ruflo mirrors every shared finding into its semantic
memory (namespace "panel-sessions") so future sessions can recall them.
"""

import asyncio
import contextvars
import itertools
import json
import os
import time
from datetime import datetime
from pathlib import Path

from services import agent, atomicio, llm, ruflo

# ─── Tunables ───────────────────────────────────────────────────────────────────

_SESSION_ITERS   = int(os.getenv("PANEL_WORKER_ITERS", "24"))
_SESSION_BUDGET  = int(os.getenv("PANEL_WORKER_BUDGET", "200000"))
_SESSION_TIMEOUT = float(os.getenv("PANEL_WORKER_TIMEOUT", "600"))
_FEED_CAP = 80          # activity rows kept per session
_CHAT_CAP = 60          # chat entries kept per session (display log)
_HOP_CAP  = 2           # peer message cascade depth (A→B→C, then inbox-only)
_DELIVER_CAP  = 40      # published deliverables kept
_HISTORY_KEEP = 30      # history messages persisted per session

_NAMES = ["Orion", "Vega", "Lyra", "Atlas", "Nova", "Rigel", "Juno", "Echo",
          "Halo", "Ceres", "Mira", "Altair", "Draco", "Vela", "Pavo", "Sirius"]

# ─── State ──────────────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}
_connections: list[dict] = []            # [{"from": sid, "to": sid}] — UNDIRECTED
_subs: set[asyncio.Queue] = set()
_seat_counter = itertools.count()        # stable board positions across removals
_mode = "singular"                       # "singular" | "consensus" (group prompt)
_state_lock = asyncio.Lock()             # guards state mutations vs. debounced save

# The one in-flight consensus launch (None between launches). Teams are
# snapshotted at launch so later graph edits can't stall the barrier:
#   {"id","text","t","teams":[{"members":[sid…seat order],"merger":sid,
#                              "done":{sid:status},"merge_started":bool,"merged":bool}]}
_group_task: dict | None = None

# Published outputs — what the board PRODUCES (panel_deliver tool).
_deliverables: list[dict] = []

# Durable board state (sessions, links, personas, mode, ledger, deliverables).
_STATE_PATH = Path.home() / ".friday" / "panel.json"
_STATE_VERSION = 1
_save_handle: asyncio.TimerHandle | None = None

# Which session (and at what cascade depth) the current asyncio task runs for —
# how the peer_send handler knows its sender. Task-local, so concurrent
# sessions never cross wires.
_cur_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "panel_session", default="")
_cur_hop: contextvars.ContextVar[int] = contextvars.ContextVar(
    "panel_hop", default=0)

_peer_tool_registered = False
_ruflo_swarm = ""

# Shared project memory — ONE log for each connected group (blackboard).
# Every turn appends a one-line "who did what → outcome"; every session's
# prompt leads with its group's log, so any agent knows the project status
# before it starts. Entries carry an audience snapshot so history written
# while A,B,C were wired together survives later graph changes.
_LEDGER_CAP = 300
_LEDGER_SHOW = 30            # most recent entries injected per prompt
_ledger: list[dict] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _emit(etype: str, **fields) -> None:
    ev = {"type": etype, "t": _now(), **fields}
    for q in list(_subs):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass                          # slow client drops frames, never blocks


def _feed(sess: dict, kind: str, **fields) -> None:
    row = {"kind": kind, "t": _now(), **fields}
    sess["feed"].append(row)
    del sess["feed"][:-_FEED_CAP]
    _emit("session_event", session_id=sess["id"], event=row)


def _chat_entry(sess: dict, who: str, text: str) -> None:
    entry = {"who": who, "text": text[:4000], "t": _now()}
    sess["chat"].append(entry)
    del sess["chat"][:-_CHAT_CAP]
    _emit("session_chat", session_id=sess["id"], entry=entry)


def _set_status(sess: dict, status: str) -> None:
    sess["status"] = status
    _emit("session_status", session_id=sess["id"], status=status)


# ─── Durable board state (atomic tmp + os.replace, debounced) ───────────────────

def _save_now() -> None:
    """Serialize the whole board. Never raises — persistence must not break
    a run. Live-only fields (status/task/lock/feed/ruflo) stay out.
    Snapshot-copies mutable lists so an async _run_turn modifying state
    across await points can't produce a partial read."""
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        snap_sessions = list(_sessions.values())
        snap_conns = list(_connections)
        snap_ledger = list(_ledger[-_LEDGER_CAP:])
        snap_delivs = list(_deliverables[-_DELIVER_CAP:])
        data = {"v": _STATE_VERSION, "mode": _mode,
                "connections": snap_conns,
                "ledger": snap_ledger,
                "deliverables": snap_delivs,
                "sessions": [{
                    "id": s["id"], "name": s["name"], "model": s["model"],
                    "persona": s.get("persona", ""), "seat": s["seat"],
                    "created": s["created"],
                    "chat": list(s["chat"][-_CHAT_CAP:]),
                    "inbox": list(s["inbox"][-20:]),
                    "history": list(s["history"][-_HISTORY_KEEP:]),
                } for s in snap_sessions]}
        if not atomicio.write_json_atomic(_STATE_PATH, data, default=str):
            print("[panel] state save failed (non-fatal): could not write panel.json")
    except Exception as e:
        print(f"[panel] state save failed (non-fatal): {e}")


def _save_soon() -> None:
    """Debounced save: burst mutations (a whole turn's writes) coalesce into
    one disk write. Outside a loop (tests, sync callers) saves inline."""
    global _save_handle
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _save_now()
        return
    if _save_handle is not None:
        _save_handle.cancel()
    _save_handle = loop.call_later(1.5, _save_now)


def _load_state() -> None:
    """Restore the board on process start. Corrupt/missing file → fresh board.
    Restored sessions come back idle (a restart kills any in-flight turn)."""
    global _mode, _seat_counter
    try:
        data = json.loads(_STATE_PATH.read_text())
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[panel] state load failed (starting fresh): {e}")
        return
    try:
        if data.get("mode") in ("singular", "consensus"):
            _mode = data["mode"]
        _ledger.extend(e for e in (data.get("ledger") or []) if isinstance(e, dict))
        _deliverables.extend(d for d in (data.get("deliverables") or [])
                             if isinstance(d, dict))
        max_seat = -1
        for row in data.get("sessions") or []:
            sid = str(row.get("id") or "")
            if not sid:
                continue
            seat = int(row.get("seat") or 0)
            max_seat = max(max_seat, seat)
            _sessions[sid] = {
                "id": sid, "name": str(row.get("name") or sid[:6])[:24],
                "model": str(row.get("model") or ""),
                "persona": str(row.get("persona") or "")[:500],
                "status": "idle", "seat": seat,
                "created": row.get("created") or _now(),
                "history": row.get("history") or [],
                "chat": row.get("chat") or [], "feed": [],
                "inbox": row.get("inbox") or [], "current": "",
                "ruflo_id": "", "task": None, "lock": asyncio.Lock()}
        _seat_counter = itertools.count(max_seat + 1)
        _connections.extend(
            c for c in (data.get("connections") or [])
            if isinstance(c, dict) and c.get("from") in _sessions
            and c.get("to") in _sessions)
        if _sessions:
            print(f"[panel] restored {len(_sessions)} session(s), "
                  f"{len(_connections)} link(s) from {_STATE_PATH.name}")
    except Exception as e:
        print(f"[panel] state restore failed (starting fresh): {e}")
        _sessions.clear()
        _connections.clear()


# ─── Models ─────────────────────────────────────────────────────────────────────

def models() -> list[str]:
    """Every model the gateway is configured for — the per-session choices."""
    out: list[str] = []
    for m in [agent.AGENT_MODEL, agent.FAST_MODEL,
              *llm.AGENT_FALLBACKS, *llm.FAST_FALLBACKS]:
        if m and m not in out:
            out.append(m)
    return out


# ─── Shared group memory (blackboard) ───────────────────────────────────────────

def _component(sid: str) -> set[str]:
    """The connected group: everyone reachable from sid over edges in EITHER
    direction. A–B plus B–C puts A, B and C in one group — one shared memory."""
    seen, frontier = {sid}, [sid]
    while frontier:
        cur = frontier.pop()
        for c in _connections:
            other = (c["to"] if c["from"] == cur
                     else c["from"] if c["to"] == cur else "")
            if other and other in _sessions and other not in seen:
                seen.add(other)
                frontier.append(other)
    return seen


def _ledger_write(sess: dict, line: str) -> None:
    entry = {"t": _now(), "author": sess["id"], "author_name": sess["name"],
             "line": line[:220], "audience": sorted(_component(sess["id"]))}
    _ledger.append(entry)
    del _ledger[:-_LEDGER_CAP]
    _emit("ledger", entry=entry)
    _save_soon()
    if ruflo.available():
        asyncio.create_task(ruflo.memory_store(
            f"ledger-{sess['name']}-{int(time.time() * 1000):x}",
            line, namespace="panel-ledger"))


def _ledger_view(sid: str) -> list[dict]:
    """Entries this session may see: authored by anyone currently in its
    group, or written while it was in the author's group (audience snapshot),
    or its own."""
    comp = _component(sid)
    return [e for e in _ledger
            if e["author"] in comp or sid in e["audience"]]


# ─── peer_send / peer_fetch tools (context transfer along user-drawn edges) ─────

def _peers_of(sid: str) -> list[dict]:
    """Direct neighbours over UNDIRECTED links — a link drawn either way lets
    both ends message each other (memory is shared both ways already)."""
    out, seen = [], set()
    for c in _connections:
        other = (c["to"] if c["from"] == sid
                 else c["from"] if c["to"] == sid else "")
        if other and other in _sessions and other not in seen:
            seen.add(other)
            out.append(_sessions[other])
    return out


async def _tool_peer_send(args: dict, ctx) -> str:
    sid = _cur_session.get()
    sender = _sessions.get(sid)
    if not sender:
        return "Error: peer_send only works inside a Panel session run."
    to = str(args.get("to") or "").strip()
    text = str(args.get("text") or "").strip()
    if not (to and text):
        return "Error: peer_send needs both `to` (session name) and `text`."
    peers = _peers_of(sid)
    target = next((p for p in peers
                   if p["name"].lower() == to.lower() or p["id"] == to), None)
    if target is None:
        names = ", ".join(p["name"] for p in peers) or "none"
        return (f"Error: not linked to '{to}'. "
                f"You can reach: {names}. The user draws links.")
    hop = _cur_hop.get() + 1
    msg = {"from": sid, "from_name": sender["name"], "text": text[:4000],
           "hop": hop, "t": _now()}
    target["inbox"].append(msg)
    _feed(sender, "peer_out", to=target["name"], text=text[:200])
    _feed(target, "peer_in", **{"from": sender["name"], "text": text[:200]})
    _ledger_write(sender, f"sent {target['name']} a finding: {text[:140]}")
    _emit("edge", **{"from": sid, "to": target["id"], "kind": "peer"})
    if ruflo.available():                 # shared findings → semantic memory
        asyncio.create_task(ruflo.memory_store(
            f"{sender['name']}->{target['name']}-{int(time.time())}",
            text, namespace="panel-sessions"))
    # Wake the receiver (bounded cascade); a busy peer drains its inbox on
    # its next turn instead.
    if hop <= _HOP_CAP and target["status"] == "idle":
        # Track the wake-up like a chat turn so stop() can cancel it.
        target["task"] = asyncio.create_task(
            _run_turn(target, "", hop=hop, source="peer"))
        return f"Delivered to {target['name']} — they're processing it now."
    return f"Delivered to {target['name']}'s inbox."


async def _tool_peer_fetch(args: dict, ctx) -> str:
    """Deep dive into a group member's context: the shared memory holds one-
    line summaries; this returns the peer's actual recent transcript."""
    sid = _cur_session.get()
    if sid not in _sessions:
        return "Error: peer_fetch only works inside a Panel session run."
    name = str(args.get("session") or "").strip()
    group = [_sessions[x] for x in _component(sid) if x != sid]
    target = next((p for p in group
                   if p["name"].lower() == name.lower() or p["id"] == name), None)
    if target is None:
        names = ", ".join(p["name"] for p in group) or "none"
        return f"Error: '{name}' is not in your connected group. Available: {names}."
    _feed(_sessions[sid], "peer_fetch", **{"from": target["name"]})
    _emit("edge", **{"from": target["id"], "to": sid, "kind": "fetch"})
    lines = [f"=== {target['name']} — full recent context "
             f"(status: {target['status']}"
             + (f", currently working on: {target['current']}" if target.get("current") else "")
             + ") ==="]
    for e in target["chat"][-12:]:
        who = "user" if e["who"] == "you" else target["name"]
        lines.append(f"[{e['t'][11:19]}] {who}: {e['text'][:1500]}")
    if len(lines) == 1:
        lines.append("(no conversation yet)")
    return "\n".join(lines)


async def _tool_panel_deliver(args: dict, ctx) -> str:
    """Publish a deliverable — the board's actual OUTPUT, not chat exhaust.
    Workspace-internal write (panel state only), so it stays gate='open'."""
    sid = _cur_session.get()
    sender = _sessions.get(sid)
    if not sender:
        return "Error: panel_deliver only works inside a Panel session run."
    title = str(args.get("title") or "").strip()[:120]
    content = str(args.get("content") or "").strip()
    if not (title and content):
        return "Error: panel_deliver needs both `title` and `content` (markdown)."
    d = {"id": f"d-{int(time.time() * 1000):x}-{len(_deliverables)}",
         "title": title, "content": content[:60000],
         "author": sid, "author_name": sender["name"],
         "task": (_group_task or {}).get("text", "")[:200],
         "team": sorted(_sessions[x]["name"] for x in _component(sid)),
         "t": _now()}
    _deliverables.append(d)
    del _deliverables[:-_DELIVER_CAP]
    _feed(sender, "deliver", title=title)
    _ledger_write(sender, f"published deliverable: {title}")
    _emit("deliverable", deliverable=d)
    _save_soon()
    return (f"Published “{title}” to the board's OUTPUT tab. "
            f"Don't repeat its full content in chat — a one-line summary is enough.")


def _ensure_peer_tool() -> None:
    global _peer_tool_registered
    if _peer_tool_registered:
        return
    agent.register_tool({
        "name": "peer_send",
        "description": ("Send a finding or message to a CONNECTED agent session "
                        "on the Panel workspace (the user draws connections). "
                        "Use for genuinely useful, distilled findings — not "
                        "chatter. `to` is the peer session's name."),
        "input_schema": {"type": "object", "properties": {
            "to": {"type": "string", "description": "Target session name"},
            "text": {"type": "string", "description": "The finding/message"},
        }, "required": ["to", "text"]},
    }, _tool_peer_send, gate="open", label="message a connected agent session")
    agent.register_tool({
        "name": "peer_fetch",
        "description": ("Fetch the FULL recent conversation of a session in "
                        "your connected group. Use when the shared PROJECT "
                        "MEMORY shows a peer already worked on something and "
                        "you need the actual detail, not the one-line summary."),
        "input_schema": {"type": "object", "properties": {
            "session": {"type": "string", "description": "Peer session name"},
        }, "required": ["session"]},
    }, _tool_peer_fetch, gate="open", label="read a connected session's context")
    agent.register_tool({
        "name": "panel_deliver",
        "description": ("Publish your FINAL polished answer as a deliverable "
                        "document on the Panel board (markdown). This is the "
                        "board's real output — use it for the finished result "
                        "(a merged team answer, a report, a ranked list), not "
                        "for progress chatter. One call per finished piece."),
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short document title"},
            "content": {"type": "string",
                        "description": "The full deliverable, markdown"},
        }, "required": ["title", "content"]},
    }, _tool_panel_deliver, gate="open", label="publish a deliverable document")
    agent.invalidate_tool_cache()
    _peer_tool_registered = True


# ─── Session lifecycle ──────────────────────────────────────────────────────────

def _auto_name() -> str:
    used = {s["name"] for s in _sessions.values()}
    for n in _NAMES:
        if n not in used:
            return n
    return f"Agent-{len(_sessions) + 1}"


def _unique_name(base: str) -> str:
    """peer_send targets by name — duplicates would misroute, so suffix them
    (a second Debate-trio spawn gets Judge 2, Advocate 2, …)."""
    used = {s["name"] for s in _sessions.values()}
    if base not in used:
        return base
    for i in range(2, 99):
        cand = f"{base} {i}"[:24]
        if cand not in used:
            return cand
    return f"{base}-{next(_seat_counter)}"[:24]


def create_session(name: str = "", model: str = "", persona: str = "") -> dict:
    _ensure_peer_tool()
    seat = next(_seat_counter)
    sid = f"s-{int(time.time() * 1000):x}-{seat}"
    base = (name or "").strip()[:24] or _auto_name()
    sess = {"id": sid, "name": _unique_name(base),
            "model": (model or "").strip(),
            "persona": (persona or "").strip()[:500], "status": "idle",
            "seat": seat, "created": _now(),
            "history": [], "chat": [], "feed": [], "inbox": [],
            "current": "", "ruflo_id": "", "task": None, "lock": asyncio.Lock()}
    _sessions[sid] = sess
    _emit("session_add", session=_public(sess))
    _save_soon()
    if ruflo.available():
        asyncio.create_task(_register_with_ruflo(sess))
    return _public(sess)


def set_persona(sid: str, persona: str) -> bool:
    sess = _sessions.get(sid)
    if sess is None:
        return False
    sess["persona"] = (persona or "").strip()[:500]
    _emit("session_persona", session_id=sid, persona=sess["persona"])
    _save_soon()
    return True


async def _register_with_ruflo(sess: dict) -> None:
    global _ruflo_swarm
    try:
        if not _ruflo_swarm:
            # The user draws arbitrary edges — mesh is the honest topology.
            _ruflo_swarm = await ruflo.swarm_init("mesh", 16, "specialized")
        rid = await ruflo.agent_spawn("coder", _ruflo_swarm)
        if rid and sess["id"] in _sessions:
            sess["ruflo_id"] = rid
            _emit("ruflo", swarm_id=_ruflo_swarm, session_id=sess["id"])
    except Exception as e:
        print(f"[panel] ruflo registration skipped: {str(e)[:100]}")


def remove_session(sid: str) -> bool:
    global _connections
    sess = _sessions.pop(sid, None)
    if sess is None:
        return False
    if sess.get("task") and not sess["task"].done():
        sess["task"].cancel()
    before = len(_connections)
    _connections = [c for c in _connections if sid not in (c["from"], c["to"])]
    if len(_connections) != before:
        _emit("connections", connections=list(_connections))
    _emit("session_remove", session_id=sid)
    # A removed member must not deadlock a waiting consensus barrier.
    _group_mark_done(sid, "removed")
    _save_soon()
    return True


def connect(src: str, dst: str) -> str:
    """'' on success, else a human-readable reason. Links are UNDIRECTED —
    a reverse duplicate is the same link."""
    if src not in _sessions or dst not in _sessions:
        return "unknown session"
    if src == dst:
        return "a session can't link to itself"
    if any({c["from"], c["to"]} == {src, dst} for c in _connections):
        return "already linked"
    _connections.append({"from": src, "to": dst})
    _emit("connection_add", **{"from": src, "to": dst})
    _save_soon()
    return ""


def disconnect(src: str, dst: str) -> bool:
    """Remove the undirected link between src and dst (either stored order)."""
    global _connections
    before = len(_connections)
    _connections = [c for c in _connections
                    if {c["from"], c["to"]} != {src, dst}]
    if len(_connections) != before:
        _emit("connection_remove", **{"from": src, "to": dst})
        _save_soon()
        return True
    return False


def set_model(sid: str, model: str) -> bool:
    sess = _sessions.get(sid)
    if sess is None:
        return False
    sess["model"] = (model or "").strip()
    _emit("session_model", session_id=sid, model=sess["model"])
    _save_soon()
    return True


# ─── Running a turn ─────────────────────────────────────────────────────────────

def _preamble(sess: dict) -> str:
    peers = ", ".join(p["name"] for p in _peers_of(sess["id"]))
    group = _component(sess["id"]) - {sess["id"]}
    return (f"You are '{sess['name']}', an independent agent session on the "
            f"Panel workspace. "
            + (f"YOUR ROLE (stay in it every turn): {sess['persona']} "
               if sess.get("persona") else "")
            + (f"Your connected group shares the PROJECT MEMORY below — check "
               f"it FIRST: if another session already did (or is doing) "
               f"something, don't redo it; when you need the full detail "
               f"behind a memory line, call peer_fetch on that session. "
               if group else
               "You are not connected to any other session, so you cannot see "
               "what other agents are doing — the user draws connections (⚡) "
               "on the Panel board. ")
            + (f"You are linked (both ways) with: {peers} — message them via "
               f"peer_send, read their full context via peer_fetch. " if peers else "")
            + "Answer the user directly and concisely.")


def _consensus_block(sess: dict) -> str:
    """Divide-and-conquer instructions for a group prompt, scoped to this
    session's linked team. Personas upgrade the division from mechanical
    seat-slices to role-based stances; seat order still fixes the merger, so
    every member has the same view of who consolidates — no negotiation."""
    team = sorted((_sessions[x] for x in _component(sess["id"])),
                  key=lambda s: s["seat"])
    names = [t["name"] for t in team]
    k = len(team)
    if k == 1:
        return ("CONSENSUS MODE: this task was sent to every session on the "
                "board, but you are not linked to anyone, so you work it "
                "alone. (The user draws links to form teams.) When you are "
                "done, publish your answer with panel_deliver(title, content).")
    i = next(idx for idx, t in enumerate(team, 1) if t["id"] == sess["id"])
    roster = ", ".join(
        f"{t['name']}" + (f" [{t['persona'][:60]}]" if t.get("persona") else "")
        for t in team)
    division = (
        f"(1) Address the task strictly from YOUR ROLE (stated above) — "
        f"complement, never duplicate, your teammates' roles. "
        if sess.get("persona") else
        f"(1) Split the task into {k} complementary slices by seat order "
        f"and do ONLY slice #{i} — never the whole task. ")
    return (f"CONSENSUS MODE — one task, one team. Team (seat order): "
            f"{roster}; you are #{i} of {k}. Everyone received this "
            f"same task simultaneously. Rules: "
            + division +
            f"(2) State which slice/angle you took in your reply's first line. "
            f"(3) Check PROJECT MEMORY and peer messages first — don't "
            f"duplicate a teammate; peer_fetch when you need their detail. "
            f"(4) When your part is done, peer_send the distilled result to "
            f"your linked peers. "
            + (f"(5) As #1 you are also the merger: when every teammate has "
               f"finished you will be woken automatically to consolidate all "
               f"findings and publish the final answer via panel_deliver."
               if i == 1 else
               f"(5) {names[0]} (#1) merges the team's findings at the end — "
               f"make sure your result reaches them (directly or via a "
               f"linked relay)."))


def _build_prompt(sess: dict, text: str, consensus: bool = False) -> str:
    parts = [_preamble(sess)]
    if consensus:
        parts.append(_consensus_block(sess))
    group = [_sessions[x] for x in _component(sess["id"]) if x != sess["id"]]
    if group:
        live = "\n".join(
            f"- {p['name']} ({p['status']})"
            + (f": {p['current'][:140]}" if p.get("current") else "")
            for p in group)
        parts.append(f"LIVE PEERS:\n{live}")
    view = _ledger_view(sess["id"])
    if view:
        log = "\n".join(f"[{e['t'][11:19]}] {e['author_name']}: {e['line']}"
                        for e in view[-_LEDGER_SHOW:])
        parts.append("PROJECT MEMORY (shared log of your connected group — "
                     "the project status; check before starting work):\n" + log)
    inbox, sess["inbox"] = sess["inbox"], []
    for m in inbox:
        parts.append(f"[Message from {m['from_name']}]: {m['text']}")
    if text:
        parts.append(text)
    elif inbox:
        parts.append("Consider the message(s) above: integrate them with your "
                     "prior context and respond. If a connected peer would "
                     "benefit, peer_send them the distilled takeaway.")
    return "\n\n".join(parts)


async def _run_turn(sess: dict, text: str, hop: int, source: str) -> None:
    """One conversation turn for one session (user chat or peer wake-up).
    Serialized per session; different sessions run concurrently."""
    async with sess["lock"]:
        if sess["id"] not in _sessions:      # removed while queued
            return
        tok_s = _cur_session.set(sess["id"])
        tok_h = _cur_hop.set(hop)
        sess["current"] = text or "processing a peer message"
        _set_status(sess, "working")
        if source in ("user", "group"):
            _chat_entry(sess, "you", text)

        async def emit(event: dict) -> None:
            et = event.get("type")
            if et == "agent_thought":
                _feed(sess, "thought", text=(event.get("text") or "")[:200])
            elif et == "tool_start":
                _feed(sess, "tool",
                      tool=event.get("name") or event.get("tool", "tool"),
                      label=(event.get("label") or "")[:160])
            elif et == "tool_done":
                _feed(sess, "tool_done", ok=bool(event.get("ok")),
                      detail=(event.get("detail") or "")[:160])

        _agent_task = None
        outcome_status = "ok"
        try:
            prompt = _build_prompt(sess, text, consensus=(source == "group"))
            coro = agent.run_task(prompt, emit,
                                  agent.Interaction(), history=sess["history"],
                                  mode="ask", unattended=True, depth=1,
                                  read_only=True, max_iterations=_SESSION_ITERS,
                                  token_budget=_SESSION_BUDGET,
                                  model=sess["model"])
            _agent_task = asyncio.ensure_future(coro)
            final = await asyncio.wait_for(
                asyncio.shield(_agent_task), timeout=_SESSION_TIMEOUT)
            if final.startswith(("I hit my step limit",
                                 "I've hit my compute budget")):
                _feed(sess, "error", text=final[:200])
            _chat_entry(sess, sess["name"], final)
            # One line into the group's shared memory: what was asked → outcome.
            outcome = next((ln.strip() for ln in final.splitlines()
                            if ln.strip()), "")[:120]
            ask = (text or "processed a peer message")[:90]
            _ledger_write(sess, f"{ask} → {outcome}")
        except asyncio.CancelledError:
            outcome_status = "cancelled"
            if _agent_task and not _agent_task.done():
                _agent_task.cancel()
            _feed(sess, "error", text="cancelled")
            raise
        except asyncio.TimeoutError:
            outcome_status = "timeout"
            if _agent_task and not _agent_task.done():
                _agent_task.cancel()
            _feed(sess, "error", text="turn timed out")
            _chat_entry(sess, sess["name"], "(turn timed out)")
        except Exception as e:
            outcome_status = "error"
            _feed(sess, "error", text=llm.sanitize_error(e))
            _chat_entry(sess, sess["name"], f"(error: {llm.sanitize_error(e)})")
        finally:
            _cur_session.reset(tok_s)
            _cur_hop.reset(tok_h)
            sess["current"] = ""
            if sess["id"] in _sessions:
                _set_status(sess, "idle")
            # Consensus barrier bookkeeping — even failed/cancelled slices
            # count, so the merge can never deadlock on a dead member.
            if source == "group":
                _group_mark_done(sess["id"], outcome_status)
            elif source == "merge":
                _group_mark_merged(sess["id"], outcome_status)
            _save_soon()


def chat(sid: str, text: str) -> bool:
    """User → session message. Runs in the background; events stream live.
    If the session is already busy, the message is queued via the per-session
    lock inside _run_turn (callers see immediate True)."""
    _ensure_peer_tool()      # restored boards never went through create_session
    sess = _sessions.get(sid)
    text = (text or "").strip()
    if sess is None or not text:
        return False
    # Cancel any stale done-but-not-cleared task reference before creating a new
    # one. If the session is genuinely busy, the lock in _run_turn serializes.
    if sess.get("task") and sess["task"].done():
        sess["task"] = None
    sess["task"] = asyncio.create_task(_run_turn(sess, text, hop=0, source="user"))
    return True


def stop(sid: str) -> bool:
    sess = _sessions.get(sid)
    if sess and sess.get("task") and not sess["task"].done():
        sess["task"].cancel()
        return True
    return False


# ─── Modes: singular (per-session prompts) / consensus (one group prompt) ───────

def set_mode(mode: str) -> bool:
    global _mode
    if mode not in ("singular", "consensus"):
        return False
    if mode != _mode:
        _mode = mode
        _emit("panel_mode", mode=mode)
        _save_soon()
    return True


def group_chat(text: str) -> int:
    """Consensus mode: ONE prompt for the whole board. Every session receives
    the same task plus divide-the-work instructions scoped to its linked team
    (disconnected sessions are told they work alone). Teams are snapshotted
    here so the done-barrier can't be stalled by later graph edits. Returns
    sessions started."""
    global _group_task
    _ensure_peer_tool()
    text = (text or "").strip()
    if not text or not _sessions:
        return 0
    teams, seen = [], set()
    for sid in _sessions:
        if sid in seen:
            continue
        comp = sorted((_sessions[x] for x in _component(sid)),
                      key=lambda s: s["seat"])
        seen.update(s["id"] for s in comp)
        teams.append({"members": [s["id"] for s in comp],
                      "merger": comp[0]["id"], "done": {},
                      "merge_started": False, "merged": False})
    _group_task = {"id": f"g-{int(time.time() * 1000):x}", "text": text,
                   "t": _now(), "teams": teams}
    _emit("group_task", task=_group_public())
    started = 0
    for sess in list(_sessions.values()):
        if sess.get("task") and sess["task"].done():
            sess["task"] = None
        sess["task"] = asyncio.create_task(
            _run_turn(sess, text, hop=0, source="group"))
        started += 1
    return started


def _group_public() -> dict | None:
    gt = _group_task
    if not gt:
        return None
    return {"id": gt["id"], "text": gt["text"][:200], "t": gt["t"],
            "teams": [{"members": t["members"],
                       "names": [_sessions[m]["name"] for m in t["members"]
                                 if m in _sessions],
                       "merger": t["merger"],
                       "done": len(t["done"]), "total": len(t["members"]),
                       "merge_started": t["merge_started"],
                       "merged": t["merged"]} for t in gt["teams"]]}


def _merge_prompt(team: dict, gt: dict) -> str:
    k = len(team["members"])
    fails = [_sessions[m]["name"] for m, st in team["done"].items()
             if st != "ok" and m in _sessions]
    return (f"ALL {k} members of your team have now finished their parts of "
            f"the task: “{gt['text'][:300]}”. Their reports are in the PROJECT "
            f"MEMORY and your inbox — peer_fetch any teammate whose full "
            f"detail you need. "
            + (f"Note: {', '.join(fails)} did not finish cleanly — merge what "
               f"exists and flag the gap honestly. " if fails else "")
            + "Consolidate everything into the team's single final answer "
              "NOW, then publish it with panel_deliver(title, content) as a "
              "self-contained markdown document.")


def _group_mark_done(sid: str, status: str) -> None:
    """Barrier bookkeeping: record one member's finished slice; when a team
    is complete, auto-wake its merger (seat #1) with the merge prompt.
    Failed/removed/cancelled slices count as done — the barrier degrades to
    an honest partial merge, never a deadlock."""
    gt = _group_task
    if not gt:
        return
    for ti, team in enumerate(gt["teams"]):
        if sid not in team["members"] or sid in team["done"]:
            continue
        team["done"][sid] = status
        _emit("group_progress", task_id=gt["id"], team=ti,
              done=len(team["done"]), total=len(team["members"]),
              member=sid, status=status)
        if len(team["done"]) < len(team["members"]) or team["merge_started"]:
            return
        team["merge_started"] = True
        merger = _sessions.get(team["merger"])
        if merger is None or len(team["members"]) == 1:
            # Solo team (did the whole task itself) or merger got removed —
            # nothing to consolidate.
            team["merged"] = True
            _emit("group_progress", task_id=gt["id"], team=ti,
                  done=len(team["done"]), total=len(team["members"]),
                  merged=True)
            _group_check_done()
            return
        _emit("group_merge", task_id=gt["id"], team=ti, merger=team["merger"])
        merger["task"] = asyncio.create_task(
            _run_turn(merger, _merge_prompt(team, gt), hop=0, source="merge"))
        return


def _group_mark_merged(merger_sid: str, status: str) -> None:
    gt = _group_task
    if not gt:
        return
    for ti, team in enumerate(gt["teams"]):
        if team["merger"] == merger_sid and team["merge_started"] and not team["merged"]:
            team["merged"] = True
            _emit("group_progress", task_id=gt["id"], team=ti,
                  done=len(team["done"]), total=len(team["members"]),
                  merged=True, status=status)
            _group_check_done()
            return


def _group_check_done() -> None:
    gt = _group_task
    if gt and all(t["merged"] for t in gt["teams"]):
        _emit("group_done", task_id=gt["id"])


# ─── Board templates (squads): pre-wired sessions + personas + links ────────────
# The merger-to-be is listed FIRST (lowest seat = seat #1 = consensus merger),
# so "Judge merges the debate" falls out of seat order with no special casing.

TEMPLATES: dict[str, dict] = {
    "debate-trio": {
        "label": "Debate trio",
        "desc": "Advocate vs Critic argue the task; the Judge weighs both and rules.",
        "mode": "consensus",
        "sessions": [
            {"name": "Judge", "persona":
                "The impartial judge. Weigh every argument on evidence and "
                "logic, demand support for claims, and deliver a decisive, "
                "balanced verdict with the reasoning spelled out."},
            {"name": "Advocate", "persona":
                "The advocate. Make the strongest possible case FOR the "
                "proposal: benefits, opportunities, why it works. Steelman "
                "it; concede nothing without a fight."},
            {"name": "Critic", "persona":
                "The devil's advocate. Attack every proposal: failure modes, "
                "hidden costs, edge cases, risks. If something survives your "
                "attack, say exactly what convinced you."},
        ],
        "links": [[0, 1], [0, 2], [1, 2]],
    },
    "research-squad": {
        "label": "Research squad",
        "desc": "Two researchers dig from different angles; the Synthesizer merges one brief.",
        "mode": "consensus",
        "sessions": [
            {"name": "Synthesizer", "persona":
                "The synthesizer. Merge the researchers' findings into one "
                "coherent, de-duplicated brief: what's known, what's "
                "uncertain, and what to do next."},
            {"name": "Scout", "persona":
                "Researcher — breadth. Survey the landscape fast: the "
                "options, players, prior art, and where the good sources are."},
            {"name": "Diver", "persona":
                "Researcher — depth. Pick the most load-bearing questions "
                "and verify them properly: primary sources, numbers, caveats."},
        ],
        "links": [[0, 1], [0, 2], [1, 2]],
    },
    "review-panel": {
        "label": "Review panel",
        "desc": "Security, performance and quality reviewers sweep; the Lead merges findings.",
        "mode": "consensus",
        "sessions": [
            {"name": "Lead", "persona":
                "The review lead. Merge the reviewers' findings, kill "
                "duplicates and false positives, rank by severity, and "
                "produce the final review report."},
            {"name": "Security", "persona":
                "The security reviewer. Hunt injection, authz gaps, secret "
                "leaks, unsafe input handling, path traversal. Cite exact "
                "locations for every finding."},
            {"name": "Perf", "persona":
                "The performance reviewer. Hunt N+1s, quadratic loops, "
                "blocking calls on hot paths, unbounded memory, missing "
                "caches. Cite exact locations."},
            {"name": "Quality", "persona":
                "The code-quality reviewer. Hunt dead code, unclear naming, "
                "missing error handling, race conditions, missing tests. "
                "Cite exact locations."},
        ],
        "links": [[0, 1], [0, 2], [0, 3]],
    },
    "ideation-lab": {
        "label": "Ideation lab",
        "desc": "Visionary dreams, Pragmatist grounds, User-champ humanizes; Editor merges.",
        "mode": "consensus",
        "sessions": [
            {"name": "Editor", "persona":
                "The editor. Merge the team's ideas into a ranked shortlist — "
                "each idea one crisp paragraph with value, effort and risk."},
            {"name": "Visionary", "persona":
                "The visionary. Propose bold, unconventional ideas others "
                "wouldn't dare. Ignore short-term constraints; optimize for "
                "wow and long-term leverage."},
            {"name": "Pragmatist", "persona":
                "The pragmatist. Ground everything in cost, effort and "
                "time-to-ship. Prefer boring proven approaches; flag "
                "overengineering ruthlessly."},
            {"name": "UserChamp", "persona":
                "The user advocate. Champion the end user: simplicity, "
                "clarity, delight. Reject anything user-hostile no matter "
                "how clever."},
        ],
        "links": [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]],
    },
}


def template_list() -> list[dict]:
    return [{"id": tid, "label": t["label"], "desc": t["desc"],
             "size": len(t["sessions"]), "mode": t["mode"]}
            for tid, t in TEMPLATES.items()]


def spawn_template(tid: str) -> dict | None:
    """One click → a pre-wired squad: sessions (merger first), personas,
    links, and the template's preferred mode. Names dedupe against the
    existing board (a second spawn gets 'Judge 2' …)."""
    t = TEMPLATES.get(tid)
    if t is None:
        return None
    created = [create_session(name=spec["name"], persona=spec["persona"])
               for spec in t["sessions"]]
    for i, j in t["links"]:
        connect(created[i]["id"], created[j]["id"])
    set_mode(t["mode"])
    return {"spawned": [c["id"] for c in created], "mode": t["mode"],
            "names": [c["name"] for c in created]}


# ─── Snapshot / subscriptions ───────────────────────────────────────────────────

def _public(sess: dict) -> dict:
    return {k: v for k, v in sess.items()
            if k not in ("task", "lock", "history")}


def snapshot() -> dict:
    return {"sessions": {sid: _public(s) for sid, s in _sessions.items()},
            "connections": list(_connections),
            "ledger": _ledger[-100:],
            "models": models(),
            "mode": _mode,
            "templates": template_list(),
            "deliverables": _deliverables[-_DELIVER_CAP:],
            "group_task": _group_public(),
            "ruflo_swarm": _ruflo_swarm}


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subs.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subs.discard(q)


# Restore the durable board once, at import (main.py imports panel lazily in
# its routes, so this runs on first touch — before any snapshot/chat).
_load_state()

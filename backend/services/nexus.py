"""Nexus — the live mind-map graph of everything COSMOS is wired into.

Aggregates every feature surface (connectors, MCP servers, skills, Kinesis
macros, scheduled routines, meetings, long-term memory) into the node/lobe
shape the Cortex visualization consumes. It is rebuilt from the real feature
stores on every request, so the moment you add a skill, record a macro, wire a
connector or schedule a routine, a new node lights up in the map — no manual
registration step.

Shape returned by build():
  {
    "core":   {stats: {...}},
    "lobes":  [ {id, cat, dir:[x,y,z], desc, kids:[[label,status,desc,detail],...]}, ...],
    "memory": [ [label,status,desc,detail], ... ],
    "counts": {nodes, lobes, ...},
  }

Status vocabulary matches the visualization: "online" | "idle" | "needs-setup".
"""
from __future__ import annotations

from datetime import datetime, timezone

from services import connectors, mcp_client, skill_synth, kinesis, scheduler, memory

# Fixed 3D directions per lobe so the map layout is stable across rebuilds
# (the visualization spreads each lobe's children around this axis).
_DIRS = {
    "connectors": [1.00, 0.16, 0.28],
    "mcp":        [0.46, 0.60, -0.62],
    "skills":     [-0.52, 0.42, 0.55],
    "kinesis":    [-1.00, -0.12, -0.24],
    "schedule":   [-0.46, -0.58, 0.58],
    "meetings":   [0.58, -0.52, -0.50],
    "watchers":   [0.05, 0.92, 0.38],
    "dossier":    [-0.05, -0.92, -0.38],
}

_LOBE_DESC = {
    "connectors": "External services COSMOS reaches into to act on your behalf.",
    "mcp":        "Model Context Protocol servers exposing tools to the agent.",
    "skills":     "Learned playbooks the agent can invoke and reason with.",
    "kinesis":    "Kinesis macros — recorded UI action sequences the agent replays.",
    "schedule":   "Recurring routines COSMOS runs on a cadence.",
    "meetings":   "Upcoming meetings the agent is tracking and preparing for.",
    "watchers":   "Vision watchers — regions COSMOS keeps its eyes on, some with reflexes.",
    "dossier":    "People COSMOS tracks — promises made, work in flight, tasks they gave you.",
}


def _trim(text: str, n: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _connectors_lobe() -> dict:
    kids = []
    for c in connectors.status():
        configured = c.get("configured")
        status = "online" if configured else "needs-setup"
        set_n = sum(1 for f in c.get("fields", []) if f.get("set"))
        total = len(c.get("fields", []))
        detail = f"{set_n}/{total} creds set" if not configured else (c.get("via") or "native")
        kids.append([c.get("label") or c.get("id"), status,
                     _trim(c.get("blurb") or ""), detail])
    return {"id": "connectors", "cat": "connectors", "dir": _DIRS["connectors"],
            "desc": _LOBE_DESC["connectors"], "kids": kids}


def _mcp_lobe() -> dict:
    kids = []
    for s in mcp_client.status():
        state = s.get("state")
        status = ("online" if state == "connected"
                  else "needs-setup" if state in ("error", "not connected")
                  else "idle")
        ntools = len(s.get("tools") or [])
        detail = (s.get("error") or "").strip()[:40] or (f"{ntools} tools" if ntools else state)
        blurb = s.get("info") or f"{s.get('transport','stdio')} server"
        kids.append([s.get("name"), status, _trim(blurb), detail])
    return {"id": "mcp", "cat": "mcp", "dir": _DIRS["mcp"],
            "desc": _LOBE_DESC["mcp"], "kids": kids}


def _skills_lobe() -> dict:
    kids = []
    for sk in skill_synth.list_skills():
        detail = "built-in" if sk.get("protected") else f"{sk.get('chars',0)} chars"
        kids.append([sk.get("title") or sk.get("name"), "online",
                     _trim(sk.get("preview") or ""), detail])
    return {"id": "skills", "cat": "skills", "dir": _DIRS["skills"],
            "desc": _LOBE_DESC["skills"], "kids": kids}


def _kinesis_lobe() -> dict:
    kids = []
    for m in kinesis.list_macros():
        steps = m.get("steps", 0)
        dur = m.get("duration_ms", 0)
        detail = f"{steps} steps · {dur/1000:.1f}s" if dur else f"{steps} steps"
        kids.append([m.get("title") or m.get("name"), "online",
                     _trim(m.get("description") or ""), detail])
    return {"id": "kinesis", "cat": "kinesis", "dir": _DIRS["kinesis"],
            "desc": _LOBE_DESC["kinesis"], "kids": kids}


def _schedule_lobe() -> dict:
    kids = []
    try:
        jobs = scheduler._load()
    except Exception:
        jobs = []
    for j in jobs:
        enabled = j.get("enabled", True) is not False
        status = "online" if enabled else "idle"
        detail = j.get("cron") or j.get("time") or "cron"
        label = j.get("label") or (j.get("id") or "job").replace("-", " ").title()
        kids.append([label, status, _trim(j.get("prompt") or ""), detail])
    return {"id": "schedule", "cat": "schedule", "dir": _DIRS["schedule"],
            "desc": _LOBE_DESC["schedule"], "kids": kids}


async def _meetings_lobe() -> dict:
    """Upcoming meetings from Google Calendar (empty lobe if not wired)."""
    kids: list = []
    try:
        from services import google
        events = await google.upcoming_events(max_results=6)
        for e in events or []:
            kids.append([_trim(e.get("summary") or "Meeting", 46), "online",
                         _trim(e.get("description") or "Prep brief ready"),
                         e.get("when") or "today"])
    except Exception:
        pass
    return {"id": "meetings", "cat": "meetings", "dir": _DIRS["meetings"],
            "desc": _LOBE_DESC["meetings"], "kids": kids}


def _watchers_lobe() -> dict:
    """Vision watchers — each region COSMOS polls, with its reflex if armed."""
    kids = []
    try:
        from services import watchers
        for w in watchers.list_watchers():
            status = ("needs-setup" if w.get("last_error")
                      else "online" if w.get("enabled", True) else "idle")
            rx = w.get("reflex") or {}
            armed = rx.get("kind") in ("macro", "prompt")
            iv = int(w.get("interval_s") or 60)
            detail = w.get("last_value") or (f"every {iv // 60}m" if iv >= 60 else f"every {iv}s")
            if armed:
                detail = f"⚡ {detail}"
            kids.append([_trim(w.get("name") or "Watcher", 40), status,
                         _trim(w.get("question") or ""), _trim(str(detail), 40)])
    except Exception:
        pass
    return {"id": "watchers", "cat": "watchers", "dir": _DIRS["watchers"],
            "desc": _LOBE_DESC["watchers"], "kids": kids}


def _dossier_lobe() -> dict:
    """Top tracked people from the Dossier — signal first (owe you / promised)."""
    kids = []
    try:
        from services import dossier
        people = (dossier.load() or {}).get("people", [])
        people.sort(key=lambda p: (len(p.get("assigned_to_me", [])) * 2
                                   + len(p.get("promises", []))
                                   + len(p.get("working_on", []))), reverse=True)
        for p in people[:8]:
            nt = len(p.get("assigned_to_me", []))
            np_ = len(p.get("promises", []))
            has_signal = bool(nt or np_ or p.get("working_on"))
            detail = " · ".join(x for x in
                                (f"{nt} for you" if nt else "",
                                 f"{np_} promised" if np_ else "") if x) or \
                     (p.get("relationship") or "tracked")
            kids.append([_trim(p.get("name") or "?", 34),
                         "online" if has_signal else "idle",
                         _trim(p.get("summary") or p.get("role") or ""), _trim(detail, 40)])
    except Exception:
        pass
    return {"id": "dossier", "cat": "dossier", "dir": _DIRS["dossier"],
            "desc": _LOBE_DESC["dossier"], "kids": kids}


_MEM_DESC = {
    "people": "Who matters, roles & relationships",
    "projects": "Active initiatives & their state",
    "preferences": "How you like things done",
    "corrections": "Things you told it to stop doing",
    "frequent_tasks": "Open work items & follow-ups",
    "learned_apps": "Apps it has learned to drive",
}


def _memory_shells() -> list:
    mem = {}
    try:
        mem = memory.load() or {}
    except Exception:
        mem = {}
    shells = []
    for key, desc in _MEM_DESC.items():
        val = mem.get(key)
        n = len(val) if isinstance(val, (list, dict)) else 0
        status = "online" if n else "idle"
        unit = {"people": "entities", "projects": "tracked", "preferences": "prefs",
                "corrections": "rules", "frequent_tasks": "tasks",
                "learned_apps": "apps"}.get(key, "items")
        shells.append([key.replace("_", " "), status, desc, f"{n} {unit}"])
    return shells


async def _map_tool(args, ctx) -> str:
    """Compact textual system map for the agent."""
    g = await build()
    lines = []
    needs = []
    for lb in g["lobes"]:
        kids = lb["kids"]
        on = sum(1 for k in kids if k[1] == "online")
        lines.append(f"- {lb['id']}: {len(kids)} nodes ({on} online) — {lb['desc']}")
        for k in kids:
            if k[1] == "needs-setup":
                needs.append(f"  ⚠ {lb['id']}/{k[0]}: {k[3]}")
    out = (f"COSMOS system map — {g['counts']['nodes']} nodes across "
           f"{g['counts']['lobes']} lobes (see the Nexus tab for the visual):\n"
           + "\n".join(lines))
    if needs:
        out += "\nNEEDS ATTENTION:\n" + "\n".join(needs[:8])
    return out


def register_agent_tool() -> None:
    """Register `system_map` so the agent can introspect what it's wired into
    ('what can you do?', 'what's connected?', 'anything broken?')."""
    try:
        from services import agent
        agent.register_tool({
            "name": "system_map",
            "description": (
                "COSMOS's live self-map (the Nexus): every connector, MCP server, skill, "
                "Kinesis macro, scheduled routine, meeting, Vision watcher and tracked "
                "person, with statuses. Use to answer 'what are you connected to', "
                "'what can you do', or to spot broken/unconfigured pieces."),
            "input_schema": {"type": "object", "properties": {}},
        }, _map_tool, gate="open", label="system map", source="nexus")
        agent.invalidate_tool_cache()
    except Exception as e:
        print(f"[nexus] agent tool registration failed (non-fatal): {e}")


async def build() -> dict:
    lobes = [
        _connectors_lobe(), _mcp_lobe(), _skills_lobe(),
        _kinesis_lobe(), _schedule_lobe(), await _meetings_lobe(),
        _watchers_lobe(), _dossier_lobe(),
    ]
    mem = _memory_shells()
    online_lobes = sum(1 for lb in lobes if any(k[1] == "online" for k in lb["kids"]))
    node_count = 1 + len(lobes) + sum(len(lb["kids"]) for lb in lobes) + len(mem)
    tool_count = sum(len(lb["kids"]) for lb in lobes if lb["id"] in ("connectors", "mcp"))
    core = {
        "stats": {
            "Tools": f"{tool_count} wired",
            "Lobes": f"{online_lobes} online",
            "Nodes": str(node_count),
            "Synced": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        }
    }
    return {
        "core": core,
        "lobes": lobes,
        "memory": mem,
        "counts": {"nodes": node_count, "lobes": len(lobes)},
    }

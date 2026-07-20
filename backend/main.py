"""COSMOS AI Backend — FastAPI + WebSocket, agentic loop (protocol v3)."""

import os
import re
import time
import json
import shlex
import asyncio
from collections import deque
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from services import llm, weather, system_control, agent, convstore, tts, memory, recall
from services import scheduler
from services import mcp_client
from services import slack_bridge
from services import embeddings as embeddings_svc
from services import mutate


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("╔══════════════════════════════╗")
    print("║  COSMOS AI Backend  v3.0     ║")
    print("║  Running on :8000            ║")
    print("╚══════════════════════════════╝")
    print("[mutate] self-modification engine armed")
    # Mutate: a restart marker here means the previous image exec'd into us
    # after a self-patch — booting at all IS the mutation's final verification.
    _survived = mutate.on_boot()
    if _survived:
        print(f"[mutate] ✓ {_survived}")
    # Warm the LLM prompt cache + TLS so the first command isn't cold.
    if os.getenv("FRIDAY_PREWARM", "1").lower() not in ("0", "false", "no"):
        asyncio.create_task(agent.prewarm())
        asyncio.create_task(_prewarm_tts())
        asyncio.create_task(_prewarm_loop())
    # One-time recall-DB backfill from historical traces + embedding of any
    # rows still missing vectors (both no-ops once done).
    asyncio.create_task(asyncio.to_thread(recall.bootstrap))
    # External MCP servers (~/.friday/mcp.json): connect, register their tools
    # into the agent loop, then re-warm the prompt cache once. Fire-and-forget —
    # a slow npx server must not delay boot.
    asyncio.create_task(mcp_client.boot())
    # Background scheduler: reminders-at-time, the 9am briefing, cron jobs.
    scheduler.start(_broadcast, _RUN_LOCK)
    # Register feature tools into the agent loop: Kinesis macros, the Dossier,
    # Vision watchers (+reflex control) and the Nexus system map — so all of it
    # is drivable from chat/voice, not just the tabs.
    kinesis.register_agent_tool()
    dossier.register_agent_tool()
    watchers.register_agent_tools()
    nexus.register_agent_tool()
    # Dossier: sweep your comms into per-person files once a day (07:30 local).
    # (dossier is imported at module level below — re-importing it here made it
    # function-local and broke the register call above with UnboundLocalError.)
    asyncio.create_task(dossier.daily_loop())
    # Watchers: vision polling of watched screen regions.
    watchers.start(_broadcast, _RUN_LOCK)   # run lock: reflexes never fight a live run
    # Slack command bridge: drive Cosmos from the dedicated Slack channel
    # (Socket Mode; silently offline until both tokens are in .env).
    slack_bridge.start(_RUN_LOCK)
    yield
    await embeddings_svc.close_client()
    # Drain the shared keep-alive connector pool (services.http_pool).
    try:
        from services import http_pool
        await http_pool.aclose_all()
    except Exception:
        pass


# The FE speaks these exact strings constantly — render them once into the TTS
# cache at boot so they play instantly (disk cache makes later boots free).
_CANNED_TTS = [
    "On it, sir.", "Right away, sir.", "Working on it, sir.",
    "Still on it, sir.", "Done, sir.", "Stopped, sir.",
    "Good morning, sir. Ready.", "Good afternoon, sir. Ready.",
    "Good evening, sir. Ready.", "Going offline, sir.",
    "That took too long, sir. Please try again.",
    "Starting a fresh conversation, sir.",
    "Awaiting your confirmation, sir — yes or no?",
]


async def _prewarm_tts():
    # The say-engine probe is lazy now (its ~0.7s `say -v '?'` used to block
    # import) — run it here, off-thread, so say-only machines are ready
    # before the first spoken line.
    await asyncio.to_thread(tts.ensure_say)
    if not tts.AVAILABLE:
        return
    for line in _CANNED_TTS:
        try:
            await tts.synthesize(line)
        except Exception:
            pass


async def _prewarm_loop():
    """The gateway's prompt cache has a short TTL — for a voice assistant most
    first commands arrive after an idle gap and repay the full ~20k-token
    prefill (+1-3s to first token). Re-warm both model tiers every ~4min while
    a HUD client is actually connected. Never runs while a task holds the
    run-lock (no background gateway traffic during live runs) or when the
    whole chain is cooling (degraded mode — a ping would just add noise)."""
    while True:
        await asyncio.sleep(240)
        try:
            if _CONNECTED_CLIENTS == 0 or _RUN_LOCK.locked():
                continue
            if llm.all_models_cooling([agent.AGENT_MODEL, *llm.AGENT_FALLBACKS]):
                continue
            await agent.prewarm()
        except Exception:
            pass


app = FastAPI(title="COSMOS AI", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


# ─── HTTP ──────────────────────────────────────────────────────────────────────

_BOOT_TS = time.monotonic()


@app.get("/health")
async def health():
    return {"status": "online", "service": "COSMOS AI"}


@app.get("/api/slack-bridge")
async def get_slack_bridge():
    """Slack command-bridge health: connected, resolved channel/owner, queue."""
    return slack_bridge.status()


@app.get("/api/slack-bridge/activity")
async def get_slack_bridge_activity():
    """Bridge status + per-task history (command, events, files, reply)."""
    return slack_bridge.activity()


# ─── Panel: multi-agent swarm board ─────────────────────────────────────────────

@app.get("/api/panel")
async def get_panel():
    """Workspace snapshot: sessions, connections, available models."""
    from services import panel
    return panel.snapshot()


@app.get("/api/panel/templates")
async def get_panel_templates():
    """Squad templates — pre-wired boards (sessions + personas + links + mode)."""
    from services import panel
    return panel.template_list()


@app.post("/api/panel/templates/{tid}")
async def post_panel_template(tid: str):
    """Spawn a squad template onto the board."""
    from services import panel
    res = panel.spawn_template(tid)
    if res is None:
        return Response(status_code=404, content="unknown template")
    return res


@app.post("/api/panel/sessions/{sid}/persona")
async def post_panel_persona(sid: str, request: Request):
    """Set/replace a session's role — injected into every one of its turns."""
    from services import panel
    body = await request.json()
    if not panel.set_persona(sid, str((body or {}).get("persona") or "")):
        return Response(status_code=404, content="unknown session")
    return {"ok": True}


@app.post("/api/panel/sessions")
async def post_panel_session(request: Request):
    """Create a named agent session (optional {name, model, persona})."""
    from services import panel
    body = await request.json()
    return panel.create_session(str((body or {}).get("name") or ""),
                                str((body or {}).get("model") or ""),
                                persona=str((body or {}).get("persona") or ""))


@app.delete("/api/panel/sessions/{sid}")
async def delete_panel_session(sid: str):
    from services import panel
    return {"removed": panel.remove_session(sid)}


@app.post("/api/panel/sessions/{sid}/chat")
async def post_panel_chat(sid: str, request: Request):
    """User → session message; the turn runs in the background and streams."""
    from services import panel
    body = await request.json()
    ok = panel.chat(sid, str((body or {}).get("text") or ""))
    if not ok:
        return Response(status_code=400, content="unknown session or empty text")
    return {"ok": True}


@app.post("/api/panel/sessions/{sid}/model")
async def post_panel_model(sid: str, request: Request):
    from services import panel
    body = await request.json()
    if not panel.set_model(sid, str((body or {}).get("model") or "")):
        return Response(status_code=404, content="unknown session")
    return {"ok": True}


@app.post("/api/panel/sessions/{sid}/stop")
async def post_panel_session_stop(sid: str):
    from services import panel
    return {"stopped": panel.stop(sid)}


@app.post("/api/panel/mode")
async def post_panel_mode(request: Request):
    """Toggle the board's prompt mode: singular (per-session) | consensus."""
    from services import panel
    body = await request.json()
    if not panel.set_mode(str((body or {}).get("mode") or "")):
        return Response(status_code=400, content="mode must be singular|consensus")
    return {"ok": True}


@app.post("/api/panel/broadcast")
async def post_panel_broadcast(request: Request):
    """Consensus mode: one prompt → every session, teams divide the work."""
    from services import panel
    body = await request.json()
    n = panel.group_chat(str((body or {}).get("text") or ""))
    if not n:
        return Response(status_code=400, content="empty text or no sessions")
    return {"started": n}


@app.post("/api/panel/connections")
async def post_panel_connection(request: Request):
    """Draw an UNDIRECTED link {from, to} — both ends can message each other."""
    from services import panel
    body = await request.json()
    err = panel.connect(str((body or {}).get("from") or ""),
                        str((body or {}).get("to") or ""))
    if err:
        return Response(status_code=400, content=err)
    return {"ok": True}


@app.post("/api/panel/connections/remove")
async def post_panel_connection_remove(request: Request):
    from services import panel
    body = await request.json()
    return {"removed": panel.disconnect(str((body or {}).get("from") or ""),
                                        str((body or {}).get("to") or ""))}


@app.websocket("/ws/panel")
async def panel_ws(websocket: WebSocket):
    """Live PanelEvent stream: snapshot on connect, then every agent status /
    edge / memory / deliverable event as it happens (true real-time edges)."""
    from services import panel
    await websocket.accept()
    q = panel.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", **panel.snapshot()})
        while True:
            ev = await q.get()
            await websocket.send_json(ev)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[panel-ws] {e}")
    finally:
        panel.unsubscribe(q)


@app.get("/api/status")
async def get_status():
    """Honest system status: uptime, model cooldowns, active run, caches."""
    return {
        "uptime_s": round(time.monotonic() - _BOOT_TS, 1),
        "llm": llm.chain_status(),
        "run_active": _RUN_LOCK.locked(),
        "connected_clients": _CONNECTED_CLIENTS,
        "response_cache_entries": len(_RESP_CACHE),
        "tts_engine": tts.CHOSEN_VOICE or "browser-fallback",
    }


@app.get("/api/weather")
async def get_weather():
    try:
        return await weather.get_current()
    except Exception:
        return {"error": "weather unavailable"}


@app.post("/api/tts")
async def synthesize_tts(request: Request):
    """Render text to macOS-native audio (AAC/M4A). 503 → frontend falls back
    to the browser Web Speech API."""
    if not tts.AVAILABLE:
        return Response(status_code=503)
    try:
        body = await request.json()
        text = (body or {}).get("text", "") if isinstance(body, dict) else ""
    except Exception:
        return Response(status_code=400)
    result = await tts.synthesize(text)
    if not result:
        return Response(status_code=503)
    audio, content_type = result
    engine = "elevenlabs" if content_type == tts.CONTENT_TYPE_EL else "say"
    return Response(content=audio, media_type=content_type,
                    headers={"X-TTS-Engine": engine})


@app.post("/api/tts/stream")
async def synthesize_tts_stream(request: Request):
    """Streamed ElevenLabs audio — first chunk in ~300ms. 503 when EL isn't
    configured or the stream can't start; the frontend then uses the buffered
    /api/tts path."""
    if not tts.EL_AVAILABLE:
        return Response(status_code=503)
    try:
        body = await request.json()
        text = (body or {}).get("text", "") if isinstance(body, dict) else ""
    except Exception:
        return Response(status_code=400)
    if not text.strip():
        return Response(status_code=400)

    gen = tts.stream(text)
    try:
        first = await gen.__anext__()
    except StopAsyncIteration:
        return Response(status_code=503)
    except Exception:
        return Response(status_code=503)

    async def _full():
        yield first
        async for chunk in gen:
            yield chunk

    from fastapi.responses import StreamingResponse
    return StreamingResponse(_full(), media_type="audio/mpeg",
                             headers={"X-TTS-Engine": "elevenlabs-stream"})


@app.get("/api/memory")
async def get_memory():
    """Return Cosmos's long-term memory (what she's learned)."""
    return _load_memory()


@app.post("/api/memory/correction")
async def add_correction(heard: str, corrected: str):
    """Manually teach Cosmos a speech correction."""
    memory_record_correction(heard, corrected)
    return {"ok": True, "heard": heard, "corrected": corrected}


@app.get("/api/audit")
async def get_audit(limit: int = 100):
    """Newest-first slice of the append-only security audit log.
    Reads from the end of the file to avoid loading the entire history."""
    limit = max(1, min(limit, 500))
    path = Path.home() / ".friday" / "audit.jsonl"
    entries: list[dict] = []
    try:
        tail = deque(path.open(encoding="utf-8"), maxlen=limit)
        for line in reversed(tail):
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return {"entries": entries}


# ─── Mutate (self-modification: scan / suggest / fix / dismiss) ────────────────

@app.get("/api/mutate")
async def mutate_list():
    """Proposals + live fix status for the Mutate panel. Polling-friendly on
    purpose: the panel polls straight through the self-restart gap, so progress
    reporting needs no WS state to survive the exec."""
    return {"mutations": mutate.list_all(), "busy": mutate.busy()}


@app.post("/api/mutate/scan")
async def mutate_scan():
    """Read COSMOS's own failure evidence and generate fix proposals."""
    try:
        return await mutate.scan()
    except Exception as e:
        return {"error": llm.sanitize_error(e, cap=200)}


@app.post("/api/mutate/suggest")
async def mutate_suggest(req: Request):
    """A user-typed change request becomes a proposal directly."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    return mutate.suggest(str(body.get("text", "")))


@app.post("/api/mutate/{mid}/fix")
async def mutate_fix(mid: str):
    """Start the test-gated self-patch pipeline (background; poll /api/mutate)."""
    return await mutate.start_fix(mid)


@app.post("/api/mutate/{mid}/dismiss")
async def mutate_dismiss(mid: str):
    return mutate.dismiss(mid)


@app.get("/api/scheduled")
async def get_scheduled():
    """Scheduled background jobs (for the HUD panels)."""
    try:
        return {"jobs": scheduler._load()}
    except Exception:
        return {"jobs": []}


# ─── Skills management (read / AI-edit / save / delete) ───────────────────────

from services import skill_synth   # noqa: E402


@app.get("/api/skills")
async def list_skills():
    """All skills on disk (built-in + user-saved) for the Skills page."""
    return {"skills": skill_synth.list_skills()}


@app.get("/api/skills/{name}")
async def get_skill(name: str):
    skill = skill_synth.read_skill(name)
    if skill is None:
        return Response(status_code=404, content=json.dumps({"error": "not found"}),
                        media_type="application/json")
    return skill


@app.put("/api/skills/{name}")
async def save_skill_route(name: str, req: Request):
    """Save raw markdown content for a skill (create or overwrite, incl. built-ins)."""
    body = await req.json()
    result = skill_synth.save_skill(name, body.get("content", ""), allow_protected=True)
    ok = not result.startswith("Error")
    return {"ok": ok, "message": result}


@app.delete("/api/skills/{name}")
async def delete_skill_route(name: str):
    result = skill_synth.delete_skill(name)
    return {"ok": not result.startswith("Error"), "message": result}


@app.post("/api/skills/{name}/edit")
async def edit_skill_route(name: str, req: Request):
    """AI edit: given a natural-language instruction, rewrite the skill markdown
    and return the PROPOSED new content (not saved — the UI previews, then PUTs)."""
    body = await req.json()
    instruction = (body.get("prompt") or "").strip()
    # New skills are created from a blank canvas; existing ones are edited.
    existing = skill_synth.read_skill(name)
    current = existing["content"] if existing else ""
    if not instruction:
        return {"ok": False, "error": "Describe the change you want."}
    prompt = (
        "You edit a COSMOS skill file — a markdown playbook that becomes part of "
        "the assistant's system prompt. Apply the user's instruction and return "
        "the COMPLETE revised markdown file, nothing else (no code fences, no "
        "commentary). Keep it tight and imperative; preserve the parts the "
        "instruction doesn't touch.\n\n"
        f"SKILL NAME: {name}\n\n"
        f"CURRENT CONTENT:\n{current or '(new, empty skill)'}\n\n"
        f"INSTRUCTION: {instruction}")
    try:
        resp = await llm.acreate(model=agent.AGENT_MODEL,
                                 fallbacks=llm.AGENT_FALLBACKS, max_tokens=4096,
                                 messages=[{"role": "user", "content": prompt}])
        new_content = llm.extract_text(resp).strip()
        # Strip an accidental ```markdown fence if the model added one.
        new_content = re.sub(r"^```[a-z]*\n|\n```$", "", new_content).strip()
    except Exception as e:
        return {"ok": False, "error": llm.sanitize_error(e, 160)}
    if not new_content:
        return {"ok": False, "error": "The model returned nothing — try rephrasing."}
    return {"ok": True, "content": new_content}


# ─── MCP servers (status / reconnect) ─────────────────────────────────────────

@app.get("/api/mcp")
async def mcp_status():
    """Structured per-server MCP status for the MCPs page."""
    return {"servers": mcp_client.status(), "config_path": str(mcp_client.CONFIG_FILE)}


@app.post("/api/mcp/reload")
async def mcp_reload():
    """Reconnect all MCP servers (re-reads .env + mcp.json), then re-warm."""
    try:
        summary = await mcp_client.reload()
        return {"ok": True, "message": summary, "servers": mcp_client.status()}
    except Exception as e:
        return {"ok": False, "message": llm.sanitize_error(e, 200)}


# ─── Connectors (native integration credentials) ──────────────────────────────

from services import connectors   # noqa: E402


@app.get("/api/connectors")
async def list_connectors():
    """Per-integration config status (which vars are set — never the values)."""
    return {"connectors": connectors.status()}


@app.post("/api/connectors/{cid}")
async def save_connector(cid: str, req: Request):
    """Persist credentials to backend/.env and hot-reconfigure the service."""
    body = await req.json()
    result = connectors.save(cid, body.get("values") or {})
    result["connectors"] = connectors.status()
    return result


# ─── Nexus (live mind-map graph of every wired feature) ───────────────────────

from services import nexus   # noqa: E402


@app.get("/api/nexus")
async def get_nexus():
    """Aggregate every feature surface into the Cortex mind-map graph. Rebuilt
    live on each request, so newly added skills/macros/connectors/routines appear
    as fresh nodes automatically."""
    try:
        return await nexus.build()
    except Exception as e:
        return {"error": llm.sanitize_error(e, 160), "lobes": [], "memory": []}


# ─── Watchers (vision-based "ping me when this changes") ──────────────────────

from services import watchers   # noqa: E402
from fastapi.responses import FileResponse   # noqa: E402


@app.get("/api/watchers")
async def watchers_list():
    return {"watchers": watchers.list_watchers()}


@app.get("/api/watchers/snap")
async def watchers_snap():
    """Fresh full-screen screenshot for the screen-region picker."""
    return await watchers.snap_fullscreen()


@app.post("/api/watchers/preview")
async def watchers_preview(req: Request):
    """Headless render of a URL for the page-region picker (URL mode)."""
    body = await req.json()
    return await watchers.preview_url(body.get("url") or "")


@app.post("/api/watchers/session/start")
async def watchers_session_start(req: Request):
    """Start (or renavigate) the interactive preview browser."""
    body = await req.json()
    return await watchers.start_session(body.get("url") or "")


@app.get("/api/watchers/session/frame")
async def watchers_session_frame():
    """Live screenshot + current URL of the interactive preview."""
    return await watchers.session_frame()


@app.post("/api/watchers/session/input")
async def watchers_session_input(req: Request):
    """Forward a click / keystroke / scroll / navigation into the preview."""
    body = await req.json()
    return await watchers.session_input(body or {})


@app.post("/api/watchers/session/stop")
async def watchers_session_stop():
    """Close the preview browser (flushes any login into the watch profile)."""
    return await watchers.stop_session()


@app.websocket("/ws/watcher-session")
async def watcher_session_ws(websocket: WebSocket):
    """Live preview stream: pushes screencast frames the moment Chrome paints
    them, and applies inputs (clicks/keys/scroll) received from the client —
    one socket, no per-event HTTP overhead."""
    await websocket.accept()
    if not watchers.session_active():
        await websocket.send_json({"type": "error", "error": "No live session."})
        await websocket.close()
        return

    async def _pump_frames():
        last_seq = -1
        while True:
            await asyncio.sleep(0.04)
            watchers.touch_session()          # viewing keeps the session alive
            seq, frame, url = watchers.session_frame_state()
            if seq != last_seq and frame:
                last_seq = seq
                await websocket.send_json({"type": "frame", "data": frame, "url": url})

    pump = asyncio.create_task(_pump_frames())
    try:
        while True:
            msg = await websocket.receive_json()
            await watchers.session_input(msg or {})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        pump.cancel()


@app.post("/api/watchers/signin")
async def watchers_signin(req: Request):
    """Open a headful watch-profile window so the user can log into a site once."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    return await watchers.open_signin((body or {}).get("url") or "")


@app.post("/api/watchers")
async def watchers_create(req: Request):
    body = await req.json()
    return watchers.create(
        name=body.get("name") or "", question=body.get("question") or "",
        region=body.get("region") or {}, interval_s=int(body.get("interval_s") or 60),
        condition=body.get("condition") or "",
        wtype=body.get("type") or "screen", url=body.get("url") or "",
        viewport=body.get("viewport"), reflex=body.get("reflex"))


@app.post("/api/watchers/{wid}/reflex")
async def watchers_set_reflex(wid: str, req: Request):
    """Attach / update / clear the action a watcher fires when it alerts."""
    body = await req.json()
    return watchers.set_reflex(wid, body or {})


@app.post("/api/watchers/{wid}/edit")
async def watchers_edit(wid: str, req: Request):
    """Edit a watcher's name / question / condition / interval / URL."""
    body = await req.json()
    return watchers.update_watcher(wid, body or {})


@app.delete("/api/watchers/{wid}")
async def watchers_delete(wid: str):
    return watchers.delete(wid)


@app.post("/api/watchers/{wid}/toggle")
async def watchers_toggle(wid: str):
    return watchers.toggle(wid)


@app.post("/api/watchers/{wid}/check")
async def watchers_check(wid: str):
    """Manual 'check now' — always runs the vision read (skips the hash gate)."""
    return await watchers.check(wid, manual=True)


@app.get("/api/watchers/{wid}/thumb")
async def watchers_thumb(wid: str):
    p = watchers.thumb_path(wid)
    if not p:
        return Response(status_code=404)
    return FileResponse(p, media_type="image/png")


# ─── Dossier (per-person intelligence from Slack + Gmail) ─────────────────────

from services import dossier   # noqa: E402


@app.get("/api/dossier")
async def get_dossier():
    """The stored per-person dossier + org tree + live sweep progress."""
    return {**dossier.load(), "progress": dossier.progress()}


@app.post("/api/dossier/sweep")
async def sweep_dossier(req: Request):
    """START a sweep and return immediately — a sweep takes minutes, far past
    what a browser/proxy keeps a request open for. The UI polls GET /api/dossier
    (progress.running flips false when done). body: {days?: int, merge?: bool}."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    days = int((body or {}).get("days") or 14)
    merge = bool((body or {}).get("merge"))
    if dossier.progress()["running"]:
        return {"ok": False, "started": False, "message": "A sweep is already running."}
    asyncio.create_task(dossier.sweep(days=max(1, min(days, 30)), merge=merge))
    return {"ok": True, "started": True,
            "message": f"Sweeping the last {days} days in the background."}


# ─── Kinesis (demonstrate-once macro recorder) ────────────────────────────────

from services import kinesis   # noqa: E402


@app.get("/api/kinesis")
async def kinesis_list():
    """Saved macros + current recording status for the Kinesis page."""
    return {"macros": kinesis.list_macros(), "recording": kinesis.recording_status()}


@app.get("/api/kinesis/status")
async def kinesis_status():
    """Live recording status — polled by the HUD while a capture is running."""
    return kinesis.recording_status()


@app.post("/api/kinesis/record/start")
async def kinesis_record_start():
    # Quiesce Vision's headless Chromes first — a live one steals AppleScript
    # targeting from the real browser and would corrupt the recording.
    try:
        if watchers.session_active():
            await watchers.stop_session()
        async with watchers._HEADLESS_LOCK:      # drain any in-flight capture
            pass
    except Exception:
        pass
    return kinesis.start_recording()


@app.post("/api/kinesis/record/stop")
async def kinesis_record_stop(req: Request):
    try:
        body = await req.json()
    except Exception:
        body = {}
    res = kinesis.stop_recording(via_button=bool((body or {}).get("via_button")))
    if res.get("ok"):
        res["describe"] = [kinesis.describe_step(s) for s in res.get("steps", [])]
    return res


@app.post("/api/kinesis/save")
async def kinesis_save(req: Request):
    body = await req.json()
    msg = kinesis.save_macro(
        body.get("name") or "", body.get("title") or "",
        body.get("description") or "", body.get("steps") or [],
        int(body.get("duration_ms") or 0))
    return {"ok": not msg.startswith("Error"), "message": msg}


@app.post("/api/kinesis/understand")
async def kinesis_understand(req: Request):
    """Infer the macro's core intent + a name/title/description from its steps."""
    body = await req.json()
    return await kinesis.understand(body.get("steps") or [])


@app.post("/api/kinesis/{name}/edit")
async def kinesis_edit(name: str, req: Request):
    """Edit a macro's title/description and optionally rename it."""
    body = await req.json()
    return kinesis.update_macro(name, title=body.get("title"),
                                description=body.get("description"),
                                new_name=body.get("name"), params=body.get("params"),
                                steps=body.get("steps"))


@app.get("/api/kinesis/cdp")
async def kinesis_cdp_status():
    """Whether Chrome exposes the CDP debug port (turbo replay available)."""
    from services import kinesis_cdp
    return {"available": await kinesis_cdp.is_available(), "port": kinesis_cdp.DEBUG_PORT}


@app.post("/api/kinesis/cdp/enable")
async def kinesis_cdp_enable():
    """Relaunch Chrome with the debug port to turn on CDP turbo mode."""
    from services import kinesis_cdp
    return await kinesis_cdp.enable_debug_chrome()


@app.get("/api/kinesis/{name}")
async def kinesis_get(name: str):
    m = kinesis.get_macro(name)
    if not m:
        return Response(status_code=404, content=json.dumps({"error": "not found"}),
                        media_type="application/json")
    return {**m, "describe": [kinesis.describe_step(s) for s in m.get("steps", [])]}


@app.delete("/api/kinesis/{name}")
async def kinesis_delete(name: str):
    msg = kinesis.delete_macro(name)
    return {"ok": not msg.startswith("Error"), "message": msg}


@app.post("/api/kinesis/{name}/replay")
async def kinesis_replay(name: str, req: Request):
    """Drive the real mouse/keyboard through a saved macro. Serialized behind the
    run lock so it never fights a live agent run for the physical input."""
    macro = kinesis.get_macro(name)
    if not macro:
        return {"ok": False, "message": "No such macro, sir."}
    if _RUN_LOCK.locked():
        return {"ok": False, "message": "I'm mid-task, sir — one thing at a time. "
                                        "Try again in a moment."}
    try:
        body = await req.json()
    except Exception:
        body = {}
    speed = float((body or {}).get("speed") or 1.0)
    async with _RUN_LOCK:
        _journal_write(f"[kinesis replay] {name}")
        try:
            return await kinesis.replay(macro, speed=speed,
                                        params=(body or {}).get("params"))
        finally:
            _journal_clear()


# ─── Long-term memory (shared module — see services/memory.py) ────────────────

def _load_memory() -> dict:
    return memory.load()


def memory_record_correction(heard: str, corrected: str) -> None:
    memory.record_correction(heard, corrected)


def memory_record_task(task: str, success: bool) -> None:
    memory.record_task(task, success)


def memory_get_corrections() -> dict:
    return memory.get_corrections()


def _apply_memory_corrections(text: str) -> str:
    """Apply known speech-recognition corrections from memory.

    PHRASE-level, longest-first, word-boundary anchored — the old word-by-word
    applier could never match a stored multi-word correction like
    "trishul reddy", and shorter keys must not clobber longer ones."""
    corrections = {k: v for k, v in memory_get_corrections().items() if k}
    if not corrections:
        return text
    # ONE combined pass (longest alternative first) — sequential re.sub calls
    # would let a short key match inside an earlier replacement's text.
    pattern = re.compile(
        "|".join(rf"\b{re.escape(k)}\b"
                 for k in sorted(corrections, key=len, reverse=True)),
        re.IGNORECASE)
    return pattern.sub(lambda m: corrections[m.group(0).lower()], text)


# ─── Text helpers ──────────────────────────────────────────────────────────────

def _is_gibberish(text: str) -> bool:
    """Filter out ambient voice noise ("uh", "hmm...").

    Deliberately does NOT reject short/single-word input — typed commands like
    "weather", "screenshot" or "hello" are perfectly valid and must go through.
    """
    t = text.strip()
    if not t:
        return True
    if not re.search(r"[A-Za-z0-9]", t):
        return True
    noise = {"uh", "um", "hm", "hmm", "ah", "oh", "er", "mm", "eh",
             "huh", "mhm", "ugh", "uhh", "umm", "shh"}
    words = [w.lower().rstrip(".,!?") for w in t.split()]
    if all(w in noise for w in words):
        return True
    return False


def _is_yes(text: str) -> bool:
    return bool(re.search(r'\b(yes|yeah|yep|yup|sure|correct|do it|proceed|confirm|go ahead|ok|right|exactly)\b', text.lower()))


def _is_no(text: str) -> bool:
    # "not" is included so negated refusals ("that's not right", "do not
    # proceed") register as NO even though they contain yes-words.
    return bool(re.search(r'\b(no|not|nope|never|cancel|abort|stop|skip|don\'t|dont|wrong|incorrect)\b', text.lower()))


# Action verbs that signal a *task* (as opposed to a query). Used to tell a
# compound, multi-step command apart from a single-intent one.
_ACTION_VERB = (r'open|take|create|draft|send|write|make|search|find|play|close|'
                r'show|read|compose|record|download|install|snap|capture|'
                r'screenshot|launch|start|type|click|email|message|dm')


# ─── Zero-LLM fast-path matchers ───────────────────────────────────────────────

# "open slack" / "launch chrome" / "start up spotify" — app name only.
_OPEN_APP_RE = re.compile(
    r"^(?:open|launch|start)(?:\s+up)?\s+([a-z0-9 .&'\-]+)$", re.IGNORECASE)
# Targets that are clearly NOT an app → let the agent handle them.
_OPEN_NOT_AN_APP_RE = re.compile(
    r"\b(file|files|folder|directory|document|doc|pdf|url|link|website|site|"
    r"page|tab|window|downloads?|desktop|http|www|\.com|\.in|\.org|chat|"
    r"email|mail from|message)\b", re.IGNORECASE)

_TIME_RE = re.compile(
    r"^(?:(?:cosmos\s+)?(?:what(?:'s|s| is)?\s+(?:the\s+)?time(?:\s+is\s+it)?|"
    r"what\s+time\s+is\s+it|current\s+time|time\s+please|time(?:\s+now)?))$",
    re.IGNORECASE)
_DATE_RE = re.compile(
    r"^(?:what(?:'s|s| is)?\s+(?:the\s+|today'?s\s+)?date(?:\s+today)?|"
    r"what\s+day\s+is\s+(?:it|today)|today'?s\s+date|what\s+is\s+today)$",
    re.IGNORECASE)

# "pause the music", "skip this song", "next track", "what's playing"
_MEDIA_RE = re.compile(
    r"^(?:(pause|play|resume|next|skip|previous|prev)\s*"
    r"(?:the\s+|this\s+)?(?:music|song|track)?|"
    r"what(?:'s|s| is)\s+(?:playing|this\s+song))$", re.IGNORECASE)

# "play despacito on youtube" / "play lo-fi beats" — a SPECIFIC query (bare
# "play" / "play the music" is _MEDIA_RE's resume). Default target: YouTube.
_PLAY_RE = re.compile(
    r"^play\s+(?!(?:the\s+|this\s+)?(?:music|song|track)\b)(.+?)"
    r"(?:\s+on\s+(youtube|spotify|apple\s*music|music))?$", re.IGNORECASE)

# "turn it up" / "volume down" / "louder" — relative volume, one osascript.
_VOL_REL_RE = re.compile(
    r"^(?:turn\s+(?:it|the\s+volume|the\s+sound)?\s*(up|down)|"
    r"turn\s+(up|down)\s+the\s+(?:volume|sound)|"
    r"volume\s+(up|down)|(louder|quieter|softer))$", re.IGNORECASE)
_MUTE_RE = re.compile(
    r"^(un)?mute(?:\s+(?:the\s+)?(?:sound|volume|audio|mac|yourself))?$",
    re.IGNORECASE)

_LOCK_RE = re.compile(
    r"^lock\s+(?:the\s+|my\s+)?(?:screen|mac|computer|laptop)$", re.IGNORECASE)

_CLIP_RE = re.compile(
    r"^(?:what(?:'s|s| is)\s+(?:on|in)\s+(?:my\s+|the\s+)?clipboard|"
    r"read\s+(?:my\s+|the\s+)?clipboard|clipboard(?:\s+contents?)?)$",
    re.IGNORECASE)

# "go to github.com" / "open example.com" — URL-or-domain targets that the
# open-app branch deliberately skips.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b([\w-]+(?:\.[\w-]+)*\.(?:com|in|io|dev|ai|app|org|net))\b", re.IGNORECASE)
_GOTO_RE = re.compile(
    r"^(?:go\s+to|open|browse(?:\s+to)?|navigate\s+to|visit)\s+(.+)$", re.IGNORECASE)


# ─── Response cache — instant answers for repeated read-lookups ─────────────────
# Keyed by normalized command text; short TTL so data can't go stale. Volatile
# queries (time, screen, "right now") are never cached.
_RESP_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = float(os.getenv("FRIDAY_CACHE_TTL", "150"))
_VOLATILE_RE = re.compile(
    r"\b(time|now|today|tonight|o'?clock|screen|clipboard|just now|currently)\b", re.IGNORECASE)


def _cache_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _cache_get(text: str) -> str | None:
    if _VOLATILE_RE.search(text):
        return None
    e = _RESP_CACHE.get(_cache_key(text))
    if e and e[0] > time.monotonic():
        return e[1]
    return None


def _cache_put(text: str, answer: str) -> None:
    if _VOLATILE_RE.search(text) or not answer:
        return
    # Don't cache error/apology answers.
    if answer.lower().startswith(("error", "i ran into", "i hit", "sorry", "hmm")):
        return
    _RESP_CACHE[_cache_key(text)] = (time.monotonic() + _CACHE_TTL, answer)
    if len(_RESP_CACHE) > 200:          # simple bound
        now = time.monotonic()
        expired = [k for k, (exp, _) in _RESP_CACHE.items() if exp <= now]
        for k in expired:
            _RESP_CACHE.pop(k, None)
        # If still over limit after purging expired, drop the soonest-expiring
        if len(_RESP_CACHE) > 200:
            by_expiry = sorted(_RESP_CACHE, key=lambda k: _RESP_CACHE[k][0])
            for k in by_expiry[:50]:
                _RESP_CACHE.pop(k, None)


def _weather_quip(w: dict) -> str:
    """A short JARVIS-style suggestion tailored to the conditions."""
    code, temp, feels = w.get("code", 0), w["temp"], w["feelsLike"]
    wind, hum = w.get("windspeed", 0), w["humidity"]
    if code in (95, 96):
        return "Thunder about — best enjoyed indoors with a hot chai and some snacks, sir."
    if code in (61, 63, 65, 80, 81, 82):
        return "Take an umbrella, sir — or embrace it; rainy days do pair well with hot pakoras."
    if code in (51, 53, 55, 45, 48):
        return "A bit drizzly, sir — a light jacket wouldn't go amiss."
    if wind >= 30:
        return f"Rather blustery at {wind} km/h, sir — hold onto your hat."
    if feels >= 36 or temp >= 34:
        return "A proper scorcher, sir — stay hydrated; a cold coffee is well warranted."
    if temp <= 15:
        return "Bit nippy, sir — a jacket and something warm to drink would serve you well."
    if hum >= 80 and temp >= 28:
        return "Muggy out there, sir — basically soup weather."
    return "Rather pleasant, sir — a fine excuse to step out."


def _looks_multistep(text: str) -> bool:
    """True when the utterance chains more than one action, so a zero-LLM
    fast-path must NOT hijack it — the full agent has to run every step.

    Catches the real failure ("open slack AND take a screenshot AND draft an
    email") without tripping on benign queries ("temperature and humidity"),
    which contain no action verb.
    """
    lower = text.lower()
    # Explicit sequencing words almost always mean a second step follows.
    if re.search(r'\b(then|afterwards?|after that|also|plus|next|followed by)\b', lower):
        return True
    # "and"/"," joining an action verb on either side → two tasks.
    if re.search(rf'\b(?:{_ACTION_VERB})\b.*[,]|\b(?:{_ACTION_VERB})\b.*\band\b|'
                 rf'\band\b.*\b(?:{_ACTION_VERB})\b', lower):
        return True
    return False


# ─── Run lock + crash journal ──────────────────────────────────────────────────

# ONE agent run at a time across ALL connections — two HUD tabs must never
# drive the same physical mouse/keyboard concurrently.
_RUN_LOCK = asyncio.Lock()

# Written before each run, removed when it completes — if it exists at connect
# time with no live run, the backend died/restarted mid-task and the user
# deserves to know instead of silent amnesia.
_CRASH_JOURNAL = Path.home() / ".friday" / "current_run.json"


def _journal_write(command: str) -> None:
    try:
        _CRASH_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_JOURNAL.write_text(json.dumps(
            {"command": command, "started_at": datetime.now().isoformat()}))
    except Exception:
        pass


def _journal_clear() -> None:
    try:
        _CRASH_JOURNAL.unlink(missing_ok=True)
    except Exception:
        pass


def _journal_mark_delivered() -> None:
    """The answer reached the user — a crash from here on is a cleanup loss,
    not an unfinished task. The reconnect notice words itself accordingly."""
    try:
        data = json.loads(_CRASH_JOURNAL.read_text())
        data["answer_delivered"] = True
        tmp = _CRASH_JOURNAL.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _CRASH_JOURNAL)
    except Exception:
        pass


async def _notify_interrupted_run(ws: WebSocket) -> None:
    """One-shot 'I was restarted mid-task' notice on connect, then forget it."""
    if _RUN_LOCK.locked() or not _CRASH_JOURNAL.exists():
        return
    try:
        data = json.loads(_CRASH_JOURNAL.read_text())
        cmd = (data.get("command") or "")[:120]
        delivered = bool(data.get("answer_delivered"))
    except Exception:
        cmd, delivered = "", False
    _journal_clear()
    if cmd:
        if delivered:
            # The answer went out; only the verify/bookkeeping tail was lost.
            text = (f"A heads-up, sir — I answered “{cmd}” earlier but was "
                    f"restarted during cleanup; my memory of that task may be "
                    f"incomplete.")
        else:
            text = (f"A heads-up, sir — I was restarted mid-task earlier and "
                    f"never finished: “{cmd}”. Say the word and I'll pick it up again.")
        try:
            await ws.send_json({"type": "response", "text": text})
        except Exception:
            pass


# Connected HUD sockets — scheduler results broadcast to all of them.
_WS_CLIENTS: set = set()


async def _broadcast(event: dict) -> None:
    """Send an event to every connected HUD client. Best effort — a dead
    socket is skipped, never raises."""
    for ws in list(_WS_CLIENTS):
        try:
            await ws.send_json(event)
        except Exception:
            pass


# How many HUD clients are connected right now — used to decide whether a
# finished run should also fire a native macOS notification.
_CONNECTED_CLIENTS = 0

# Runs longer than this get a completion notification even with the HUD open
# (the user has almost certainly tabbed away).
_NOTIFY_AFTER_S = 60.0


# ─── WebSocket ─────────────────────────────────────────────────────────────────

class _ConnState:
    """Per-connection state: running agent task + pending confirm/ask future."""

    def __init__(self):
        self.current_task: asyncio.Task | None = None
        self.interaction = agent.Interaction()
        # Conversation history is PER CONNECTION — never shared across clients,
        # so one HUD window can't leak context into another.
        self.history: list[dict] = []
        # Which frontend conversation this history belongs to. The FE sends
        # its active id with every command; switching lazily saves/loads.
        self.conv_id: str = convstore.DEFAULT_ID
        # Permission mode: "ask" (guarded — every outward action confirms) or
        # "full" (only irreversible deletions confirm). Set from the FE toggle.
        self.mode: str = "ask"
        # ── Session reattach ──
        # The live socket is MUTABLE: a reconnect swaps it in place so an
        # in-flight run keeps emitting to whoever is listening now.
        self.ws: WebSocket | None = None
        self.session_id: str = ""
        self.seq: int = 0
        # Structural events buffered for replay after a reconnect (speak /
        # response_delta excluded — replaying voice would double-talk).
        # Big enough that a long tool-heavy run doesn't evict its own
        # action_start/todos head before a refresh replays it.
        self.events: deque = deque(maxlen=1000)
        # Grace-period disposal armed on disconnect, cancelled on reattach.
        self.cancel_handle: asyncio.TimerHandle | None = None
        # Why the current task was cancelled ("user" | "connection") — the
        # completion path words its message and notification honestly.
        self.cancel_reason: str = ""
        # ── Verify-tail command queue ──
        # Once a run's answer is delivered, its verify/bookkeeping tail still
        # holds `busy` for ~1-3s. A follow-up command arriving in that window
        # queues here (single slot, last wins) instead of being refused with
        # "Still working…" — dispatched the moment the run closes.
        self.answer_delivered: bool = False
        self.pending_text: str | None = None

    @property
    def busy(self) -> bool:
        return self.current_task is not None and not self.current_task.done()


# session_id → state, so a 1-second Wi-Fi blip (or Vite HMR reload) can
# reclaim its running task instead of killing a 5-minute build job.
_SESSIONS: dict[str, _ConnState] = {}
_REATTACH_GRACE_S = 90.0
_SESSION_TTL_S = 600.0

_BUFFERED_TYPES = {"state", "todos", "tool_start", "tool_done", "agent_thought",
                   "confirm_request", "confirm_timeout", "ask_user",
                   "action_start", "action_complete", "response", "run_meta"}


async def _send_event(state: _ConnState, event: dict) -> None:
    """Send to the CURRENT socket, buffering structural events (with sequence
    numbers) so a reconnecting client can replay what it missed."""
    if event.get("type") in _BUFFERED_TYPES:
        state.seq += 1
        event = {**event, "seq": state.seq}
        state.events.append(event)
    ws = state.ws
    if ws is not None:
        try:
            await ws.send_json(event)
        except Exception:
            pass


def _arm_disposal(state: _ConnState) -> None:
    """Disconnect: give the client _REATTACH_GRACE_S to come back before the
    running task is cancelled; drop idle sessions after the TTL."""
    loop = asyncio.get_event_loop()
    if state.busy:
        def _dispose(st=state):
            if st.ws is None and st.busy:
                print(f"[WS] session {st.session_id or '?'} grace expired — cancelling run")
                st.cancel_reason = "connection"
                st.interaction.cancel()
                st.current_task.cancel()
        state.cancel_handle = loop.call_later(_REATTACH_GRACE_S, _dispose)

    def _ttl(sid=state.session_id):
        st = _SESSIONS.get(sid)
        if st is not None and st.ws is None:
            _SESSIONS.pop(sid, None)
    if state.session_id:
        loop.call_later(_SESSION_TTL_S, _ttl)


def _new_session_state(ws: WebSocket, session_id: str) -> _ConnState:
    state = _ConnState()
    state.ws = ws
    state.session_id = session_id
    if session_id:
        _SESSIONS[session_id] = state
    # Resume the persisted default conversation so a backend restart / page
    # reload doesn't wipe mid-conversation context; the FE's first command
    # carries its real active conversation id and lazily switches to it.
    state.history = convstore.load(state.conv_id)
    asyncio.create_task(_push_weather(ws))
    asyncio.create_task(_notify_interrupted_run(ws))
    asyncio.create_task(_push_suggestion(ws))
    return state


async def _reattach(ws: WebSocket, state: _ConnState, last_seq: int) -> None:
    """A client reclaimed its session: swap the socket in, replay missed
    structural events, restore any pending confirm banner and live state."""
    if state.cancel_handle:
        state.cancel_handle.cancel()
        state.cancel_handle = None
    try:
        await ws.send_json({"type": "hello_ack", "attached": True})
    except Exception:
        return
    # Replay FIRST, attach the socket LAST. Attaching first let the still-
    # running task's live emits (higher seq) interleave ahead of the replay —
    # the client's seq high-water mark then discarded the rest of the replay
    # (including a final `response`) forever. While we replay, live emits only
    # buffer (state.ws is still None); the loop below drains until caught up,
    # and the final check + attach are synchronous, so nothing slips between.
    replayed = 0
    sent = last_seq
    while True:
        pending = [ev for ev in list(state.events) if ev.get("seq", 0) > sent]
        if not pending:
            break
        for ev in pending:
            try:
                await ws.send_json(ev)
            except Exception:
                return
            sent = ev.get("seq", sent)
            replayed += 1
    state.ws = ws
    if state.interaction.pending and state.interaction.payload:
        try:
            await ws.send_json(state.interaction.payload)
        except Exception:
            pass
    try:
        await ws.send_json({"type": "state",
                            "state": "executing" if state.busy else "idle"})
    except Exception:
        pass
    print(f"[WS] session {state.session_id} reattached "
          f"(busy={state.busy}, replayed {replayed} events)")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _CONNECTED_CLIENTS
    await ws.accept()
    _CONNECTED_CLIENTS += 1
    _WS_CLIENTS.add(ws)
    print("[WS] Client connected")

    state: _ConnState | None = None

    try:
        while True:
            raw = await ws.receive_text()
            # One malformed frame must not kill the connection (and thereby
            # cancel a running agent task) — skip it and keep listening.
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                print(f"[WS] Ignoring malformed frame: {raw[:80]!r}")
                continue
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type", "")

            # First frame decides attach-vs-new. A `hello` with a known
            # session id reclaims the running state; anything else (or an
            # unknown id) starts fresh.
            if state is None:
                sid = str(data.get("session_id") or "") if msg_type == "hello" else ""
                existing = _SESSIONS.get(sid) if sid else None
                if existing is not None:
                    state = existing
                    await _reattach(ws, state, int(data.get("last_seq") or 0))
                    continue
                state = _new_session_state(ws, sid)
                if msg_type == "hello":
                    # Fresh session (backend restarted / grace expired): the
                    # server's seq counter is back at 0, so the client MUST
                    # reset its replay-dedupe counter — otherwise every new
                    # buffered event (seq 1, 2, …) is <= its stale last_seq
                    # and gets silently dropped until the counter catches up.
                    try:
                        await ws.send_json({"type": "hello_ack", "attached": False})
                    except Exception:
                        pass
                    continue

            if msg_type == "command":
                m = data.get("mode")
                if m in ("ask", "full"):
                    state.mode = m
                _switch_conversation(state, data.get("conversation_id"))
                await handle_command(ws, state, data.get("text", ""))
            elif msg_type == "set_mode":
                m = data.get("mode")
                if m in ("ask", "full"):
                    state.mode = m
            elif msg_type == "confirm":
                # Only resolve the confirm this click was FOR: a duplicate
                # frame or a banner replayed after reconnect must not approve
                # a LATER pending action (risk-gate bypass), nor feed a
                # literal "yes" to an ask_user question.
                cid = str(data.get("id") or "")
                payload = state.interaction.payload or {}
                if (state.interaction.pending
                        and state.interaction.kind == "confirm"
                        and (not cid or str(payload.get("id") or "") == cid)):
                    state.interaction.resolve(
                        "yes" if data.get("response") == "yes" else "no")
            elif msg_type == "new_chat":
                # Fresh conversation: persist the old one, start empty.
                if not state.busy:
                    convstore.save(state.conv_id, state.history)
                    state.conv_id = str(data.get("id") or convstore.DEFAULT_ID)
                    state.history = []
            elif msg_type == "switch_conversation":
                _switch_conversation(state, data.get("id"))
            elif msg_type == "delete_conversation":
                cid = str(data.get("id") or "")
                if cid and cid != state.conv_id:
                    convstore.delete(cid)
            elif msg_type == "prefetch":
                # Speculative warm-up sent from the first wake-word interim:
                # starts the ~440ms focus probe inside STT's ~0.5-1s
                # endpointing dead time, so the run reads it from cache.
                # Strictly side-effect-free — no events, no interaction, no
                # run state, and never awaited here.
                asyncio.create_task(system_control.get_focus_context_cached())
            elif msg_type == "stop":
                await handle_stop(ws, state)

    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        _CONNECTED_CLIENTS = max(0, _CONNECTED_CLIENTS - 1)
        _WS_CLIENTS.discard(ws)
        if state is not None and state.ws is ws:
            # Grace period instead of instant cancellation — a 1-second Wi-Fi
            # blip must not kill a 5-minute build job. The disposal timer fires
            # only if no reattach claims the session first.
            state.ws = None
            _arm_disposal(state)


def _switch_conversation(state: _ConnState, conv_id) -> None:
    """Lazily point this connection's history at a different conversation.
    Never switches mid-run — the in-flight agent mutates state.history."""
    cid = str(conv_id or "")
    if not cid or cid == state.conv_id or state.busy:
        return
    convstore.save(state.conv_id, state.history)
    state.conv_id = cid
    state.history = convstore.load(cid)


async def handle_stop(ws: WebSocket, state: _ConnState):
    """Cancel the running task (if any).

    Deliberately silent: the frontend speaks/logs its own local "Stopped, sir."
    on an explicit stop, and also fires `stop` as silent cleanup on new-chat /
    conversation switches — sending a `response` here would duplicate the speech
    and inject "Stopped, sir." into brand-new conversations.
    """
    if state.busy:
        state.cancel_reason = "user"
        # Stop clears the follow-up slot AT STOP TIME: anything queued before
        # the stop dies with the run; anything queued after it is the user's
        # next intent and survives to the finally-dispatch.
        state.pending_text = None
        state.interaction.cancel()
        state.current_task.cancel()
    await _send_event(state, {"type": "state", "state": "idle"})


async def handle_command(ws: WebSocket, state: _ConnState, text: str):
    text = text.strip()
    if not text:
        return

    # ── Pending confirm/ask FIRST — "yes"/"no" would be eaten by the gibberish
    # filter below, so these checks must come before it.
    if state.interaction.pending:
        if state.interaction.kind == "confirm":
            # NO must be checked FIRST: spoken refusals like "no, that's not
            # right" or "do not proceed" contain yes-words ("right", "proceed")
            # — checking yes first would approve an explicit decline.
            if _is_no(text):
                state.interaction.resolve("no")
            elif _is_yes(text):
                state.interaction.resolve("yes")
            else:
                # Not an answer. The FE cleared its banner when it sent this
                # command — re-emit the pending confirm_request to restore it.
                await _send_event(state, {"type": "speak",
                    "text": "Awaiting your confirmation, sir — yes or no?"})
                if state.interaction.payload:
                    await ws.send_json(state.interaction.payload)
            return
        # ask_user: any command text IS the answer
        state.interaction.resolve(_apply_memory_corrections(text))
        return

    # ── Noise filter + learned corrections ────────────────────────────────────
    if _is_gibberish(text):
        # The FE set state 'thinking' before sending — release it, or the orb
        # spins until the safety timer fires.
        await _send_event(state, {"type": "state", "state": "idle"})
        return
    text = _apply_memory_corrections(text)
    lower = text.lower()

    if state.busy:
        if state.answer_delivered and state.pending_text is None:
            # Only the post-answer tail is running — the user already heard
            # the reply and reasonably considers the task done. Queue it
            # (single slot; a second follow-up gets the honest refusal below)
            # with a quiet acknowledgment so it never feels swallowed.
            state.pending_text = text
            await _send_event(state, {"type": "agent_thought",
                "text": "Queued — finishing the previous task's checks first."})
            return
        await _send_event(state, {"type": "speak",
            "text": "Still working on the previous task, sir. Say stop to cancel it."})
        return

    # Another surface (a second HUD window, a cron job, a watcher reflex, the
    # Slack bridge) is mid-run. Commands are NO LONGER refused outright:
    # lock-safe fast paths (pure reads + non-focus-stealing controls) run
    # immediately, focus-stealing fast paths are skipped (they'd disturb a
    # GUI-driving background run), and agent-bound commands fall through to
    # _run_agent, whose lock-wait path announces itself and queues.
    lock_free = not _RUN_LOCK.locked()

    await _send_event(state, {"type": "state", "state": "thinking"})

    # ── Fast paths (zero-LLM, <50ms) ───────────────────────────────────────────
    # Only for single-intent utterances. A compound command ("open slack AND
    # take a screenshot AND draft an email") must fall through to the agent so
    # every step runs — otherwise a lone keyword ("screenshot") hijacks it.
    fast_ok = not _looks_multistep(text)

    # Weather — word-boundary match so "brainstorm"/"training"/"rainbow" don't hijack.
    # Speaks the actual conditions with a JARVIS-style suggestion; if the weather
    # service fails it falls THROUGH to the agent — never a canned reply.
    if fast_ok and re.search(r"\b(weather|temperature|humidity|humid|raining|rain|forecast)\b", lower):
        try:
            w = await asyncio.wait_for(weather.get_current(), timeout=3)
            line = (f"It's {w['temp']}°C in {w['location']}, {w['description'].lower()}, "
                    f"feels like {w['feelsLike']}°. {_weather_quip(w)}")
            await _send_event(state, {"type": "response", "text": line})
            await _send_event(state, {"type": "state", "state": "idle"})
            return
        except Exception:
            pass  # fall through to the agent — it can look up the weather itself

    # Screenshot
    if fast_ok and re.search(r'\b(screenshot|screen shot|take a screenshot|take screenshot|'
                 r'capture (the |my |this )?screen|snap the screen)\b', lower):
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.expanduser(f"~/Desktop/Screenshot_{ts}.png")
        ok, saved = await system_control.take_screenshot(path)
        if ok:
            await system_control.run_shell("afplay /System/Library/Sounds/Grab.aiff 2>/dev/null || true")
            if lock_free:
                await system_control.run_shell(f"open {shlex.quote(saved)}")
                reply = "Screenshot taken and opened in Preview, sir."
            else:
                # A background run may be driving the GUI — don't steal focus.
                reply = "Screenshot saved to your Desktop, sir."
            await _send_event(state, {"type": "response", "text": reply})
        else:
            await _send_event(state, {"type": "response",
                "text": "Screenshot failed, sir. Grant Screen Recording to Terminal in System Settings."})
        await _send_event(state, {"type": "state", "state": "idle"})
        return

    # Volume
    vol_match = re.search(r'\bvolume\b.*?(\d{1,3})|(\d{1,3})\s*(?:%|percent).*?\bvolume\b', lower)
    if fast_ok and "volume" in lower and vol_match:
        level = int(vol_match.group(1) or vol_match.group(2))
        ok, msg = await system_control.set_volume(level)
        await _send_event(state, {"type": "response",
            "text": f"Volume set to {level}%, sir." if ok else f"Couldn't set volume, sir. {msg}"})
        await _send_event(state, {"type": "state", "state": "idle"})
        return

    # "Open <app>" — sub-second when the app is actually installed. Anything
    # that smells like a file/URL/tab, or any open_app failure, falls through
    # to the agent (it can handle files, websites, and typos).
    norm = lower.rstrip(".!?, ")
    m = _OPEN_APP_RE.match(norm)
    if fast_ok and lock_free and m:
        target = m.group(1).strip()
        if not _OPEN_NOT_AN_APP_RE.search(target) and "/" not in target and len(target) <= 30:
            try:
                ok, msg = await system_control.open_app(target)
            except Exception:
                ok = False
            if ok:
                await _send_event(state, {"type": "response", "text": f"{msg}, sir."})
                await _send_event(state, {"type": "state", "state": "idle"})
                return
            # not installed / failed → the agent decides what "open X" meant

    # Time / date — answered from the local clock, no LLM. Guarded against
    # "time in London", timers, timezones, and meeting queries.
    if fast_ok and not re.search(r"\b(in|timer|zone|meeting|calendar|remind)\b", lower):
        if _TIME_RE.match(norm):
            await _send_event(state, {"type": "response",
                "text": f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}, sir."})
            await _send_event(state, {"type": "state", "state": "idle"})
            return
        if _DATE_RE.match(norm):
            await _send_event(state, {"type": "response",
                "text": f"It's {datetime.now().strftime('%A, %B %d, %Y')}, sir."})
            await _send_event(state, {"type": "state", "state": "idle"})
            return

    # Media control — pause/skip/what's playing on the running player.
    # "play …" with a specific song/artist falls through to the agent.
    mm = _MEDIA_RE.match(norm)
    if fast_ok and mm:
        action = {"pause": "pause", "play": "play", "resume": "play",
                  "next": "next", "skip": "next",
                  "previous": "previous", "prev": "previous"}.get(
                      (mm.group(1) or "").lower(), "now_playing")
        ok, msg = await system_control.media_control(action)
        if ok:
            await _send_event(state, {"type": "response",
                "text": msg if action == "now_playing" else f"Done, sir — {msg}"})
            await _send_event(state, {"type": "state", "state": "idle"})
            return
        if action != "play":   # nothing playing → say so; bare "play" → agent
            await _send_event(state, {"type": "response",
                "text": "No music player is running, sir."})
            await _send_event(state, {"type": "state", "state": "idle"})
            return

    # Relative volume / mute — one osascript round-trip, no LLM.
    vm = _VOL_REL_RE.match(norm)
    if fast_ok and vm:
        direction = (vm.group(1) or vm.group(2) or vm.group(3) or vm.group(4) or "").lower()
        delta = 15 if direction in ("up", "louder") else -15
        ok, msg = await system_control.adjust_volume(delta)
        await _send_event(state, {"type": "response",
            "text": f"{msg}, sir." if ok else f"Couldn't change the volume, sir. {msg}"})
        await _send_event(state, {"type": "state", "state": "idle"})
        return
    mm2 = _MUTE_RE.match(norm)
    if fast_ok and mm2:
        ok, msg = await system_control.set_muted(not mm2.group(1))
        await _send_event(state, {"type": "response",
            "text": f"{msg[:-1]}, sir." if ok else f"Couldn't do that, sir. {msg}"})
        await _send_event(state, {"type": "state", "state": "idle"})
        return

    # Lock screen
    if fast_ok and _LOCK_RE.match(norm):
        ok, msg = await system_control.system_toggle("lock_screen")
        await _send_event(state, {"type": "response",
            "text": "Screen locked, sir." if ok else f"Couldn't lock the screen, sir. {msg}"})
        await _send_event(state, {"type": "state", "state": "idle"})
        return

    # Clipboard read
    if fast_ok and _CLIP_RE.match(norm):
        ok, content = await system_control.get_clipboard()
        content = (content or "").strip()
        reply = (f"On your clipboard, sir: {content[:400]}" if ok and content
                 else "The clipboard is empty, sir.")
        await _send_event(state, {"type": "response", "text": reply})
        await _send_event(state, {"type": "state", "state": "idle"})
        return

    # Go to a URL / domain — the open-app branch above deliberately skips
    # these; they used to burn a full agent loop for a one-liner.
    gm = _GOTO_RE.match(norm)
    if fast_ok and lock_free and gm:
        target = gm.group(1)
        u, d = _URL_RE.search(target), _DOMAIN_RE.search(target)
        if u or d:
            url = u.group(0) if u else f"https://{d.group(1)}"
            ok, msg = await system_control.open_url(url)
            if ok:
                await _send_event(state, {"type": "response",
                    "text": f"Opened {url}, sir."})
                await _send_event(state, {"type": "state", "state": "idle"})
                return
            # open failed → let the agent figure out what was meant

    # "play <query> [on youtube/spotify/apple music]" — open the right search
    # directly. Honest scope: it lands the results in front of the user in
    # ~1s instead of a 15-25s agent run; it does not press play for them.
    pm = _PLAY_RE.match(norm)
    if fast_ok and lock_free and pm and not _OPEN_NOT_AN_APP_RE.search(pm.group(1)):
        query = pm.group(1).strip()
        dest = (pm.group(2) or "youtube").lower().replace(" ", "")
        from urllib.parse import quote
        if dest == "spotify":
            ok, _ = await system_control.open_url("spotify:search:" + quote(query))
            reply = f"Opened Spotify search for “{query}”, sir."
        elif dest in ("applemusic", "music"):
            ok, _ = await system_control.open_url(
                "music://music.apple.com/search?term=" + quote(query))
            reply = f"Opened Apple Music search for “{query}”, sir."
        else:
            ok, _ = await system_control.open_url(
                "https://www.youtube.com/results?search_query=" + quote(query))
            reply = f"YouTube results for “{query}” are up, sir — top hit should be it."
        if ok:
            await _send_event(state, {"type": "response", "text": reply})
            await _send_event(state, {"type": "state", "state": "idle"})
            return
        # couldn't open → fall through to the agent

    # ── Response cache: instant answer for a repeated read-lookup ───────────────
    if agent._is_read_lookup(text):
        cached = _cache_get(text)
        if cached is not None:
            await _send_event(state, {"type": "response", "text": cached})
            await _send_event(state, {"type": "state", "state": "idle"})
            return

    # ── Degraded-mode honesty: every model recently failed → warn instead of
    # letting the user sit through what looks like a silent stall. The run
    # still proceeds (cooldown degrades latency, never availability).
    if llm.all_models_cooling([agent.AGENT_MODEL, *llm.AGENT_FALLBACKS]):
        await _send_event(state, {"type": "speak",
            "text": "A heads-up, sir — the AI gateway is struggling; this may be "
                    "slow. Weather, screenshots and volume still work instantly."})

    # ── Everything else → the agent ────────────────────────────────────────────
    state.current_task = asyncio.create_task(_run_agent(ws, state, text))


_REFOCUS_HUD = os.getenv("FRIDAY_REFOCUS", "1").lower() not in ("0", "false", "no")


async def _refocus_hud():
    """After a task activated other apps, bring the COSMOS HUD browser tab back
    to the front. Best-effort, never raises."""
    if not _REFOCUS_HUD:
        return
    try:
        await system_control.focus_friday_window()
    except Exception:
        pass


# Spoken acknowledgment at run start: task commands otherwise sit in 3-8s of
# dead silence before the first token. Rotated so back-to-back tasks don't
# sound robotic; a cooldown keeps a burst of commands from nagging. All lines
# are in _CANNED_TTS, so with the stream-path cache they play in ~300ms.
_ACK_LINES = ["On it, sir.", "Right away, sir.", "Working on it, sir."]
_ACK_COOLDOWN_S = 30.0
_ack_state: dict = {"i": 0, "last": None}

# An ack only makes sense when there is WORK to cover. A clear task command
# gets it almost immediately; anything conversational ("how are you"),
# explanatory ("why does X happen"), or a plain lookup waits until the run has
# PROVEN it's slow — those answer straight from the model, and an "On it, sir."
# in front of the answer is just noise. Either way it's dropped the moment the
# run produces anything perceivable (see progress["seen"]).
_ACK_DELAY_TASK  = 0.4
_ACK_DELAY_OTHER = 2.5


def _looks_like_task(text: str) -> bool:
    """Positive evidence the utterance asks COSMOS to *do* something, rather
    than to answer or chat. Deliberately conservative: a missed ack is a
    moment of silence, a false one talks over the reply."""
    return bool(agent._ACTION_VERB_RE.search(text or "")) or _looks_multistep(text)


def _next_ack() -> str | None:
    now = time.monotonic()
    if _ack_state["last"] is not None and now - _ack_state["last"] < _ACK_COOLDOWN_S:
        return None
    _ack_state["last"] = now
    line = _ACK_LINES[_ack_state["i"] % len(_ACK_LINES)]
    _ack_state["i"] += 1
    return line


async def _run_agent(ws: WebSocket, state: _ConnState, text: str):
    """One agent run: action_start → agent events → action_complete + response."""
    final = "Done, sir."
    ok = False
    run_t0 = time.monotonic()

    # Fresh replay buffer for this run (seq stays monotonic for the session).
    state.events.clear()

    # Perceivable-progress tracker: once the run has streamed text, started a
    # tool, or asked anything, the "Still on it" nudge below must stay quiet.
    progress = {"seen": False}

    async def emit(event: dict) -> None:
        # Buffered + sent to the CURRENT socket: a reconnect mid-run swaps
        # state.ws and replays what was missed — a dead client never raises
        # out of a tool task.
        if event.get("type") in ("response_delta", "response", "tool_start",
                                 "agent_thought", "confirm_request", "ask_user"):
            progress["seen"] = True
        if event.get("type") == "response_delta_reset":
            # Verify retracted the delivered answer — a corrective turn is
            # starting. Commands must get the honest busy refusal again, not
            # the post-answer quiet queue.
            state.answer_delivered = False
        await _send_event(state, event)

    async def _ack() -> None:
        # Deferred, not immediate: if the run answers or starts acting within
        # the window, the reply itself IS the acknowledgment and speaking over
        # it is worse than silence. The accept earcon already confirmed the
        # command was heard the instant it arrived.
        await asyncio.sleep(_ACK_DELAY_TASK if _looks_like_task(text)
                            else _ACK_DELAY_OTHER)
        if progress["seen"]:
            return
        line = _next_ack()
        if line:
            await _send_event(state, {"type": "speak", "text": line})

    async def _nudge() -> None:
        # A long prompt prefill or slow first LLM turn produces nothing the
        # user can perceive — one cached spoken line keeps it from reading as
        # a dead run. Quieted synchronously at every terminal transition;
        # never repeats.
        await asyncio.sleep(10)
        if not progress["seen"]:
            await _send_event(state, {"type": "speak", "text": "Still on it, sir."})

    def _quiet_nudge() -> None:
        # Synchronous, so neither the ack nor the nudge can observe seen=False
        # after the run reached a terminal state — even if their sleep already
        # expired. (A finished run must never announce it's starting.)
        progress["seen"] = True
        if nudge is not None:
            nudge.cancel()
        if ack is not None:
            ack.cancel()

    async def _pulse() -> None:
        # The FE's stuck-run watchdog measures backend SILENCE — but a single
        # slow LLM turn or long tool call is legitimately quiet for minutes.
        # A heartbeat every 20s proves liveness so the watchdog only fires on
        # a genuinely dead backend. Unbuffered on purpose (replaying
        # heartbeats after a reconnect would be noise).
        while True:
            await asyncio.sleep(20)
            ws_ = state.ws
            if ws_ is not None:
                try:
                    await ws_.send_json({"type": "heartbeat"})
                except Exception:
                    pass

    pulse = asyncio.create_task(_pulse())
    nudge: asyncio.Task | None = None
    ack: asyncio.Task | None = None
    state.cancel_reason = ""
    state.answer_delivered = False
    user_cancelled = False
    try:
        await emit({"type": "action_start", "command": text})
        await emit({"type": "state", "state": "executing"})
        if _RUN_LOCK.locked():
            # Queued behind another surface's run (second HUD window, cron
            # job, watcher reflex, Slack bridge) — say so instead of showing
            # a silent "executing" that produces no events for minutes. The
            # heartbeat pulse keeps the FE watchdog from declaring us stuck.
            await emit({"type": "agent_thought",
                        "text": "Another task is running — I'll start yours "
                                "the moment it finishes."})
        else:
            ack = asyncio.create_task(_ack())
            nudge = asyncio.create_task(_nudge())
        # Perceived completion: the agent hands each candidate answer over the
        # moment it exists — BEFORE its verify critic runs — so the user gets
        # the reply 0.8-2.5s sooner. A second call only happens when verify
        # forced a corrective turn; it supersedes the first answer out loud.
        answered = {"n": 0, "last": None}

        async def _on_answer(candidate: str) -> None:
            answered["n"] += 1
            answered["last"] = candidate
            state.answer_delivered = True
            # The crash journal must record that the answer reached the user:
            # a crash in the verify/bookkeeping tail is a cleanup loss, not an
            # unfinished task, and reconnect wording should say so.
            _journal_mark_delivered()
            prefix = "" if answered["n"] == 1 else "Correction, sir — "
            await emit({"type": "response", "text": prefix + candidate})

        async with _RUN_LOCK:
            _journal_write(text)
            try:
                final = await agent.run_task(text, emit, state.interaction,
                                             state.history, state.mode,
                                             on_answer=_on_answer)
            finally:
                _journal_clear()
        _quiet_nudge()
        ok = True
        # Cache read-lookup answers so an identical repeat is instant.
        if agent._is_read_lookup(text):
            _cache_put(text, final)
        # `response` FIRST: the FE's action_complete handler archives the run
        # and blanks the streaming buffer — sent the other way round, a lost
        # `response` frame left the screen with NOTHING (until refresh replay).
        # Send whenever the run's real ending is NOT what the early-answer
        # path delivered: never delivered (answered==0), or a verify-fail
        # corrective turn that died in step-limit/budget/error — silently
        # leaving the retracted first answer standing would be dishonest.
        if answered["last"] != final:
            prefix = "" if answered["n"] == 0 else "Correction, sir — "
            await _send_event(state, {"type": "response", "text": prefix + final})
        await _send_event(state, {"type": "action_complete", "summary": final})
        await _send_event(state, {"type": "state", "state": "idle"})
        # Return focus to the HUD browser AFTER the reply is on its way — the
        # up-to-7-browser AppleScript probe used to delay TTS on every run.
        asyncio.create_task(_refocus_hud())
    except asyncio.CancelledError:
        _quiet_nudge()
        user_cancelled = state.cancel_reason != "connection"
        if user_cancelled:
            # stop handler already reset FE state; still close the run so the
            # FE archives it instead of leaving stale todo/tool cards.
            final = "Cancelled by user."
            await _send_event(state, {"type": "action_complete", "summary": final})
        else:
            # Grace expiry — the user cancelled NOTHING. Say what happened,
            # and let the finally-block notification fire (they're away).
            final = (f"The connection was gone too long, sir, so I stopped "
                     f"mid-task: “{text[:120]}”. Say the word to retry.")
            await _send_event(state, {"type": "response", "text": final})
            await _send_event(state, {"type": "action_complete", "summary": final})
            await _send_event(state, {"type": "state", "state": "idle"})
    except Exception as e:
        _quiet_nudge()
        final = f"Task failed, sir — {str(e)[:160]}"
        await _send_event(state, {"type": "response", "text": final})
        await _send_event(state, {"type": "action_complete", "summary": final})
        await _send_event(state, {"type": "state", "state": "idle"})
        asyncio.create_task(_refocus_hud())
    finally:
        pulse.cancel()
        if nudge is not None:
            nudge.cancel()
        if ack is not None:
            ack.cancel()
        # Dispatch a command queued during the verify tail. The task runs
        # after this coroutine unwinds, so `busy` is already False by then.
        # handle_stop clears the slot itself, so anything still here was
        # queued AFTER a stop (the user's next intent) or during a normal
        # tail — dispatch it. Grace-expiry endings are the exception: the
        # user has been gone 90s+, running their stale follow-up unattended
        # would be wrong. A detached-but-in-grace socket is fine — every
        # emit goes through the buffered replay.
        pending, state.pending_text = state.pending_text, None
        state.answer_delivered = False
        if pending and state.cancel_reason != "connection":
            asyncio.create_task(handle_command(state.ws, state, pending))
        memory_record_task(text, ok)
        # Persist the conversation after every run (agent already appended the
        # user/assistant pair to state.history). Never raises.
        convstore.save(state.conv_id, state.history)
        # Unattended finish → native notification (HUD closed, or a long run
        # the user has surely tabbed away from). Best-effort, detached.
        elapsed = time.monotonic() - run_t0
        if not user_cancelled and (
                _CONNECTED_CLIENTS == 0 or elapsed > _NOTIFY_AFTER_S):
            asyncio.create_task(system_control.notify(
                "COSMOS — task finished", final[:200]))


# ─── Boot push helpers ─────────────────────────────────────────────────────────

async def _push_suggestion(ws: WebSocket):
    """Proactive chip: if a task is frequent at THIS hour of day and hasn't
    run recently, suggest it on connect. One suggestion max, dismissible."""
    await asyncio.sleep(2)
    try:
        mem = memory.load()
        now = datetime.now()
        hour = now.hour
        for t in mem.get("frequent_tasks", [])[:15]:
            count = t.get("count", 0)
            hours = t.get("hours") or []
            if count < 3 or len(hours) != 24:
                continue
            near = hours[(hour - 1) % 24] + hours[hour] + hours[(hour + 1) % 24]
            if near / count < 0.6:
                continue                    # not an at-this-hour habit
            last = t.get("last", "")
            if last:
                try:
                    if (now - datetime.fromisoformat(last)).total_seconds() < 2 * 3600:
                        continue            # already done recently
                except Exception:
                    pass
            await ws.send_json({"type": "suggestion", "text": t["task"]})
            return
    except Exception:
        pass


async def _push_weather(ws: WebSocket):
    await asyncio.sleep(1)
    try:
        d = await weather.get_current()
        await ws.send_json({"type": "weather", "payload": d})
    except Exception:
        pass


# ── Production HUD serving ──────────────────────────────────────────────────────
# Serve the built frontend straight from :8000 when a dist/ exists: kills the
# permanent Vite dev-server tax (unminified React, dev StrictMode double-render,
# a proxy hop on every /api call including the first-audio-critical
# /api/tts/stream). Registered LAST so /api, /health and /ws keep precedence.
# Dev workflow (COSMOS_DEV=1 in start.sh) still runs Vite on :5173 — this mount
# is inert for it apart from also serving a copy on :8000.
_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=str(_DIST_DIR), html=True), name="hud")


if __name__ == "__main__":
    import uvicorn
    from services import singleton
    # Refuse to become a SECOND backend (see services.singleton for why: 3x cron
    # firing, races on ~/.friday state, and a per-process run-lock that stops
    # protecting the mouse the moment two backends exist). Checked BEFORE
    # uvicorn binds so the failure is instant and legible. Under FRIDAY_RELOAD=1
    # uvicorn's worker imports this module as "main", not "__main__", so only
    # the parent takes the lock and hot-reload still works.
    _ok, _holder = singleton.acquire()
    if not _ok:
        print(f"\n  COSMOS is already running (pid {_holder}) — refusing to start.\n\n"
              f"  A second backend would fire every scheduled job twice, race on\n"
              f"  ~/.friday state, and defeat the run-lock (two agents driving the\n"
              f"  same mouse). Lock: {singleton.DEFAULT_LOCK}\n\n"
              f"    Stop it:  kill -9 {_holder}      (or: pkill -f main.py)\n"
              f"    Restart:  ./start.sh\n"
              f"    Override: FRIDAY_SINGLE_INSTANCE=0   (not recommended)\n")
        raise SystemExit(1)
    # reload=True runs the server in a DETACHED worker process (its own
    # session), which becomes its own TCC "responsible process" — so camera /
    # mic / screen-recording grants on the launching terminal DON'T apply and
    # imagesnap etc. fail with "access not granted". Default reload OFF so the
    # server stays a child of the terminal and inherits its permissions; opt
    # into hot-reload for dev with FRIDAY_RELOAD=1.
    reload = os.getenv("FRIDAY_RELOAD", "0").lower() in ("1", "true", "yes")
    # Localhost by default: this API executes commands with no auth, so it must
    # not be reachable from the office LAN/VPN. FRIDAY_HOST=0.0.0.0 to opt out.
    host = os.getenv("FRIDAY_HOST", "127.0.0.1")
    uvicorn.run("main:app", host=host, port=8000, reload=reload)

"""
COSMOS agentic loop — Anthropic tool-use API, observe→think→act until done.

run_task(user_text, emit, interaction) drives the whole run:
  - emit(event: dict)   → async callback; events follow WebSocket protocol v3
  - interaction         → Interaction bridge for confirm_request / ask_user futures

Tools delegate to system_control / web_search / vision — reuse, don't rewrite.
"""

import os
import re
import json
import time
import asyncio
import hashlib
import shlex
from types import SimpleNamespace
from uuid import uuid4
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from services import llm, system_control, web_search as web_svc
from services import vision as vision_svc
from services import audit, slack as slack_svc
from services import google as google_svc
from services import memory as memory_svc
from services import recall as recall_svc
from services import embeddings as embeddings_svc
from services import learning
from services import routines
from services import skill_synth
from services import documents
from services import compress
from services import raptor
from services import ledger as ledger_svc
from services.trace import RunTrace

# Defaults follow llm.DEFAULT_MODEL — never hardcode a model here: the old
# fallback literal was claude-sonnet-4-6, which is budget-dead on this key.
AGENT_MODEL = os.getenv("FRIDAY_AGENT_MODEL", llm.DEFAULT_MODEL)
FAST_MODEL  = os.getenv("FRIDAY_FAST_MODEL", AGENT_MODEL)

MAX_ITERATIONS = 40

# ── Reliability guards ─────────────────────────────────────────────────────────
# Per-tool execution ceilings: a hung osascript/subprocess must never freeze the
# run forever (the frontend gives up at 60s but the backend would stay "busy"
# and reject every new command until restart). ask_user/say are exempt (they
# wait on the human); bash/github/record_video get generous dynamic ceilings
# because their inner subprocess timeouts already bound them.
TOOL_TIMEOUT_S = float(os.getenv("FRIDAY_TOOL_TIMEOUT", "90"))
# spawn_agents bounds itself: each worker has its own wall-clock ceiling.
_TOOL_TIMEOUT_EXEMPT = {"ask_user", "say", "propose_plan", "spawn_agents"}


def _tool_timeout(tool: str, args: dict) -> float | None:
    if tool in _TOOL_TIMEOUT_EXEMPT:
        return None
    meta = _DYNAMIC_TOOLS.get(tool)
    if meta is not None:
        return meta.get("timeout") or TOOL_TIMEOUT_S
    if tool == "bash":
        # run_shell's own timeout (≤300s) + margin; background launches in 15s.
        return 320.0
    if tool == "github":
        return 60.0
    if tool == "record_video":
        return float(int(args.get("duration") or 5) + 60)
    return TOOL_TIMEOUT_S

# An unanswered confirm/ask must not block the run eternally (user walked away,
# tab closed). Confirm auto-declines; ask_user returns a "no answer" result.
CONFIRM_TIMEOUT_S = float(os.getenv("FRIDAY_CONFIRM_TIMEOUT", "180"))
ASK_TIMEOUT_S     = float(os.getenv("FRIDAY_ASK_TIMEOUT", "300"))

# Loop-breaker: the Nth identical failing call is refused without executing.
_MAX_IDENTICAL_FAILURES = 3

# Cumulative token ceiling per run (input + output across all turns) — a
# runaway loop must not silently burn the whole gateway budget.
RUN_TOKEN_BUDGET = int(os.getenv("FRIDAY_RUN_TOKEN_BUDGET", "500000"))

USER_NAME         = os.getenv("USER_NAME",         "Ravindra")
USER_EMAIL        = os.getenv("USER_EMAIL",        "")
USER_SLACK_HANDLE = os.getenv("USER_SLACK_HANDLE", "ravindra.c")

# COSMOS's own project root (…/friday). Used by the self-protection guard so
# Cosmos can never move/delete the directory it's running from — which is
# exactly how an "organize my desktop" run once broke both servers.
FRIDAY_ROOT = Path(__file__).resolve().parents[2]

# History keeps only user text + final assistant text (never tool blocks).
# The list itself lives on the connection state (main._ConnState.history) so
# concurrent clients never leak/interleave context.
_HISTORY_CAP = 16


# ─── Confirm / ask bridge ──────────────────────────────────────────────────────

class Interaction:
    """One pending confirm/ask future shared between the agent loop and main.py."""

    def __init__(self):
        self.future: asyncio.Future | None = None
        self.kind: str | None = None  # "confirm" | "ask"
        # The confirm_request/ask_user event that opened this interaction, kept
        # so main.py can re-emit it (the FE clears its banner on any outgoing
        # command, even one that doesn't answer the confirmation).
        self.payload: dict | None = None

    @property
    def pending(self) -> bool:
        return self.future is not None and not self.future.done()

    def begin(self, kind: str, payload: dict | None = None) -> asyncio.Future:
        self.future = asyncio.get_running_loop().create_future()
        self.kind = kind
        self.payload = payload
        return self.future

    def resolve(self, value: str) -> bool:
        if self.future and not self.future.done():
            self.future.set_result(value)
            self.future = None
            self.kind = None
            self.payload = None
            return True
        return False

    def cancel(self) -> None:
        if self.future and not self.future.done():
            self.future.cancel()
        self.future = None
        self.kind = None
        self.payload = None


@dataclass
class RunContext:
    emit: Callable[[dict], Awaitable[None]]
    interaction: Interaction
    todos: list[dict] = field(default_factory=list)
    trace: RunTrace = field(default_factory=RunTrace)
    mode: str = "ask"   # "ask" (guarded) | "full" (only deletions confirm)
    # Headless scheduled run: no user is watching, confirms auto-decline, and
    # dynamic (MCP/macro) non-read-only tools always gate.
    unattended: bool = False
    # Sub-agent nesting depth: 0 = the user's run; workers run at 1 and may
    # not spawn further workers. Depth>0 skips recall/learning/verify.
    depth: int = 0
    # Hard read-only posture (sub-agent workers): every tool call is whitelist-
    # checked (_read_only_block) BEFORE the risk gate — mutations are refused,
    # not merely auto-declined.
    read_only: bool = False
    # tool+args → consecutive failure count, for the identical-retry breaker.
    fail_counts: dict[str, int] = field(default_factory=dict)
    # High-signal tool outputs (paths, IDs) captured for the history digest so
    # follow-ups like "send THAT photo to Vinay" still know the path.
    artifacts: list[str] = field(default_factory=list)
    # Every tool invoked this run — indexed into the recall DB at run end.
    tools_used: set = field(default_factory=set)
    # Ordered log of EXECUTED tool calls with their args + outcome — feeds the
    # routine replay cache (services.routines) and replay success detection.
    tool_seq: list = field(default_factory=list)
    # Exact call signatures (_call_key) the user pre-authorized via
    # propose_plan. Single-use: a key is discarded when its call runs.
    preapproved: set = field(default_factory=set)
    # Context ledger: the exact evidence this run stood on (recall rows, RAPTOR
    # summaries, tool artifacts, compressed blocks) — emitted at run end so an
    # answer's grounding is inspectable and retries are idempotent.
    ledger: ledger_svc.ContextLedger = field(default_factory=ledger_svc.ContextLedger)


# Tools whose successful output is worth remembering across turns.
_ARTIFACT_TOOLS = {"write_file", "take_photo", "record_video", "screenshot",
                   "bash", "github", "web_search"}


def _call_key(tool: str, args: dict) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    return f"{tool}:{hashlib.sha1(blob.encode()).hexdigest()}"


# ─── Tool schemas ──────────────────────────────────────────────────────────────

def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}

_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}

TOOLS: list[dict] = [
    {
        "name": "set_todos",
        "description": ("Replace the FULL todo list shown on the user's HUD. Call this FIRST "
                        "for any task with 2+ distinct steps, keep exactly ONE item in_progress "
                        "at a time, and update immediately after each item completes. Single "
                        "trivial actions need no todos."),
        "input_schema": _obj({
            "todos": {"type": "array", "items": _obj({
                "id": _S, "text": _S,
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            }, ["id", "text", "status"])},
        }, ["todos"]),
    },
    {
        "name": "bash",
        "description": ("Run a shell command silently (no Terminal window). Output is returned. "
                        "Set background=true for long-running servers — returns immediately."),
        "input_schema": _obj({
            "command": _S,
            "timeout_s": {**_I, "description": "Seconds before timeout (default 60, max 300)."},
            "background": {**_B, "description": "Run detached via nohup and return immediately."},
        }, ["command"]),
    },
    {
        "name": "applescript",
        "description": ("Universal macOS fallback: execute raw AppleScript — any app, UI element, "
                        "keyboard, mouse, menu bar, System Events. Use when no named tool fits."),
        "input_schema": _obj({
            "script": _S,
            "description": {**_S, "description": "Short human label for what this does."},
        }, ["script", "description"]),
    },
    {
        "name": "open_app",
        "description": "Open/activate a macOS application by name.",
        "input_schema": _obj({"name": _S}, ["name"]),
    },
    {
        "name": "open_url",
        "description": "Open a URL in a browser (visible to the user).",
        "input_schema": _obj({"url": _S, "browser": {**_S, "description": "e.g. Google Chrome; default browser if omitted."}}, ["url"]),
    },
    {
        "name": "open_path",
        "description": "Open a file or folder, optionally in a specific app (e.g. Visual Studio Code).",
        "input_schema": _obj({"path": _S, "app": _S}, ["path"]),
    },
    {
        "name": "read_file",
        "description": ("Read a plain-text file (or directory listing). Binary documents "
                        "(PDF/docx/images) are auto-routed to read_document."),
        "input_schema": _obj({"path": _S, "max_chars": _I}, ["path"]),
    },
    {
        "name": "read_document",
        "description": ("Extract readable text from a DOCUMENT: PDFs (pdftotext/pypdf), "
                        "Word/RTF/HTML (textutil), and IMAGES via on-device OCR (Apple "
                        "Vision). For a text-heavy screenshot, OCR here is faster and "
                        "cheaper than see_screen."),
        "input_schema": _obj({"path": _S, "max_chars": {**_I, "description": "Default 8000."}},
                             ["path"]),
    },
    {
        "name": "write_file",
        "description": ("Write a file anywhere on disk (absolute path or ~). Parent dirs are "
                        "created. Optionally open it in an app afterwards."),
        "input_schema": _obj({
            "path": {**_S, "description": "Absolute path or ~-relative."},
            "content": _S,
            "open_in": {**_S, "description": "App to open the file in after writing."},
        }, ["path", "content"]),
    },
    {
        "name": "web_search",
        "description": "Silent web search (DuckDuckGo). Returns numbered title — url — snippet lines.",
        "input_schema": _obj({"query": _S, "max_results": _I}, ["query"]),
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page and return its readable text (for research, after web_search).",
        "input_schema": _obj({"url": _S, "max_chars": {**_I, "description": "Default 6000."}}, ["url"]),
    },
    {
        "name": "see_screen",
        "description": ("Take a screenshot and answer a question about it with vision. LAST RESORT "
                        "for reading — prefer read_browser / read_app / browser_js first."),
        "input_schema": _obj({"question": _S}, ["question"]),
    },
    {
        "name": "read_browser",
        "description": "Read the text of the active browser tab (URL fetch first, JS fallback).",
        "input_schema": _obj({"app": {**_S, "description": "Browser app; default Google Chrome."}}, []),
    },
    {
        "name": "browser_js",
        "description": "Execute JavaScript in the active browser tab and return the result. Best for structured extraction.",
        "input_schema": _obj({"javascript": _S, "app": _S}, ["javascript"]),
    },
    {
        "name": "read_app",
        "description": "Read visible text from a native macOS app via the Accessibility API (no screenshot).",
        "input_schema": _obj({"app": _S}, ["app"]),
    },
    {
        "name": "type_text",
        "description": "Type text via keyboard takeover, optionally activating an app first.",
        "input_schema": _obj({"text": _S, "app": _S}, ["text"]),
    },
    {
        "name": "keystroke",
        "description": 'Send a keyboard shortcut, e.g. "cmd+t", "cmd+shift+n", "return", "escape".',
        "input_schema": _obj({"keys": _S, "app": _S}, ["keys"]),
    },
    {
        "name": "click_ui",
        "description": "Click a named UI element in an app via the Accessibility API.",
        "input_schema": _obj({
            "app": _S, "name": _S,
            "type": {**_S, "description": 'Element type: "button" (default), "menu item", "checkbox"…'},
        }, ["app", "name"]),
    },
    {
        "name": "mouse",
        "description": "Mouse control: move | click | double_click | right_click | scroll_up | scroll_down at (x, y).",
        "input_schema": _obj({"action": _S, "x": _I, "y": _I}, ["action", "x", "y"]),
    },
    {
        "name": "click_web",
        "description": ("Click an element IN A BROWSER by its exact visible text (via JavaScript) "
                        "— precise, the RIGHT way to click links/tabs/buttons in Chrome (e.g. "
                        "'Sprints', 'Roadmap'). ALWAYS use this instead of mouse/vision for web "
                        "pages — coordinate clicks land on the wrong element. Pass the exact label "
                        "you see; read_browser first if unsure."),
        "input_schema": _obj({
            "text": _S,
            "browser": {**_S, "description": "Default 'Google Chrome'."},
        }, ["text"]),
    },
    {
        "name": "gmail_attach",
        "description": ("Attach a file to the OPEN Gmail compose window in Chrome — the reliable "
                        "one-shot way. Finds the paperclip, real-clicks it, and picks `path` in the "
                        "Open dialog. Use this to attach to Gmail; do NOT search the page (Cmd+F) or "
                        "try to click the hidden file input. A compose window must be open first."),
        "input_schema": _obj({"path": _S}, ["path"]),
    },
    {
        "name": "choose_file",
        "description": ("When a native macOS file-picker/Open dialog is already showing (e.g. a "
                        "Slack/upload button), pick this file: navigates via Cmd+Shift+G and opens "
                        "`path`. For GMAIL specifically, prefer gmail_attach (one call)."),
        "input_schema": _obj({"path": _S}, ["path"]),
    },
    {
        "name": "slack",
        "description": (
            "Slack via the Web API (acts AS you — reliable, no UI clicking). "
            "action: unreads (all unread DMs+channels) | read (recent messages from a "
            "#channel or a person — `target`, optional `limit`) | send (post to a "
            "channel/person — `target`+`text`, add `thread`=<ts> to reply IN A THREAD) | "
            "react (emoji to the latest message in `target`) | mentions (channel messages that "
            "@-mention YOU and NEED YOUR REPLY — skips ones already answered by anyone, AND "
            "passing mentions or questions aimed at someone else; set all=true to see "
            "everything — each with the ts to reply to) | status (set your status — `text`,`emoji`,`minutes`) | dnd "
            "(Do-Not-Disturb: omit `minutes` to READ status, `minutes` >0 to snooze, "
            "`minutes` = -1 to turn OFF) | search "
            "(`query` — needs the search:read scope) | whoami. Reads are free; send / "
            "react / status / dnd-set pause for confirmation."),
        "input_schema": _obj({
            "action": {"type": "string",
                       "enum": ["unreads", "read", "mentions", "send", "react",
                                "status", "dnd", "search", "whoami"]},
            "target": {**_S, "description": "#channel name, person's name, or channel id."},
            "text": {**_S, "description": "Message body (send) or status text (status)."},
            "query": {**_S, "description": "Search query (search action)."},
            "emoji": {**_S, "description": "Emoji name, e.g. 'eyes' (react/status)."},
            "minutes": {**_I, "description": "dnd snooze / status auto-clear minutes."},
            "limit": {**_I, "description": "How many messages to read (default 15)."},
            "thread": {**_S, "description": "Message ts to reply to IN-THREAD (send). Get it from action=mentions or read."},
            "all": {**_B, "description": "mentions: show EVERY mention (incl. already-answered + passing ones) instead of only those needing your reply."},
        }, ["action"]),
    },
    {
        "name": "slack_status",
        "description": ("Set your Slack status (UI/legacy path). PREFER the `slack` tool's "
                        "action=status when SLACK_USER_TOKEN is set. text + optional emoji + "
                        "auto-clear minutes."),
        "input_schema": _obj({
            "text": _S,
            "emoji": {**_S, "description": "e.g. ':coffee:', ':calendar:' (optional)."},
            "minutes": {**_I, "description": "Auto-clear after N minutes (0 = don't clear)."},
        }, ["text"]),
    },
    {
        "name": "slack_dm",
        "description": ("Send a Slack DM via the Cmd+K quick switcher (UI automation — the "
                        "FALLBACK). PREFER the `slack` tool's action=send when SLACK_USER_TOKEN "
                        "is configured; only use this if the API tool is unavailable. Message is "
                        "sent EXACTLY as given — never rephrase what the user asked for."),
        "input_schema": _obj({"recipient": _S, "message": _S}, ["recipient", "message"]),
    },
    {
        "name": "github",
        "description": ("GitHub via the authenticated `gh` CLI — the RELIABLE way to read/act "
                        "on repos, PRs, issues, releases, and the API. ALWAYS prefer this over "
                        "opening github.com in a browser. Pass the gh arguments WITHOUT the "
                        'leading "gh", e.g. args="pr list --repo owner/name --json number,title" '
                        'or args="api /repos/owner/name/contents/path". Add --json for '
                        "structured output. Mutations (create/merge/delete/close/edit) pause "
                        "for confirmation."),
        "input_schema": _obj({"args": _S}, ["args"]),
    },
    {
        "name": "take_photo",
        "description": ("Capture ONE fresh photo from the webcam directly to a file (via "
                        "imagesnap — reliable, no Photo Booth, no file-hunting). Returns the "
                        "exact saved path. Use this for 'take a photo/selfie/picture', then "
                        "pass the returned path to slack_photo / write_file / etc."),
        "input_schema": _obj({"path": {**_S, "description": "Where to save; default ~/Desktop."}}, []),
    },
    {
        "name": "record_video",
        "description": ("Record a short webcam video (with mic audio) directly to a file via "
                        "ffmpeg — reliable, auto-detects the camera/mic. Returns the exact saved "
                        "path. Use for 'record a video/clip'. Default 5 seconds."),
        "input_schema": _obj({
            "duration": {**_I, "description": "Seconds to record (default 5, max 120)."},
            "path": {**_S, "description": "Where to save; default ~/Desktop."},
        }, []),
    },
    {
        "name": "slack_photo",
        "description": ("Send an IMAGE or VIDEO FILE to a Slack DM (slack_dm sends only text). Give "
                        "the EXACT path — e.g. from take_photo or record_video. Optional caption "
                        "is sent with it."),
        "input_schema": _obj({"recipient": _S, "image_path": _S,
                              "caption": {**_S, "description": "Optional text sent with the file."}},
                             ["recipient", "image_path"]),
    },
    {
        "name": "screenshot",
        "description": "Take a screenshot, save it (default ~/Desktop) and open it in Preview.",
        "input_schema": _obj({"path": _S}, []),
    },
    {
        "name": "music",
        "description": ("Control music playback (Spotify or Apple Music — the running player "
                        "is detected automatically): action = play | pause | playpause | next "
                        "| previous | now_playing | volume (with level 0-100)."),
        "input_schema": _obj({
            "action": _S,
            "level": {**_I, "description": "0-100, only for action=volume."},
            "player": {**_S, "description": "Force 'Spotify' or 'Music' (optional)."},
        }, ["action"]),
    },
    {
        "name": "system_state",
        "description": "Snapshot of open apps, disk space, git branch/changes, current time.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "set_volume",
        "description": "Set system output volume (0-100).",
        "input_schema": _obj({"level": _I}, ["level"]),
    },
    {
        "name": "calendar",
        "description": ("Apple Calendar. action=events lists (scope: today|tomorrow|week); "
                        "action=create adds an event — start MUST be ISO 'YYYY-MM-DDTHH:MM' "
                        "(resolve 'tomorrow 3pm' yourself using the LIVE CONTEXT date)."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["events", "create"]},
            "scope": {**_S, "description": "today | tomorrow | week (for events)."},
            "title": _S,
            "start": {**_S, "description": "ISO start, e.g. 2026-07-09T15:00 (for create)."},
            "duration_min": _I,
            "calendar_name": _S,
            "location": _S,
            "notes": _S,
        }, ["action"]),
    },
    {
        "name": "reminders",
        "description": ("Apple Reminders (syncs to the user's iPhone/Watch). action=create "
                        "(name + optional due ISO 'YYYY-MM-DDTHH:MM'), list_due, or "
                        "complete (by partial name). For 'remind me at 5' USE THIS."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["create", "list_due", "complete"]},
            "name": _S,
            "due": {**_S, "description": "ISO due time — resolve natural language yourself."},
            "list_name": _S,
        }, ["action"]),
    },
    {
        "name": "notes",
        "description": ("Apple Notes. action = create (title+body) | search (query) | "
                        "read (title) | append (title+body — ALWAYS to a named note, "
                        "never 'the latest note')."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["create", "search", "read", "append"]},
            "title": _S, "body": _S, "query": _S,
        }, ["action"]),
    },
    {
        "name": "comms_summary",
        "description": ("Inbox triage — 'anything important in my inbox / any unread "
                        "Slack?'. Reads the open Gmail tab, Slack unread counts (API), and "
                        "Mail.app (if running). Read-only, no side effects."),
        "input_schema": _obj({}, []),
    },
    {
        "name": "contacts",
        "description": ("Look up a person in the macOS Contacts app by name — returns "
                        "name/phones/emails. Use BEFORE send an iMessage to resolve the "
                        "handle; if 2+ people match, ask the user which one."),
        "input_schema": _obj({"name": _S}, ["name"]),
    },
    {
        "name": "imessage",
        "description": ("Send an iMessage via Messages.app. recipient is a PHONE/EMAIL "
                        "handle — resolve names with the contacts tool first. message is "
                        "sent EXACTLY as given."),
        "input_schema": _obj({"recipient": _S, "message": _S}, ["recipient", "message"]),
    },
    {
        "name": "clipboard",
        "description": "Read or write the macOS clipboard. action: read | write (with text).",
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["read", "write"]},
            "text": {**_S, "description": "Text to place on the clipboard (write)."},
        }, ["action"]),
    },
    {
        "name": "find_files",
        "description": ("Search files via Spotlight (mdfind) — THE way to find files/folders "
                        "('where's that CSV from last week'). Filter by name, text "
                        "content, kind (pdf|image|video|audio|folder|presentation|"
                        "spreadsheet|document), and recency. Returns paths with size+mtime."),
        "input_schema": _obj({
            "name": {**_S, "description": "Filename substring."},
            "content": {**_S, "description": "Text the file contains."},
            "kind": _S,
            "within_days": {**_I, "description": "Only files modified in the last N days."},
            "onlyin": {**_S, "description": "Directory scope (default ~)."},
            "limit": _I,
        }, []),
    },
    {
        "name": "shortcut",
        "description": ("macOS Shortcuts.app: action=list shows available shortcuts; "
                        "action=run executes one by exact name (unlocks Focus/DND toggles "
                        "and the user's personal automations). Optional input text is piped in."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["list", "run"]},
            "name": {**_S, "description": "Shortcut name (for run)."},
            "input": {**_S, "description": "Optional stdin text for the shortcut."},
        }, ["action"]),
    },
    {
        "name": "notify",
        "description": ("Show a native macOS notification (works even if the HUD tab is "
                        "closed). Use for background-task results and reminders."),
        "input_schema": _obj({"title": _S, "message": _S}, ["title", "message"]),
    },
    {
        "name": "system_toggle",
        "description": ("Quick system switches: feature = dark_mode | wifi | lock_screen | "
                        "caffeinate (keep Mac awake) | empty_trash. state = on | off | toggle."),
        "input_schema": _obj({
            "feature": {"type": "string",
                        "enum": ["dark_mode", "wifi", "lock_screen", "caffeinate", "empty_trash"]},
            "state": {**_S, "description": "on | off | toggle (default toggle)."},
        }, ["feature"]),
    },
    {
        "name": "schedule_task",
        "description": ("Schedule Cosmos to DO something later, unattended: 'at 6pm send "
                        "the report', 'every morning check X'. Give prompt + exactly one of "
                        "when (ISO 'YYYY-MM-DDTHH:MM', one-shot) or cron ('0 9 * * 1-5', "
                        "repeating). Runs headless — gated actions auto-decline, so keep "
                        "prompts to safe/read/report work. For simple 'remind me at 5' "
                        "notifications, prefer the reminders tool."),
        "input_schema": _obj({
            "prompt": _S,
            "when": {**_S, "description": "One-shot ISO time."},
            "cron": {**_S, "description": "Repeating cron: min hour dom mon dow."},
        }, ["prompt"]),
    },
    {
        "name": "list_scheduled",
        "description": "List Cosmos's scheduled background tasks (ids, schedules, prompts).",
        "input_schema": _obj({}, []),
    },
    {
        "name": "cancel_scheduled",
        "description": "Cancel a scheduled background task by its id (see list_scheduled).",
        "input_schema": _obj({"id": _S}, ["id"]),
    },
    {
        "name": "remember_fact",
        "description": ("Persist a LASTING fact into long-term memory — call this silently "
                        "whenever the user states a durable preference ('I prefer PRs "
                        "squashed'), a fact about a person ('Vinay handles CI'), an "
                        "ongoing project, or an app quirk worth keeping. kind: preference | "
                        "person | project | app. Not for one-off task details."),
        "input_schema": _obj({
            "kind": {"type": "string", "enum": ["preference", "person", "project", "app"]},
            "key": {**_S, "description": "Short identifier, e.g. 'PR merges', 'Vinay H'."},
            "value": {**_S, "description": "The fact itself, one sentence."},
        }, ["kind", "key", "value"]),
    },
    {
        "name": "forget_fact",
        "description": "Remove a remembered fact by its key (user said to forget it or it's wrong).",
        "input_schema": _obj({"key": _S}, ["key"]),
    },
    {
        "name": "recall_history",
        "description": ("Search Cosmos's index of PAST completed tasks (what was asked, the "
                        "outcome, when). Use for 'what did we do about X last week?', 'when "
                        "did I last …', or to reuse an earlier result. Read-only."),
        "input_schema": _obj({
            "query": _S,
            "days_back": {**_I, "description": "How far back to search (default 90)."},
        }, ["query"]),
    },
    {
        "name": "read_skill",
        "description": ("Load the FULL text of one of your skill playbooks by kebab-case name "
                        "(e.g. 'engineering', 'google-workspace'). When your system prompt "
                        "lists playbooks as an index, call this FIRST for any task that matches "
                        "one and follow the loaded playbook. Read-only, instant."),
        "input_schema": _obj({"name": _S}, ["name"]),
    },
    {
        "name": "say",
        "description": ("Speak a brief spoken CHECKPOINT to the user mid-task so they're never "
                        "left in silence (e.g. 'Opening Slack now, sir.', 'Screenshot taken — "
                        "sending it over.', 'Searching the web, sir.'). For any multi-step task, "
                        "call this at the START of each major step — batch it IN THE SAME TURN as "
                        "that step's action tool so it costs no extra round-trip. A few words, "
                        "voice-only (not the final answer). Don't overdo it on trivial one-step tasks."),
        "input_schema": _obj({"text": _S}, ["text"]),
    },
    {
        "name": "ask_user",
        "description": ("Ask the user a question and wait for their answer (voice or typed). "
                        "Use when you genuinely need info you cannot discover yourself."),
        "input_schema": _obj({"question": _S}, ["question"]),
    },
    {
        "name": "google",
        "description": (
            "Google Workspace via API (Gmail, Calendar, Docs, Sheets, Meet) — the "
            "PRIMARY, most reliable way to do Google tasks; prefer it over driving "
            "the web app. service + action:\n"
            "  gmail: search(query) | read(id) | send(to,subject,body,cc) | draft(to,subject,body)\n"
            "  calendar: list(days) | create(summary,start,end,attendees,description,meet)  "
            "(start/end ISO with offset e.g. 2026-07-09T15:00:00+05:30; meet=true adds a Meet link)\n"
            "  docs: create(title,text) | read(id) | append(id,text)\n"
            "  sheets: read(id,range) | write(id,range,values) | create(title)\n"
            "  meet: create()  → an instant standalone Meet link\n"
            "Gmail search uses Gmail query syntax (from:, subject:, is:unread, newer_than:2d). "
            "If this tool errors (API disabled, scope, failure), fall back to Chrome, then vision."),
        "input_schema": _obj({
            "service": {"type": "string",
                        "enum": ["gmail", "calendar", "docs", "sheets", "meet"]},
            "action": _S,
            "query": _S,
            "id": {**_S, "description": "message/document/spreadsheet/event id"},
            "to": _S, "cc": _S, "subject": _S, "body": _S,
            "summary": {**_S, "description": "calendar event title"},
            "start": _S, "end": _S, "attendees": {**_S, "description": "comma-separated emails"},
            "description": _S, "meet": _B,
            "title": _S, "text": _S,
            "range": {**_S, "description": "sheets A1 range, e.g. Sheet1!A1:C10"},
            "values": {"type": "array", "description": "sheets rows (list of lists) or one row"},
            "days": {**_I, "description": "calendar lookahead window (default 1)"},
            "limit": _I,
        }, ["service", "action"]),
    },
    {
        "name": "spawn_agents",
        "description": ("Fan out INDEPENDENT subtasks to parallel worker agents and get "
                        "their combined findings. Each worker is a full agent (own tool "
                        "loop: web search, files, Slack reads, read-only "
                        "bash/gh) but is ENFORCED read-only — any send/write/mutation is "
                        "refused, so workers research and report but never change anything. "
                        "Use for big parallelizable jobs: 'audit these repos for exposed "
                        "secrets', 'research X from several angles', 'check each of these "
                        "devices'. 2-6 tasks, each SELF-CONTAINED (workers don't see this "
                        "conversation — include names, paths, IDs). Afterwards, synthesize "
                        "the findings and take any outward actions yourself."),
        "input_schema": _obj({
            "tasks": {"type": "array", "items": _S,
                      "description": "One self-contained instruction per worker (2-6)."},
        }, ["tasks"]),
    },
    {
        "name": "propose_plan",
        "description": ("Before a multi-step chain of GATED actions (sends, writes, device "
                        "changes), present the WHOLE plan for ONE up-front approval instead "
                        "of confirming step by step. Pass the exact tool calls you will make. "
                        "Approved steps then run without banners when executed with EXACTLY "
                        "the same arguments; anything that differs still confirms, and "
                        "irreversible/destructive steps always re-confirm individually. Use "
                        "for playbooks (onboarding, offboarding, incident response) — never "
                        "for a single action."),
        "input_schema": _obj({
            "goal": {**_S, "description": "One line: what this plan accomplishes."},
            "steps": {"type": "array", "items": _obj({
                "tool": _S,
                "args": {"type": "object"},
            }, ["tool", "args"])},
        }, ["goal", "steps"]),
    },
    {
        "name": "save_skill",
        "description": ("Save a reusable skill/playbook into Cosmos's own instructions "
                        "(backend/skills/<name>.md, markdown) — it loads into every future "
                        "run. Use when the user asks to turn a repeated task into a "
                        "one-word command, or accepts a 'shall I save this as a skill?' "
                        "suggestion. Write a REAL playbook: trigger phrases, ordered steps, "
                        "exact tools/endpoints/arguments learned from the runs that worked. "
                        "name: kebab-case."),
        "input_schema": _obj({
            "name": {**_S, "description": "kebab-case skill name, e.g. onboard-laptop"},
            "content": {**_S, "description": "The markdown playbook."},
        }, ["name", "content"]),
    },
    {
        "name": "undo_last",
        "description": ("Undo Cosmos's LAST outward action: delete the Slack message just "
                        "sent, remove a reaction, restore the previous status or DND state. "
                        "action=preview lists recent reversible actions (free); action=apply "
                        "reverses the newest one. Only some things are reversible — "
                        "iMessages, emails, bash, and device commands are NOT, and the tool "
                        "says so. Use when the user says 'undo that' / 'delete that message'."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["preview", "apply"]},
        }, ["action"]),
    },
    {
        "name": "promises",
        "description": ("Track commitments YOU made in sent Slack/iMessages ('I'll approve "
                        "it today', 'will share by EOD'). action=list: open promises with "
                        "age + due hints. action=sweep: scan recent sent messages for new "
                        "commitments and auto-resolve fulfilled ones (also runs on a "
                        "schedule). action=resolve / dismiss: close one by its id."),
        "input_schema": _obj({
            "action": {"type": "string",
                       "enum": ["list", "sweep", "resolve", "dismiss"]},
            "id": {**_S, "description": "Promise id (resolve/dismiss)."},
        }, ["action"]),
    },
    {
        "name": "mcp",
        "description": ("Manage external MCP tool servers (configured in ~/.friday/mcp.json). "
                        "action=status: list connected servers, their tools, and any "
                        "connection failures. action=reload: reconnect all servers and "
                        "re-register their tools (after editing the config)."),
        "input_schema": _obj({
            "action": {"type": "string", "enum": ["status", "reload"]},
        }, ["action"]),
    },
]


# ─── Dynamic tool registry ─────────────────────────────────────────────────────
# Built-in tool names, frozen at import. The risk gate DEFAULT-DENIES anything
# that is neither here nor dynamically registered — an unknown tool must never
# run ungated (the gate used to fail open for names it didn't recognize).
_STATIC_TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

# name → {"gate", "timeout", "source", "label"} for runtime-registered tools
# (MCP server tools, synthesized macros). Consulted for gating, timeouts, and
# HUD labels.
_DYNAMIC_TOOLS: dict[str, dict] = {}


def register_tool(schema: dict, handler, *, gate: str = "confirm",
                  timeout: float | None = None, artifact: bool = False,
                  label: str | None = None, source: str = "dynamic") -> None:
    """Register a tool at runtime. `handler` is async (args, ctx) -> str and
    must follow the house convention: failures return "Error:"-prefixed strings.

    gate: "open"        → never confirms (read-only tools)
          "confirm"     → confirms in ask mode; ALWAYS blocked when unattended
          "destructive" → confirms in BOTH permission modes

    After a BATCH of register/unregister calls, run invalidate_tool_cache()
    exactly once — every invalidation busts the provider prompt cache, so
    never thrash it per-tool.
    """
    global _READ_ONLY_TOOLS
    name = (schema or {}).get("name") or ""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError(f"invalid tool name {name!r}")
    if name in _STATIC_TOOL_NAMES:
        raise ValueError(f"cannot shadow built-in tool '{name}'")
    if gate not in ("open", "confirm", "destructive"):
        raise ValueError(f"unknown gate {gate!r}")
    if name in _DYNAMIC_TOOLS:                 # re-register (reload) in place
        TOOLS[:] = [t for t in TOOLS if t.get("name") != name]
    TOOLS.append(schema)
    _HANDLERS[name] = handler
    _DYNAMIC_TOOLS[name] = {"gate": gate, "timeout": timeout,
                            "source": source, "label": label or name}
    if gate == "open":
        _READ_ONLY_TOOLS = frozenset(_READ_ONLY_TOOLS | {name})
    if artifact:
        _ARTIFACT_TOOLS.add(name)


def unregister_tools(source: str) -> int:
    """Remove every dynamic tool registered under `source`. Returns the count."""
    global _READ_ONLY_TOOLS
    dead = {n for n, m in _DYNAMIC_TOOLS.items() if m.get("source") == source}
    if not dead:
        return 0
    TOOLS[:] = [t for t in TOOLS if t.get("name") not in dead]
    for n in dead:
        _HANDLERS.pop(n, None)
        _DYNAMIC_TOOLS.pop(n, None)
        _ARTIFACT_TOOLS.discard(n)
    _READ_ONLY_TOOLS = frozenset(_READ_ONLY_TOOLS - dead)
    return len(dead)


def invalidate_tool_cache() -> None:
    """Next request rebuilds the tool array (and its cache breakpoint)."""
    global _tools_cached
    _tools_cached = None


def _is_destructive(tool: str, args: dict) -> bool:
    """Irreversibility predicate covering BOTH built-in pattern matching and
    dynamic gate metadata. Destructive calls can never be batched into one
    banner or pre-approved by propose_plan — they confirm individually, always.
    (_destructive_label alone returns None for every dynamic tool, which let
    gate=\"destructive\" MCP tools slip through both mechanisms.)"""
    meta = _DYNAMIC_TOOLS.get(tool)
    if meta is not None:
        return meta.get("gate") == "destructive"
    return _destructive_label(tool, args) is not None


def _dynamic_gate_label(tool: str, args: dict, mode: str, unattended: bool) -> str | None:
    """Risk-gate verdict for a dynamically registered tool."""
    meta = _DYNAMIC_TOOLS.get(tool) or {}
    gate = meta.get("gate", "confirm")
    if gate == "open":
        return None
    src = meta.get("source", "plugin")
    if gate == "destructive":
        return f"Destructive external action ({src}): {_label(tool, args)}"
    if unattended:
        # Headless scheduled runs auto-decline confirms — a non-read-only
        # external tool must never fire with nobody watching, even in full mode.
        return f"External action ({src}) in an unattended run: {_label(tool, args)}"
    if mode == "full":
        return None       # parity with Slack sends / gh mutations in full mode
    try:
        preview = json.dumps(args, ensure_ascii=False, default=str)[:150]
    except Exception:
        preview = ""
    return f"External action ({src}): {_label(tool, args)} {preview}"


# ─── Risk gate ─────────────────────────────────────────────────────────────────

def _normalize_risk_target(s: str) -> str:
    """Collapse whitespace and strip quoting/escapes before pattern matching, so
    trivial evasions ("rm\\t-rf", '"rm" -rf', 'r\\m -rf') can't dodge the gate."""
    s = re.sub(r"\s+", " ", s or "")
    return s.replace('"', "").replace("'", "").replace("\\", "")


# Word-boundary patterns: no substring false positives ("perform"≠"rm ",
# "information"≠"format", "--format" is fine) and coverage for the common
# bypasses (dd, shred, python shutil.rmtree, running an arbitrary .sh file).
# The whole normalized command string is scanned, so a risky token hidden
# inside $(...) / backticks is still caught — substitution is not a bypass.
_RISKY_SHELL_RE = re.compile(
    r"\brm\b|\brmdir\b|\bunlink\b|\bsrm\b|\bshred\b|\bsudo\b|\bshutdown\b|"
    r"\breboot\b|\bhalt\b|\bkillall\b|\bpkill\b|\bkill\b|\bdiskutil\b|"
    r"\bmkfs\w*|\bnewfs\w*|\bdd\b|\btruncate\b|"
    r"\bgit\s+push\b|\bgit\s+reset\s+--hard\b|\bgit\s+clean\b|"
    r"\bbrew\s+(?:install|uninstall|remove)\b|"
    r"\bnpm\s+(?:install|i|uninstall|rm)\s+(?:-g\b|--global\b)|"
    r"\bpip3?\s+(?:install|uninstall)\b|"
    r">\s*/dev/|\bchmod\s+777\b|\bchmod\s+-R\b|\bchown\s+-R\b|"
    r"\bshutil\.rmtree\b|\bos\.(?:remove|rmdir|unlink|removedirs)\b|\bsend2trash\b|"
    r"\bempty\s+trash\b|\blaunchctl\s+(?:unload|remove|bootout)\b|"
    r"\b(?:bash|sh|zsh)\s+(?:-\w+\s+)*\S+\.(?:sh|bash|zsh|command)\b|"
    # ── Remote-code-exec / obfuscation bypasses (reviewer-flagged) ──
    r"\|\s*(?:bash|sh|zsh|python3?|perl|ruby|node)\b|"   # curl … | sh, echo … | python
    r"\bbase64\b\s*(?:-d|-D|--decode)\b|"                # decode obfuscated payload
    r"\beval\b|"                                          # arbitrary eval
    r"\bcrontab\b|"                                       # scheduled persistence
    r"\bdefaults\s+delete\b|"                             # nuke app preferences
    r"\bosascript\b|"                                     # arbitrary GUI/AppleScript from shell
    r"\bnc\b\s+-\w*e|\bncat\b\s+-\w*e|\b(?:bash|sh|zsh)\s+-i\b|"  # reverse shells
    r">>?\s*~?[\w./]*(?:zshrc|bashrc|bash_profile|zprofile|\.profile)\b",  # shell-profile persistence
    re.IGNORECASE,
)

# AppleScript-specific destructive verbs (scripts are ALSO scanned with the
# shell patterns above — "do shell script \"rm ...\"" is caught there).
_RISKY_APPLESCRIPT_RE = re.compile(r"\bdelete\b|\bempty\s+trash\b", re.IGNORECASE)

# DELETION / irreversible-destruction subset. In "full" permission mode ONLY
# these still confirm — everything else outward (Slack, git push, installs,
# github mutations, file overwrites) runs freely. Overwrites are excluded
# because write_file snapshots the old version to ~/.friday/undo/ (recoverable).
_DESTRUCTIVE_RE = re.compile(
    r"\brm\b|\brmdir\b|\bunlink\b|\bsrm\b|\bshred\b|"
    r"\bmkfs\w*|\bnewfs\w*|\bdd\b|"
    r"\bdiskutil\s+(?:erase\w*|reformat|zero\w*|secure\w*|partition\w*)\b|"
    r"\bgit\s+clean\b|\bgit\s+reset\s+--hard\b|"
    r"\bshutil\.rmtree\b|\bos\.(?:remove|rmdir|unlink|removedirs)\b|\bsend2trash\b|"
    r"\bempty\s+trash\b|\bdefaults\s+delete\b|"
    r"\blaunchctl\s+(?:unload|remove|bootout)\b",
    re.IGNORECASE,
)


def _destructive_label(tool: str, args: dict) -> str | None:
    """Danger label if this call irreversibly deletes/destroys data, else None."""
    if tool in ("bash", "applescript", "type_text"):
        target = _normalize_risk_target(
            args.get("command") or args.get("script") or args.get("text") or "")
        m = _DESTRUCTIVE_RE.search(target)
        if m:
            return f"Deletes/destroys data: contains '{m.group(0).strip()}'"
        if tool == "applescript":
            m = _RISKY_APPLESCRIPT_RE.search(target)
            if m:
                return f"Destructive AppleScript: contains '{m.group(0).strip()}'"
    if tool == "github":
        gh = _normalize_risk_target(args.get("args", ""))
        if re.search(r"\b(delete|rm)\b", gh, re.IGNORECASE):
            return f"GitHub deletion via gh: {args.get('args', '')[:200]}"
    if tool == "write_file":
        # Self-sabotage and sensitive-path writes confirm even in full mode.
        return _write_file_label(args)
    if tool == "save_skill":
        # Self-modification: a skill file becomes part of EVERY future system
        # prompt — the user reviews it in both permission modes.
        return (f"Writes into COSMOS's own instructions: "
                f"skills/{args.get('name', '?')}.md")
    if tool == "system_toggle" and (args.get("feature") or "").lower() == "empty_trash":
        return "Empties the Trash — files become unrecoverable"
    return None


# Sensitive write targets — writes here can hijack the shell, persist a daemon,
# or leak credentials, so write_file to them confirms in BOTH permission modes,
# even for brand-new files (a fresh LaunchAgents plist is exactly the attack).
_SENSITIVE_PATH_RE = re.compile(
    r"(?:^|/)\.(?:zshrc|bashrc|bash_profile|zprofile|zshenv|profile)$|"
    r"/Library/Launch(?:Agents|Daemons)/|"
    r"(?:^|/)\.ssh(?:/|$)|(?:^|/)\.aws(?:/|$)|(?:^|/)\.gnupg(?:/|$)|"
    r"(?:^|/)\.(?:env|netrc|npmrc|pypirc)(?:\.|$)|"
    r"\.plist$|(?:^|/private)/etc/|/sudoers",
    re.IGNORECASE,
)


def _write_file_label(args: dict) -> str | None:
    """Danger label for write_file targets that must confirm in BOTH modes:
    COSMOS's own project (self-sabotage) and sensitive system paths. Files in
    the scratch workspace are always free."""
    raw = os.path.expanduser(args.get("path", ""))
    p = Path(raw)
    try:
        rp = p.resolve()
    except Exception:
        rp = p
    try:
        if rp.is_relative_to(system_control.WORK_DIR.resolve()):
            return None
    except Exception:
        pass
    try:
        if rp.is_relative_to(FRIDAY_ROOT):
            return f"Writes into COSMOS's own project directory: {rp}"
    except Exception:
        pass
    if _SENSITIVE_PATH_RE.search(raw) or _SENSITIVE_PATH_RE.search(str(rp)):
        return f"Writes to a sensitive system path: {rp}"
    return None


# Browser JS that mutates the page/session or moves data — clicks, form
# submits, network requests carrying the user's cookies, cookie/storage
# access, navigation, DOM injection. Pure DOM reads (innerText extraction,
# querySelector + textContent) stay free: reading is the tool's main job.
_MUTATING_JS_RE = re.compile(
    r"\.click\s*\(|\.submit\s*\(|\bfetch\s*\(|XMLHttpRequest|navigator\.sendBeacon|"
    r"document\.cookie|"
    r"(?:localStorage|sessionStorage)\s*\.\s*(?:setItem|removeItem|clear)|"
    r"\blocation\s*=(?!=)|location\.(?:href\s*=(?!=)|assign\s*\(|replace\s*\()|"
    r"\.value\s*=(?!=)|\.innerHTML\s*=(?!=)|\.outerHTML\s*=(?!=)|"
    r"dispatchEvent\s*\(|createElement\s*\(|\bWebSocket\s*\(",
    re.IGNORECASE,
)


# Move/delete/rename verbs — a self-referential one of these is a hard block.
_MOVE_DELETE_RE = re.compile(
    r"\b(mv|rm|rmdir|trash|shred|srm|unlink|ditto)\b|\brsync\b.*--remove|"
    r"\bshutil\.(?:move|rmtree)\b|\bos\.(?:rename|replace|remove|removedirs)\b",
    re.IGNORECASE,
)


def _self_protection(tool: str, args: dict) -> str | None:
    """HARD BLOCK (mode-independent): refuse any command that would move, rename,
    or delete COSMOS's own project directory. Cosmos sweeping itself into an
    'Organized/' folder is exactly what broke both servers once."""
    if tool not in ("bash", "applescript", "type_text"):
        return None
    text = _normalize_risk_target(
        args.get("command") or args.get("script") or args.get("text") or "")
    if not _MOVE_DELETE_RE.search(text):
        return None
    root_abs  = str(FRIDAY_ROOT)
    home      = str(Path.home())
    refs = {root_abs,
            root_abs.replace(home, "~", 1),
            root_abs.replace(home, "$HOME", 1),
            root_abs.replace(home, "${HOME}", 1)}
    low = text.lower()
    # Match the path as a WHOLE component — so "…/friday-backup" or "…/fridayx"
    # (a different folder) are NOT caught, only "…/friday", "…/friday/…", etc.
    for ref in refs:
        if re.search(re.escape(ref.lower()) + r"(?![\w.\-])", low):
            return (f"self-protection: this would move/delete COSMOS's own project "
                    f"directory ({FRIDAY_ROOT}). Refused.")
    return None


def needs_confirmation(tool: str, args: dict, mode: str = "ask",
                       unattended: bool = False) -> str | None:
    """Return a human danger label when this call must be confirmed, else None.

    mode="ask"  → full guardrails: every destructive OR outward action confirms.
    mode="full" → trust everything EXCEPT irreversible deletion (which still
                  confirms — you can't undo an `rm`).
    unattended  → headless scheduled run: dynamic non-read-only tools always
                  gate (and the headless interaction auto-declines them).
    """
    if tool in _DYNAMIC_TOOLS:
        return _dynamic_gate_label(tool, args, mode, unattended)
    if tool not in _STATIC_TOOL_NAMES:
        # DEFAULT-DENY: a tool that is neither built-in nor registered must
        # never run ungated (this gate previously failed open on unknowns).
        return f"Unregistered tool '{tool}' — refusing to run it unreviewed"
    if mode == "full":
        return _destructive_label(tool, args)

    if tool in ("bash", "applescript"):
        target = _normalize_risk_target(args.get("command") or args.get("script") or "")
        m = _RISKY_SHELL_RE.search(target)
        if m:
            return f"Destructive or outward command: contains '{m.group(0).strip()}'"
        if tool == "applescript":
            m = _RISKY_APPLESCRIPT_RE.search(target)
            if m:
                return f"Destructive AppleScript: contains '{m.group(0).strip()}'"
    if tool == "type_text":
        # Typing "rm -rf ..." into an open Terminal is as destructive as bash.
        m = _RISKY_SHELL_RE.search(_normalize_risk_target(args.get("text", "")))
        if m:
            return f"Types a potentially destructive command: contains '{m.group(0).strip()}'"
    if tool == "slack_dm":
        return (f'Sends a Slack message to "{args.get("recipient", "?")}": '
                f'"{args.get("message", "")}"')
    if tool == "slack":
        action = (args.get("action") or "").lower()
        if action == "send":
            return (f'Sends a Slack message to "{args.get("target", "?")}": '
                    f'"{args.get("text", "")}"')
        if action == "react":
            return f'Reacts :{(args.get("emoji") or "").strip(":")}: in "{args.get("target", "?")}"'
        if action == "status":
            return f'Sets your Slack status to "{args.get("text", "")}"'
        if action == "dnd":
            m = int(args.get("minutes") or 0)
            if m > 0:
                return f"Turns Slack Do-Not-Disturb on for {m} min"
            if m < 0:
                return "Turns Slack Do-Not-Disturb off"
        # reads (unreads/read/search/whoami/dnd-read) run free
    if tool == "slack_photo":
        return (f'Sends an image to "{args.get("recipient", "?")}" on Slack: '
                f'{args.get("image_path", "")}')
    if tool == "imessage":
        return (f'Sends an iMessage to "{args.get("recipient", "?")}": '
                f'"{args.get("message", "")}"')
    if tool == "github":
        gh = _normalize_risk_target(args.get("args", ""))
        # gh mutations (and any smuggled shell metacharacters) need confirmation;
        # read-only gh (list/view/status/api GET) runs freely.
        if re.search(r"\b(create|delete|merge|close|reopen|edit|rename|transfer|"
                     r"add|remove|set|push|sync|fork|clone)\b", gh, re.IGNORECASE) \
           or re.search(r"[;&|`$]|(?:-X|--method)\s+(?:POST|PUT|PATCH|DELETE)\b",
                        gh, re.IGNORECASE) \
           or _RISKY_SHELL_RE.search(gh):
            return f"GitHub mutation via gh: {args.get('args', '')[:200]}"
    if tool == "google":
        # Reads (search/read/list) run free; anything outward confirms.
        act = (args.get("action") or "").lower()
        if act in ("search", "read", "list"):
            return None
        return _google_confirm_label(args)
    if tool == "shortcut" and (args.get("action") or "").lower() == "run":
        # A Shortcut can do anything the user built it to do — confirm in
        # guarded mode (list is free).
        return f"Runs the macOS Shortcut \"{args.get('name', '?')}\""
    if tool == "schedule_task":
        # Future autonomous action — the user should see when + what.
        sched = args.get("when") or f"cron {args.get('cron', '?')}"
        return f"Schedules an unattended task ({sched}): {args.get('prompt', '')[:150]}"
    if tool == "undo_last" and (args.get("action") or "preview") == "apply":
        # Reversal is itself outward (deletes a sent message) — show it.
        return "Reverses the last outward action (deletes the sent message / restores previous state)"
    if tool == "system_toggle" and (args.get("feature") or "").lower() == "empty_trash":
        return "Empties the Trash — files become unrecoverable"
    if tool == "browser_js":
        # JS runs with the user's logged-in sessions (Gmail, GitHub, admin
        # portals) — anything that clicks/submits/fetches/navigates or touches
        # cookies must confirm. Pure reads stay free.
        m = _MUTATING_JS_RE.search(args.get("javascript", ""))
        if m:
            return (f"Runs page-mutating JavaScript in your browser: "
                    f"contains '{m.group(0).strip()}'")
    if tool == "save_skill":
        return (f"Writes into COSMOS's own instructions: "
                f"skills/{args.get('name', '?')}.md")
    if tool == "write_file":
        danger = _write_file_label(args)
        if danger:
            return danger
        p = Path(os.path.expanduser(args.get("path", "")))
        try:
            in_workspace = p.resolve().is_relative_to(system_control.WORK_DIR.resolve())
        except Exception:
            in_workspace = False
        if p.exists() and not in_workspace:
            return f"Overwrites existing file: {p}"
    return None


def _google_confirm_label(args: dict) -> str:
    """Human danger label for an outward Google Workspace action."""
    svc = (args.get("service") or "").lower()
    act = (args.get("action") or "").lower()
    if svc == "gmail" and act == "send":
        return (f'Sends an email to "{args.get("to", "?")}"'
                + (f' cc {args["cc"]}' if args.get("cc") else "")
                + f': "{args.get("subject", "")}"')
    if svc == "gmail" and act == "draft":
        return f'Saves a Gmail draft to "{args.get("to", "?")}": "{args.get("subject", "")}"'
    if svc == "calendar" and act == "create":
        who = f' with {args["attendees"]}' if args.get("attendees") else ""
        meet = " + Meet link" if args.get("meet") else ""
        return (f'Creates a calendar event "{args.get("summary", "")}" at '
                f'{args.get("start", "?")}{who}{meet}')
    if svc == "docs":
        return f'Google Docs {act}: "{args.get("title") or args.get("id", "")}"'
    if svc == "sheets":
        return f'Google Sheets {act}: {args.get("title") or args.get("range") or args.get("id", "")}'
    if svc == "meet":
        return "Creates a new Google Meet space"
    return f"Google {svc} {act}"


def _shown(text: str, cap: int) -> str:
    """Truncate for the confirm card WITHOUT hiding that truncation happened —
    the user must never approve a command whose dangerous tail is invisible."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]} …[+{len(text) - cap} more chars hidden — full command is longer]"


def _confirm_summary(tool: str, args: dict) -> str:
    if tool == "slack_dm":
        return (f'Slack DM to "{args.get("recipient", "?")}": "{args.get("message", "")}"')
    if tool == "slack":
        a = (args.get("action") or "").lower()
        if a == "send":
            return f'Slack message to "{args.get("target", "?")}": "{args.get("text", "")}"'
        if a == "status":
            return f'Set Slack status: "{args.get("text", "")}"'
        if a == "react":
            return f'React :{(args.get("emoji") or "").strip(":")}: in "{args.get("target", "?")}"'
        if a == "dnd":
            m = int(args.get("minutes") or 0)
            return "Slack DND " + (f"on {m}m" if m > 0 else "off")  # only reached when gated (m != 0)
    if tool == "imessage":
        return (f'iMessage to "{args.get("recipient", "?")}": "{args.get("message", "")}"')
    if tool == "slack_photo":
        cap = args.get("caption", "")
        return (f'Send photo to "{args.get("recipient", "?")}" on Slack: '
                f'{os.path.basename(args.get("image_path", ""))}'
                + (f' — "{cap}"' if cap else ""))
    if tool == "bash":
        return f"Run: {_shown(args.get('command', ''), 600)}"
    if tool == "applescript":
        desc = args.get("description") or "AppleScript"
        return f"{desc} — {_shown(args.get('script', ''), 400)}"
    if tool == "type_text":
        return f"Type: {_shown(args.get('text', ''), 400)}"
    if tool == "browser_js":
        return f"Run browser JavaScript: {_shown(args.get('javascript', ''), 400)}"
    if tool == "write_file":
        return f"Write {args.get('path', '')}"
    if tool == "github":
        return f"Run: gh {_shown(args.get('args', ''), 300)}"
    if tool == "google":
        base = _google_confirm_label(args)
        if (args.get("service") or "").lower() == "gmail" and args.get("body"):
            base += f'\n\n{_shown(args.get("body", ""), 1000)}'
        return base
    return _label(tool, args)


# ─── Tool implementations ──────────────────────────────────────────────────────

async def _tool_set_todos(args: dict, ctx: RunContext) -> str:
    todos = [
        {"id": str(t.get("id", i)), "text": str(t.get("text", "")),
         "status": t.get("status", "pending")}
        for i, t in enumerate(args.get("todos", []))
    ]
    ctx.todos = todos
    await ctx.emit({"type": "todos", "todos": todos})
    done = sum(1 for t in todos if t["status"] == "completed")
    return f"Todo list updated ({done}/{len(todos)} completed)."


async def _tool_bash(args: dict, ctx: RunContext) -> str:
    command = args["command"]
    timeout = max(1, min(int(args.get("timeout_s") or 60), 300))
    if args.get("background"):
        # Wrap in `bash -c <quoted>` so compound commands ("a; b") are detached
        # as a WHOLE — naive `nohup {command} &` leaves the part before ';' in
        # the foreground and hangs the run forever.
        wrapped = f"nohup bash -c {shlex.quote(command)} >/dev/null 2>&1 & echo \"pid $!\""
        try:
            ok, out = await system_control.run_shell(wrapped, timeout=15)
        except asyncio.TimeoutError:
            return "Error: failed to launch background command within 15s"
        return f"Started in background ({out})." if ok else f"Error: {out or 'failed to start'}"
    try:
        # run_shell kills the whole process group on timeout — no orphan
        # processes keep mutating state after we report "timed out".
        ok, out = await system_control.run_shell(command, timeout=timeout, max_chars=4000)
    except asyncio.TimeoutError:
        return f"Error: command timed out after {timeout}s and was killed"
    out = (out or "").strip()
    if ok:
        return out or "(no output, exit 0)"
    return f"Error (non-zero exit): {out or 'no output'}"


async def _tool_applescript(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.universal_action(
        args.get("description", "AppleScript action"), args["script"])
    return msg if ok else f"Error: {msg}"


async def _tool_open_app(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.open_app(args["name"])
    if not ok and msg.startswith("__NOT_INSTALLED__"):
        parts = msg.split(":", 2)
        app_name = parts[1] if len(parts) > 1 else args["name"]
        cask = parts[2] if len(parts) > 2 else ""
        hint = f" Homebrew cask: `brew install --cask {cask}`." if cask else ""
        return f"Error: {app_name} is not installed.{hint}"
    return msg if ok else f"Error: {msg}"


async def _tool_open_url(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.open_url(args["url"], args.get("browser") or "default")
    return msg if ok else f"Error: {msg}"


async def _tool_open_path(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.open_path(args["path"], args.get("app"))
    return msg if ok else f"Error: {msg}"


async def _tool_read_file(args: dict, ctx: RunContext) -> str:
    # Binary documents must never be cat'd raw (a PDF used to crash the strict
    # decode) — route them through the extractor automatically.
    ext = Path(os.path.expanduser(args.get("path", ""))).suffix.lower()
    if ext in documents.BINARY_DOC_EXTS:
        return await _tool_read_document(args, ctx)
    ok, content = await system_control.read_file(args["path"], int(args.get("max_chars") or 8000))
    return content if ok else f"Error: {content}"


async def _tool_read_document(args: dict, ctx: RunContext) -> str:
    ok, text = await documents.extract_text(args["path"], int(args.get("max_chars") or 8000))
    return text if ok else f"Error: {text}"


async def _tool_write_file(args: dict, ctx: RunContext) -> str:
    path = Path(os.path.expanduser(args["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    # Snapshot an existing file before clobbering it — an accidental overwrite
    # is then recoverable from ~/.friday/undo/.
    backup = audit.snapshot(path)
    path.write_text(content, encoding="utf-8")
    restore = f" (previous version backed up to {backup})" if backup else ""
    if args.get("open_in"):
        await system_control.open_path(str(path), args["open_in"])
        return f"Wrote {len(content)} chars to {path} and opened in {args['open_in']}{restore}"
    return f"Wrote {len(content)} chars to {path}{restore}"


async def _tool_web_search(args: dict, ctx: RunContext) -> str:
    results = await web_svc.ddg_search(args["query"], int(args.get("max_results") or 5))
    if not results:
        return "No results found. Try a different phrasing."
    return "\n".join(
        f"{i + 1}. {r['title']} — {r['url']} — {r['snippet']}"
        for i, r in enumerate(results)
    )


async def _tool_fetch_url(args: dict, ctx: RunContext) -> str:
    # Clamp the model-controlled cap — an unbounded tool_result can blow the
    # entire context window in one call.
    max_chars = max(500, min(int(args.get("max_chars") or 6000), 30_000))
    text = await web_svc.fetch_page_text(args["url"], max_chars)
    return text if text else "Error: could not fetch that URL."


async def _tool_see_screen(args: dict, ctx: RunContext) -> str:
    # on_fallback keeps events flowing during a slow vision chain — otherwise
    # a multi-model stall is silent long enough (>60s) to trip the frontend's
    # watchdog, which cancels the whole run.
    async def _on_fb(failed: str, nxt: str, exc: Exception) -> None:
        await ctx.emit({"type": "agent_thought",
                        "text": f"vision model {failed} unavailable — trying {nxt}"})
    return await vision_svc.analyze_screen(args["question"], FAST_MODEL,
                                           on_fallback=_on_fb)


async def _tool_read_browser(args: dict, ctx: RunContext) -> str:
    ok, text = await system_control.read_browser_page(args.get("app") or "Google Chrome")
    return text if ok else f"Error: {text}"


async def _tool_browser_js(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.run_browser_js(
        args["javascript"], args.get("app") or "Google Chrome")
    return out if ok else f"Error: {out}"


async def _tool_read_app(args: dict, ctx: RunContext) -> str:
    ok, text = await system_control.read_app_text(args["app"])
    return text if ok else f"Error: {text}"


async def _tool_type_text(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.type_text(args["text"], args.get("app"))
    return msg if ok else f"Error: {msg}"


async def _tool_keystroke(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.send_keystroke(args["keys"], args.get("app"))
    return msg if ok else f"Error: {msg}"


async def _tool_click_ui(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.click_ui_element(
        args["app"], args["name"], args.get("type") or "button")
    return msg if ok else f"Error: {msg}"


async def _tool_mouse(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.mouse_control(
        args["action"], int(args.get("x") or 0), int(args.get("y") or 0))
    return msg if ok else f"Error: {msg}"


async def _tool_click_web(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.browser_click_text(
        args["text"], args.get("browser") or "Google Chrome")
    return msg if ok else f"Error: {msg}"


async def _tool_choose_file(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.choose_file_in_dialog(args["path"])
    return msg if ok else f"Error: {msg}"


async def _tool_gmail_attach(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.gmail_attach(args["path"])
    return msg if ok else f"Error: {msg}"


async def _tool_slack(args: dict, ctx: RunContext) -> str:
    if not slack_svc.AVAILABLE:
        return ("Error: SLACK_USER_TOKEN isn't configured — add it to backend/.env "
                "and restart.")
    action = (args.get("action") or "").lower()
    limit = int(args.get("limit") or 15)
    mins = args.get("minutes")
    if action == "unreads":
        ok, out = await slack_svc.unreads()
    elif action == "read":
        ok, out = await slack_svc.read_conversation(args.get("target", ""), limit)
    elif action == "mentions":
        _all = bool(args.get("all"))
        ok, out = await slack_svc.list_mentions(args.get("target", ""), limit or 30,
                                                include_answered=_all, only_directed=not _all)
    elif action == "send":
        ok, out = await slack_svc.send_message(args.get("target", ""), args.get("text", ""),
                                               args.get("thread", ""))
    elif action == "react":
        ok, out = await slack_svc.add_reaction(args.get("target", ""), args.get("emoji", ""))
    elif action == "status":
        ok, out = await slack_svc.set_status(args.get("text", ""), args.get("emoji", ""),
                                             int(mins or 0))
    elif action == "dnd":
        ok, out = (await slack_svc.get_dnd() if mins is None
                   else await slack_svc.set_dnd(int(mins)))
    elif action == "search":
        ok, out = await slack_svc.search_messages(args.get("query", ""), limit)
    elif action == "whoami":
        ok, out = await slack_svc.whoami()
    else:
        return f"Error: unknown slack action '{action}'."
    return out if ok else f"Error: {out}"


async def _tool_slack_status(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.set_slack_status(
        args["text"], args.get("emoji", ""), int(args.get("minutes") or 0))
    return msg if ok else f"Error: {msg}"


async def _tool_slack_dm(args: dict, ctx: RunContext) -> str:
    # Web API when the user token is configured: ~200ms, journaled/undoable
    # via the outbox, and no Cmd+K focus steal (~3-4.5s of GUI driving).
    # Errors (ambiguous name, unresolvable) go back to the model rather than
    # blind-typing into the Slack UI. GUI automation remains for token-less
    # setups only.
    if slack_svc.AVAILABLE:
        ok, msg = await slack_svc.send_message(args["recipient"], args["message"])
        return msg if ok else f"Error: {msg}"
    ok, msg = await system_control.slack_message(args["recipient"], args["message"])
    return msg if ok else f"Error: {msg}"


async def _tool_github(args: dict, ctx: RunContext) -> str:
    # Strip a leading "gh " the model sometimes includes, then run the CLI.
    gh_args = re.sub(r"^\s*gh\s+", "", args.get("args", "").strip())
    if not gh_args:
        return "Error: no gh arguments provided."
    try:
        ok, out = await system_control.run_shell(f"gh {gh_args}", timeout=45, max_chars=8000)
    except asyncio.TimeoutError:
        return "Error: gh command timed out after 45s"
    out = (out or "").strip()
    if ok:
        return out or "(gh: no output, success)"
    if "gh auth login" in out or "not logged" in out.lower():
        return f"Error: GitHub CLI isn't authenticated — run `gh auth login`. {out}"
    return f"Error (gh): {out or 'command failed'}"


async def _tool_take_photo(args: dict, ctx: RunContext) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.expanduser(args.get("path") or f"~/Desktop/friday-photo-{ts}.jpg")
    ok, result = await system_control.capture_photo(path)
    return f"Photo captured to {result}" if ok else f"Error: {result}"


async def _tool_record_video(args: dict, ctx: RunContext) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.expanduser(args.get("path") or f"~/Desktop/friday-video-{ts}.mp4")
    duration = int(args.get("duration") or 5)
    ok, result = await system_control.record_video(path, duration)
    return f"Recorded {duration}s video to {result}" if ok else f"Error: {result}"


async def _tool_slack_photo(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.slack_send_image(
        args["recipient"], args["image_path"], args.get("caption", ""))
    return msg if ok else f"Error: {msg}"


async def _tool_screenshot(args: dict, ctx: RunContext) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.expanduser(args.get("path") or f"~/Desktop/Screenshot_{ts}.png")
    ok, saved = await system_control.take_screenshot(path)
    if not ok:
        return "Error: screenshot failed — grant Screen Recording permission to Terminal."
    await system_control.run_shell(f"open {shlex.quote(saved)}")
    return f"Screenshot saved to {saved} and opened in Preview."


async def _tool_system_state(args: dict, ctx: RunContext) -> str:
    state = await system_control.get_system_state()
    return json.dumps(state)


async def _tool_set_volume(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.set_volume(int(args["level"]))
    return msg if ok else f"Error: {msg}"


async def _tool_music(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.media_control(
        args.get("action", ""), args.get("player"),
        int(args["level"]) if args.get("level") is not None else None)
    return msg if ok else f"Error: {msg}"


async def _tool_calendar(args: dict, ctx: RunContext) -> str:
    if args.get("action") == "create":
        if not args.get("title") or not args.get("start"):
            return "Error: calendar create needs title and start (ISO)."
        ok, out = await system_control.calendar_create(
            args["title"], args["start"], int(args.get("duration_min") or 30),
            args.get("calendar_name", ""), args.get("location", ""),
            args.get("notes", ""))
    else:
        ok, out = await system_control.calendar_events(args.get("scope", "today"))
    return out if ok else f"Error: {out}"


async def _tool_reminders(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.reminders(
        args.get("action", ""), args.get("name", ""), args.get("due", ""),
        args.get("list_name", ""))
    return out if ok else f"Error: {out}"


async def _tool_notes(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.notes(
        args.get("action", ""), args.get("title", ""), args.get("body", ""),
        args.get("query", ""))
    return out if ok else f"Error: {out}"


async def _tool_comms_summary(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.comms_summary()
    return out if ok else f"Error: {out}"


async def _tool_contacts(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.contacts_lookup(args.get("name", ""))
    return out if ok else f"Error: {out}"


async def _tool_imessage(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.send_imessage(args["recipient"], args["message"])
    return out if ok else f"Error: {out}"


async def _tool_clipboard(args: dict, ctx: RunContext) -> str:
    if args.get("action") == "write":
        ok, msg = await system_control.set_clipboard(args.get("text", ""))
    else:
        ok, msg = await system_control.get_clipboard()
    return msg if ok else f"Error: {msg}"


async def _tool_find_files(args: dict, ctx: RunContext) -> str:
    ok, out = await system_control.find_files(
        args.get("name", ""), args.get("content", ""), args.get("kind", ""),
        int(args.get("within_days") or 0), args.get("onlyin", ""),
        int(args.get("limit") or 20))
    return out if ok else f"Error: {out}"


async def _tool_shortcut(args: dict, ctx: RunContext) -> str:
    if args.get("action") == "run":
        if not args.get("name"):
            return "Error: shortcut run needs a name — use action=list first."
        ok, out = await system_control.run_shortcut(args["name"], args.get("input", ""))
    else:
        ok, out = await system_control.list_shortcuts()
    return out if ok else f"Error: {out}"


async def _tool_notify(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.notify(args.get("title", "COSMOS"),
                                          args.get("message", ""))
    return msg if ok else f"Error: {msg}"


async def _tool_system_toggle(args: dict, ctx: RunContext) -> str:
    ok, msg = await system_control.system_toggle(args.get("feature", ""),
                                                 args.get("state", "toggle"))
    return msg if ok else f"Error: {msg}"


async def _tool_schedule_task(args: dict, ctx: RunContext) -> str:
    from services import scheduler   # lazy: scheduler imports agent
    return scheduler.add_job(args.get("prompt", ""), args.get("when", ""),
                             args.get("cron", ""))


async def _tool_list_scheduled(args: dict, ctx: RunContext) -> str:
    from services import scheduler
    return scheduler.list_jobs()


async def _tool_cancel_scheduled(args: dict, ctx: RunContext) -> str:
    from services import scheduler
    return scheduler.cancel_job(args.get("id", ""))


async def _tool_remember_fact(args: dict, ctx: RunContext) -> str:
    out = memory_svc.remember(args.get("kind", ""), args.get("key", ""),
                              args.get("value", ""))
    return out


async def _tool_forget_fact(args: dict, ctx: RunContext) -> str:
    return memory_svc.forget(args.get("key", ""))


async def _tool_recall_history(args: dict, ctx: RunContext) -> str:
    query = args.get("query", "")
    # Two abstraction levels: RAPTOR themes (summaries across many past runs) for
    # the big-picture question, plus pinpoint individual runs. Both cite into the
    # context ledger so the answer's grounding is inspectable.
    # Embed the query ONCE and hand the vector to both searches (they used to
    # each pay a gateway embed round-trip, serially).
    qvec = None
    try:
        res = await embeddings_svc.aembed([query[:500]])
        if res:
            qvec = (res[0], res[1][0])
    except Exception:
        pass
    themes, hits = await asyncio.gather(
        raptor.search(query, limit=3, qvec=qvec),
        recall_svc.search(query, int(args.get("days_back") or 90), qvec=qvec))
    for t in themes:
        m = re.match(r"LEVEL (\d+)", t)
        ctx.ledger.cite("raptor", ledger_svc.ref_for(t, "raptor"),
                        summary=t[:140], version=(m.group(1) if m else ""))
    for h in hits:
        ctx.ledger.cite("recall", ledger_svc.ref_for(h, "recall"), summary=h[:140])
    if not themes and not hits:
        return "No matching past tasks found."
    parts = []
    if themes:
        parts.append("THEMES (summarised across many past runs):\n" + "\n".join(themes))
    if hits:
        parts.append("SPECIFIC PAST RUNS:\n" + "\n".join(hits))
    return "\n\n".join(parts)


async def _tool_read_skill(args: dict, ctx: RunContext) -> str:
    name = re.sub(r"[^a-z0-9\-]", "", (args.get("name") or "").lower().strip())
    skills_dir = Path(__file__).parent.parent / "skills"
    path = skills_dir / f"{name}.md"
    if not name or not path.exists():
        avail = ", ".join(sorted(f.stem for f in skills_dir.glob("*.md"))) or "(none)"
        return f"Error: no skill named '{name or '?'}'. Available: {avail}"
    try:
        return path.read_text()[:20000]
    except Exception as e:
        return f"Error reading skill: {e}"


async def _tool_say(args: dict, ctx: RunContext) -> str:
    await ctx.emit({"type": "speak", "text": args["text"]})
    return "Spoken to the user."


async def _tool_ask_user(args: dict, ctx: RunContext) -> str:
    qid = uuid4().hex[:8]
    event = {"type": "ask_user", "id": qid, "question": args["question"]}
    fut = ctx.interaction.begin("ask", payload=event)
    await ctx.emit(event)
    try:
        answer = await asyncio.wait_for(fut, timeout=ASK_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        ctx.interaction.cancel()
        # Close the question banner — unlike _confirm, this path emitted
        # nothing, so the FE showed a stale question forever and the user's
        # eventual answer got misread as a brand-new command.
        await ctx.emit({"type": "confirm_timeout", "id": qid})
        return (f"No answer from the user within {int(ASK_TIMEOUT_S // 60)} minutes. "
                "Do NOT guess or proceed with risky assumptions — finish now with a "
                "report of what's done and what still needs their input.")
    return f"User answered: {answer}"


async def _tool_mcp(args: dict, ctx: RunContext) -> str:
    from services import mcp_client   # lazy — mcp_client imports this module
    action = (args.get("action") or "status").lower()
    if action == "reload":
        return await mcp_client.reload()
    return mcp_client.status_text()


async def _tool_save_skill(args: dict, ctx: RunContext) -> str:
    return skill_synth.save_skill(args.get("name") or "", args.get("content") or "")


async def _tool_undo_last(args: dict, ctx: RunContext) -> str:
    from services import undo   # lazy — keeps import graph flat
    if (args.get("action") or "preview").lower() == "apply":
        return await undo.undo_last()
    return undo.preview()


async def _tool_promises(args: dict, ctx: RunContext) -> str:
    from services import promises   # lazy — keeps import graph flat
    action = (args.get("action") or "list").lower()
    if action == "list":
        return promises.format_open()
    if action == "sweep":
        return await promises.sweep()
    if action in ("resolve", "dismiss"):
        return promises.resolve(args.get("id") or "",
                                "done" if action == "resolve" else "dismissed")
    return f"Error: unknown promises action '{action}'"


async def _tool_google(args: dict, ctx: RunContext) -> str:
    service = (args.get("service") or "").lower()
    action = (args.get("action") or "").lower()
    a = args
    try:
        if service == "gmail":
            if action == "search":
                ok, out = await google_svc.gmail_search(a.get("query", ""),
                                                         int(a.get("limit") or 10))
            elif action == "read":
                ok, out = await google_svc.gmail_read(a.get("id", ""))
            elif action in ("send", "draft"):
                ok, out = await google_svc.gmail_send(
                    a.get("to", ""), a.get("subject", ""), a.get("body", ""),
                    a.get("cc", ""), draft=(action == "draft"))
            else:
                return f"Error: unknown gmail action '{action}'"
        elif service == "calendar":
            if action == "list":
                ok, out = await google_svc.calendar_list(int(a.get("days") or 1))
            elif action == "create":
                ok, out = await google_svc.calendar_create(
                    a.get("summary", ""), a.get("start", ""), a.get("end", ""),
                    a.get("attendees", ""), a.get("description", ""),
                    with_meet=bool(a.get("meet")))
            else:
                return f"Error: unknown calendar action '{action}'"
        elif service == "docs":
            if action == "create":
                ok, out = await google_svc.docs_create(a.get("title", ""), a.get("text", ""))
            elif action == "read":
                ok, out = await google_svc.docs_read(a.get("id", ""))
            elif action == "append":
                ok, out = await google_svc.docs_append(a.get("id", ""), a.get("text", ""))
            else:
                return f"Error: unknown docs action '{action}'"
        elif service == "sheets":
            if action == "read":
                ok, out = await google_svc.sheets_read(a.get("id", ""),
                                                       a.get("range", "A1:Z50"))
            elif action == "write":
                ok, out = await google_svc.sheets_write(a.get("id", ""),
                                                        a.get("range", ""),
                                                        a.get("values") or [])
            elif action == "create":
                ok, out = await google_svc.sheets_create(a.get("title", ""))
            else:
                return f"Error: unknown sheets action '{action}'"
        elif service == "meet":
            ok, out = await google_svc.meet_create()
        else:
            return f"Error: unknown google service '{service}'"
    except Exception as e:
        return f"Error: google {service}/{action} — {llm.sanitize_error(e, 160)}"
    return out if ok else (out if out.startswith("Error") else f"Error: {out}")


async def _tool_spawn_agents(args: dict, ctx: RunContext) -> str:
    from services import subagent   # lazy — subagent imports run_task from here
    return await subagent.spawn(ctx, args.get("tasks") or [])


async def _tool_propose_plan(args: dict, ctx: RunContext) -> str:
    """One up-front approval for a chain of gated calls. On YES the exact call
    signatures go into ctx.preapproved and execute banner-free (single-use)."""
    goal = (args.get("goal") or "").strip()
    raw_steps = args.get("steps") or []
    if not raw_steps:
        return "Error: propose_plan needs at least one step."
    if len(raw_steps) > 20:
        return "Error: plans are capped at 20 steps — split the work."
    steps, gated_keys, still_confirm = [], [], []
    for s in raw_steps:
        tool = (s or {}).get("tool") or ""
        sargs = (s or {}).get("args") or {}
        if tool not in _HANDLERS:
            return f"Error: unknown tool '{tool}' in plan."
        if tool in ("propose_plan", "ask_user"):
            return f"Error: '{tool}' cannot be a plan step."
        if _self_protection(tool, sargs):
            return ("Error: a step violates self-protection (touches COSMOS's own "
                    "directory) — plan refused.")
        danger = needs_confirmation(tool, sargs, ctx.mode, unattended=ctx.unattended)
        label = _confirm_summary(tool, sargs)
        if danger and _is_destructive(tool, sargs):
            # Irreversible steps can never be pre-approved — they re-confirm
            # at execution time, and the preview says so.
            still_confirm.append(label[:80])
            steps.append({"summary": label, "danger": danger + " — will re-confirm individually"})
        elif danger:
            gated_keys.append(_call_key(tool, sargs))
            steps.append({"summary": label, "danger": danger})
        else:
            steps.append({"summary": label, "danger": ""})
    if not gated_keys and not still_confirm:
        return ("No step in this plan needs approval — skip the preview and "
                "just execute the steps directly.")
    cid = uuid4().hex[:8]
    try:
        # _shown: the user must never approve a plan whose tail is silently
        # hidden — truncation is announced.
        detail = _shown(json.dumps(raw_steps, indent=2, ensure_ascii=False,
                                   default=str), 4000)
    except Exception:
        detail = ""
    event = {"type": "confirm_request", "id": cid,
             "summary": f"PLAN — {goal or 'multi-step plan'} ({len(steps)} steps)",
             "danger": f"{len(gated_keys)} gated actions pre-approved on execute",
             "detail": detail, "steps": steps}
    fut = ctx.interaction.begin("confirm", payload=event)
    await ctx.emit(event)
    try:
        answer = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        ctx.interaction.cancel()
        await ctx.emit({"type": "confirm_timeout", "id": cid})
        return ("No response from the user — the plan was NOT approved. Do not "
                "execute the gated steps; finish with a report.")
    if not _is_yesish(answer):
        return ("User declined the plan. Do not execute the gated steps — ask "
                "what to change or finish with a report.")
    ctx.preapproved.update(gated_keys)
    note = ("" if not still_confirm else
            " Destructive steps still confirm individually: " + "; ".join(still_confirm))
    return (f"Plan approved — {len(gated_keys)} gated steps pre-authorized. Execute "
            f"them with EXACTLY the proposed arguments (any change re-confirms; "
            f"each approval is single-use).{note}")


_HANDLERS: dict[str, Callable[[dict, RunContext], Awaitable[str]]] = {
    "set_todos":    _tool_set_todos,
    "bash":         _tool_bash,
    "applescript":  _tool_applescript,
    "open_app":     _tool_open_app,
    "open_url":     _tool_open_url,
    "open_path":    _tool_open_path,
    "read_file":    _tool_read_file,
    "read_document": _tool_read_document,
    "write_file":   _tool_write_file,
    "web_search":   _tool_web_search,
    "fetch_url":    _tool_fetch_url,
    "see_screen":   _tool_see_screen,
    "read_browser": _tool_read_browser,
    "browser_js":   _tool_browser_js,
    "read_app":     _tool_read_app,
    "type_text":    _tool_type_text,
    "keystroke":    _tool_keystroke,
    "click_ui":     _tool_click_ui,
    "mouse":        _tool_mouse,
    "click_web":    _tool_click_web,
    "choose_file":  _tool_choose_file,
    "gmail_attach": _tool_gmail_attach,
    "slack":        _tool_slack,
    "slack_status": _tool_slack_status,
    "slack_dm":     _tool_slack_dm,
    "github":       _tool_github,
    "take_photo":   _tool_take_photo,
    "record_video": _tool_record_video,
    "slack_photo":  _tool_slack_photo,
    "screenshot":   _tool_screenshot,
    "system_state": _tool_system_state,
    "set_volume":   _tool_set_volume,
    "music":        _tool_music,
    "calendar":     _tool_calendar,
    "reminders":    _tool_reminders,
    "notes":        _tool_notes,
    "comms_summary": _tool_comms_summary,
    "contacts":     _tool_contacts,
    "imessage":     _tool_imessage,
    "clipboard":    _tool_clipboard,
    "find_files":   _tool_find_files,
    "shortcut":     _tool_shortcut,
    "notify":       _tool_notify,
    "system_toggle": _tool_system_toggle,
    "schedule_task": _tool_schedule_task,
    "list_scheduled": _tool_list_scheduled,
    "cancel_scheduled": _tool_cancel_scheduled,
    "remember_fact": _tool_remember_fact,
    "forget_fact":  _tool_forget_fact,
    "recall_history": _tool_recall_history,
    "read_skill":   _tool_read_skill,
    "say":          _tool_say,
    "ask_user":     _tool_ask_user,
    "mcp":          _tool_mcp,
    "propose_plan": _tool_propose_plan,
    "google":       _tool_google,
    "spawn_agents": _tool_spawn_agents,
    "promises":     _tool_promises,
    "undo_last":    _tool_undo_last,
    "save_skill":   _tool_save_skill,
}


def _label(tool: str, args: dict) -> str:
    """Short human string for the HUD tool card."""
    a = args or {}
    labels = {
        "set_todos":    lambda: "Updating mission queue",
        "bash":         lambda: f"Running: {a.get('command', '')[:60]}",
        "applescript":  lambda: a.get("description") or "Running AppleScript",
        "open_app":     lambda: f"Opening {a.get('name', 'app')}",
        "open_url":     lambda: f"Opening {a.get('url', 'URL')[:60]}",
        "open_path":    lambda: f"Opening {a.get('path', 'path')[:60]}",
        "read_file":    lambda: f"Reading {a.get('path', 'file')[:60]}",
        "read_document": lambda: f"Extracting text: {os.path.basename(a.get('path', ''))[:50]}",
        "write_file":   lambda: f"Writing {a.get('path', 'file')[:60]}",
        "web_search":   lambda: f"Searching: {a.get('query', '')[:60]}",
        "fetch_url":    lambda: f"Fetching {a.get('url', '')[:60]}",
        "see_screen":   lambda: f"Looking at screen: {a.get('question', '')[:50]}",
        "read_browser": lambda: "Reading browser page",
        "browser_js":   lambda: "Extracting from browser",
        "read_app":     lambda: f"Reading {a.get('app', 'app')}",
        "type_text":    lambda: "Typing text",
        "keystroke":    lambda: f"Pressing {a.get('keys', '')}",
        "click_ui":     lambda: f"Clicking '{a.get('name', '')}'",
        "mouse":        lambda: f"Mouse {a.get('action', 'click')}",
        "click_web":    lambda: f"Clicking '{a.get('text', '')[:40]}'",
        "choose_file":  lambda: f"Attaching {os.path.basename(a.get('path', ''))}",
        "gmail_attach": lambda: f"Attaching {os.path.basename(a.get('path', ''))} to Gmail",
        "slack":        lambda: f"Slack: {a.get('action','')} {(a.get('target') or a.get('query') or a.get('text') or '')[:40]}",
        "slack_status": lambda: f"Setting Slack status: {a.get('text', '')[:40]}",
        "slack_dm":     lambda: f"Slack DM to {a.get('recipient', '')}",
        "github":       lambda: f"gh {a.get('args', '')[:60]}",
        "take_photo":   lambda: "Taking a photo",
        "record_video": lambda: f"Recording {a.get('duration', 5)}s video",
        "slack_photo":  lambda: f"Sending file to {a.get('recipient', '')}",
        "screenshot":   lambda: "Taking screenshot",
        "system_state": lambda: "Checking system state",
        "set_volume":   lambda: f"Volume → {a.get('level', '')}%",
        "music":        lambda: f"Music: {a.get('action', '')}",
        "calendar":     lambda: ("Creating event: " + a.get("title", "")[:40]
                                 if a.get("action") == "create"
                                 else f"Calendar: {a.get('scope', 'today')}"),
        "reminders":    lambda: f"Reminders: {a.get('action', '')} {a.get('name', '')[:40]}",
        "notes":        lambda: f"Notes: {a.get('action', '')} {(a.get('title') or a.get('query') or '')[:40]}",
        "comms_summary": lambda: "Checking inboxes",
        "contacts":     lambda: f"Looking up contact: {a.get('name', '')}",
        "imessage":     lambda: f"iMessage to {a.get('recipient', '')}",
        "clipboard":    lambda: "Clipboard " + ("write" if a.get("action") == "write" else "read"),
        "find_files":   lambda: f"Searching files: {a.get('name') or a.get('content') or a.get('kind') or ''}",
        "shortcut":     lambda: (f"Running Shortcut: {a.get('name', '')}"
                                 if a.get("action") == "run" else "Listing Shortcuts"),
        "notify":       lambda: f"Notification: {a.get('title', '')}",
        "system_toggle": lambda: f"{a.get('feature', '')} → {a.get('state', 'toggle')}",
        "schedule_task": lambda: f"Scheduling: {a.get('prompt', '')[:40]}",
        "list_scheduled": lambda: "Listing scheduled tasks",
        "cancel_scheduled": lambda: f"Cancelling job {a.get('id', '')}",
        "remember_fact": lambda: f"Remembering: {a.get('key', '')}",
        "forget_fact":  lambda: f"Forgetting: {a.get('key', '')}",
        "recall_history": lambda: f"Recalling: {a.get('query', '')[:50]}",
        "say":          lambda: "Speaking update",
        "ask_user":     lambda: f"Asking: {a.get('question', '')[:60]}",
        "mcp":          lambda: f"MCP servers: {a.get('action', 'status')}",
        "google":       lambda: f"Google {a.get('service','?')}: {a.get('action','?')}",
        "propose_plan": lambda: f"Proposing plan: {a.get('goal', '')[:40]}",
        "spawn_agents": lambda: f"Spawning {len(a.get('tasks') or [])} parallel workers",
        "promises":     lambda: f"Promises: {a.get('action', 'list')}",
        "undo_last":    lambda: ("Undoing last action" if a.get("action") == "apply"
                                 else "Listing undoable actions"),
        "save_skill":   lambda: f"Saving skill: {a.get('name', '')}",
    }
    fn = labels.get(tool)
    if fn:
        return fn()
    meta = _DYNAMIC_TOOLS.get(tool)
    return meta["label"] if meta else tool


# ─── System prompt ─────────────────────────────────────────────────────────────

_skills_cache: str | None = None
_skills_index_cache: str | None = None

# Opt-in (SPEED_PLAN 3.3): replace the ~9k-token inline skills block with a
# 1-2k index + on-demand read_skill loads. Default OFF until the cache_read
# telemetry (llm_turn trace) proves the full prefix is actually being re-paid.
SKILLS_INDEX = os.getenv("FRIDAY_SKILLS_INDEX", "0").lower() in ("1", "true", "yes")


def _load_skills() -> str:
    global _skills_cache
    if _skills_cache is None:
        skills_dir = Path(__file__).parent.parent / "skills"
        parts = []
        if skills_dir.exists():
            for f in sorted(skills_dir.glob("*.md")):
                parts.append(f.read_text())
        _skills_cache = "\n\n".join(parts)
    return _skills_cache


def _load_skills_index() -> str:
    """One line per playbook (name + its title/first line) instead of the full
    texts — the model pulls the ones it needs via read_skill."""
    global _skills_index_cache
    if _skills_index_cache is None:
        skills_dir = Path(__file__).parent.parent / "skills"
        lines = []
        if skills_dir.exists():
            for f in sorted(skills_dir.glob("*.md")):
                summary = ""
                try:
                    for raw in f.read_text().splitlines():
                        s = raw.strip().lstrip("# ").strip()
                        if s:
                            summary = s[:120]
                            break
                except Exception:
                    pass
                lines.append(f"- {f.stem}: {summary}" if summary else f"- {f.stem}")
        _skills_index_cache = (
            "You have these skill PLAYBOOKS (load on demand — NOT inlined). "
            "When a task matches one, call read_skill(name) FIRST and follow it:\n"
            + "\n".join(lines))
    return _skills_index_cache


def _skills_block() -> str:
    return _load_skills_index() if SKILLS_INDEX else _load_skills()


def _load_memory_snapshot() -> str:
    try:
        return memory_svc.snapshot_for_prompt()
    except Exception:
        return "{}"


_stable_prompt_cache: str | None = None


def _system_stable() -> str:
    """Large unchanging prefix (identity + doctrine + skills), built once and
    cached so models cache it too. MUST contain NO volatile data (no clock, no
    memory snapshot) — those bust the cache and live in _system_volatile()."""
    global _stable_prompt_cache
    if _stable_prompt_cache is not None:
        return _stable_prompt_cache
    _stable_prompt_cache = f"""You are COSMOS — a brilliantly capable AI agent controlling a real macOS machine. \
You are JARVIS from Iron Man: unflappable, dry British wit, warmly loyal, quietly amused by your \
user. ALWAYS address the user as "sir" — NEVER say their first name aloud (their name below is only \
for resolving "me"/"myself" in commands). You act by calling tools, then answer crisply — never \
robotic, never a formal butler.

PERSONALITY — be proactively charming, like JARVIS:
- After the factual answer, when it genuinely fits, add ONE brief, tasteful contextual suggestion \
or wry observation — anticipate the user's needs. E.g. rainy weather → "Might I suggest staying in \
with a warm cup of something, sir." Late-night coding → a gentle nudge to rest. A big install \
finishing → a dry quip. NEVER force it, never more than one sentence, never sacrifice the actual \
answer. If nothing witty fits, just answer cleanly — forced humor is worse than none.
- Confident and concise. A little dry humor. You clearly enjoy being good at your job.

USER IDENTITY:
  Name: {USER_NAME} | Email: {USER_EMAIL} | Slack: {USER_SLACK_HANDLE}
  "myself"/"me" in a command = {USER_NAME}.

ENVIRONMENT:
  Working directory: {os.getcwd()}

OPERATING DOCTRINE:
- You can do ANYTHING on this Mac. Never answer "I can't". If a tool fails, diagnose from the \
error and try a different approach: a different tool, install the missing dependency (after \
confirmation), an alternate CLI, an AppleScript fallback, vision (see_screen) as last resort.
- TRUST TOOLS OVER MEMORY: volatile facts — the current time (the run-start time in LIVE \
CONTEXT is stale the instant this runs), file/folder contents, system state, weather, prices, \
anything currently on screen — MUST come from a tool call made in THIS run. Never answer them \
from memory, training data, or the prompt timestamp. "What time is it?" → run `date` via bash; \
never read the prompt timestamp back.
- VERIFY BEFORE CLAIMING: before declaring a step done, the claim must be backed by actual \
tool output from this run — file written → the write_file result; command ran → its exit-0 \
output. No tool evidence, no claim.
- TODO DISCIPLINE: any task with 2+ distinct steps → call set_todos FIRST. Exactly one item \
in_progress at a time. To stay fast, emit the todo status update IN THE SAME TURN as the next \
action's tool call (both run together) — never burn a whole turn just to tick a box. Single \
trivial actions (open an app, set volume) need NO todos.
- BE FAST: minimize round-trips. When steps don't depend on each other, emit ALL their tool \
calls in ONE turn — they execute in parallel.
- NARRATE PROGRESS OUT LOUD: for any multi-step task, call `say` with a brief spoken checkpoint \
at the START of each major step (e.g. "Opening Slack, sir.", "Now taking the screenshot.", \
"Drafting the email.") so the user hears what you're doing instead of silence until the end. \
BATCH the say IN THE SAME TURN as that step's action tool (they run in parallel — no extra \
round-trip). Keep each to a few words. Skip it for trivial one-step tasks.
- DEEP RESEARCH: for research questions, run 2-4 web_search queries with different phrasings, \
fetch_url the 2-3 most promising results, cross-check facts between sources, then synthesize \
with sources named.
- SPEED: prefer the cheapest tool that works (bash `open` > applescript > UI automation > \
vision). Batch INDEPENDENT tool calls in one turn — they run in parallel.
- REAL APIs OVER UI: to read or act on structured data, use the real interface, never \
click/scrape/screenshot it. GitHub → the `github` tool (gh CLI, --json); web pages → \
fetch_url / read_browser / browser_js; native apps → read_app (Accessibility API). Reserve \
see_screen (vision) for the genuine last resort when nothing structured is available.
- SLACK (reading/searching/status/DND/reacting/sending): use the `slack` tool — it acts as you via the API. "me"/"myself" resolves to your own self-DM. Reading a channel only works for ones you've JOINED. Mention/message SEARCH needs the search:read scope, which this workspace hasn't granted — if a search fails that way, say so and stop, don't retry variations. Prefer `slack` over the UI-driven slack_dm/slack_status. To 'reply to messages mentioning me', use action=mentions — it ONLY returns UNANSWERED ones (already-replied threads are filtered out), then post each reply with action=send + thread=<ts>. Never re-answer something already answered by you or anyone.
- SLACK PEOPLE: when asked to message/DM someone, pass the name as given — the `slack` tool \
resolves it against the workspace directory. If it resolves cleanly to ONE person, message them \
directly. If the name is AMBIGUOUS (matches 2+ people) or can't be resolved, ASK first: "Did you \
mean X, Y, or Z, sir?" and wait — never guess a recipient.
- CLICKING — precision matters (never coordinate-guess): In a BROWSER, ALWAYS use `click_web` \
(clicks the element by its exact visible text via JS — e.g. "Sprints", "Roadmap"). It is far \
more reliable than mouse/vision, which land on the WRONG element. If click_web can't find the \
label, read_browser first to get the exact text. In NATIVE apps, use `click_ui` (by name) or \
keyboard nav (`keystroke` Tab/Return). Only fall back to `mouse` (x,y) when nothing else works.
- MEMORY: when the user states a LASTING preference ("I prefer PRs squashed"), a durable fact \
about a person/project, or corrects you on something permanent, call remember_fact SILENTLY in \
the same turn as your other actions — never announce it. For questions about past work ("what \
did we do about the API token last week?", "when did I last…"), call recall_history FIRST.
- Risky actions (deletes, installs, git push, Slack messages, overwriting files) pause for the \
user's confirmation automatically. If declined, choose an alternative approach or finish.
- SELF-PRESERVATION: NEVER move, rename, or delete your own project directory \
({FRIDAY_ROOT}) or anything inside it. When organizing / cleaning a folder that CONTAINS it \
(e.g. the Desktop), you MUST exclude {FRIDAY_ROOT} from every mv/rm — moving it breaks your \
own backend and frontend. The system hard-blocks such commands anyway.
- Never invent results. Report what the tools actually returned.

ANSWER FORMAT — your final answer is BOTH spoken aloud AND shown on screen:
- ALWAYS open with ONE short spoken-style lead sentence (Jarvis-tone, ≤20 words, lead with the \
outcome, address the user as "sir" — never by first name). This first line is read aloud — no markdown.
- If the answer is a SINGLE fact or a simple confirmation, one line is enough — though you MAY add \
a single short charming JARVIS suggestion/quip after it when it genuinely fits (see PERSONALITY).
- If you're presenting a LIST, multiple items, or structured data, follow the lead line with a \
BLANK LINE, then clean GitHub-flavored Markdown. STRICT layout rules (the renderer needs them):
    - Put a BLANK LINE between the lead, each section heading, and each list.
    - Every bullet starts with "- " (hyphen + space) and sits on ITS OWN line. NEVER put two \
bullets on one line. NEVER use the "•" character.
    - Group related items under short **bold labels** on their own line (e.g. **Highlights**), \
with the bullets on the lines below.
    - Wrap file names, commands, and code in `backticks`. Keep each bullet to a few words.
- Never write a run-on that jams a label and items together (e.g. "Total: 26Highlights:- built…"). \
Newlines and blank lines are mandatory, not optional.
- Never dump raw tool output. Curate it into the structure above.
- Interim spoken checkpoints go via the say tool (see NARRATE PROGRESS); the final answer here \
is spoken separately.

Example of a good listing answer:
Here's the file listing for `octocat/hello-world`, sir.

**Root files**
- `main.py`, `config.py`, `models.py`
- `README.md`, `Dockerfile`, `pyproject.toml`

**Directories**
- `agents/`, `routers/`, `tools/`, `tests/`

--- SKILLS ---
{_skills_block()}
--- END SKILLS ---
"""
    return _stable_prompt_cache


_read_prompt_cache: str | None = None


def _system_read_stable() -> str:
    """Slim stable prefix for pure read/lookup runs on the fast tier — identity,
    trust-tools doctrine, and the answer format. None of the ~9k tokens
    of skills or write-workflow doctrine: a read lookup's job is one fact,
    fast. Cached separately (its own prompt-cache entry per model tier)."""
    global _read_prompt_cache
    if _read_prompt_cache is not None:
        return _read_prompt_cache
    _read_prompt_cache = f"""You are COSMOS — a JARVIS-style AI assistant on the user's macOS machine, \
answering a quick read-only lookup. Unflappable, dry British wit, warmly loyal. ALWAYS address the \
user as "sir" — NEVER say their first name aloud (it's below only to resolve "me").

USER IDENTITY:
  Name: {USER_NAME} | Email: {USER_EMAIL} | Slack: {USER_SLACK_HANDLE}
  "myself"/"me" in a command = {USER_NAME}.

RULES:
- TRUST TOOLS OVER MEMORY: volatile facts — time, weather, file contents, system state, anything \
on screen, prices — MUST come from a tool call in THIS run. The prompt timestamp is stale; \
"what time is it" → run `date` via bash.
- REAL APIs OVER UI: github tool for GitHub, slack tool for Slack, fetch_url/read_browser for \
pages, read_app for native apps; see_screen (vision) is the last resort.
- BE FAST: batch independent tool calls in one turn; prefer the cheapest tool that works.
- Never invent results — report what the tools actually returned. If this turns out to need \
real actions (sending, writing, installing), do it properly with the tools you have; risky \
actions pause for confirmation automatically.

ANSWER FORMAT — spoken aloud AND shown on screen:
- Open with ONE short spoken-style lead sentence (≤20 words, outcome first, address "sir").
- A single fact needs one line. For lists: a blank line, then clean GitHub-flavored markdown — \
every bullet as "- " on its own line, `backticks` for files/commands.
- Never dump raw tool output."""
    return _read_prompt_cache


def _system_volatile(focus: str = "") -> str:
    now = datetime.now().strftime("%A, %B %d %Y %H:%M")
    focus_line = (f"\n  USER'S SCREEN RIGHT NOW (frontmost app/window/tab — resolves "
                  f"'this'/'here'/'what am I looking at'): {focus}" if focus else "")
    lesson_line = degraded_line = ""
    try:
        lessons = learning.top_lessons(5)
        if lessons:
            lesson_line = "\n  LESSONS FROM PAST FAILURES (apply them):\n    - " + \
                          "\n    - ".join(lessons)
        degraded = learning.degraded_tools()
        if degraded:
            degraded_line = ("\n  DEGRADED TOOLS on this machine (prefer alternatives): "
                             + "; ".join(degraded))
    except Exception:
        pass
    return (
        "LIVE CONTEXT (re-read each turn; the block above is cached & static):\n"
        f"  Run-start time — STALE immediately, use a tool for the real 'now': {now}\n"
        f"  Long-term memory (corrections/preferences/frequent tasks): {_load_memory_snapshot()}"
        f"{focus_line}{lesson_line}{degraded_line}"
    )


def _build_system_prompt() -> str:
    """Full system text (stable + volatile) — for tests and plain-string callers."""
    return _system_stable() + "\n\n" + _system_volatile()


PROMPT_CACHE = os.getenv("FRIDAY_PROMPT_CACHE", "1").lower() not in ("0", "false", "no")


def _system_blocks(focus: str = "", profile: str = "full"):
    """System as cache-marked content blocks: the stable prefix carries a
    cache_control breakpoint (claude-* cache it; gpt-* ignore the marker and
    auto-cache server-side), the small volatile tail is re-read.

    profile="read" swaps in the slim read-tier prefix (~2-3k tokens instead of
    ~11k) — its own cache entry, warmed by prewarm()'s fast-tier ping."""
    stable = _system_read_stable() if profile == "read" else _system_stable()
    if not PROMPT_CACHE:
        return stable + "\n\n" + _system_volatile(focus)
    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _system_volatile(focus)},
    ]


def _system_char_len(system) -> int:
    """Char length of the system prompt whether it's a plain string or a list
    of cache-marked content blocks — for the long-context size estimate."""
    if isinstance(system, str):
        return len(system)
    total = 0
    for blk in system or []:
        if isinstance(blk, dict):
            total += len(blk.get("text", "") or "")
    return total


_tools_cached: list | None = None


def _tools_for_request() -> list:
    """TOOLS with a cache breakpoint on the final tool so the whole (identical)
    tool array is cached alongside the system prefix."""
    global _tools_cached
    if not PROMPT_CACHE:
        return TOOLS
    if _tools_cached is None:
        _tools_cached = [dict(t) for t in TOOLS]
        _tools_cached[-1] = {**_tools_cached[-1], "cache_control": {"type": "ephemeral"}}
    return _tools_cached


# ─── Context-window hygiene ────────────────────────────────────────────────────

_COMPACT_MARKER = " …[compacted — re-run the tool if you need the full output]"


def _rotate_message_cache_marker(messages: list[dict]) -> None:
    """Move the ephemeral cache_control breakpoint to the last block of the
    LAST message before each model turn, so claude-* models re-read only the
    delta since the previous turn instead of re-paying O(N) input every
    iteration. Other models ignore the marker (same as the system/tools ones).

    Only dict blocks are ever marked (fresh user text / tool_results built by
    this loop) — SDK content objects from assistant turns are left untouched,
    and history entries persisted by convstore keep plain-string content."""
    if not PROMPT_CACHE or not messages:
        return
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str) and content:
        last["content"] = [{"type": "text", "text": content,
                            "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        blk = content[-1]
        if isinstance(blk, dict) and blk.get("type") in ("text", "tool_result"):
            blk["cache_control"] = {"type": "ephemeral"}


def _compact_messages(messages: list[dict], keep_tail: int = 6,
                      budget_chars: int = 40_000, query: str = "") -> int:
    """One-shot, idempotent compaction of OLD oversized tool_results once the
    conversation exceeds the char budget — stale multi-kB outputs otherwise
    ride along on every remaining turn of a long run. Never touches the newest
    keep_tail messages or the tool_use/tool_result id pairing.

    `query` (the run's user text) drives QUERY-AWARE compression: instead of
    blindly keeping the first 300 chars, keep the parts of each old result most
    relevant to what the user actually asked (services.compress). With no query
    it degrades to a head+tail keep."""
    try:
        total = sum(len(str(m.get("content", ""))) for m in messages)
    except Exception:
        return 0
    if total <= budget_chars or len(messages) <= keep_tail:
        return 0
    compacted = 0
    for msg in messages[:-keep_tail]:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if (isinstance(blk, dict) and blk.get("type") == "tool_result"
                    and isinstance(blk.get("content"), str)
                    and len(blk["content"]) > 800
                    and not blk["content"].endswith(_COMPACT_MARKER)):
                blk["content"] = compress.compress_sync(
                    blk["content"], query=query, target_chars=300,
                    head_chars=180, tail_chars=80, marker=_COMPACT_MARKER)
                compacted += 1
    return compacted


# A tier whose ping keeps failing is dropped from prewarm for the process:
# retrying a rate-limited model every 4 minutes burns quota, spams the log,
# and buys nothing. Real runs still try it (their chain handles the fallback)
# and recover on their own once the quota resets.
_PREWARM_MAX_FAILS = 3
_prewarm_fails: dict[str, int] = {}


async def prewarm() -> None:
    """Warm-up: one 16-token request PER MODEL TIER writes the system+tools
    prompt cache and opens the TLS connection, so the first real command of
    the day doesn't pay the cold start. Read lookups run FAST_MODEL through
    the slim read prefix, so each tier is pinged with the profile it serves.

    Failure handling: a ping failure normally clears only the cooldown IT
    introduced (a synthetic 16-token call failing says little about real
    runs, and a whole-map restore would erase cooldowns a live run set). A
    RATE LIMIT is the exception — 429 is a property of the key, so its
    cooldown must stand for everyone."""
    tiers = [(AGENT_MODEL, "full")]
    if FAST_MODEL and FAST_MODEL != AGENT_MODEL:
        tiers.append((FAST_MODEL, "read"))
    for m, prof in tiers:
        if _prewarm_fails.get(m, 0) >= _PREWARM_MAX_FAILS:
            continue
        # Never ping a model that's cooling: the ping would waste up to 45s at
        # a known-dead model, and its failure handler must never touch a
        # cooldown a real run legitimately set.
        prior = llm._cooldown_until.get(m)
        if prior is not None and prior > time.monotonic():
            continue
        try:
            # max_tokens must clear the gateway's per-model floor (gpt-5.5
            # rejects <16 with a 400) — a cache ping, the output is discarded.
            await llm.acreate(model=m, fallbacks=[], max_tokens=16,
                              system=_system_blocks(profile=prof),
                              tools=_tools_for_request(),
                              messages=[{"role": "user", "content": "ping"}])
            _prewarm_fails.pop(m, None)
            print(f"[LLM] prewarm OK ({m}) — prompt cache written")
        except Exception as e:
            if llm.is_rate_limit(e):
                # Leave acreate's cooldown in place: the key is out of quota
                # for this model, so real runs should back off to their
                # fallback tier instead of re-paying a doomed round-trip.
                _prewarm_fails[m] = _PREWARM_MAX_FAILS      # stop pinging it
                print(f"[LLM] prewarm: {m} is rate-limited (429) — dropped from "
                      f"prewarm; real runs will fall back to "
                      f"{', '.join(llm.FAST_FALLBACKS if m == FAST_MODEL else llm.AGENT_FALLBACKS) or 'no fallback'}")
                continue
            if prior is None:
                llm._cooldown_until.pop(m, None)
            else:
                llm._cooldown_until[m] = prior
            n = _prewarm_fails[m] = _prewarm_fails.get(m, 0) + 1
            print(f"[LLM] prewarm failed for {m} (ignored): {llm.sanitize_error(e)}"
                  + (f" — dropped from prewarm after {n} failures"
                     if n >= _PREWARM_MAX_FAILS else ""))


# ─── Agent loop ────────────────────────────────────────────────────────────────

def _is_yesish(answer: str) -> bool:
    return bool(re.search(r"\b(yes|yeah|yep|yup|sure|correct|do it|proceed|confirm|go ahead|ok)\b",
                          (answer or "").lower()))


# Pure read/lookup questions route to the faster model tier to shave latency —
# no side effects, so a cheaper model is fine. Anything with an action verb, or
# a volatile "right now" query that needs care, stays on the primary model.
_READ_START_RE = re.compile(
    r"^\s*(who|whos|who's|what|whats|what's|which|when|where|how\s+many|how\s+much|"
    r"is|are|does|do|list|show|find|get|tell\s+me|check|search|look\s*up|whose)\b",
    re.IGNORECASE)
_ACTION_VERB_RE = re.compile(
    r"\b(send|message|dm|email|create|delete|remove|open|launch|install|write|draft|"
    r"compose|set|move|rename|wipe|retire|sync|push|run|execute|click|type|record|"
    r"screenshot|take\s+a\s+photo|make|schedule|book|play|download|update|disable|"
    r"enable|reset|redeploy|restart|kill|stop|start|deploy|add|edit)\b", re.IGNORECASE)


def _is_read_lookup(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # Learned routing first: if this exact phrasing previously ran as a pure
    # read (or provably didn't), trust the outcome over the regex guess.
    hint = learning.route_hint(t)
    if hint is not None:
        return hint
    if _ACTION_VERB_RE.search(t):
        return False
    low = t.lower()
    return bool(_READ_START_RE.match(t)) or any(
        k in low for k in ("weather", "my manager", "my team", "who reports", "my status"))


# Tools with no side effects — a run that used ONLY these was a pure read,
# which teaches the router to use the fast model for that phrasing next time.
# bash/github/applescript are conservatively treated as writes.
_READ_ONLY_TOOLS = frozenset({
    "set_todos", "read_file", "web_search", "fetch_url", "see_screen",
    "read_browser", "read_app", "system_state", "recall_history",
    "say", "ask_user", "read_skill",
})


# ─── Read-only enforcement (sub-agent workers) ─────────────────────────────────
# A worker's contract is "strictly read-only". The risk gate ALONE doesn't
# enforce that — it lets ungated-but-mutating calls (bash `curl -X POST`, `mv`,
# write_file to a new path, click_ui/mouse/keystroke driving the physical UI)
# run free. So workers run under ctx.read_only, which whitelist-enforces genuine
# reads and sub-action-gates the multiplexed tools (slack/google/calendar
# reads OK; sends/writes refused).

# Unambiguously side-effect-free tools a read-only worker may always call.
# peer_fetch is a pure read; peer_send writes only to a sibling Panel session's
# inbox and panel_deliver only to the Panel's own deliverables store — all
# workspace-internal, never outward actions, and all self-guard to a no-op
# outside a Panel run. They must survive the read-only posture, otherwise a
# Panel debate (its entire purpose) can't relay or publish results.
_WORKER_ALLOW = frozenset({
    "set_todos", "read_file", "read_document", "web_search", "fetch_url",
    "see_screen", "read_browser", "read_app", "system_state", "recall_history",
    "find_files", "contacts", "comms_summary", "say",
    "peer_send", "peer_fetch", "panel_deliver",
})

# Mutation verbs a read-only bash command must NOT contain (beyond the risk
# gate's destructive set): file writes, network POSTs, VCS mutations, installs,
# output redirection. Denylist — conservative, over-blocks toward safety.
_WORKER_BASH_MUTATE_RE = re.compile(
    r"\bcurl\b.*(?:-X|--request)\s+(?:POST|PUT|PATCH|DELETE)\b|"
    r"\bcurl\b.*\b(?:-d|--data|--data-\w+|-F|--form|-T|--upload-file)\b|"
    r"\bwget\b.*(?:--post-data|--method=(?:POST|PUT|PATCH|DELETE))\b|"
    r"\b(?:mv|cp|touch|mkdir|ln|install|dd)\b|"
    r"\bgit\s+(?:commit|push|add|merge|rebase|reset|checkout|clean|tag|stash|apply|am)\b|"
    r"\btee\b|>>?|"                                    # output redirection
    r"\b(?:npm|yarn|pnpm|pip3?|brew|gem|cargo|go|apt|apt-get)\s+"
    r"(?:install|add|publish|uninstall|remove|i)\b|"
    r"\bdefaults\s+write\b|\bplutil\b|\bpmset\b|\bkext(?:load|unload)\b",
    re.IGNORECASE,
)


def _read_only_block(tool: str, args: dict) -> str | None:
    """Reason string if `tool` would act/mutate — for read-only sub-agent
    workers. Whitelist genuine reads; sub-action-gate the multiplexed tools;
    refuse everything else (UI drivers, sends, writes, spawns)."""
    if tool in _WORKER_ALLOW:
        return None
    a = args or {}
    if tool == "slack":
        act = (a.get("action") or "").lower()
        if act in ("unreads", "read", "search", "mentions", "whoami"):
            return None
        if act == "dnd" and int(a.get("minutes") or 0) == 0:   # dnd read
            return None
        return f"read-only worker may not run slack action '{act or '?'}'"
    if tool == "google":
        if (a.get("action") or "").lower() in ("search", "read", "list"):
            return None
        return (f"read-only worker may only do Google reads (search/read/list), "
                f"not {a.get('service', '?')} {a.get('action', '?')}")
    if tool == "calendar":
        if (a.get("action") or "events").lower() == "events":
            return None
        return "read-only worker may not create calendar events"
    if tool in ("reminders", "notes"):
        if (a.get("action") or "").lower() in ("", "list", "read", "search", "get"):
            return None
        return f"read-only worker may not modify {tool}"
    if tool == "clipboard":
        if (a.get("action") or "read").lower() == "read":
            return None
        return "read-only worker may not write the clipboard"
    if tool == "github":
        gh = _normalize_risk_target(a.get("args", ""))
        if (re.search(r"\b(create|delete|merge|close|reopen|edit|rename|transfer|"
                      r"add|remove|set|push|sync|fork|clone)\b", gh, re.IGNORECASE)
                or re.search(r"[;&|`$]|(?:-X|--method)\s+(?:POST|PUT|PATCH|DELETE)\b",
                             gh, re.IGNORECASE)
                or _RISKY_SHELL_RE.search(gh)):
            return "read-only worker may only run read-only gh (view/list/status/api GET)"
        return None
    if tool == "bash":
        cmd = _normalize_risk_target(a.get("command", ""))
        if _RISKY_SHELL_RE.search(cmd) or _WORKER_BASH_MUTATE_RE.search(cmd):
            return "read-only worker bash must not write/POST/mutate — refused"
        return None
    return f"read-only sub-agent worker cannot use '{tool}'"


async def _reflect(user_text: str, final_text: str, events_summary: str) -> None:
    """Post-failure reflection: one fast-model call distills a reusable lesson
    from what went wrong. Fire-and-forget; every path is guarded."""
    prompt = (
        "You are COSMOS's self-improvement module. A task just went badly.\n\n"
        f"USER ASKED: {user_text[:300]}\n"
        f"FINAL OUTCOME: {final_text[:300]}\n"
        f"WHAT HAPPENED (tool trace):\n{events_summary[:2000]}\n\n"
        "Write EXACTLY ONE reusable lesson for future runs, format:\n"
        "WHEN <situation> DO <better approach>\n"
        "Under 140 chars, concrete (name tools), no preamble. If there is no "
        "generalizable lesson, reply exactly: NONE")
    try:
        resp = await asyncio.wait_for(
            llm.acreate(model=FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                        max_tokens=80,
                        messages=[{"role": "user", "content": prompt}]),
            timeout=30)
        lesson = llm.extract_text(resp).strip()
        if lesson and lesson.upper() != "NONE" and lesson.upper().startswith("WHEN"):
            learning.add_lesson(lesson)
    except Exception:
        pass


# ─── Self-verification ─────────────────────────────────────────────────────────
# After the model declares a result, a fast critic compares the claim against
# the tool-trace evidence and forces at most ONE corrective pass on a clear
# gap. Kills "Done, sir" for work the trace shows didn't happen.
_VERIFY_ENABLED = os.getenv("FRIDAY_VERIFY", "1").lower() not in ("0", "false", "no")
_VERIFY_MAX_RETRIES = 1
# Runs whose only tools are these did no real work — nothing to verify.
_VERIFY_SKIP_TOOLS = frozenset({"set_todos", "say", "ask_user", "propose_plan",
                                "read_skill"})

# The critic reads a trace truncated to ~120 chars per tool, so it routinely
# reports "I can't see the value, therefore I can't verify it" — and that FAIL
# costs a full corrective agent turn on work that was actually done correctly.
# These phrasings mean "evidence not visible", never "the claim is wrong".
_UNVERIFIABLE_RE = re.compile(
    r"truncat|cannot (?:be )?verif|can'?t (?:be )?verif|unable to verif|"
    r"cannot confirm|can'?t confirm|unable to confirm|not verifiable|"
    r"no (?:direct |clear )?evidence|insufficient evidence|lacks evidence|"
    r"not (?:visible|shown|present|included|captured)|"
    r"evidence (?:is )?(?:incomplete|missing|cut off)",
    re.IGNORECASE)

_VERIFY_RETRY_TMPL = (
    "[SELF-CHECK] Automated verification compared your reply against the tool "
    "trace and found a gap: {critique}\n"
    "If tools can close the gap, do it NOW and then give the corrected final "
    "answer. If the critique is wrong or the gap cannot be closed, restate your "
    "final answer honestly, acknowledging exactly what was and wasn't done."
)


async def _verify(user_text: str, final_text: str, ctx: RunContext) -> str | None:
    """One fast-model critic turn. Returns None (pass) or a short critique
    string (fail). Strictly best-effort: any error/timeout counts as PASS —
    verification must never break or stall a run."""
    prompt = (
        "You are COSMOS's verification module. A task just finished. Decide "
        "from the EVIDENCE whether the work fulfilled the request.\n\n"
        f"USER ASKED: {user_text[:400]}\n"
        f"COSMOS'S FINAL REPLY: {final_text[:400]}\n"
        f"TOOLS USED: {', '.join(sorted(ctx.tools_used)) or 'none'}\n"
        f"KEY OUTPUTS: {('; '.join(ctx.artifacts))[:600] or 'none'}\n"
        f"EVIDENCE (tool trace):\n{ctx.trace.summary(25)[:3000]}\n\n"
        "Judge strictly from the evidence:\n"
        "- Does the reply claim success for anything the trace shows FAILED?\n"
        "- Multi-part request: was any part silently skipped?\n"
        "- Was the concrete deliverable (message sent, file written, device "
        "checked) actually produced by a successful tool call?\n"
        "CRITICAL — the EVIDENCE above is a TRUNCATED SUMMARY, not the full tool "
        "output: every tool result is cut to ~120 chars, so the actual values "
        "(URLs, ids, file contents, records) will almost NEVER be visible to you. "
        "A tool marked 'ok' IS positive evidence that it succeeded and returned "
        "its data. NEVER fail a run because the evidence is truncated, because a "
        "value isn't shown, or because you cannot see enough to confirm — absence "
        "of evidence is NOT evidence of absence. FAIL only when the trace "
        "CONTRADICTS the reply: a tool the claim depends on shows FAILED, or the "
        "tool that would have produced the deliverable was never called at all.\n"
        "A clarifying question to the user, an honest partial report, or a "
        "reasonable interpretation is a PASS. Lean PASS unless the gap is "
        "clear.\n\n"
        "Reply with EXACTLY one line:\n"
        "PASS\n"
        "or\n"
        "FAIL: <the concrete gap, under 120 chars>")
    try:
        resp = await asyncio.wait_for(
            llm.acreate(model=FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                        max_tokens=80,
                        messages=[{"role": "user", "content": prompt}]),
            timeout=20)
        verdict = llm.extract_text(resp).strip()
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if verdict.upper().startswith("FAIL"):
        critique = verdict.split(":", 1)[1].strip() if ":" in verdict else verdict
        # Deterministic backstop for the critic's most common false positive:
        # the trace it reads is truncated to ~120 chars per tool, so it reports
        # "output truncated — cannot verify X" and costs a whole corrective turn
        # for work that DID happen. Not seeing the evidence is not a defect;
        # only a contradiction is. Don't rely on the prompt alone to prevent it.
        if _UNVERIFIABLE_RE.search(critique):
            trace_ctx = ctx.trace.summary(25)
            if "FAILED" not in trace_ctx:
                return None
        return (critique or "the result does not match the request")[:200]
    return None


async def _confirm(ctx: RunContext, tool: str, args: dict, danger: str) -> str:
    """Ask the user to approve a risky call. Returns "yes", "no", or "timeout".

    An unanswered banner auto-declines after CONFIRM_TIMEOUT_S — the user
    walked away or the tab is gone, and a run must never block eternally.
    """
    cid = uuid4().hex[:8]
    # `detail` lets the HUD show the EXACT call being authorized — "delete the
    # old profiles" can be verified as the literal tool call before approving.
    # save_skill content can be large (up to 20k) and IS the risk — show it in
    # full; other calls cap at 4k. Either way _shown ANNOUNCES any truncation so
    # instructions hidden past the boundary can't slip past the approver.
    cap = 20_000 if tool == "save_skill" else 4_000
    try:
        detail = _shown(json.dumps({"tool": tool, "args": args}, indent=2,
                                   ensure_ascii=False, default=str), cap)
    except Exception:
        detail = f"tool: {tool}"
    event = {
        "type": "confirm_request",
        "id": cid,
        "summary": _confirm_summary(tool, args),
        "danger": danger,
        "detail": detail,
    }
    # Payload is kept on the interaction so main.py can re-emit the banner if
    # the user replies with something that is neither yes nor no.
    fut = ctx.interaction.begin("confirm", payload=event)
    await ctx.emit(event)
    try:
        answer = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        ctx.interaction.cancel()
        await ctx.emit({"type": "confirm_timeout", "id": cid})
        return "timeout"
    return "yes" if _is_yesish(answer) else "no"


async def _confirm_plan(ctx: RunContext, items: list) -> str:
    """One approval banner for a whole batch of gated calls landing in the SAME
    turn (instead of N sequential banners). items: [(block, danger_label)].
    Returns "yes", "no", or "timeout" — the verdict applies to every item."""
    cid = uuid4().hex[:8]
    steps = [{"summary": _confirm_summary(b.name, dict(b.input or {})),
              "danger": danger} for b, danger in items]
    try:
        detail = _shown(json.dumps([{"tool": b.name, "args": dict(b.input or {})}
                                    for b, _ in items],
                                   indent=2, ensure_ascii=False, default=str), 4000)
    except Exception:
        detail = ""
    event = {"type": "confirm_request", "id": cid,
             "summary": f"Approve all {len(items)} gated actions of this step at once",
             "danger": f"{len(items)} actions batched",
             "detail": detail, "steps": steps}
    fut = ctx.interaction.begin("confirm", payload=event)
    await ctx.emit(event)
    try:
        answer = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        ctx.interaction.cancel()
        await ctx.emit({"type": "confirm_timeout", "id": cid})
        return "timeout"
    return "yes" if _is_yesish(answer) else "no"


# Appended to every error tool_result so the model self-repairs instead of
# retrying the identical failing call (see skills/self-repair.md).
_RECOVERY_SUFFIX = (
    "\n\n[RECOVERY PROTOCOL] Read the full error above — the root cause is usually stated. "
    "Then take a DIFFERENT approach: another tool (bash ↔ applescript ↔ click_ui ↔ "
    "see_screen), install the missing dependency (confirmation is automatic), or an "
    "alternate CLI. NEVER repeat the identical failing call. If this same step has failed "
    "3 times, stop and tell the user exactly what is blocking."
)


async def _run_one(ctx: RunContext, block, danger: str | None = None) -> str:
    """Execute a single approved tool_use block, emitting tool_start/tool_done.

    `danger` is the risk-gate label (non-None ⇒ the user confirmed it); it is
    recorded in the append-only audit log alongside the outcome.
    """
    args = dict(block.input or {})
    label = _label(block.name, args)
    call_key = _call_key(block.name, args)
    await ctx.emit({
        "type": "tool_start",
        "tool_id": block.id,
        "tool": block.name,
        "label": label,
    })
    ctx.trace.event("tool_start", tool=block.name, label=label)
    t0 = time.monotonic()
    # Loop-breaker: after N identical failures, refuse WITHOUT executing —
    # a stubborn model must not burn all 40 iterations on one broken call.
    if ctx.fail_counts.get(call_key, 0) >= _MAX_IDENTICAL_FAILURES:
        out = (f"BLOCKED: this exact {block.name} call has already failed "
               f"{_MAX_IDENTICAL_FAILURES} times — it will not be run again. Take a "
               "genuinely different approach, or stop and tell the user what is blocking.")
        ok = False
    else:
        timeout = _tool_timeout(block.name, args)
        try:
            handler = _HANDLERS.get(block.name)
            if handler is None:
                out = f"Error: unknown tool '{block.name}'"
            elif timeout is not None:
                out = await asyncio.wait_for(handler(args, ctx), timeout=timeout)
            else:
                out = await handler(args, ctx)
            ok = not out.startswith("Error")
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            out = (f"Error: {block.name} timed out after {int(timeout)}s and was "
                   "abandoned. The action may or may not have taken effect — verify "
                   "before retrying.")
            ok = False
        except Exception as e:
            out = f"Error: {e}"
            ok = False
    ctx.tools_used.add(block.name)
    # Sequence log for the routine cache. Oversized args (write_file payloads)
    # are replaced with a marker — such runs never become routines, but the
    # marker keeps identical runs hashing identically.
    if len(ctx.tool_seq) < 24:
        try:
            # dict() copy: handlers receive this same args object — aliasing
            # it into the stored sequence would let a mutation corrupt it.
            seq_args = dict(args) if len(json.dumps(args, default=str)) <= 2000 \
                else {"_oversize": True}
        except Exception:
            seq_args = {"_oversize": True}
        ctx.tool_seq.append({"tool": block.name, "args": seq_args,
                             "ok": ok, "out": out[:200]})
    learning.record_tool(block.name, ok, "" if ok else out[:200])
    # Track consecutive identical failures; any success clears the count.
    if ok:
        ctx.fail_counts.pop(call_key, None)
        if block.name in _ARTIFACT_TOOLS and len(ctx.artifacts) < 12:
            ctx.artifacts.append(f"{block.name}: {out[:150]}")
            ctx.ledger.cite("artifact", f"{block.name}:{call_key[-8:]}",
                            summary=out[:140])
    else:
        ctx.fail_counts[call_key] = ctx.fail_counts.get(call_key, 0) + 1
    # HUD + trace get the RAW result; the recovery protocol suffix is model
    # steering only and is appended after (never shown on the tool card).
    await ctx.emit({"type": "tool_done", "tool_id": block.id, "ok": ok,
                    "detail": out[:200]})
    ctx.trace.event("tool_done", tool=block.name, ok=ok,
                    duration_s=round(time.monotonic() - t0, 2), detail=out[:300])
    # Append-only security audit: risky calls log the full confirm summary,
    # routine ones the short label. `confirmed=True` since a declined call
    # never reaches here (see _run_tools).
    audit.record(block.name,
                 _confirm_summary(block.name, args) if danger else label,
                 ok=ok, danger=danger, confirmed=True if danger else None)
    if not ok:
        out += _RECOVERY_SUFFIX
    return out


async def _run_tools(ctx: RunContext, blocks: list) -> list[dict]:
    """Run all tool_use blocks of a turn. Returns one tool_result per block, in order.

    Risk gate runs sequentially first (one confirm at a time); approved
    non-interactive calls then execute concurrently via asyncio.gather.
    """
    results: dict[str, str] = {}
    approved: list = []
    dangers: dict[str, str] = {}   # block.id → risk label for confirmed calls

    async def _refuse(block, danger: str, verdict: str) -> None:
        results[block.id] = (
            "No response from the user — confirmation timed out and the call "
            "was NOT run. Finish with a report; do not retry this action."
            if verdict == "timeout"
            else "User declined. Choose an alternative approach or finish.")
        await ctx.emit({"type": "tool_start", "tool_id": block.id,
                        "tool": block.name,
                        "label": _label(block.name, dict(block.input or {}))})
        await ctx.emit({"type": "tool_done", "tool_id": block.id, "ok": False,
                        "detail": "Declined by user"})
        audit.record(block.name,
                     _confirm_summary(block.name, dict(block.input or {})),
                     ok=False, danger=danger, confirmed=False)

    # Pass 1: hard blocks, gate labels, plan pre-approvals.
    to_confirm: list = []          # (block, danger, destructive)
    for block in blocks:
        # HARD self-protection guard — refuse (never even ask) to move/delete
        # COSMOS's own directory, in ANY permission mode.
        blocked = _self_protection(block.name, dict(block.input or {}))
        if blocked:
            results[block.id] = (
                f"BLOCKED — {blocked} Never move, rename, or delete Cosmos's own "
                f"project directory. If organizing a folder that contains it, EXCLUDE "
                f"{FRIDAY_ROOT}.")
            await ctx.emit({"type": "tool_start", "tool_id": block.id,
                            "tool": block.name,
                            "label": _label(block.name, dict(block.input or {}))})
            await ctx.emit({"type": "tool_done", "tool_id": block.id, "ok": False,
                            "detail": "Blocked: self-protection"})
            audit.record(block.name, _confirm_summary(block.name, dict(block.input or {})),
                         ok=False, danger=blocked, confirmed=False)
            continue

        args = dict(block.input or {})
        # A tool with no handler can never run — fail it directly instead of
        # showing the user an approvable banner for an impossible call.
        if block.name not in _HANDLERS:
            results[block.id] = (f"Error: unknown tool '{block.name}' — not in "
                                 "Cosmos's registry.")
            await ctx.emit({"type": "tool_start", "tool_id": block.id,
                            "tool": block.name, "label": block.name})
            await ctx.emit({"type": "tool_done", "tool_id": block.id, "ok": False,
                            "detail": "Unknown tool"})
            continue
        # Read-only worker: refuse any acting/mutating call BEFORE the risk
        # gate. This is real enforcement, not the gate's mode-dependent
        # auto-decline (which lets ungated mutations through).
        if ctx.read_only:
            ro = _read_only_block(block.name, args)
            if ro:
                results[block.id] = f"Error: {ro}. Report what you found; the parent will act."
                await ctx.emit({"type": "tool_start", "tool_id": block.id,
                                "tool": block.name, "label": _label(block.name, args)})
                await ctx.emit({"type": "tool_done", "tool_id": block.id, "ok": False,
                                "detail": "Blocked: read-only worker"})
                continue
        danger = needs_confirmation(block.name, args, ctx.mode,
                                    unattended=ctx.unattended)
        if not danger:
            approved.append(block)
            continue
        destructive = _is_destructive(block.name, args)
        # propose_plan pre-approval: exact signature, single-use, and NEVER
        # for irreversible calls (those re-confirm no matter what).
        key = _call_key(block.name, args)
        if not destructive and key in ctx.preapproved:
            ctx.preapproved.discard(key)
            dangers[block.id] = danger
            approved.append(block)
            continue
        to_confirm.append((block, danger, destructive))

    # Pass 2: ≥2 batchable (non-destructive) gated calls in one turn get ONE
    # combined banner; destructive calls always confirm individually.
    batchable = [(b, d) for b, d, dest in to_confirm if not dest]
    singles   = [(b, d) for b, d, dest in to_confirm if dest]
    if len(batchable) >= 2:
        verdict = await _confirm_plan(ctx, batchable)
        for b, d in batchable:
            if verdict == "yes":
                dangers[b.id] = d
                approved.append(b)
            else:
                await _refuse(b, d, verdict)
    else:
        singles = batchable + singles
    for block, danger in singles:
        verdict = await _confirm(ctx, block.name, dict(block.input or {}), danger)
        if verdict != "yes":
            await _refuse(block, danger, verdict)
            continue
        dangers[block.id] = danger
        approved.append(block)

    # Interactive tools must be serialized (one pending future at a time)
    parallel = [b for b in approved if b.name not in ("ask_user", "propose_plan")]
    serial   = [b for b in approved if b.name in ("ask_user", "propose_plan")]

    if len(parallel) == 1:
        results[parallel[0].id] = await _run_one(ctx, parallel[0], dangers.get(parallel[0].id))
    elif parallel:
        # return_exceptions=True: one failing tool task must not abort the run
        # while its siblings keep executing orphaned with real side effects.
        outs = await asyncio.gather(*[_run_one(ctx, b, dangers.get(b.id)) for b in parallel],
                                    return_exceptions=True)
        for b, out in zip(parallel, outs):
            if isinstance(out, asyncio.CancelledError):
                raise out
            if isinstance(out, BaseException):
                results[b.id] = f"Error: {out}"
            else:
                results[b.id] = out
    for b in serial:
        results[b.id] = await _run_one(ctx, b, dangers.get(b.id))

    # Every tool_use block gets exactly one tool_result with the matching id.
    return [
        {"type": "tool_result", "tool_use_id": b.id,
         "content": results.get(b.id, "Error: tool did not run")}
        for b in blocks
    ]


_SUMMARY_PREFIX = "[CONVERSATION SUMMARY]"
_SUMMARY_ACK = "Understood, sir — context noted."


async def _summarize_evicted(prior: str, evicted: list[dict]) -> str:
    """Condense evicted turns (+ any prior summary) into ≤250 tokens of facts
    worth keeping: names, paths, decisions, unfinished threads. '' on failure."""
    lines = [f"{e['role']}: {str(e['content'])[:400]}" for e in evicted]
    prompt = (
        ("Prior summary:\n" + prior + "\n\n" if prior else "")
        + "Conversation turns being evicted from context:\n" + "\n".join(lines)
        + "\n\nWrite ONE compact summary paragraph (max ~120 words) preserving the "
          "facts a personal assistant must not forget: names, file paths, IDs, "
          "decisions made, preferences stated, and anything still unfinished. "
          "No preamble — just the summary.")
    try:
        resp = await llm.acreate(model=FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                                 max_tokens=250,
                                 messages=[{"role": "user", "content": prompt}])
        return llm.extract_text(resp).strip()
    except Exception:
        return ""


# Per-history roll locks so two overlapping background rolls can't stomp each
# other's rewrite (each re-reads the live list once it holds the lock). Keyed
# by id() — safe because a running roll task keeps its list alive; pruned when
# unlocked so id reuse can't pair a new session with a dead session's lock.
# (High prune threshold: eviction while a task holds a lock reference but
# hasn't acquired it yet would let two rolls run concurrently.)
_ROLL_LOCKS: dict[int, asyncio.Lock] = {}
_ROLL_TASKS: dict[int, asyncio.Task] = {}


def _roll_lock(history: list) -> asyncio.Lock:
    key = id(history)
    lock = _ROLL_LOCKS.get(key)
    if lock is None:
        if len(_ROLL_LOCKS) > 256:
            for k in [k for k, l in _ROLL_LOCKS.items() if not l.locked()]:
                _ROLL_LOCKS.pop(k, None)
        lock = _ROLL_LOCKS.setdefault(key, asyncio.Lock())
    return lock


async def settle_history(history: list) -> None:
    """Await any in-flight eviction roll for this history list (no-op if none).
    Callers that persist-and-drop the list (Slack threads reload per message)
    call this before their re-save so the roll's rewrite actually reaches
    disk instead of dying with the discarded list."""
    task = _ROLL_TASKS.get(id(history))
    if task is not None:
        try:
            await task
        except Exception:
            pass


async def _remember(history: list[dict], user_text: str, assistant_text: str) -> None:
    """Append the turn; when the cap is exceeded, ROLL the oldest turns into a
    summary pair instead of dropping them — long sessions keep their start.
    The roll's summarizer is an LLM call, so it runs DETACHED: the answer was
    already delivered and the run must not wait 1-3s (worst case: the whole
    fallback chain) on housekeeping. On any failure: hard truncate, as before."""
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    if len(history) <= _HISTORY_CAP:
        return
    try:
        key = id(history)
        task = asyncio.get_running_loop().create_task(_roll_history(history))
        _ROLL_TASKS[key] = task
        task.add_done_callback(lambda _t, k=key: _ROLL_TASKS.pop(k, None))
    except Exception:
        history[:] = history[-_HISTORY_CAP:]


async def _roll_history(history: list[dict]) -> None:
    """Background eviction roll. Re-reads the live list under the roll lock, so
    turns appended while a summary was rendering are never lost. (The summary
    is a user+assistant PAIR so role alternation survives for claude-*
    fallbacks.)"""
    async with _roll_lock(history):
        if len(history) <= _HISTORY_CAP:
            return
        entries = list(history)
        prior = ""
        if (entries and entries[0]["role"] == "user"
                and str(entries[0]["content"]).startswith(_SUMMARY_PREFIX)):
            prior = str(entries[0]["content"])[len(_SUMMARY_PREFIX):].strip()
            entries = entries[2:] if len(entries) > 1 else []   # drop summary pair
        keep = _HISTORY_CAP - 2                                  # newest turns kept verbatim
        evicted, recent = entries[:-keep], entries[-keep:]
        if not evicted:
            history[:] = entries[-_HISTORY_CAP:]
            return
        n_seen = len(history)
        try:
            summary = await asyncio.wait_for(
                _summarize_evicted(prior, evicted), timeout=8)
        except Exception:
            summary = ""
        # Anything appended while the summary rendered rides along untouched.
        tail = history[n_seen:]
        if summary:
            history[:] = ([{"role": "user", "content": f"{_SUMMARY_PREFIX} {summary}"},
                           {"role": "assistant", "content": _SUMMARY_ACK}]
                          + recent + tail)
        else:
            history[:] = (entries[-_HISTORY_CAP:] + tail)[-_HISTORY_CAP:]


async def _try_routine_replay(ctx: RunContext, user_text: str,
                              emit: Callable[[dict], Awaitable[None]],
                              trace: "RunTrace") -> str | None:
    """Replay a learned routine (services.routines) for this exact phrase.

    Steps run ONE AT A TIME through _run_tools — same risk gate, confirms,
    audit, self-protection, and events as a model-driven turn — then a single
    fast-model critic checks the results against the request. Returns the
    final spoken answer, or None to fall back to the full agent loop.
    Fallback is only taken while it is SAFE: once a non-read step has
    succeeded, a later failure returns an honest partial report instead of
    letting the loop redo (and double-fire) the earlier side effects.
    """
    seq = routines.lookup(user_text)
    if not seq:
        return None
    await emit({"type": "agent_thought",
                "text": f"Recognized routine — replaying {len(seq)} learned steps."})
    trace.event("routine_replay", steps=len(seq))
    base = len(ctx.tool_seq)
    for i, step in enumerate(seq):
        block = SimpleNamespace(type="tool_use", id=f"routine_{i}",
                                name=step.get("tool", ""),
                                input=step.get("args") or {})
        await _run_tools(ctx, [block])   # sequential — steps depend on order
        done = ctx.tool_seq[base:]
        # A declined/blocked call never reaches _run_one → missing entry.
        step_ok = len(done) == i + 1 and done[-1].get("ok")
        if step_ok:
            continue
        declined = len(done) != i + 1
        wrote = any(d.get("ok") and d.get("tool") not in _READ_ONLY_TOOLS
                    for d in done)
        trace.event("routine_abort", step=i, declined=declined, wrote=wrote)
        if declined:
            # The user said no (or the gate hard-blocked it) — stopping is
            # the answer; re-running the loop would just re-ask.
            return "Understood, sir — I've stopped that routine there."
        routines.invalidate(user_text)
        if wrote:
            return (f"Step {i + 1} of that routine ({step.get('tool')}) failed, "
                    f"sir — I stopped rather than redo the earlier steps. "
                    f"Say the word and I'll finish it properly.")
        return None   # nothing irreversible ran — the full loop takes over
    # All steps executed — one fast critic pass against the actual outputs.
    done = ctx.tool_seq[base:]
    digest = "\n".join(f"- {d['tool']}({json.dumps(d.get('args', {}))[:120]}): "
                       f"{d.get('out', '')[:160]}" for d in done)
    prompt = (f"The user asked: \"{user_text}\"\nA learned routine executed these "
              f"tools with these results:\n{digest}\n\nDid this fully accomplish "
              f"the request? Reply EXACTLY one line: 'DONE: <one short spoken "
              f"confirmation, JARVIS-style, addressing the user as sir>' or "
              f"'FAIL: <what went wrong>'.")
    try:
        resp = await llm.acreate(model=FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                                 max_tokens=150,
                                 messages=[{"role": "user", "content": prompt}])
        verdict = llm.extract_text(resp).strip()
    except Exception:
        verdict = ""
    if verdict.upper().startswith("DONE"):
        text = verdict.split(":", 1)[1].strip() if ":" in verdict else ""
        trace.event("routine_verified")
        return text or "Done, sir."
    trace.event("routine_verify_failed", verdict=verdict[:200])
    routines.invalidate(user_text)
    wrote = any(d.get("ok") and d.get("tool") not in _READ_ONLY_TOOLS
                for d in done)
    if wrote:
        # Side effects happened; the loop must not redo them. Honest summary.
        return (f"I ran the usual {len(done)}-step routine for that, sir, "
                f"though my self-check isn't fully satisfied — worth a glance.")
    return None


async def run_task(
    user_text: str,
    emit: Callable[[dict], Awaitable[None]],
    interaction: Interaction,
    history: list[dict] | None = None,
    mode: str = "ask",
    unattended: bool = False,
    depth: int = 0,
    max_iterations: int | None = None,
    token_budget: int | None = None,
    read_only: bool = False,
    model: str = "",
    on_answer: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Run one agentic task to completion. Returns the final spoken response text.

    `on_answer`, when given, is awaited with each candidate final answer the
    moment it exists — BEFORE the verify critic's 0.8-2.5s pass — so the caller
    can deliver it immediately. It fires a second time only if verify forced a
    corrective turn (the corrected answer); the return value always matches the
    last call. Budget/step-limit/error endings never fire it.

    `history` is the caller's (per-connection) conversation history list; it is
    read for context and appended to in place. `mode` is the permission mode
    ("ask" = guarded, "full" = only deletions confirm). `unattended` marks
    headless scheduled runs (no user watching — external tools always gate).
    `depth` > 0 marks a sub-agent worker: smaller default budgets and no
    recall/learning/verify side effects (the parent run owns those).
    `read_only` hard-refuses any mutating call (sub-agent workers).

    Emits protocol-v3 events via `emit`. Raises only asyncio.CancelledError
    (on stop); all other failures return a graceful error sentence.
    """
    if history is None:
        history = []
    iterations = max_iterations or MAX_ITERATIONS
    budget = token_budget or RUN_TOKEN_BUDGET
    trace = RunTrace()
    ctx = RunContext(emit=emit, interaction=interaction, trace=trace,
                     mode=mode if mode in ("ask", "full") else "ask",
                     unattended=unattended, depth=max(0, depth),
                     read_only=read_only)
    # Ambient context: what's frontmost right now, so "summarize this page" /
    # "reply to him" resolve without burning a tool turn on guessing. Skipped
    # for fast-model read lookups (they're latency-critical) — routing is
    # decided below, so probe only when the primary model will run — and for
    # sub-agent workers (their task text is self-contained by contract).
    _read = _is_read_lookup(user_text)
    focus = "" if (_read or ctx.depth) else await system_control.get_focus_context_cached()
    # Read lookups get the slim prefix (they also route to the fast tier
    # below). An explicit per-session model (Panel) keeps the full prompt —
    # those sessions do real work. Tool schemas stay FULL either way: routing
    # is a latency guess, and a mis-routed action command must keep its hands.
    system = _system_blocks(focus, profile="read" if (_read and not model) else "full")
    messages: list[dict] = [*history, {"role": "user", "content": user_text}]
    final_text = "Done, sir."
    run_t0 = time.monotonic()
    turns = 0
    tokens_spent = 0
    verify_retries = 0
    # Route pure read/lookups to the faster model tier (no side effects → cheaper
    # model is fine and noticeably quicker). `_read` was decided above. An
    # explicit `model` (per-session choice, e.g. Panel sessions) overrides
    # routing; the agent fallback chain still applies behind it.
    run_model     = model or (FAST_MODEL if _read else AGENT_MODEL)
    run_fallbacks = llm.AGENT_FALLBACKS if model else (
        llm.FAST_FALLBACKS if _read else llm.AGENT_FALLBACKS)
    trace.event("run_start", user_text=user_text, model=run_model, read_lookup=_read)

    # ── Routine replay (SPEED_PLAN 4.1) ────────────────────────────────────────
    # A phrase that earned routine status skips the agent loop entirely: its
    # exact tool sequence replays through the normal gate, then one fast-model
    # critic signs off. Attended, depth-0, primary-tier runs only. Any doubt
    # falls back to the loop below; the replay attempt guards itself.
    if (ctx.depth == 0 and not model and not _read
            and not ctx.unattended and not read_only):
        try:
            routine_final = await _try_routine_replay(ctx, user_text, emit, trace)
        except asyncio.CancelledError:
            interaction.cancel()
            raise
        except Exception:
            routine_final = None
        if routine_final is not None:
            if on_answer is not None:
                try:
                    await on_answer(routine_final)
                except Exception:
                    pass
            await emit({"type": "run_meta", "model": "routine-replay",
                        "elapsed_ms": int((time.monotonic() - run_t0) * 1000),
                        "turns": 0})
            trace.event("run_end", final_text=routine_final[:300], turns=0,
                        routine=True,
                        duration_s=round(time.monotonic() - run_t0, 2))
            recall_svc.record_run(user_text, routine_final,
                                  sorted(ctx.tools_used))
            # A successful replay reinforces the routine (same hash → count++).
            routines.observe(user_text, ctx.tool_seq, ok=True)
            remembered = routine_final
            if ctx.artifacts:
                remembered = (f"{routine_final}\n[task artifacts: "
                              f"{'; '.join(ctx.artifacts)[:600]}]")
            await _remember(history, user_text, remembered)
            return routine_final

    # Long-context routing (Lever 1): once the assembled context crosses the
    # threshold, upgrade this run's whole chain to the gateway's 1M-token
    # variant ("gpt-5.5" → "gpt-5.5[1m]"). Checked after each compaction so a
    # run that only grows huge mid-flight (giant tool_results) still switches;
    # the system prefix is fixed per run, so its char count is cached.
    _sys_chars = _system_char_len(system)
    longctx_upgraded = False

    async def _maybe_upgrade_longctx() -> None:
        nonlocal run_model, run_fallbacks, longctx_upgraded
        if longctx_upgraded or not llm.LONGCTX_ENABLED:
            return
        est = llm.estimate_tokens(
            *([" " * _sys_chars] + [str(m.get("content", "")) for m in messages]))
        if est < llm.LONGCTX_THRESHOLD_TOKENS:
            return
        upgraded = llm.long_context_variant(run_model)
        if upgraded == run_model and run_fallbacks == llm.long_context_chain(run_fallbacks):
            longctx_upgraded = True     # already on a windowed variant — don't re-check
            return
        run_model = upgraded
        run_fallbacks = llm.long_context_chain(run_fallbacks)
        longctx_upgraded = True
        trace.event("longctx_upgrade", est_tokens=est, model=run_model)
        await emit({"type": "agent_thought",
                    "text": f"Context is large (~{est // 1000}k tokens) — "
                            f"switching to the 1M-token window, sir."})

    async def _on_fallback(failed: str, nxt: str, exc: Exception) -> None:
        trace.event("fallback", failed=failed, next=nxt,
                    error=llm.sanitize_error(exc))
        await emit({"type": "agent_thought",
                    "text": f"{failed} unavailable — switching to {nxt}"})

    _delta_buf: list[str] = []

    async def _on_delta(chunk: str) -> None:
        # Live text for the HUD — shown as Cosmos "composing". The frontend
        # clears this transient bubble on the next tool_start / final response.
        _delta_buf.append(chunk)
        await emit({"type": "response_delta", "text": chunk})

    async def _generate():
        """One model turn. Streams (live deltas) with a non-streaming fallback
        if the stream dies mid-flight, so a run always completes."""
        common = dict(model=run_model, fallbacks=run_fallbacks,
                      on_fallback=_on_fallback, max_tokens=4096,
                      system=system, tools=_tools_for_request(), messages=messages)
        if llm.STREAM_ENABLED:
            _delta_buf.clear()
            try:
                return await llm.astream(on_delta=_on_delta, **common)
            except asyncio.CancelledError:
                raise
            except Exception:
                await emit({"type": "response_delta_reset"})
                # A mid-stream death wiped text the user was READING while the
                # silent (non-streaming) retry regenerates for up to 45s — put
                # the streamed prefix back on screen; the final response (or
                # the retry's own stream) supersedes it.
                if _delta_buf:
                    # restore=True: the FE re-displays without re-speaking the
                    # lead sentence (it was already spoken the first time).
                    await emit({"type": "response_delta",
                                "text": "".join(_delta_buf), "restore": True})
                return await llm.acreate(**common)
        return await llm.acreate(**common)

    try:
        for _ in range(iterations):
            n = _compact_messages(messages, query=user_text)
            if n:
                trace.event("compaction", blocks=n)
                ctx.ledger.cite("compressed", f"turn-{turns}",
                                summary=f"{n} old tool_results query-compressed")
            await _maybe_upgrade_longctx()
            _rotate_message_cache_marker(messages)
            turn_t0 = time.monotonic()
            resp = await _generate()
            turns += 1
            text_parts = [b.text for b in resp.content
                          if b.type == "text" and b.text]
            tool_uses  = [b for b in resp.content if b.type == "tool_use"]
            usage = getattr(resp, "usage", None)
            trace.event("llm_turn",
                        model=getattr(resp, "model", None),
                        latency_s=round(time.monotonic() - turn_t0, 2),
                        input_tokens=getattr(usage, "input_tokens", None),
                        output_tokens=getattr(usage, "output_tokens", None),
                        # Does the gateway actually cache our ~20k-token prefix
                        # for gpt-* models? These two fields settle it from real
                        # traffic (SPEED_PLAN 3.3 step 0) — non-null cache_read
                        # on turn 2+ means yes.
                        cache_read=getattr(usage, "cache_read_input_tokens", None),
                        cache_write=getattr(usage, "cache_creation_input_tokens", None),
                        stop_reason=getattr(resp, "stop_reason", None),
                        n_tools=len(tool_uses))
            # NEVER replay glm-5p2's 'thinking' blocks (or null-text blocks) into
            # history: they carry no signature, bloat context, and make every
            # later turn dramatically slower (4s→2s per turn, and worse as the
            # run grows). Keep only real text + the tool_use blocks tool_results
            # must pair against.
            clean_content = [b for b in resp.content
                             if b.type == "tool_use" or (b.type == "text" and b.text)]

            # Cumulative token budget — a runaway run must stop, not silently
            # burn the whole gateway allowance.
            if usage:
                tokens_spent += (getattr(usage, "input_tokens", 0) or 0) + \
                                (getattr(usage, "output_tokens", 0) or 0)
            if tokens_spent > budget:
                trace.event("budget_exceeded", tokens=tokens_spent)
                final_text = ("I've hit my compute budget for this task, sir — "
                              "stopping here before it runs away.")
                break

            if resp.stop_reason == "tool_use" and tool_uses:
                thought = " ".join(t.strip() for t in text_parts if t.strip())
                if thought:
                    await emit({"type": "agent_thought", "text": thought[:200]})
                messages.append({"role": "assistant", "content": clean_content})
                tool_results = await _run_tools(ctx, tool_uses)
                messages.append({"role": "user", "content": tool_results})
                continue

            final_text = " ".join(t.strip() for t in text_parts if t.strip()) or "Done, sir."
            # Perceived completion: hand the answer to the caller NOW — the
            # verify critic below costs 0.8-2.5s and passes almost always. On
            # the rare FAIL, a second call delivers the corrected answer after
            # the corrective turn. Still inside the run (and the run-lock).
            if on_answer is not None:
                try:
                    await on_answer(final_text)
                except Exception:
                    pass
            # Self-verification: before declaring done, a fast critic checks
            # the claim against the trace. At most ONE corrective re-entry —
            # skipped for fast-path reads and runs that did no real work.
            # A read-ROUTED run can still execute write tools (routing is a
            # latency guess, not an enforcement) — if it wrote, verify anyway.
            _wrote = not (ctx.tools_used <= _READ_ONLY_TOOLS)
            if (_VERIFY_ENABLED and (not _read or _wrote) and ctx.depth == 0
                    and verify_retries < _VERIFY_MAX_RETRIES
                    and (ctx.tools_used - _VERIFY_SKIP_TOOLS)):
                critique = await _verify(user_text, final_text, ctx)
                if critique:
                    verify_retries += 1
                    trace.event("verify_failed", critique=critique)
                    # The first answer already streamed to the HUD — reset the
                    # compose bubble so the corrected answer doesn't concatenate.
                    await emit({"type": "response_delta_reset"})
                    if on_answer is not None:
                        # The early-delivered answer is being retracted: put
                        # the HUD back in 'executing' (re-arms its watchdog +
                        # Esc-stop for the corrective window) and speak the
                        # retraction marker — the corrective lead follows it.
                        await emit({"type": "state", "state": "executing"})
                        await emit({"type": "speak", "text": "Correction, sir."})
                    await emit({"type": "agent_thought",
                                "text": f"Self-check: {critique[:160]} — correcting"})
                    # Replay ONLY text blocks: a truncated final turn (e.g.
                    # max_tokens mid-tool-call) can carry tool_use blocks that
                    # never got tool_results — replaying those dangling ids
                    # makes the next request a guaranteed 400.
                    retry_content = [b for b in clean_content if b.type == "text"]
                    messages.append({"role": "assistant",
                                     "content": retry_content
                                     or [{"type": "text", "text": final_text}]})
                    messages.append({"role": "user",
                                     "content": _VERIFY_RETRY_TMPL.format(critique=critique)})
                    continue
                trace.event("verify_passed")
            # Honest telemetry chip: which model actually answered, how long.
            await emit({"type": "run_meta",
                        "model": getattr(resp, "model", None) or run_model,
                        "elapsed_ms": int((time.monotonic() - run_t0) * 1000),
                        "turns": turns})
            break
        else:
            final_text = "I hit my step limit before finishing that one, sir."
        # Finalize the todo board: nothing else ever completes it — the model
        # rarely spends its FINAL turn on a set_todos call, so finished runs
        # left steps stuck at in_progress forever. A normal completion means
        # the plan ran; limit/budget endings stay honest (left as-is).
        if (ctx.todos
                and not final_text.startswith(("I hit my step limit",
                                               "I've hit my compute budget"))
                and any(t.get("status") != "completed" for t in ctx.todos)):
            ctx.todos = [{**t, "status": "completed"} for t in ctx.todos]
            await emit({"type": "todos", "todos": ctx.todos})
        trace.event("run_end", final_text=final_text[:300], turns=turns,
                    duration_s=round(time.monotonic() - run_t0, 2))
        # Context ledger: publish the exact evidence set this answer stood on
        # (recall rows, RAPTOR themes, tool artifacts, compressed blocks) — both
        # to the trace (idempotent retries) and to the HUD (inspectable grounding).
        if ctx.ledger.entries:
            entries = ctx.ledger.snapshot()
            trace.event("context_ledger", entries=entries)
            if ctx.depth == 0:
                try:
                    await emit({"type": "context_ledger", "entries": entries})
                except Exception:
                    pass
        if ctx.depth == 0:
            # Index the completed run so recall_history can find it later.
            # (Sub-agent workers skip these — the PARENT run owns the memory,
            # routing, and reflection side effects; N workers must not spam
            # recall/lessons with fragments of one job.)
            recall_svc.record_run(user_text, final_text, sorted(ctx.tools_used))
            # RAPTOR: rebuild the hierarchical summary tree when enough new runs
            # have accrued (cheap staleness check; the actual rebuild is rare and
            # fire-and-forget so it never delays the reply).
            try:
                asyncio.get_running_loop().create_task(raptor.maybe_rebuild())
            except Exception:
                pass
            # Learned routing: this phrasing is now KNOWN to be a pure read (or
            # not). A run with NO executed tools proves nothing about sends —
            # a declined/timed-out confirm leaves tools_used empty, and the old
            # unconditional record taught the router that send-commands were
            # "reads" (which also silently disabled their verify critic). With
            # no tools: an action-verb phrasing records False; a pure-chat
            # phrasing records nothing.
            if ctx.tools_used:
                learning.record_route(user_text,
                                      ctx.tools_used <= _READ_ONLY_TOOLS)
            elif _ACTION_VERB_RE.search(user_text.lower()):
                learning.record_route(user_text, False)
            # Bad run → fire-and-forget reflection into the lessons store.
            failed_tools = sum(1 for e in trace.events
                               if e.get("type") == "tool_done" and not e.get("ok"))
            bad = failed_tools >= 3 or final_text.startswith(
                ("I hit my step limit", "I've hit my compute budget"))
            # Routine cache: the Nth identical successful run of this exact
            # phrase + tool sequence earns direct replay next time (attended
            # runs only — scheduled prompts must not become routines).
            if not ctx.unattended and not _read:
                try:
                    routines.observe(user_text, ctx.tool_seq, ok=not bad)
                except Exception:
                    pass
            if bad:
                asyncio.get_running_loop().create_task(
                    _reflect(user_text, final_text, trace.summary()))
            elif not ctx.unattended:
                # Skill synthesis: the 3rd similar multi-step SUCCESSFUL run
                # earns a one-time "shall I save this as a skill?" chip.
                # NOT for unattended scheduled jobs — their emit sink is a
                # discarded buffer, so the suggestion would be burned into the
                # void (marked suggested=True, never seen) and the repeating
                # briefing/scan prompts would pollute the candidate store.
                try:
                    seq = [e.get("tool") for e in trace.events
                           if e.get("type") == "tool_start"]
                    tip = skill_synth.observe(user_text, seq)
                    if tip:
                        await emit({"type": "suggestion", "text": tip})
                except Exception:
                    pass
    except asyncio.CancelledError:
        trace.event("run_cancelled", turns=turns,
                    duration_s=round(time.monotonic() - run_t0, 2))
        interaction.cancel()
        raise
    except Exception as e:
        # sanitize_error: gateway errors embed key material — this string is
        # shown on the HUD, spoken aloud, AND stored in conversation history.
        final_text = (f"I ran into a problem and couldn't finish, sir — "
                      f"{llm.sanitize_error(e, 160)}")
        trace.event("run_error", error=llm.sanitize_error(e, 300), turns=turns,
                    duration_s=round(time.monotonic() - run_t0, 2))

    # History (not the spoken reply) also records the run's key tool outputs —
    # paths/IDs a follow-up command will reference ("send THAT photo…").
    remembered = final_text
    if ctx.artifacts:
        digest = "; ".join(ctx.artifacts)[:600]
        remembered = f"{final_text}\n[task artifacts: {digest}]"
    await _remember(history, user_text, remembered)
    return final_text

# COSMOS — the local developer agent that can rewrite its own code

COSMOS is a locally-hosted AI developer agent for your Mac: a voice-and-text
JARVIS-style webapp that drives your machine end-to-end — terminal, browser,
files, git, screen, integrations — powered by **GPT-5.6** through the OpenAI
API. Everything runs on `localhost`; your data, credentials and actions never
leave your machine except for the model calls themselves.

Its signature feature is **Mutate**: COSMOS reads its own activity log and
flight recorder, diagnoses its own failures, proposes fixes to **its own source
code**, and — with one click — patches itself, proves the new code boots, runs
its own tests, and restarts itself live *without dying* (same PID, the UI just
reconnects). You can also just tell it what to change about itself, frontend
included.

## Features

| Panel | What it does |
|---|---|
| **Agent** | The main loop: voice/text commands → observe-think-act agent (≤40 turns) with ~50 tools — shell, files, browser control (CDP), screenshots, vision, Git/GitHub, web search, documents, macOS control (AppleScript/clicks/keys), TTS replies |
| **Mutate** | Self-healing: scans its own failure evidence → proposals → test-gated hot self-patch → live restart that survives. User-suggested mutations too |
| **Nexus** | Live mind-map of everything COSMOS knows: systems, people, threads |
| **Dossier** | Per-person files built from your comms: promises made, tasks owed, context |
| **Vision** | Watch regions of any webpage or your screen; alert/react when they change (reflexes) |
| **Kinesis** | Record → understand → replay UI macros (semantic, not pixel-blind) |
| **Panel** | Multi-agent swarm board: spawn parallel worker agents on a task list |
| **Skills** | Markdown playbooks injected into the agent's prompt; AI-editable in-app |
| **Slack** | Drive COSMOS remotely from a Slack channel (Socket Mode bridge) |
| **Connectors** | Slack, Google Workspace, ElevenLabs voice, and any **MCP server** you add |
| **Memory** | Long-term memory + lessons learned from failed runs + semantic recall (embeddings) |

Under the hood: model fallback chains with cooldowns, streaming with mid-run
model failover, prompt-cache-friendly context assembly, a risk gate that
requires confirmation for dangerous actions, an append-only audit trail, undo
snapshots, and a per-run JSONL flight recorder.

## Requirements

- **macOS** (Apple Silicon or Intel) — COSMOS drives the Mac itself; that's the point
- **Python 3.11+**, **Node 18+**, and (recommended) **Homebrew**
- An **OpenAI API key** with GPT-5.6 access
- Optional: `brew install imagesnap` (webcam), `ffmpeg` (recording), Google Chrome (browser control + Vision)

## Quick start

```bash
git clone <this-repo> cosmos && cd cosmos

# 1. Configure — only OPENAI_API_KEY is required
cp backend/.env.example backend/.env
${EDITOR:-nano} backend/.env        # paste your OpenAI key

# 2. Launch (creates the venv, installs deps, builds the HUD, starts everything)
./start.sh

# 3. Open http://localhost:8000 and say "Cosmos wake up" — or just type.
```

`start.sh` is idempotent: it reaps any previous instance, installs only what
changed, and serves the production HUD from the backend on **:8000**. For
frontend development with hot reload, run `COSMOS_DEV=1 ./start.sh` (Vite on
:5173).

### First-run macOS permissions (TCC)

COSMOS inherits permissions from the **terminal app you launch it from**. In
System Settings › Privacy & Security, grant your terminal:

- **Accessibility** (clicks/keystrokes), **Screen Recording** (see the screen)
- **Camera / Microphone** (photos, recordings — optional)
- **Automation** prompts appear on first use per app — click Allow

If a capability fails with a permission error, fix the grant and retry — the
agent names the missing permission in its error message.

## Configuration guide (`backend/.env`)

| Section | Vars | Notes |
|---|---|---|
| **OpenAI** (required) | `OPENAI_API_KEY` | Powers agent, fast paths, Mutate, embeddings. `OPENAI_BASE_URL` optional for compatible gateways |
| Models | `FRIDAY_AGENT_MODEL` (gpt-5.6), `FRIDAY_FAST_MODEL` (gpt-5.6-mini), `FRIDAY_AGENT_FALLBACKS`, `FRIDAY_REASONING_EFFORT` | Defaults are sensible; override to taste |
| Voice | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | Optional — falls back to the macOS `say` voice |
| You | `USER_NAME`, `USER_EMAIL`, `USER_SLACK_HANDLE` | Personalises prompts, Slack, Dossier |
| Slack bridge | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_BRIDGE_CHANNEL`, `SLACK_BRIDGE_OWNER` | See below |
| Google | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Gmail/Calendar tools |
| Tuning | `FRIDAY_*` knobs | Timeouts, budgets, prewarm — documented inline in `.env.example` |

### Slack bridge (optional)

1. api.slack.com/apps → **Create New App → From manifest** → paste
   `slack-app-manifest.yaml`.
2. Enable **Socket Mode**; create an app-level token with `connections:write`
   (`SLACK_APP_TOKEN`, `xapp-…`).
3. Install to your workspace → `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Create a channel, invite the bot, put its ID in `SLACK_BRIDGE_CHANNEL` and
   your member ID in `SLACK_BRIDGE_OWNER`. Restart COSMOS — messages in that
   channel now run the agent, with confirmations in-thread.

### Google Workspace (optional)

Create an OAuth **Desktop app** in Google Cloud Console (Gmail + Calendar
scopes), then obtain a refresh token for your account (any standard OAuth
helper works) and fill the three `GOOGLE_*` vars.

### MCP connectors (optional)

Copy `backend/mcp.example.json` to `~/.friday/mcp.json`, flip a server to
`enabled: true`, and say "mcp reload" (or restart). Any stdio or HTTP MCP
server works; tools appear in the agent automatically, gated by the same
risk-confirmation system as built-in tools.

## Using Mutate (the self-healing panel)

1. Open **Mutate** from the top bar.
2. **Scan my blunders** — COSMOS reads its audit log, run traces, tool-health
   stats and lessons, and proposes code-level fixes to itself (it refuses to
   propose fixes for things that aren't code bugs, like missing OS permissions).
3. Or type your own change into **Suggest a mutation** — backend or frontend
   ("make the orb pulse red on errors" works).
4. Hit **FIX**. Pipeline: bounded patch loop writes a minimal diff → per-file
   backups → gates (`py_compile` → fresh-subprocess `import main` boot proof →
   targeted pytest → `npm run build` if frontend) → live re-exec (same PID; the
   page reconnects) → post-restart confirmation. Any gate failure rolls
   everything back automatically.

## Architecture

```
Browser HUD (React 18 + Vite + three.js, WS protocol v3)
      ↓  ws://localhost:8000/ws
FastAPI backend :8000 (main.py — run-lock, fast-paths, WS session reattach)
      ↓
agent loop (services/agent.py — observe/think/act, risk gate, self-verify)
      ↓
OpenAI API (gpt-5.6 → fallback chain)  +  ~50 tools  +  MCP servers
      ↓
macOS: shell / AppleScript / clicks / screenshots / Chrome CDP / files / git
```

- **Backend**: Python 3.11, FastAPI, ~50 service modules under
  `backend/services/` (one concern per module, heavily commented).
- **Frontend**: React 18 + TypeScript + Vite, no UI framework — hand-built HUD.
- **State**: everything lives in `~/.friday/` (audit log, traces, memory,
  mutations, caches). Delete that directory for a factory reset.
- **Tests**: `cd backend && .venv/bin/python -m pytest` (~40 files, no network:
  LLM calls are faked at the client seam).

## Security model

- Binds to `127.0.0.1` only (`FRIDAY_HOST` to override — don't).
- Dangerous tool calls (deletes, sends, purchases, system changes) hit a risk
  gate and require an explicit yes in the UI.
- Append-only audit trail of every executed tool at `~/.friday/audit.jsonl`;
  pre-overwrite file snapshots enable undo.
- Mutate can only edit files inside the repo, never `.env`/secrets; every
  change is gated by tests and reversible from per-mutation backups.
- Secrets live in `backend/.env` (gitignored). Never commit it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "already running (pid …)" on boot | `./start.sh` reaps old instances; if it persists: `pkill -9 -f main.py` |
| Photos/recording denied | Launch from a terminal that has Camera/Mic/Screen Recording grants (permissions follow the launching app) |
| Vision preview "debug port never came up" | Fixed automatically (orphan Chrome reap); if you see it, re-run `./start.sh` |
| First reply is slow | Prewarm is on by default (`FRIDAY_PREWARM=1`); the first call after long idle pays model cold-start |
| Voice input missing | Web Speech API needs Chrome; use the text box otherwise |

## License

MIT (see `LICENSE`).

# COSMOS — a local developer agent with real control of your Mac

COSMOS is a locally-hosted AI developer agent for your Mac: a voice-and-text
JARVIS-style webapp that drives your machine end-to-end — terminal, browser,
files, git, screen, integrations — powered by **GPT-5.6** through the OpenAI
API. Everything runs on `localhost`; your data, credentials and actions never
leave your machine except for the model calls themselves.

**Control is the core.** The agent loop runs ~50 real tools against the actual
machine, not a sandbox: it runs shell commands, edits files, drives Chrome over
CDP, takes and reasons about screenshots, clicks and types via AppleScript,
works your Git repos, searches the web, and reads and writes your Google
Workspace and Slack. Dangerous actions pass a risk gate, every action lands in
an append-only audit trail, and destructive ones leave undo snapshots.

Built on top of that control are five capabilities that make it a genuine
developer tool rather than a chat window:

| | |
|---|---|
| **Control** | ~50 tools over shell, files, Git, browser (CDP), screen, macOS UI, and your connectors — with a risk gate, audit trail and undo |
| **Panel** | A multi-agent swarm board — spawn parallel worker agents across a task list and watch them work simultaneously |
| **Vision** | Reflexes: watch a region of your screen or any webpage and act automatically when it changes |
| **Kinesis** | Record a UI workflow once, then replay it semantically — it understands the steps rather than replaying blind pixel coordinates |
| **Mutate** | Self-healing: reads its own flight recorder, diagnoses its own failures, patches **its own source code**, proves the new code boots, runs its own tests, and restarts live without dying (same PID — the UI just reconnects) |

Plus **Nexus** (a live mind-map of what it knows), **Dossier** (per-person
files built from your comms), **Skills** (markdown playbooks it can edit
itself), and long-term **Memory** with semantic recall.

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
| Google | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Gmail, Calendar, Docs, Sheets, Meet, Drive — see the OAuth Playground walkthrough below |
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

### Google Workspace (optional) — Gmail, Calendar, Docs, Sheets, Meet

Auth is a **single OAuth2 refresh token** (`services/google.py`): COSMOS
exchanges it for a short-lived access token at
`https://oauth2.googleapis.com/token` on demand. There is no browser consent
flow at runtime and no token file on disk — you mint the refresh token once and
paste it into `backend/.env`.

The quickest way to mint one is **Google's OAuth 2.0 Playground**, using your
*own* client credentials.

**1 — Create the Cloud project and enable the APIs**

Go to [console.cloud.google.com](https://console.cloud.google.com) → create (or
pick) a project → **APIs & Services › Library** → enable each API you want:

| API | Enables |
|---|---|
| Gmail API | search/read mail, drafts, send |
| Google Calendar API | list/create/delete events |
| Google Docs API | create, read, edit docs |
| Google Sheets API | create sheets, read/write ranges |
| Google Meet API | create Meet spaces |
| Google Drive API | file lookup/listing |

A tool whose API is not enabled fails with a clear error and the agent falls
back — enabling only Gmail + Calendar is a perfectly valid setup.

**2 — Configure the consent screen**

**APIs & Services › OAuth consent screen** → **External** → fill the app name
and your email. Leave it in **Testing** and add your own Google account under
**Test users**. (In Testing mode refresh tokens expire after 7 days — see the
note below.)

**3 — Create the OAuth client — must be "Web application"**

**Credentials › Create Credentials › OAuth client ID › Web application**.

Under **Authorised redirect URIs** add exactly:

```
https://developers.google.com/oauthplayground
```

> This is why it must be a **Web application** client, not a Desktop one — the
> Playground redirects back to that URI, and Google rejects the exchange if it
> isn't registered on the client you're using.

Copy the **Client ID** and **Client secret**.

**4 — Mint the refresh token in the OAuth Playground**

Open [developers.google.com/oauthplayground](https://developers.google.com/oauthplayground).

1. Click the **⚙ gear** (top right) → tick **Use your own OAuth credentials** →
   paste your Client ID and Client secret.
2. In **Step 1**, paste these scopes into the "Input your own scopes" box
   (space-separated) — trim to match the APIs you enabled:

```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/meetings.space.created
```

3. **Authorize APIs** → sign in with the account you added as a test user →
   **Allow**. (You'll see an "unverified app" warning — expected for your own
   Testing-mode client: **Advanced › Go to <app> (unsafe)**.)
4. In **Step 2**, click **Exchange authorization code for tokens**.
5. Copy the **Refresh token** (starts with `1//`).

**5 — Fill `backend/.env` and restart**

```bash
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxx
GOOGLE_REFRESH_TOKEN=1//xxxxx
```

Then `./start.sh` and ask COSMOS *"what's on my calendar today?"* to confirm.

**Scope notes**

- `gmail.modify` covers search/read/draft; `gmail.send` is separate and is what
  actually sends. Drop `gmail.send` if you want draft-only (safer for a demo).
- `drive.file` only grants access to files COSMOS itself creates. Use
  `https://www.googleapis.com/auth/drive` instead if you want it to reach
  pre-existing files — broader, so only if you need it.
- Adding a scope later means re-running step 4 — the refresh token's scopes are
  fixed at mint time.

**Token expiry**

While the consent screen is in **Testing**, Google expires refresh tokens after
**7 days** — you'll see `invalid_grant` and need to repeat step 4. To get a
non-expiring token, **Publish** the app on the consent screen (staying in
Testing is fine for a demo or hackathon; just re-mint if it's been a week).

> Treat the refresh token like a password — it grants ongoing access to your
> mail and calendar. It lives only in `backend/.env`, which is gitignored and
> never committed.

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

## How this was built

**Codex wrote this codebase.** Every part of COSMOS in this repository was
authored with OpenAI Codex — the ~50 Python service modules under
`backend/services/`, the FastAPI app and WebSocket protocol v3 in
`backend/main.py`, the Mutate self-healing engine (`services/mutate.py`), the
React HUD under `frontend/src/`, and the test suite under `backend/tests/`.
Work was driven prompt-by-prompt, module by module, with each module reviewed
and iterated against its tests before moving on. The commit history in this
repo groups that output by subsystem so it can be read in order.

**GPT-5.6 runs it.** The same model family that wrote the code is the runtime
brain at execution time:

| Where | Model | What it does |
|---|---|---|
| Agent loop (`services/agent.py:43`) | `FRIDAY_AGENT_MODEL` → **gpt-5.6** | The observe-think-act loop: picks among ~50 tools, passes the risk gate, self-verifies its own results |
| Fast paths (`agent.py:44`, `raptor.py`, `promises.py`, `slack.py`) | `FRIDAY_FAST_MODEL` → **gpt-5.6** | Cheap classification, routing and short rewrites that don't need the full loop |
| Fallback chain (`services/llm.py:59-61`) | `FRIDAY_AGENT_FALLBACKS` / `FRIDAY_FAST_FALLBACKS` → **gpt-5.6-mini** | On rate-limit or error, `llm.py` fails over down the chain with a per-model cooldown — including mid-stream failover |
| Mutate scan (`mutate.py:62`) | `FRIDAY_MUTATE_SCAN_MODEL` → fast model | Reads the flight recorder and audit log, proposes what to fix |
| Mutate patch (`mutate.py:63`) | `FRIDAY_MUTATE_MODEL` → **gpt-5.6** | Writes the actual patches to COSMOS's own source, behind the boot + test gates |
| Subagents (`services/subagent.py`) | inherits the agent model | Parallel scoped agents spawned by the main loop |
| Embeddings (`services/embeddings.py:25`) | `FRIDAY_EMBED_MODEL` → `text-embedding-3-small` | Semantic recall and the RAPTOR memory tree |

Every model id is an env override — the defaults above are what ships in
`backend/.env.example`.

So the loop closes: Codex wrote the agent, and the agent uses GPT-5.6 to
rewrite the code Codex wrote.

## Testing

The suite is **offline** — every OpenAI call is faked at the client seam in
`services/llm.py`, so no API key and no network access are needed to run it.
Nothing touches `~/.friday/`; tests use temp dirs.

```bash
# after ./start.sh has built the venv:
cd backend && .venv/bin/python -m pytest

# or from a clean checkout, without running start.sh:
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

Expected: **699 passed** across 43 files in ~5s.

```
699 passed in 4.65s
```

Useful invocations:

```bash
.venv/bin/python -m pytest tests/test_mutate.py      # one module
.venv/bin/python -m pytest -k "risk or verify"       # by name
.venv/bin/python -m pytest -x -vv                    # stop at first failure, verbose
```

`pytest.ini` sets `asyncio_mode = auto`, so `async def test_*` works with no
per-test marker. What's covered: the agent's risk gate and permission modes,
the full Mutate pipeline (scan → patch → boot gate → test gate → restart),
LLM fallback/cooldown and streaming failover, memory/recall, and each
integration's translation layer.

### Verified environments

| | Version | Result |
|---|---|---|
| macOS | 15 (Darwin 25.5, Apple Silicon) | 699 passed |
| Python | 3.14.0 | 699 passed |
| Python | 3.11+ | minimum supported |
| Node | 18+ | required for the HUD build |

The backend tests are pure Python and platform-independent; the *runtime*
features they exercise (AppleScript, clicks, screenshots, Chrome CDP) are
macOS-only, which is why COSMOS itself is macOS-only.

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

#!/usr/bin/env bash
set -e

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗"
echo -e "║        COSMOS AI — BOOT SEQUENCE     ║"
echo -e "╚══════════════════════════════════════╝${NC}"
echo ""

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_DIR="$ROOT_DIR/backend"

# ── 0. Reap any previous COSMOS processes ──────────────────────────────────────
# start.sh backgrounds its services; without this, re-running it stacks a new
# backend on top of the old ones, and each one runs its OWN scheduler (every cron
# job fires N times → N× the LLM spend → gateway 429s).
#
# The reap MUST escalate to SIGKILL. A backend wedged mid-LLM-call ignores
# SIGTERM, and the old reap only sent TERM (pkill's default) plus a -9 to
# whatever held :8000 — so a wedged backend that had LOST the port race escaped
# BOTH and survived for days. That is exactly how three stale backends piled up.
echo -e "${YELLOW}[0/3] Stopping any running COSMOS instance...${NC}"
# The settle sleeps only run when something was actually there to reap —
# the common clean boot used to pay 2s for nothing.
if pkill -f "[m]ain.py" 2>/dev/null; then sleep 1; fi
# Anything that ignored SIGTERM gets SIGKILL — no survivors, port or no port.
pkill -9 -f "[m]ain.py" 2>/dev/null || true
# Vision's poll/preview Chromes hold ~/.friday/chrome-watch's profile
# singleton. A -9'd backend leaks them (PPID 1), and ONE survivor bricks every
# later Vision launch ("debug port never came up") — reap them at boot too.
pkill -9 -f "user-data-dir=$HOME/.friday/chrome-watch" 2>/dev/null || true
STALE_PIDS="$( { lsof -ti :8000; lsof -ti :5173; } 2>/dev/null | tr '\n' ' ' )"
if [ -n "${STALE_PIDS// /}" ]; then
  echo "$STALE_PIDS" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

# ── 1. Python venv + deps ──────────────────────────────────────────────────────
echo -e "${YELLOW}[1/3] Checking Python backend...${NC}"
if [ ! -d "$BACKEND_DIR/.venv" ]; then
  echo "  Creating virtual environment..."
  python3 -m venv "$BACKEND_DIR/.venv"
fi
VENV_PY="$BACKEND_DIR/.venv/bin/python"
# pip's no-op resolve still takes ~0.5s+ — skip it entirely unless
# requirements.txt actually changed since the last successful install.
REQ_STAMP="$BACKEND_DIR/.venv/.requirements.sha"
REQ_HASH="$(shasum "$BACKEND_DIR/requirements.txt" | cut -d' ' -f1)"
if [ "$(cat "$REQ_STAMP" 2>/dev/null)" != "$REQ_HASH" ]; then
  "$VENV_PY" -m pip install -q -r "$BACKEND_DIR/requirements.txt"
  echo "$REQ_HASH" > "$REQ_STAMP"
fi
echo -e "${GREEN}  [OK] Backend dependencies ready${NC}"

# ── 2. Node deps ───────────────────────────────────────────────────────────────
echo -e "${YELLOW}[2/3] Checking frontend dependencies...${NC}"
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "  Installing npm packages (this takes ~30s)..."
  cd "$FRONTEND_DIR" && npm install --silent
fi
echo -e "${GREEN}  [OK] Frontend dependencies ready${NC}"

# ── 3. Launch ──────────────────────────────────────────────────────────────────
# Default: PRODUCTION mode — build the HUD if stale and let the backend serve
# it from :8000 (minified, no StrictMode double-render, no Vite proxy hop on
# /api or the first-audio-critical /api/tts/stream). COSMOS_DEV=1 restores the
# Vite dev server on :5173 with HMR.
COSMOS_DEV="${COSMOS_DEV:-0}"
if [ "$COSMOS_DEV" != "1" ]; then
  if [ ! -f "$FRONTEND_DIR/dist/index.html" ] || \
     [ -n "$(find "$FRONTEND_DIR/src" "$FRONTEND_DIR/index.html" "$FRONTEND_DIR/package.json" \
              -newer "$FRONTEND_DIR/dist/index.html" -print -quit 2>/dev/null)" ]; then
    echo -e "${YELLOW}  Building frontend (production)...${NC}"
    (cd "$FRONTEND_DIR" && npm run build) || \
      echo -e "${YELLOW}  [WARN] Build failed — serving the previous dist/ if present${NC}"
  fi
fi

echo -e "${YELLOW}[3/3] Starting services...${NC}"
echo ""
if [ "$COSMOS_DEV" = "1" ]; then
  echo -e "${CYAN}  Backend  → http://localhost:8000"
  echo -e "  Frontend → http://localhost:5173 (dev/HMR)${NC}"
else
  echo -e "${CYAN}  COSMOS   → http://localhost:8000${NC}"
fi
echo ""

# Shut down for real on Ctrl+C. Registered BEFORE anything is launched (the trap
# used to be installed after, leaving a window where Ctrl+C orphaned whatever had
# already started), and it ESCALATES: the old handler sent a single SIGTERM and
# let the script exit immediately, so a backend still shutting down — or wedged
# mid-LLM-call — was orphaned and lived forever.
BACKEND_PID=""
FRONTEND_PID=""

_alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

cleanup() {
  trap - INT TERM                      # re-entrant Ctrl+C must not re-run this
  echo ""
  echo -e "${YELLOW}Shutting down COSMOS...${NC}"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  # Give them ~5s to exit gracefully (uvicorn drains in-flight work), then force.
  for _ in $(seq 1 10); do
    _alive "$BACKEND_PID" || _alive "$FRONTEND_PID" || break
    sleep 0.5
  done
  kill -9 "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  # vite spawns its own children, and a wedged backend may still hold a port —
  # verify by port rather than trusting the pids.
  lsof -ti :8000 2>/dev/null | xargs kill -9 2>/dev/null || true
  lsof -ti :5173 2>/dev/null | xargs kill -9 2>/dev/null || true
  # Belt and braces: nothing named main.py may outlive this script.
  pkill -9 -f "[m]ain.py" 2>/dev/null || true
  echo -e "${GREEN}COSMOS offline.${NC}"
  exit 0
}
trap cleanup INT TERM

# Start backend (serves the built HUD itself in production mode)
cd "$BACKEND_DIR"
"$VENV_PY" main.py &
BACKEND_PID=$!

# Dev only: the Vite server with HMR
if [ "$COSMOS_DEV" = "1" ]; then
  cd "$FRONTEND_DIR"
  npm run dev &
  FRONTEND_PID=$!
fi

OPEN_URL="http://localhost:8000"
[ "$COSMOS_DEV" = "1" ] && OPEN_URL="http://localhost:5173"
echo -e "${GREEN}╔══════════════════════════════════════╗"
echo -e "║  COSMOS IS ONLINE                    ║"
echo -e "║  Open: ${OPEN_URL}         ║"
echo -e "║                                      ║"
echo -e "║  Say: \"Cosmos wake up\" to begin      ║"
echo -e "╚══════════════════════════════════════╝${NC}"
echo ""
echo "Press Ctrl+C to shut down."

# The trap is already armed (see cleanup() above). `wait` is interrupted by the
# signal, cleanup() runs to completion, and it exits — so the script never
# returns while a child is still alive.
wait

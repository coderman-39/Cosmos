"""Mutate — COSMOS's self-modification engine (the Mutate panel).

COSMOS reads its own failure evidence (audit trail, run traces, tool-health
stats, lessons), proposes fixes to its OWN codebase, and — on user approval —
applies them live with a test-gated hot apply:

    proposed → analyzing → patching → testing → restarting → applied
                                   ↘ failed / rolled_back

The "without dying" contract, in order of defence:
  1. Every touched file is backed up BEFORE the first edit
     (~/.friday/mutations/<id>/backup/…) — rollback works on a dirty git tree
     and is executed by the OLD, still-running process image.
  2. Gate A: `py_compile` every changed .py file.
  3. Gate B: a FRESH subprocess runs `import main` in backend/ — proof the new
     image can boot before we bet the live process on it.
  4. Gate C: targeted pytest (tests/test_<name>.py for each changed
     services/<name>.py), hard timeout — never the full suite inline.
  5. Gate D: frontend touched → `npm run build` must succeed (also refreshes
     the dist/ the backend serves).
  6. Apply = os.execv: SAME pid, so we stay start.sh's child, keep the
     terminal's TCC grants (a detached relauncher would lose camera/mic/screen
     permissions — see main.py's FRIDAY_RELOAD comment), and the flock
     singleton releases automatically (CLOEXEC fd) for the new image.
  7. A restart marker is written pre-exec; main.py's boot hook finding it means
     "came back alive" — the final verification, surfaced on the panel.

The patch loop is DELIBERATELY not agent.run_task: no run-lock, no mouse, no
40-turn budget — a small bounded read/edit loop restricted to the repo tree.
"""

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from services import atomicio, audit, learning, llm

_DIR           = Path.home() / ".friday"
MUTATIONS_FILE = _DIR / "mutations.json"
WORK_DIR       = _DIR / "mutations"
RESTART_MARKER = _DIR / "mutate_restart.json"

REPO_ROOT    = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR  = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
TRACE_DIR    = _DIR / "traces"
AUDIT_FILE   = _DIR / "audit.jsonl"

# sys.executable IS backend/.venv/bin/python when launched via start.sh; fall
# back to it explicitly so gates work even from an odd launch context.
_VENV_PY = str(BACKEND_DIR / ".venv" / "bin" / "python")
VENV_PY  = _VENV_PY if Path(_VENV_PY).exists() else sys.executable

SCAN_MODEL  = os.getenv("FRIDAY_MUTATE_SCAN_MODEL", os.getenv("FRIDAY_FAST_MODEL", llm.DEFAULT_MODEL))
PATCH_MODEL = os.getenv("FRIDAY_MUTATE_MODEL", llm.DEFAULT_MODEL)

_MAX_MUTATIONS   = 40          # store bound
_MAX_TURNS       = 22          # patch-loop turns
_MAX_FILES       = 8           # distinct files one mutation may touch
_MAX_TOTAL_CHARS = 250_000     # total chars written across the mutation
_MAX_LOG_LINES   = 80          # per-mutation progress log bound
_DIFF_CAP        = 20_000      # stored unified-diff chars

# Paths the patch loop may never read OR write. `.env` holds live credentials —
# it must not enter model context (proposals/diffs are shown on the HUD and may
# end up in a public demo video), let alone be edited.
_FORBIDDEN_PARTS = {".venv", "node_modules", ".git", "__pycache__", "dist",
                    ".pytest_cache"}
_FORBIDDEN_NAMES = {".env", "mcp.json"}

_REPO_MAP = """\
Repo layout (a locally-hosted agent webapp — FastAPI backend + React HUD):
  backend/main.py            FastAPI app: /api/* endpoints, WS /ws (protocol v3), serves frontend/dist
  backend/services/agent.py  the agent loop (run_task, TOOLS list, risk gate, self-verify)
  backend/services/llm.py    model chain: acreate/astream with fallbacks + cooldowns
  backend/services/*.py      audit, learning, memory, scheduler, tts, watchers, slack_bridge,
                             mcp_client, panel, vision, kinesis, nexus, dossier, mutate (THIS feature) …
  backend/skills/*.md        playbooks injected into the agent's system prompt
  backend/tests/test_*.py    pytest suite (tests/test_<name>.py covers services/<name>.py)
  frontend/src/App.tsx       page router; store.ts = zustand store (Page union type)
  frontend/src/components/   one .tsx per page/widget (PageShell, NavMenu, *Page.tsx)"""


# ─── Store ─────────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        data = json.loads(MUTATIONS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(muts: list[dict]) -> None:
    if not atomicio.write_json_atomic(MUTATIONS_FILE, muts[-_MAX_MUTATIONS:], indent=1):
        print("[mutate] save failed (non-fatal)")


def list_all() -> list[dict]:
    """Newest first, for the panel."""
    return sorted(_load(), key=lambda m: m.get("created", ""), reverse=True)


def get(mid: str) -> dict | None:
    return next((m for m in _load() if m.get("id") == mid), None)


def _update(mid: str, **fields) -> dict | None:
    muts = _load()
    for m in muts:
        if m.get("id") == mid:
            m.update(fields)
            m["updated"] = datetime.now().isoformat(timespec="seconds")
            _save(muts)
            return m
    return None


def _log(mid: str, line: str) -> None:
    """Append one timestamped progress line (drives the panel's timeline)."""
    muts = _load()
    for m in muts:
        if m.get("id") == mid:
            log = m.setdefault("log", [])
            log.append(f"{datetime.now().strftime('%H:%M:%S')}  {line}")
            m["log"] = log[-_MAX_LOG_LINES:]
            m["updated"] = datetime.now().isoformat(timespec="seconds")
            _save(muts)
            print(f"[mutate:{mid}] {line}")
            return


def _new(title: str, diagnosis: str, fix_hint: str, source: str,
         area: str = "backend", confidence: float = 0.5,
         evidence: list[str] | None = None) -> dict:
    m = {
        "id": uuid4().hex[:8],
        "title": (title or "").strip()[:120],
        "diagnosis": (diagnosis or "").strip()[:1000],
        "fix_hint": (fix_hint or "").strip()[:1000],
        "source": source,                     # "auto" | "user"
        "area": area if area in ("backend", "frontend", "either") else "either",
        "confidence": max(0.0, min(1.0, float(confidence or 0))),
        "evidence": [str(e)[:300] for e in (evidence or [])][:6],
        "status": "proposed",
        "files": [],                          # [{path, action: modified|created}]
        "diff": "",
        "log": [],
        "error": "",
        "created": datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    muts = _load()
    muts.append(m)
    _save(muts)
    return m


def suggest(text: str) -> dict:
    """A user-typed change request becomes a proposal immediately — no scan."""
    text = (text or "").strip()
    if not text:
        return {"error": "Empty suggestion."}
    title = text.splitlines()[0][:90]
    return _new(title=title, diagnosis="User-requested change.",
                fix_hint=text[:1000], source="user", area="either",
                confidence=0.9)


def dismiss(mid: str) -> dict:
    m = _update(mid, status="dismissed")
    return m or {"error": f"No mutation {mid}."}


def busy() -> str | None:
    """Id of the mutation currently being fixed, or None."""
    return _active_mid


# ─── Evidence: read COSMOS's own failure record ────────────────────────────────

def _tail_jsonl(path: Path, max_lines: int) -> list[dict]:
    """Last N parsed lines of a JSONL file. Reads at most ~512KB from the end."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512_000))
            raw = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in raw.splitlines()[-max_lines:]:
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                out.append(e)
        except Exception:
            continue
    return out


def _audit_failures(cap: int = 30) -> list[str]:
    out = []
    for e in _tail_jsonl(AUDIT_FILE, 400):
        if e.get("ok") is False:
            out.append(f"[audit {e.get('ts', '')}] tool {e.get('tool')} FAILED: "
                       f"{e.get('summary', '')}")
    return out[-cap:]


def _trace_errors(days: int = 2, cap: int = 25) -> list[str]:
    """run_error + failed tool_done events from the newest trace files."""
    out: list[str] = []
    try:
        day_dirs = sorted(TRACE_DIR.iterdir(), reverse=True)[:days]
    except Exception:
        return []
    for day in day_dirs:
        try:
            runs = sorted(day.glob("*.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:30]
        except Exception:
            continue
        for run in runs:
            for e in _tail_jsonl(run, 120):
                t = e.get("type")
                if t == "run_error":
                    out.append(f"[trace {run.stem}] run_error: "
                               f"{str(e.get('error', e))[:250]}")
                elif t == "tool_done" and e.get("ok") is False:
                    out.append(f"[trace {run.stem}] tool {e.get('tool')} failed: "
                               f"{str(e.get('detail', ''))[:200]}")
            if len(out) >= cap * 2:
                break
    return out[-cap:]


def gather_evidence() -> list[str]:
    ev = _audit_failures()
    ev += _trace_errors()
    for t in learning.degraded_tools():
        ev.append(f"[tool-health] {t}")
    for l in learning.top_lessons(8):
        ev.append(f"[lesson] {l}")
    return ev


# ─── Scanner: evidence → proposals ─────────────────────────────────────────────

_SCAN_SYSTEM = f"""You are the self-repair analyst inside COSMOS, a locally-hosted \
developer-agent webapp. Below is COSMOS's own recent failure evidence, collected \
from its audit trail, run traces, tool-health stats and learned lessons.

{_REPO_MAP}

Propose up to 4 fixes to COSMOS'S OWN SOURCE CODE that would prevent these \
failures from recurring. Only propose a fix when the root cause is plausibly in \
COSMOS's code (backend Python or frontend TypeScript). NEVER propose fixes for: \
macOS permission (TCC) problems, external-service outages, rate limits, missing \
API keys, or one-off user errors — those are not code bugs.

Reply with a STRICT JSON array, nothing else:
[{{"title": "short imperative title",
   "diagnosis": "what is going wrong and why, grounded in the evidence",
   "fix_hint": "concrete code-level direction: which file/function, what change",
   "area": "backend" or "frontend",
   "confidence": 0.0-1.0,
   "evidence": ["verbatim evidence lines that support this"]}}]
Return [] if nothing qualifies."""


def _parse_json_array(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _is_dupe(title: str, existing: list[dict]) -> bool:
    t = (title or "").lower()
    for m in existing:
        if m.get("status") == "dismissed":
            continue
        if difflib.SequenceMatcher(None, m.get("title", "").lower(), t).ratio() > 0.72:
            return True
    return False


async def scan() -> dict:
    """Read the failure evidence, ask the model for proposals, dedup, store."""
    evidence = gather_evidence()
    if not evidence:
        return {"added": 0, "message": "No failure evidence found — nothing to learn from yet."}
    resp = await llm.acreate(
        model=SCAN_MODEL, fallbacks=llm.FAST_FALLBACKS, max_tokens=2000,
        system=_SCAN_SYSTEM,
        messages=[{"role": "user", "content": "EVIDENCE:\n" + "\n".join(evidence[-60:])}])
    proposals = _parse_json_array(llm.extract_text(resp))
    existing = _load()
    added = []
    for p in proposals[:4]:
        if not p.get("title") or _is_dupe(p["title"], existing + added):
            continue
        added.append(_new(title=p.get("title", ""), diagnosis=p.get("diagnosis", ""),
                          fix_hint=p.get("fix_hint", ""), source="auto",
                          area=p.get("area", "backend"),
                          confidence=p.get("confidence", 0.5),
                          evidence=p.get("evidence") or []))
    return {"added": len(added), "evidence_lines": len(evidence),
            "message": f"Scanned {len(evidence)} evidence lines → {len(added)} new proposal(s)."}


# ─── Patch loop: bounded read/edit agent restricted to the repo ────────────────

def _resolve(path_str: str) -> Path | None:
    """Repo-relative or absolute path → resolved Path inside the repo, or None
    if it escapes the repo / hits a forbidden dir or file."""
    try:
        p = Path(path_str)
        p = (REPO_ROOT / p).resolve() if not p.is_absolute() else p.resolve()
        p.relative_to(REPO_ROOT)                      # raises if outside
    except Exception:
        return None
    parts = set(p.parts)
    if parts & _FORBIDDEN_PARTS or p.name in _FORBIDDEN_NAMES:
        return None
    return p


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT))


_PATCH_TOOLS = [
    {"name": "list_dir",
     "description": "List one directory of the COSMOS repo (files + subdirs).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "read_file",
     "description": "Read a repo file. Large files are truncated; pass offset to page.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "offset": {"type": "integer",
                                                "description": "Start char, default 0."}},
                      "required": ["path"]}},
    {"name": "replace_in_file",
     "description": ("Edit a repo file by exact-string replacement. `old_text` must occur "
                     "EXACTLY ONCE in the file (include surrounding lines to disambiguate). "
                     "Preferred over write_file for existing files."),
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "write_file",
     "description": "Create a new repo file, or fully overwrite a small existing one.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "done",
     "description": "Finish the patch. Summarise what you changed and why it fixes the issue.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                      "required": ["summary"]}},
]

_PATCH_SYSTEM = f"""You are COSMOS patching YOUR OWN source code. Work like a careful \
senior engineer shipping a minimal, reviewable fix.

{_REPO_MAP}

Rules — all hard:
- Explore first (list_dir/read_file), understand the real code, THEN edit.
- Minimal diff. Match the file's existing style, naming and comment density.
- Max {_MAX_FILES} files. Never delete files. No new dependencies.
- Never touch .env, mcp.json, .venv, node_modules, .git, dist, or anything outside the repo.
- Backend is Python 3.11 / FastAPI; frontend is React 18 + TypeScript (strict) + Vite.
- Your patch will be gated: py_compile → fresh-subprocess `import main` → targeted \
pytest → (frontend) `npm run build`. A failure rolls everything back — so keep edits \
consistent and import-safe.
- When the fix is complete, call done(summary). Do not call done before editing."""


class _PatchState:
    def __init__(self, mid: str):
        self.mid = mid
        self.backup_dir = WORK_DIR / mid / "backup"
        self.changed: dict[str, str] = {}     # relpath → "modified" | "created"
        self.written = 0                      # chars written, for the cap
        self.summary = ""


def _backup(st: _PatchState, p: Path) -> None:
    """First-touch backup preserving the repo-relative tree."""
    rel = _rel(p)
    if rel in st.changed:
        return
    dest = st.backup_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copy2(p, dest)
        st.changed[rel] = "modified"
    else:
        st.changed[rel] = "created"


def _exec_patch_tool(st: _PatchState, name: str, args: dict) -> str:
    """Run one patch-loop tool. Returns the tool_result string (errors are
    strings too — the model self-corrects)."""
    if name == "done":
        st.summary = (args.get("summary") or "").strip()[:1500]
        return "ok"

    p = _resolve(str(args.get("path", "")))
    if p is None:
        return "Error: path is outside the repo or in a forbidden location (.env/.venv/node_modules/.git/dist)."

    if name == "list_dir":
        if not p.is_dir():
            return f"Error: {_rel(p) if p.exists() else args.get('path')} is not a directory."
        try:
            rows = []
            for c in sorted(p.iterdir()):
                if c.name in _FORBIDDEN_PARTS or c.name.startswith(".") and c.name != ".gitignore":
                    continue
                rows.append(f"{c.name}/" if c.is_dir() else f"{c.name} ({c.stat().st_size}b)")
            return "\n".join(rows) or "(empty)"
        except Exception as e:
            return f"Error: {e}"

    if name == "read_file":
        if not p.is_file():
            return f"Error: no file {args.get('path')}."
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error: {e}"
        off = max(0, int(args.get("offset") or 0))
        chunk = text[off:off + 24_000]
        note = f"\n…[truncated at {off + len(chunk)}/{len(text)} chars — re-call with offset]" \
            if off + len(chunk) < len(text) else ""
        return chunk + note

    if name == "replace_in_file":
        if not p.is_file():
            return f"Error: no file {args.get('path')} — use write_file to create files."
        old, new = args.get("old_text", ""), args.get("new_text", "")
        if not old:
            return "Error: old_text is empty."
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return f"Error: {e}"
        n = text.count(old)
        if n == 0:
            return "Error: old_text not found — read_file and copy the exact text."
        if n > 1:
            return f"Error: old_text occurs {n} times — include more surrounding context."
        if len(st.changed) >= _MAX_FILES and _rel(p) not in st.changed:
            return f"Error: file cap reached ({_MAX_FILES})."
        st.written += len(new)
        if st.written > _MAX_TOTAL_CHARS:
            return "Error: total write budget exhausted."
        _backup(st, p)
        try:
            p.write_text(text.replace(old, new, 1), encoding="utf-8")
        except Exception as e:
            return f"Error: {e}"
        return f"ok — edited {_rel(p)}"

    if name == "write_file":
        content = args.get("content", "")
        if len(st.changed) >= _MAX_FILES and (not p.exists() or _rel(p) not in st.changed):
            return f"Error: file cap reached ({_MAX_FILES})."
        st.written += len(content)
        if st.written > _MAX_TOTAL_CHARS:
            return "Error: total write budget exhausted."
        _backup(st, p)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except Exception as e:
            return f"Error: {e}"
        return f"ok — wrote {_rel(p)} ({len(content)} chars)"

    return f"Error: unknown tool {name}."


async def _run_patch_loop(m: dict, st: _PatchState) -> bool:
    """Bounded tool loop. True when done() was called after at least one edit."""
    task = (f"ISSUE: {m['title']}\n\nDIAGNOSIS: {m['diagnosis']}\n\n"
            f"FIX DIRECTION: {m['fix_hint']}\n")
    if m.get("evidence"):
        task += "\nEVIDENCE:\n" + "\n".join(m["evidence"])
    messages: list[dict] = [{"role": "user", "content": task}]
    for turn in range(_MAX_TURNS):
        resp = await llm.acreate(model=PATCH_MODEL, fallbacks=llm.AGENT_FALLBACKS,
                                 max_tokens=8000, system=_PATCH_SYSTEM,
                                 messages=messages, tools=_PATCH_TOOLS)
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        clean = [b for b in resp.content
                 if b.type == "tool_use" or (b.type == "text" and b.text)]
        messages.append({"role": "assistant", "content": clean or [
            {"type": "text", "text": "(continuing)"}]})
        if resp.stop_reason != "tool_use" or not tool_uses:
            # Model stopped talking instead of calling done — nudge once, then bail.
            if turn < _MAX_TURNS - 1 and not st.changed:
                messages.append({"role": "user", "content":
                                 "Use the tools to implement the fix, then call done()."})
                continue
            break
        results = []
        for b in tool_uses:
            out = _exec_patch_tool(st, b.name, b.input or {})
            if b.name in ("replace_in_file", "write_file") and out.startswith("ok"):
                _log(st.mid, f"patch: {out}")
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": out[:26_000]})
        messages.append({"role": "user", "content": results})
        if any(b.name == "done" for b in tool_uses):
            break
    return bool(st.changed) and bool(st.summary)


def _build_diff(st: _PatchState) -> str:
    chunks = []
    for rel, action in st.changed.items():
        new_p = REPO_ROOT / rel
        old_text = ""
        if action == "modified":
            try:
                old_text = (st.backup_dir / rel).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        try:
            new_text = new_p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            new_text = ""
        diff = difflib.unified_diff(old_text.splitlines(keepends=True),
                                    new_text.splitlines(keepends=True),
                                    fromfile=f"a/{rel}", tofile=f"b/{rel}")
        chunks.append("".join(diff))
    return "".join(chunks)[:_DIFF_CAP]


# ─── Gates ─────────────────────────────────────────────────────────────────────

def _run_cmd(cmd: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    """Blocking subprocess (call via asyncio.to_thread). Never raises."""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout)
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        return r.returncode == 0, out[-4000:]
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _gates(mid: str, st: _PatchState) -> tuple[bool, str]:
    """Run every applicable gate in order. Returns (ok, failure_log)."""
    changed_py = [r for r in st.changed if r.endswith(".py")]
    backend_touched = any(r.startswith("backend/") for r in st.changed)
    frontend_touched = any(r.startswith("frontend/") for r in st.changed)

    if changed_py:
        _log(mid, f"gate: py_compile on {len(changed_py)} file(s)")
        ok, out = await asyncio.to_thread(
            _run_cmd, [VENV_PY, "-m", "py_compile",
                       *[str(REPO_ROOT / r) for r in changed_py]], BACKEND_DIR, 60)
        if not ok:
            return False, f"py_compile failed:\n{out}"

    if backend_touched:
        _log(mid, "gate: fresh-subprocess `import main` (boot check)")
        ok, out = await asyncio.to_thread(
            _run_cmd, [VENV_PY, "-c", "import main"], BACKEND_DIR, 120)
        if not ok:
            return False, f"boot check (`import main`) failed:\n{out}"

    tests = []
    for r in changed_py:
        name = Path(r).stem
        cand = BACKEND_DIR / "tests" / f"test_{name}.py"
        if cand.exists():
            tests.append(str(cand))
    if tests:
        _log(mid, f"gate: pytest {len(tests)} targeted file(s)")
        ok, out = await asyncio.to_thread(
            _run_cmd, [VENV_PY, "-m", "pytest", "-q", "-x", *tests], BACKEND_DIR, 300)
        if not ok:
            return False, f"targeted pytest failed:\n{out}"
    elif changed_py:
        _log(mid, "gate: no matching test files — skipping pytest")

    if frontend_touched:
        _log(mid, "gate: npm run build (frontend)")
        ok, out = await asyncio.to_thread(
            _run_cmd, ["npm", "run", "build"], FRONTEND_DIR, 480)
        if not ok:
            return False, f"npm run build failed:\n{out}"

    return True, ""


def _rollback(st: _PatchState) -> None:
    """Executed by the OLD process image — works even if the patch broke mutate.py."""
    for rel, action in st.changed.items():
        target = REPO_ROOT / rel
        try:
            if action == "created":
                target.unlink(missing_ok=True)
            else:
                shutil.copy2(st.backup_dir / rel, target)
        except Exception as e:
            print(f"[mutate] rollback of {rel} failed: {e}")


# ─── Fix pipeline ──────────────────────────────────────────────────────────────

_active_mid: str | None = None
_fix_task: asyncio.Task | None = None


async def start_fix(mid: str) -> dict:
    """Kick off the fix as a background task; the panel polls for progress."""
    global _active_mid, _fix_task
    if _active_mid:
        return {"error": f"A mutation ({_active_mid}) is already being applied."}
    m = get(mid)
    if not m:
        return {"error": f"No mutation {mid}."}
    if m["status"] not in ("proposed", "failed", "rolled_back"):
        return {"error": f"Mutation is {m['status']} — can only fix from "
                         "proposed/failed/rolled_back."}
    _active_mid = mid
    _fix_task = asyncio.create_task(_fix(mid))
    return {"ok": True, "id": mid, "status": "analyzing"}


async def _fix(mid: str) -> None:
    global _active_mid
    st = _PatchState(mid)
    try:
        m = _update(mid, status="analyzing", error="", diff="", files=[])
        if m is None:
            _log(mid, "failed: mutation vanished from the store")
            return
        _log(mid, "analyzing: gathering context")
        _update(mid, status="patching")
        _log(mid, f"patching with {PATCH_MODEL} (bounded loop, ≤{_MAX_TURNS} turns)")
        done = await _run_patch_loop(m, st)
        if not st.changed:
            _update(mid, status="failed", error="Patch loop made no changes.")
            _log(mid, "failed: no changes were made")
            return
        files = [{"path": r, "action": a} for r, a in st.changed.items()]
        _update(mid, files=files, diff=_build_diff(st))
        if not done:
            _log(mid, "warning: loop ended without done() — gating anyway")

        _update(mid, status="testing")
        ok, fail_log = await _gates(mid, st)
        if not ok:
            _log(mid, "gates failed — rolling back")
            _rollback(st)
            if any(r.startswith("frontend/") for r in st.changed):
                _log(mid, "rebuilding frontend after rollback")
                await asyncio.to_thread(_run_cmd, ["npm", "run", "build"],
                                        FRONTEND_DIR, 480)
            _update(mid, status="rolled_back", error=fail_log[:4000])
            audit.record("mutate", f"rolled back '{m['title']}': gates failed", ok=False)
            return

        backend_touched = any(r.startswith("backend/") for r in st.changed)
        summary = st.summary or m["title"]
        audit.record("mutate", f"applied '{m['title']}' "
                     f"({len(st.changed)} file(s)): {summary[:150]}", ok=True)
        if backend_touched:
            _update(mid, status="restarting", note=summary)
            _log(mid, "all gates green — restarting into the new code")
            _write_restart_marker(mid)
            loop = asyncio.get_running_loop()
            loop.call_later(1.2, _do_execv)     # let the panel poll this state first
        else:
            _update(mid, status="applied", note=summary)
            _log(mid, "applied (frontend rebuilt — reload the HUD to see it)")
    except Exception as e:
        err = llm.sanitize_error(e, cap=300)
        _log(mid, f"failed: {err}")
        try:
            if st.changed:
                _rollback(st)
                _log(mid, "rolled back after error")
                _update(mid, status="rolled_back", error=err)
            else:
                _update(mid, status="failed", error=err)
        except Exception:
            pass
    finally:
        _active_mid = None


# ─── Restart machinery ─────────────────────────────────────────────────────────

def _write_restart_marker(mid: str) -> None:
    atomicio.write_json_atomic(RESTART_MARKER, {
        "id": mid, "ts": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid()})


def _do_execv() -> None:
    """Replace this process image with a fresh backend. Same PID: still
    start.sh's child (its `wait` keeps waiting), TCC grants survive, and the
    CLOEXEC flock fd releases the singleton for the new image."""
    print("[mutate] ⟳ re-exec: loading mutated code (same pid, HUD will reattach)")
    try:
        from services import singleton
        singleton.release()
    except Exception:
        pass
    try:
        os.chdir(BACKEND_DIR)
        os.execv(VENV_PY, [VENV_PY, "main.py"])
    except Exception as e:
        # execv failed — we are STILL the old, working image. Surface it.
        print(f"[mutate] execv failed ({e}) — still running the previous code")
        try:
            marker = json.loads(RESTART_MARKER.read_text())
            _update(marker.get("id", ""), status="failed",
                    error=f"execv failed: {e}")
            RESTART_MARKER.unlink(missing_ok=True)
        except Exception:
            pass


def on_boot() -> str | None:
    """main.py calls this at startup. A marker present means the previous image
    exec'd into us and we booted — the mutation survived. Final verification."""
    try:
        if not RESTART_MARKER.exists():
            return None
        marker = json.loads(RESTART_MARKER.read_text())
        RESTART_MARKER.unlink(missing_ok=True)
        mid = marker.get("id", "")
        m = _update(mid, status="applied")
        if m:
            _log(mid, "survived restart — mutation is live ✓")
            return f"mutation {mid} applied and survived restart"
    except Exception as e:
        print(f"[mutate] on_boot marker handling failed: {e}")
    return None

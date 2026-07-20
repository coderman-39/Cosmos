"""Background scheduler — Cosmos acts WITHOUT a live command.

Jobs live in ~/.friday/jobs.json:
  {id, prompt, when: "YYYY-MM-DDTHH:MM" (one-shot) | cron: "m h dom mon dow",
   enabled, last_run, created}

A 30s tick loop runs due jobs headless through the normal agent loop:
  - events are buffered, never streamed (no client may be attached)
  - a fresh, isolated history per job (never the live conversation)
  - confirms AUTO-DECLINE instantly — an unattended job must never take a
    destructive/outward gated action; it reports what it wanted instead
  - results are broadcast to any connected HUD clients AND always delivered
    as a native notification
  - missed schedules (laptop asleep) run once on wake, not N times

The default job — a weekday 9am briefing — is seeded on first run.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from croniter import croniter

from services import agent, atomicio, system_control

JOBS_FILE = Path.home() / ".friday" / "jobs.json"

TICK_S = 30.0

# The pre-card briefing prompt — kept so existing installs can be upgraded
# in place (jobs.json survives deploys; the seed only runs on first boot).
_LEGACY_BRIEFING_PROMPT = (
    "Morning briefing, keep it tight: today's weather, today's calendar "
    "events, and comms_summary (unread Gmail/Slack). Lead with "
    "anything urgent.")

_DEFAULT_BRIEFING = {
    "prompt": (
        "Morning briefing. Gather these IN PARALLEL (batch the tool calls in one "
        "turn): today's calendar (calendar action=events scope=today), Slack "
        "mentions needing my reply (slack action=mentions), unread DMs (slack "
        "action=unreads), GitHub PRs awaiting my review (github args: search prs "
        "--review-requested=@me --state=open), my open promises (promises "
        "action=list), and the weather in one line.\n"
        "Then write a RANKED briefing, not a data dump, as markdown:\n"
        "## Needs you today — max 5 items, most urgent first, one line each with WHY "
        "it matters and who's waiting.\n"
        "## FYI — 2-3 lines of ambient awareness.\n"
        "Close with one line: weather + first calendar event time.\n"
        "Skip empty sections. If a source fails, say 'couldn't check X' in FYI — "
        "never fake data. Whole briefing under 20 lines."),
    "cron": "0 9 * * 1-5",
    "deliver": "card",
}

# Periodic scan jobs (meeting prep) end with this exact sentinel when there is
# nothing to deliver — the run is then silent: no card, no notification.
NOTHING_SENTINEL = "NOTHING_TO_REPORT"

_PREP_SEED_MARKER = Path.home() / ".friday" / ".prep-seeded"
_SWEEP_SEED_MARKER = Path.home() / ".friday" / ".promise-sweep-seeded"

_PROMISE_SWEEP_JOB = {
    "id": "promise-sweep",
    "prompt": (
        "Promise sweep (quiet background job). Run promises action=sweep. "
        "If it reports NEW promises, or any open promise is overdue (due "
        "today/earlier, or made 3+ days ago), output a short markdown list "
        "of what needs chasing — most overdue first. Otherwise reply exactly "
        f"{NOTHING_SENTINEL} and nothing else."),
    "cron": "15 10,14,18 * * 1-5",
    "deliver": "card",
    "quiet": True,
}

_MEETING_PREP_JOB = {
    "id": "meeting-prep-scan",
    "prompt": (
        "Meeting prep scan (quiet background job). Check today's calendar "
        "(calendar action=events scope=today). Find meetings WITH OTHER "
        "ATTENDEES starting within the NEXT 30 MINUTES. If there are none, "
        f"reply exactly {NOTHING_SENTINEL} and do nothing else. "
        "Also check recall_history for a prep brief already written for the "
        f"same meeting today — if found, reply exactly {NOTHING_SENTINEL}. "
        "Otherwise, for each such meeting build a prep brief: who's attending, "
        "your most recent Slack "
        "exchange with each attendee (slack action=read), any open PRs "
        "involving them (github search), and anything recall_history has on "
        "the meeting topic. Output markdown: '## Prep: <title> @ <time>' then "
        "ONE paragraph of context and 3-5 bullets of talking points/open "
        "threads. Read-only — never send anything. If the calendar can't be "
        f"read at all, reply exactly {NOTHING_SENTINEL} (don't report the error)."),
    "cron": "*/30 9-18 * * 1-5",
    "deliver": "card",
    "quiet": True,
}

_task: asyncio.Task | None = None
_running_job = False


def _load() -> list[dict]:
    try:
        jobs = json.loads(JOBS_FILE.read_text())
        return jobs if isinstance(jobs, list) else []
    except Exception:
        return []


def _save(jobs: list[dict]) -> bool:
    """Persist the job list. Returns True on success — callers MUST check it:
    a job whose last_run didn't reach disk will re-fire on the very next tick."""
    ok = atomicio.write_json_atomic(JOBS_FILE, jobs, indent=1)
    if not ok:
        print("[scheduler] save failed (non-fatal): could not persist jobs.json")
    return ok


def _seed_default() -> None:
    if not JOBS_FILE.exists():
        _save([{
            "id": "morning-briefing",
            "prompt": _DEFAULT_BRIEFING["prompt"],
            "cron": _DEFAULT_BRIEFING["cron"],
            "deliver": _DEFAULT_BRIEFING["deliver"],
            "enabled": True,
            "last_run": "",
            "created": datetime.now().isoformat(timespec="seconds"),
        }])
        print("[scheduler] seeded default weekday 9am briefing (edit/cancel any time)")
    else:
        _upgrade_legacy_briefing()
    _seed_once(_MEETING_PREP_JOB, _PREP_SEED_MARKER,
               "meeting-prep scan (every 30min, weekdays 9-18)")
    _seed_once(_PROMISE_SWEEP_JOB, _SWEEP_SEED_MARKER,
               "promise sweep (3x weekdays)")


def _seed_once(job: dict, marker: Path, desc: str) -> None:
    """One-time seed of a background job (marker file, NOT job presence: a
    user who cancels the job must not get it re-seeded every boot)."""
    if marker.exists():
        return
    jobs = _load()
    if not any(j.get("id") == job["id"] for j in jobs):
        if not _save(jobs + [{**job, "enabled": True, "last_run": "",
                              "created": datetime.now().isoformat(timespec="seconds")}]):
            # Don't drop the marker: the seed didn't land, so a later boot must
            # be allowed to retry it (marking it seeded here loses it forever).
            print(f"[scheduler] could not seed {desc} — will retry next boot")
            return
        print(f"[scheduler] seeded {desc} — cancel any time")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:
        pass


def _upgrade_legacy_briefing() -> None:
    """Existing installs carry the pre-card briefing in jobs.json — upgrade it
    in place ONLY if the user never customized the prompt."""
    jobs = _load()
    changed = False
    for j in jobs:
        if (j.get("id") == "morning-briefing"
                and j.get("prompt") == _LEGACY_BRIEFING_PROMPT):
            j["prompt"] = _DEFAULT_BRIEFING["prompt"]
            j["deliver"] = _DEFAULT_BRIEFING["deliver"]
            changed = True
    if changed:
        _save(jobs)
        print("[scheduler] upgraded morning briefing to the ranked card format")


# ─── Job CRUD (used by the agent tools) ────────────────────────────────────────

def add_job(prompt: str, when: str = "", cron: str = "") -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "Error: a scheduled task needs a prompt."
    if bool(when) == bool(cron):
        return "Error: give exactly one of when (ISO, one-shot) or cron (repeating)."
    if when:
        try:
            datetime.fromisoformat(when)
        except Exception:
            return f"Error: bad when '{when}' — use YYYY-MM-DDTHH:MM."
    if cron and not croniter.is_valid(cron):
        return f"Error: invalid cron expression '{cron}'."
    jobs = _load()
    jid = uuid.uuid4().hex[:8]
    jobs.append({"id": jid, "prompt": prompt, "when": when, "cron": cron,
                 "enabled": True, "last_run": "",
                 "created": datetime.now().isoformat(timespec="seconds")})
    # Never claim a schedule that didn't reach disk — it would silently vanish
    # on the next tick (which re-reads jobs.json).
    if not _save(jobs):
        return "Error: couldn't write jobs.json — the task was NOT scheduled."
    sched = when if when else f"cron '{cron}'"
    return f"Scheduled [{jid}] {sched}: {prompt[:120]}"


def list_jobs() -> str:
    jobs = _load()
    if not jobs:
        return "No scheduled tasks."
    lines = []
    for j in jobs:
        sched = j.get("when") or f"cron {j.get('cron')}"
        state = "" if j.get("enabled", True) else " (disabled)"
        last = f" — last ran {j['last_run']}" if j.get("last_run") else ""
        lines.append(f"[{j['id']}] {sched}{state}: {j.get('prompt', '')[:100]}{last}")
    return "\n".join(lines)


def cancel_job(job_id: str) -> str:
    jobs = _load()
    kept = [j for j in jobs if j.get("id") != job_id]
    if len(kept) == len(jobs):
        return f"No job with id '{job_id}'. Use list_scheduled to see ids."
    if not _save(kept):
        return (f"Error: couldn't write jobs.json — [{job_id}] is still "
                f"scheduled and WILL run.")
    return f"Cancelled scheduled task [{job_id}]."


# ─── Due-ness ──────────────────────────────────────────────────────────────────

def _is_due(job: dict, now: datetime) -> bool:
    if not job.get("enabled", True):
        return False
    if job.get("when"):
        if job.get("last_run"):
            return False                      # one-shot already ran
        try:
            return datetime.fromisoformat(job["when"]) <= now
        except Exception:
            return False
    if job.get("cron"):
        try:
            prev = croniter(job["cron"], now).get_prev(datetime)
        except Exception:
            return False
        last = None
        if job.get("last_run"):
            try:
                last = datetime.fromisoformat(job["last_run"])
            except Exception:
                last = None
        # Due if a scheduled moment passed since the last run — catches ONE
        # missed slot after laptop sleep instead of replaying all of them.
        return last is None or prev > last
    return False


# ─── Headless execution ────────────────────────────────────────────────────────

class _HeadlessInteraction(agent.Interaction):
    """Confirms auto-decline, ask_user auto-answers 'no user available' —
    an unattended job must never block or take a gated action."""

    def begin(self, kind: str, payload: dict | None = None):
        fut = asyncio.get_running_loop().create_future()
        if kind == "confirm":
            fut.set_result("no")
        else:
            fut.set_result("No user is available — this is a scheduled headless "
                           "run. Use sensible defaults or report what you need.")
        return fut


async def _run_job(job: dict, broadcast, run_lock: asyncio.Lock) -> None:
    global _running_job
    _running_job = True
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    prompt = job["prompt"]
    print(f"[scheduler] running [{job['id']}]: {prompt[:80]}")
    try:
        async with run_lock:      # never fight a live user run for the mouse
            final = await agent.run_task(prompt, emit, _HeadlessInteraction(),
                                         history=[], mode="full", unattended=True)
    except Exception as e:
        final = f"Scheduled task failed: {str(e)[:160]}"
    finally:
        _running_job = False

    # Quiet scan jobs (meeting-prep, promise-sweep) stay SILENT when there's
    # nothing to say — no card, no notification. Two silence triggers, and BOTH
    # only apply to jobs that opted in via "quiet" (a plain reminder whose text
    # happens to contain the sentinel must still fire):
    #   1. the model returned the sentinel (strict: stripped output starts with
    #      it — a genuine prep that merely NARRATES the sentinel still delivers);
    #   2. the run itself failed (calendar unavailable, step/budget limit, error)
    #      — a scan that can't run must not spam an error card every 30 minutes.
    if job.get("quiet"):
        f = (final or "").strip()
        if (f.startswith(NOTHING_SENTINEL)
                or f.startswith(("I ran into a problem", "I hit my step limit",
                                 "I've hit my compute budget",
                                 "Scheduled task failed"))):
            print(f"[scheduler] [{job['id']}] nothing to report / run failed — silent")
            return

    label = "Scheduled" if job.get("cron") else "Reminder"
    # HUD (if anyone is watching) + native notification (always).
    # deliver="card": render as a silent briefing card — a multi-section digest
    # must NOT be TTS'd in full on every open tab (that's what `response` does).
    try:
        if job.get("deliver") == "card":
            await broadcast({"type": "briefing_card",
                             "title": job.get("id", "briefing").replace("-", " "),
                             "markdown": final,
                             "ts": datetime.now().isoformat(timespec="seconds")})
        else:
            await broadcast({"type": "response", "text": f"[{label}] {final}"})
    except Exception:
        pass
    try:
        await system_control.notify(f"COSMOS — {label.lower()} task", final[:200])
    except Exception:
        pass


async def _tick_loop(broadcast, run_lock: asyncio.Lock) -> None:
    _seed_default()
    while True:
        try:
            now = datetime.now()
            jobs = _load()
            changed = False
            for job in jobs:
                if not _is_due(job, now):
                    continue
                if run_lock.locked():
                    break                     # user is mid-task — retry next tick
                prev_last_run = job.get("last_run", "")
                job["last_run"] = now.isoformat(timespec="seconds")
                # Persist BEFORE running (crash safety) — and REFUSE to run if the
                # persist failed. last_run only lives in memory at this point; the
                # next tick re-reads jobs.json from disk, so running now would
                # re-fire this job on EVERY tick (30s) forever, hammering the LLM
                # gateway into rate limits. Skip and retry next tick instead.
                if not _save(jobs):
                    # ROLL BACK the in-memory mutation. `job` is a dict inside the
                    # shared `jobs` list, and _save serialises the WHOLE list — so
                    # a later due job's SUCCESSFUL _save(jobs) in this same tick
                    # would otherwise commit this job's last_run to disk, marking
                    # it as run when it never ran (and the end-of-tick prune would
                    # then delete a one-shot job outright).
                    job["last_run"] = prev_last_run
                    print(f"[scheduler] skipping [{job.get('id')}] — last_run "
                          f"didn't persist; will retry next tick")
                    continue
                changed = True
                await _run_job(job, broadcast, run_lock)
            if changed:
                # One-shot jobs that ran are pruned.
                _save([j for j in _load()
                       if not (j.get("when") and j.get("last_run"))])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[scheduler] tick error (non-fatal): {e}")
        await asyncio.sleep(TICK_S)


def start(broadcast, run_lock: asyncio.Lock) -> None:
    """Start the tick loop (idempotent). `broadcast(event)` sends a WS event to
    every connected HUD client; `run_lock` is main's global run lock."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_tick_loop(broadcast, run_lock))
    print("[scheduler] online — tick every 30s")

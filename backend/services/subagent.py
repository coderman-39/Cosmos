"""Parallel sub-agents — fan out independent read-only subtasks (F1).

Each worker is a full agent.run_task loop with:
  - a fresh history and trace (nothing leaks between workers or into the
    parent's conversation)
  - HARD read-only posture: read_only=True whitelist-enforces genuine reads
    (research tools, slack/google/calendar READS) and refuses every
    mutating call — bash writes/POSTs, write_file, UI drivers, sends — BEFORE
    the risk gate. Workers research and report; the PARENT acts after synthesis.
  - smaller iteration/token budgets and a wall-clock ceiling per worker
  - progress relayed to the HUD through the parent's emit, labeled [wN] —
    no new WebSocket event types needed.

spawn() returns a synthesis-ready digest of every worker's report; worker
crashes and timeouts are isolated (the batch never dies with one worker).
"""

import asyncio
import os

MAX_WORKERS = 6
WORKER_TIMEOUT_S = float(os.getenv("FRIDAY_SUBAGENT_TIMEOUT", "300"))
WORKER_MAX_ITERATIONS = int(os.getenv("FRIDAY_SUBAGENT_ITERATIONS", "15"))
WORKER_TOKEN_BUDGET = int(os.getenv("FRIDAY_SUBAGENT_TOKENS", "150000"))

# Worker events relayed to the HUD; everything else (response deltas, run
# meta, confirms — impossible anyway) is swallowed so the parent's own
# streaming bubble and telemetry are never corrupted.
_RELAY_TYPES = {"tool_start", "tool_done", "agent_thought"}


class _AutoDecline:
    """Worker-side Interaction: confirms decline instantly, questions get a
    'no user here' answer. Mirrors the scheduler's headless behavior."""

    pending = False

    def begin(self, kind: str, payload: dict | None = None):
        fut = asyncio.get_running_loop().create_future()
        if kind == "confirm":
            fut.set_result("no")
        else:
            fut.set_result("No user is available inside a sub-agent worker — "
                           "use sensible defaults or report what you need.")
        return fut

    def resolve(self, value: str) -> bool:
        return False

    def cancel(self) -> None:
        pass


async def spawn(ctx, tasks: list[str]) -> str:
    """Run 2-{MAX_WORKERS} worker agents concurrently; returns the combined
    report. `ctx` is the parent's RunContext."""
    from services import agent   # late import — agent lazy-imports us too

    tasks = [t.strip() for t in (tasks or []) if (t or "").strip()]
    if ctx.depth >= 1:
        return "Error: sub-agents cannot spawn sub-agents — do the work directly."
    if len(tasks) < 2:
        return ("Error: spawn_agents needs 2 or more independent tasks — for a "
                "single task, just do it yourself.")
    if len(tasks) > MAX_WORKERS:
        return (f"Error: {len(tasks)} workers requested but the cap is "
                f"{MAX_WORKERS} — merge related tasks or drop some.")

    async def run_worker(i: int, task: str):
        async def wemit(event: dict) -> None:
            if event.get("type") not in _RELAY_TYPES:
                return
            ev = dict(event)
            if ev["type"] == "agent_thought":
                ev["text"] = f"[w{i}] {ev.get('text', '')}"[:200]
            elif ev.get("label"):
                ev["label"] = f"[w{i}] {ev.get('label', '')}"[:120]
            await ctx.emit(ev)

        try:
            out = await asyncio.wait_for(
                agent.run_task(task, wemit, _AutoDecline(),
                               history=[],            # isolated context
                               mode="ask",
                               unattended=True,
                               read_only=True,        # HARD: mutations refused,
                                                      # not just auto-declined
                               depth=ctx.depth + 1,
                               max_iterations=WORKER_MAX_ITERATIONS,
                               token_budget=WORKER_TOKEN_BUDGET),
                timeout=WORKER_TIMEOUT_S)
            return i, task, out, True
        except asyncio.CancelledError:
            raise                                     # parent run was stopped
        except (asyncio.TimeoutError, TimeoutError):
            return (i, task,
                    f"(worker timed out after {int(WORKER_TIMEOUT_S)}s — "
                    "partial work lost)", False)
        except Exception as e:
            return i, task, f"(worker crashed: {str(e)[:160]})", False

    await ctx.emit({"type": "agent_thought",
                    "text": f"Fanning out to {len(tasks)} parallel workers…"})
    results = await asyncio.gather(*[run_worker(i + 1, t)
                                     for i, t in enumerate(tasks)])
    ok = sum(1 for *_, good in results if good)
    parts = [f"[{ok}/{len(tasks)} workers completed successfully. Workers ran "
             "READ-ONLY (mutating calls were refused, not performed) — review "
             "their findings and take any outward actions yourself.]"]
    for i, task, out, _good in results:
        parts.append(f"### Worker {i} — {task[:120]}\n{(out or '').strip()[:4000]}")
    return "\n\n".join(parts)

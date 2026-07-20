"""Run tracing — JSONL flight recorder for agent runs.

Each agent run appends one JSON object per line to
    ~/.friday/traces/<YYYYMMDD>/<run_id>.jsonl
Every record carries `ts` (ISO) and `type`; everything else is event payload.

Tracing is strictly best-effort: every path is guarded, every failure is
swallowed. A full disk, bad permission, or unserializable payload must NEVER
break — or even slow noticeably — the run it is recording.
"""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

TRACE_DIR = Path.home() / ".friday" / "traces"


class RunTrace:
    """Append-only JSONL trace of one agent run.

    Usage:
        trace = RunTrace()
        trace.event("run_start", user_text="...", model="...")

    If the trace directory can't be created, the instance goes inert
    (`self.path is None`) and every event() becomes a no-op.
    """

    def __init__(self):
        self.run_id = uuid4().hex[:8]
        self.path: Path | None = None
        # Bounded in-memory mirror so post-run reflection can read what
        # happened without re-parsing the file.
        self.events: list[dict] = []
        try:
            day_dir = TRACE_DIR / datetime.now().strftime("%Y%m%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            self.path = day_dir / f"{self.run_id}.jsonl"
        except Exception:
            self.path = None  # tracing disabled for this run

    def event(self, etype: str, **fields) -> None:
        """Append one event line. Never raises."""
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"),
                  "type": etype, **fields}
        if len(self.events) < 300:
            self.events.append(record)
        if self.path is None:
            return
        try:
            # default=str: an SDK object sneaking into a field must not kill
            # the write — stringify anything json can't handle natively.
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # tracing must never break the run

    def summary(self, n: int = 15) -> str:
        """Compact text of the last `n` significant events — for reflection."""
        keep = [e for e in self.events
                if e.get("type") in ("tool_start", "tool_done", "fallback",
                                     "run_error", "budget_exceeded")]
        lines = []
        for e in keep[-n:]:
            if e["type"] == "tool_done":
                lines.append(f"tool {e.get('tool')} → "
                             f"{'ok' if e.get('ok') else 'FAILED'}: "
                             f"{str(e.get('detail', ''))[:120]}")
            elif e["type"] == "tool_start":
                continue
            else:
                lines.append(f"{e['type']}: {str(e)[:150]}")
        return "\n".join(lines)

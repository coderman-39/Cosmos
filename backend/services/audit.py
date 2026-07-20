"""Security audit trail + pre-overwrite snapshots for COSMOS.

Two concerns, both fully fault-isolated (a failure here must NEVER break a run):
  - record(): append-only log of every executed tool to ~/.friday/audit.jsonl,
    flagging which ones tripped the risk gate and whether the user approved.
  - snapshot(): copy a file about to be overwritten into ~/.friday/undo/ so an
    accidental clobber is recoverable.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

_DIR       = Path.home() / ".friday"
_AUDIT     = _DIR / "audit.jsonl"
_UNDO_DIR  = _DIR / "undo"


def record(tool: str, summary: str, ok: bool,
           danger: str | None = None, confirmed: bool | None = None) -> None:
    """Append one action to the audit log. Silent on any failure."""
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        entry: dict = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "ok": bool(ok),
            "summary": (summary or "")[:300],
        }
        if danger:
            entry["danger"] = danger[:200]
        if confirmed is not None:
            entry["confirmed"] = bool(confirmed)
        with _AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        print(f"[audit] write failed ({tool}): {e}")


def snapshot(path: Path) -> str | None:
    """Back up an existing file before it's overwritten. Returns the backup
    path (for surfacing to the user), or None if there was nothing to back up
    or the copy failed. Never raises."""
    try:
        if not path.exists() or not path.is_file():
            return None
        _UNDO_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = _UNDO_DIR / f"{stamp}_{path.name}"
        # De-collide if two overwrites hit the same file in the same second.
        i = 1
        while dest.exists():
            dest = _UNDO_DIR / f"{stamp}_{i}_{path.name}"
            i += 1
        shutil.copy2(path, dest)
        return str(dest)
    except Exception:
        return None

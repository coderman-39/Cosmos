"""Atomic, concurrency-safe file writes — one hardened implementation for every store.

The `foo.json.tmp` pattern (a FIXED temp name beside the target) is NOT safe when
two writers overlap — and "two writers" is not hypothetical: a second COSMOS
backend, a scheduled job and a live run, or two threads all hit the same store.

    tmp = FILE.with_suffix(".json.tmp")   # same path for EVERYONE
    tmp.write_text(...)                   # writer B truncates writer A's temp
    os.replace(tmp, FILE)                 # first replace wins; the loser gets ENOENT

That is precisely what took the scheduler down: three backends raced on
~/.friday/jobs.json.tmp → "No such file or directory: jobs.json.tmp -> jobs.json"
→ last_run never persisted → the cron job re-fired on every 30s tick and hammered
the LLM gateway into 429s.

watchers.py already learned this the hard way and switched to mkstemp ("safe
against any concurrency"); this module generalises that fix so every store gets
it, instead of each one re-discovering the bug.

Contract:
  - Unique temp file, created in the SAME directory as the target (os.replace is
    only atomic within a filesystem, so the temp must not live in /tmp).
  - Returns True/False and NEVER raises — callers that must not proceed on a
    failed persist (the scheduler's crash-safety invariant) can check the bool
    instead of silently continuing.
  - Mode: an existing target's permissions are preserved; a NEW file inherits
    mkstemp's 0600 (private by default — these files hold personal data and,
    in connectors' case, credentials).
  - The temp is always cleaned up on failure — no litter beside the target.
"""

import os
import stat
import tempfile
from pathlib import Path


def write_text_atomic(path, text: str, *, encoding: str = "utf-8") -> bool:
    """Atomically replace `path` with `text`. True on success, False on any
    failure. Never raises."""
    p = Path(path)
    tmp: str | None = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Unique name in the target's own directory: concurrent writers can no
        # longer truncate each other's temp or lose the replace race.
        #
        # The temp must stay INSIDE the target's own ignore namespace. Blindly
        # prefixing a dot double-dots a dotfile (".env" → "..env.XXXX.tmp"),
        # which no ".env.*" gitignore rule matches — so an orphaned temp full of
        # live credentials would show up as a committable untracked file. Only
        # add the hiding dot when the target isn't already hidden.
        prefix = f"{p.name}." if p.name.startswith(".") else f".{p.name}."
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=prefix, suffix=".tmp")
        fd_owned = True
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                fd_owned = False        # fdopen owns the fd now; `with` closes it
                f.write(text)
        finally:
            if fd_owned:                # fdopen itself failed — don't leak the fd
                try:
                    os.close(fd)
                except Exception:
                    pass
        # Preserve the existing file's permissions; a brand-new file keeps
        # mkstemp's private 0600 rather than the process umask's 0644.
        try:
            if p.exists():
                os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))
        except Exception:
            pass
        os.replace(tmp, p)
        tmp = None                      # consumed by the rename
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def write_json_atomic(path, data, *, indent: int | None = None,
                      ensure_ascii: bool = False, default=None) -> bool:
    """json.dumps + write_text_atomic. False on serialisation OR write failure."""
    import json
    try:
        text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii,
                          default=default)
    except Exception:
        return False
    return write_text_atomic(path, text)

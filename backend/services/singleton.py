"""Single-instance guard — exactly one COSMOS backend per machine.

Three stale backends once ran side by side for two days. Each had its OWN
scheduler, Slack bridge and watchers, so:

  - every cron job fired 3x → 3x the LLM spend → gateway 429s;
  - they raced on the shared jobs.json temp file (see services.atomicio), which
    could leave jobs.json corrupt;
  - and worst: main's global _RUN_LOCK is an asyncio.Lock — PER-PROCESS. With
    three backends nothing stopped three agent runs driving the same physical
    mouse and keyboard at once, which is the exact thing that lock exists to
    prevent.

start.sh's `pkill -f main.py` reap is best-effort and doesn't survive across
terminal sessions. flock is the right primitive: the kernel holds it for the
process lifetime and releases it on ANY exit — including SIGKILL and crashes —
so unlike a PID file it can never go stale.

The guard always FAILS OPEN: if flock is unavailable (non-POSIX, odd fs), boot
proceeds rather than bricking on a diagnostic.
"""

import os
from pathlib import Path

DEFAULT_LOCK = Path.home() / ".friday" / "cosmos.lock"

# Held for the process lifetime. Closing the fd releases the lock, so this must
# stay referenced — never let it be garbage-collected.
_held_fd: int | None = None


def acquire(lock_path=None) -> tuple[bool, str]:
    """Try to become THE backend.

    Returns (ok, holder): ok=True → the lock is ours (holder = our pid);
    ok=False → another live process holds it (holder = its pid, "?" if unknown).
    """
    global _held_fd
    if os.getenv("FRIDAY_SINGLE_INSTANCE", "1").lower() in ("0", "false", "no"):
        return True, ""
    path = Path(lock_path or DEFAULT_LOCK)
    fd = None
    try:
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # EWOULDBLOCK/EAGAIN is the ONLY errno that means "someone holds it".
            # Every other OSError (ENOLCK / EOPNOTSUPP on an NFS or FUSE home,
            # say) means the LOCK is unavailable, not the app — those fall
            # through to the fail-open handler below instead of refusing boot
            # with a bogus "already running (pid ?)".
            try:
                holder = os.read(fd, 32).decode(errors="replace").strip() or "?"
            except Exception:
                holder = "?"
            os.close(fd)
            return False, holder
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        _held_fd = fd
        return True, str(os.getpid())
    except Exception as e:
        # Guard unavailable — FAIL OPEN. Close the fd first: leaking it would
        # hold the flock for the process lifetime and lock out every later boot.
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        print(f"[singleton] guard unavailable, continuing: {e}")
        return True, ""


def release() -> None:
    """Drop the lock. The OS does this automatically at exit; this exists for
    tests and orderly shutdown."""
    global _held_fd
    if _held_fd is not None:
        try:
            os.close(_held_fd)
        except Exception:
            pass
        _held_fd = None


def holds_lock() -> bool:
    return _held_fd is not None

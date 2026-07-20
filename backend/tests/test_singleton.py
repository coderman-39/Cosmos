"""services.singleton: one backend per machine.

Three stale backends running side by side is what caused the 429 storm — each
had its own scheduler (3x cron firing), they raced on ~/.friday state files, and
main's global _RUN_LOCK is an asyncio.Lock (per-process), so it stopped
protecting the mouse entirely.

Contract under test:
  - The first caller gets the lock; a second concurrent caller is refused and
    learns the holder's pid.
  - Releasing frees it for the next caller.
  - flock dies with the process, so the lock can never go stale.
  - The guard can be disabled, and fails OPEN if flock is unavailable.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from services import singleton


@pytest.fixture(autouse=True)
def _clean_lock(monkeypatch):
    monkeypatch.delenv("FRIDAY_SINGLE_INSTANCE", raising=False)
    singleton.release()
    yield
    singleton.release()


def test_first_caller_acquires(tmp_path):
    ok, holder = singleton.acquire(tmp_path / "cosmos.lock")
    assert ok is True
    assert holder == str(os.getpid())
    assert singleton.holds_lock() is True


def test_lock_file_records_pid(tmp_path):
    lock = tmp_path / "cosmos.lock"
    singleton.acquire(lock)
    assert lock.read_text().strip() == str(os.getpid())


def test_second_acquire_is_refused_and_names_holder(tmp_path):
    """flock is tied to the open file description, so a second independent
    open+flock conflicts even from within the same process."""
    lock = tmp_path / "cosmos.lock"
    ok1, _ = singleton.acquire(lock)
    assert ok1 is True
    ok2, holder = singleton.acquire(lock)
    assert ok2 is False, "a second backend must be refused"
    assert holder == str(os.getpid())


def test_release_frees_the_lock(tmp_path):
    lock = tmp_path / "cosmos.lock"
    assert singleton.acquire(lock)[0] is True
    singleton.release()
    assert singleton.holds_lock() is False
    assert singleton.acquire(lock)[0] is True    # reacquirable


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FRIDAY_SINGLE_INSTANCE", "0")
    lock = tmp_path / "cosmos.lock"
    assert singleton.acquire(lock) == (True, "")
    assert singleton.acquire(lock) == (True, "")   # no guard at all


def test_fails_open_when_flock_unavailable(tmp_path, monkeypatch):
    """A broken guard must never brick boot."""
    import builtins
    real_import = builtins.__import__

    def no_fcntl(name, *a, **k):
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    ok, _ = singleton.acquire(tmp_path / "cosmos.lock")
    assert ok is True


@pytest.mark.parametrize("err", ["ENOLCK", "EOPNOTSUPP"])
def test_fails_open_when_the_filesystem_cant_flock(tmp_path, monkeypatch, err):
    """Only EWOULDBLOCK means "someone holds it". A filesystem that can't do
    flock at all (NFS home without lockd, some FUSE mounts) must FAIL OPEN —
    not refuse boot with a bogus 'already running (pid ?)'."""
    import errno as errno_mod
    import fcntl

    code = getattr(errno_mod, err)

    def cant_lock(fd, op):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(fcntl, "flock", cant_lock)
    ok, holder = singleton.acquire(tmp_path / "cosmos.lock")
    assert ok is True, f"{err} was misread as contention → backend unbootable"
    assert holder == ""


def test_contention_is_still_detected_after_narrowing(tmp_path):
    """The narrowed handler must not stop detecting a REAL second instance."""
    lock = tmp_path / "cosmos.lock"
    assert singleton.acquire(lock)[0] is True
    ok, holder = singleton.acquire(lock)
    assert ok is False and holder == str(os.getpid())


def test_fail_open_does_not_leak_the_lock_fd(tmp_path, monkeypatch):
    """Failing open must close the fd — leaking it would hold the flock for the
    process lifetime and lock out every later boot."""
    import errno as errno_mod
    import fcntl

    def cant_lock(fd, op):
        raise OSError(errno_mod.ENOLCK, "no locks available")

    lock = tmp_path / "cosmos.lock"
    monkeypatch.setattr(fcntl, "flock", cant_lock)
    assert singleton.acquire(lock)[0] is True     # failed open, fd should be closed
    monkeypatch.undo()
    # If the fd had leaked it would still hold the flock and this would be refused.
    assert singleton.acquire(lock)[0] is True


def test_lock_dies_with_the_process(tmp_path):
    """The whole reason for flock over a PID file: a SIGKILLed holder must not
    leave a stale lock that blocks every future boot."""
    lock = tmp_path / "cosmos.lock"
    backend = str(tmp_path.parent)          # unused; keep paths tidy
    _ = backend
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        from services import singleton
        ok, _ = singleton.acquire({str(lock)!r})
        print("ACQUIRED" if ok else "REFUSED", flush=True)
        time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ACQUIRED"
        # While the child holds it, we must be refused.
        assert singleton.acquire(lock)[0] is False
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Child is gone (SIGKILL — no cleanup ran): the lock must be free again.
    assert singleton.acquire(lock)[0] is True, "stale lock survived a SIGKILLed holder"

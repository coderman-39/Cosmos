"""Atomic, concurrency-safe writes (services.atomicio).

This module exists because of a real incident: three COSMOS backends raced on the
shared temp path ~/.friday/jobs.json.tmp, the loser's os.replace() died with
ENOENT, last_run never persisted, and the promise-sweep cron re-fired on every
30s tick until the LLM gateway started returning 429s.

Contract under test:
  - The write lands and is atomic (target is never a partial file).
  - CONCURRENT writers all succeed — the bug that started this.
  - Failure returns False rather than raising, and leaves no temp litter.
  - Existing file permissions are preserved; a new file is private (0600).
"""

import json
import os
import stat
import threading

from services import atomicio


def test_writes_text(tmp_path):
    p = tmp_path / "a.txt"
    assert atomicio.write_text_atomic(p, "hello") is True
    assert p.read_text() == "hello"


def test_replaces_existing_content(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("old")
    assert atomicio.write_text_atomic(p, "new") is True
    assert p.read_text() == "new"


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "a.json"
    assert atomicio.write_json_atomic(p, {"x": 1}) is True
    assert json.loads(p.read_text()) == {"x": 1}


def test_no_temp_litter_left_behind(tmp_path):
    p = tmp_path / "a.txt"
    atomicio.write_text_atomic(p, "hello")
    assert [f.name for f in tmp_path.iterdir()] == ["a.txt"]


def test_returns_false_when_target_dir_is_a_file(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    # Writing "blocker/child.txt" can't work — must return False, not raise.
    assert atomicio.write_text_atomic(blocker / "child.txt", "x") is False


def test_write_json_returns_false_on_unserialisable(tmp_path):
    p = tmp_path / "a.json"
    assert atomicio.write_json_atomic(p, {"bad": object()}) is False
    assert not p.exists()          # nothing half-written


def test_write_json_default_str_handles_odd_types(tmp_path):
    p = tmp_path / "a.json"
    assert atomicio.write_json_atomic(p, {"o": object()}, default=str) is True
    assert "object at" in json.loads(p.read_text())["o"]


# ─── the actual bug: concurrent writers ────────────────────────────────────────

def test_concurrent_writers_all_succeed(tmp_path):
    """The regression that started it all. With a FIXED temp name, overlapping
    writers truncate each other's temp and the loser's os.replace() raises
    ENOENT. With a unique temp per writer, every one of them must succeed."""
    p = tmp_path / "jobs.json"
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def writer(i: int):
        barrier.wait()                      # maximise overlap
        ok = atomicio.write_json_atomic(p, {"writer": i, "payload": "x" * 5000})
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [True] * 12, "every concurrent writer must succeed"
    # The survivor must be ONE complete, valid document — never a spliced mix.
    data = json.loads(p.read_text())
    assert data["payload"] == "x" * 5000
    # And no temp files may be left lying around.
    assert [f.name for f in tmp_path.iterdir()] == ["jobs.json"]


def test_reader_never_sees_a_partial_file(tmp_path):
    """os.replace is atomic: a concurrent reader sees either the old or the new
    document, never a truncated one."""
    p = tmp_path / "s.json"
    atomicio.write_json_atomic(p, {"v": 0, "pad": "a" * 20000})
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                json.loads(p.read_text())      # must always parse
            except FileNotFoundError:
                bad.append("missing")
            except json.JSONDecodeError:
                bad.append("torn")

    r = threading.Thread(target=reader)
    r.start()
    try:
        for v in range(60):
            atomicio.write_json_atomic(p, {"v": v, "pad": "b" * 20000})
    finally:
        stop.set()
        r.join()
    assert bad == [], f"reader observed {bad[:3]}"


# ─── permissions ───────────────────────────────────────────────────────────────

def test_dotfile_temp_stays_in_the_targets_ignore_namespace(tmp_path):
    """A ".env" target must not produce "..env.XXXX.tmp": that double dot escapes
    the ".env.*" gitignore rule, so an orphaned temp full of live credentials
    would be offered to git as a committable untracked file."""
    seen: list[str] = []
    real_mkstemp = atomicio.tempfile.mkstemp

    def spy(*a, **kw):
        seen.append(kw.get("prefix", ""))
        return real_mkstemp(*a, **kw)

    atomicio.tempfile.mkstemp = spy
    try:
        atomicio.write_text_atomic(tmp_path / ".env", "TOKEN=secret")
    finally:
        atomicio.tempfile.mkstemp = real_mkstemp
    assert seen == [".env."], f"temp prefix {seen} escapes the .env.* ignore rule"
    assert not seen[0].startswith(".."), "double-dotted a dotfile"


def test_regular_file_temp_is_still_hidden(tmp_path):
    seen: list[str] = []
    real_mkstemp = atomicio.tempfile.mkstemp

    def spy(*a, **kw):
        seen.append(kw.get("prefix", ""))
        return real_mkstemp(*a, **kw)

    atomicio.tempfile.mkstemp = spy
    try:
        atomicio.write_json_atomic(tmp_path / "jobs.json", {"a": 1})
    finally:
        atomicio.tempfile.mkstemp = real_mkstemp
    assert seen == [".jobs.json."], "non-dotfile temps should still be hidden"


def test_new_file_is_private(tmp_path):
    p = tmp_path / "secret.env"
    atomicio.write_text_atomic(p, "KEY=value")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_preserves_existing_mode(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{}")
    os.chmod(p, 0o644)
    atomicio.write_text_atomic(p, '{"x":1}')
    assert stat.S_IMODE(p.stat().st_mode) == 0o644

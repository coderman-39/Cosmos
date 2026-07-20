"""services.scheduler: persistence honesty — the bug family behind the 429 storm.

Three stale backends raced on the shared jobs.json temp file. _save() swallowed
the resulting ENOENT, and _tick_loop ran the job ANYWAY — so last_run never
reached disk, the next tick re-read the old last_run, and the cron job re-fired
every 30s forever, hammering the LLM gateway.

Contract under test:
  - _save() reports success/failure instead of silently swallowing it.
  - _tick_loop REFUSES to run a job whose last_run didn't persist.
  - add_job / cancel_job never claim success for a write that didn't land.
  - _seed_once doesn't drop its marker when the seed failed to persist.
"""

import asyncio

import pytest

from services import atomicio, scheduler


@pytest.fixture(autouse=True)
def jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(scheduler, "_PREP_SEED_MARKER", tmp_path / ".prep-seeded")
    monkeypatch.setattr(scheduler, "_SWEEP_SEED_MARKER", tmp_path / ".sweep-seeded")
    return tmp_path / "jobs.json"


@pytest.fixture
def failing_writes(monkeypatch):
    """Simulate the lost race: every atomic write fails."""
    monkeypatch.setattr(atomicio, "write_json_atomic", lambda *a, **k: False)


# ─── _save reports its outcome ─────────────────────────────────────────────────

def test_save_returns_true_on_success():
    assert scheduler._save([{"id": "x"}]) is True


def test_save_returns_false_on_failure(failing_writes):
    assert scheduler._save([{"id": "x"}]) is False


# ─── the storm: a job must not run if last_run didn't persist ──────────────────

async def test_tick_does_not_run_job_when_persist_fails(monkeypatch, failing_writes):
    """THE regression. If last_run can't be written, running the job would
    re-fire it on every subsequent tick — so it must be skipped."""
    ran = []

    async def fake_run_job(job, broadcast, run_lock):
        ran.append(job["id"])

    monkeypatch.setattr(scheduler, "_load",
                        lambda: [{"id": "sweep", "prompt": "p", "cron": "* * * * *",
                                  "enabled": True, "last_run": ""}])
    monkeypatch.setattr(scheduler, "_is_due", lambda job, now: True)
    monkeypatch.setattr(scheduler, "_run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_seed_default", lambda: None)

    task = asyncio.create_task(_one_tick(scheduler))
    await asyncio.sleep(0.05)
    task.cancel()
    assert ran == [], "job ran despite last_run not persisting → would re-fire forever"


async def test_tick_runs_job_when_persist_succeeds(monkeypatch):
    ran = []

    async def fake_run_job(job, broadcast, run_lock):
        ran.append(job["id"])

    monkeypatch.setattr(scheduler, "_load",
                        lambda: [{"id": "sweep", "prompt": "p", "cron": "* * * * *",
                                  "enabled": True, "last_run": ""}])
    monkeypatch.setattr(scheduler, "_is_due", lambda job, now: True)
    monkeypatch.setattr(scheduler, "_run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_seed_default", lambda: None)

    task = asyncio.create_task(_one_tick(scheduler))
    await asyncio.sleep(0.05)
    task.cancel()
    assert ran == ["sweep"]


async def _one_tick(sched):
    """Run _tick_loop just long enough for one pass."""
    try:
        await sched._tick_loop(lambda ev: None, asyncio.Lock())
    except asyncio.CancelledError:
        pass


async def test_failed_persist_does_not_leak_into_a_later_jobs_save(monkeypatch, jobs_file):
    """A skipped job's last_run must NOT ride along on the NEXT job's successful
    save. `job` is a dict inside the shared `jobs` list and _save serialises the
    whole list, so without a rollback job A gets marked as run (and, being a
    one-shot, pruned off disk) despite never running."""
    import json
    ran = []
    saves = {"n": 0}
    real_write = atomicio.write_json_atomic

    def flaky_write(path, data, **kw):
        saves["n"] += 1
        if saves["n"] == 1:          # A's save fails; B's succeeds
            return False
        return real_write(path, data, **kw)

    async def fake_run_job(job, broadcast, run_lock):
        ran.append(job["id"])

    jobs = [{"id": "A", "prompt": "p", "when": "2020-01-01T09:00",
             "enabled": True, "last_run": ""},
            {"id": "B", "prompt": "p", "cron": "* * * * *",
             "enabled": True, "last_run": ""}]
    jobs_file.write_text(json.dumps(jobs))

    monkeypatch.setattr(atomicio, "write_json_atomic", flaky_write)
    monkeypatch.setattr(scheduler, "_is_due", lambda job, now: True)
    monkeypatch.setattr(scheduler, "_run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_seed_default", lambda: None)

    task = asyncio.create_task(_one_tick(scheduler))
    await asyncio.sleep(0.05)
    task.cancel()

    assert ran == ["B"], "only B should have run"
    on_disk = {j["id"]: j for j in json.loads(jobs_file.read_text())}
    assert "A" in on_disk, "one-shot A was pruned off disk despite never running"
    assert not on_disk["A"]["last_run"], \
        "A's last_run was committed by B's save — A will never run"


# ─── never claim a write that didn't land ──────────────────────────────────────

def test_add_job_reports_persist_failure(failing_writes):
    out = scheduler.add_job("do a thing", when="2030-01-01T09:00")
    assert out.startswith("Error")
    assert "NOT scheduled" in out


def test_cancel_job_reports_persist_failure(monkeypatch):
    out = scheduler.add_job("do a thing", when="2030-01-01T09:00")
    jid = out.split("[")[1].split("]")[0]
    # Now break writes and try to cancel — it must NOT claim success.
    monkeypatch.setattr(atomicio, "write_json_atomic", lambda *a, **k: False)
    msg = scheduler.cancel_job(jid)
    assert msg.startswith("Error") and "still" in msg


def test_seed_marker_not_dropped_when_seed_fails(tmp_path, failing_writes):
    marker = tmp_path / ".sweep-seeded"
    scheduler._seed_once({"id": "promise-sweep", "prompt": "p", "cron": "0 9 * * *"},
                         marker, "promise sweep")
    assert not marker.exists(), "marker dropped despite the seed never persisting"


def test_seed_marker_written_on_success(tmp_path):
    marker = tmp_path / ".sweep-seeded"
    scheduler._seed_once({"id": "promise-sweep", "prompt": "p", "cron": "0 9 * * *"},
                         marker, "promise sweep")
    assert marker.exists()

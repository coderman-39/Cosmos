"""services.scheduler: job CRUD, due-ness (one-shot, cron, missed-slot catch-up)."""

from datetime import datetime

import pytest

from services import scheduler


@pytest.fixture(autouse=True)
def jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "JOBS_FILE", tmp_path / "jobs.json")
    # Keep the one-time seed markers OUT of the real ~/.friday.
    monkeypatch.setattr(scheduler, "_PREP_SEED_MARKER", tmp_path / ".prep-seeded")
    monkeypatch.setattr(scheduler, "_SWEEP_SEED_MARKER", tmp_path / ".sweep-seeded")
    return tmp_path / "jobs.json"


def test_add_list_cancel_roundtrip():
    out = scheduler.add_job("check the deploy", when="2030-01-01T09:00")
    assert out.startswith("Scheduled [")
    jid = out.split("[")[1].split("]")[0]
    assert "check the deploy" in scheduler.list_jobs()
    assert "Cancelled" in scheduler.cancel_job(jid)
    assert scheduler.list_jobs() == "No scheduled tasks."


def test_add_requires_exactly_one_schedule():
    assert scheduler.add_job("x").startswith("Error")
    assert scheduler.add_job("x", when="2030-01-01T09:00", cron="0 9 * * *").startswith("Error")


def test_add_validates_inputs():
    assert scheduler.add_job("x", when="tomorrow 5pm").startswith("Error")
    assert scheduler.add_job("x", cron="not a cron").startswith("Error")
    assert scheduler.add_job("", when="2030-01-01T09:00").startswith("Error")


def test_oneshot_due_only_once():
    job = {"id": "a", "prompt": "x", "when": "2020-01-01T09:00", "cron": "",
           "enabled": True, "last_run": ""}
    now = datetime(2026, 7, 8, 12, 0)
    assert scheduler._is_due(job, now) is True
    job["last_run"] = "2026-07-08T12:00:00"
    assert scheduler._is_due(job, now) is False


def test_oneshot_future_not_due():
    job = {"id": "a", "prompt": "x", "when": "2030-01-01T09:00", "cron": "",
           "enabled": True, "last_run": ""}
    assert scheduler._is_due(job, datetime(2026, 7, 8, 12, 0)) is False


def test_cron_due_after_slot_passes():
    job = {"id": "b", "prompt": "x", "when": "", "cron": "0 9 * * *",
           "enabled": True, "last_run": "2026-07-07T09:00:00"}
    # 8:59 — today's 9am hasn't happened since last run
    assert scheduler._is_due(job, datetime(2026, 7, 8, 8, 59)) is False
    # 9:01 — due
    assert scheduler._is_due(job, datetime(2026, 7, 8, 9, 1)) is True


def test_cron_missed_slot_runs_once_on_wake():
    # Laptop slept through 9am; at 14:30 the job is due exactly once.
    job = {"id": "c", "prompt": "x", "when": "", "cron": "0 9 * * *",
           "enabled": True, "last_run": "2026-07-07T09:00:12"}
    now = datetime(2026, 7, 8, 14, 30)
    assert scheduler._is_due(job, now) is True
    job["last_run"] = now.isoformat(timespec="seconds")
    assert scheduler._is_due(job, now) is False


def test_disabled_job_never_due():
    job = {"id": "d", "prompt": "x", "when": "2020-01-01T09:00", "cron": "",
           "enabled": False, "last_run": ""}
    assert scheduler._is_due(job, datetime(2026, 7, 8, 12, 0)) is False


def test_seed_default_briefing(jobs_file):
    scheduler._seed_default()
    jobs = {j["id"]: j for j in scheduler._load()}
    assert set(jobs) == {"morning-briefing", "meeting-prep-scan", "promise-sweep"}
    assert jobs["morning-briefing"]["cron"] == "0 9 * * 1-5"
    assert jobs["morning-briefing"]["deliver"] == "card"
    assert scheduler.NOTHING_SENTINEL in jobs["promise-sweep"]["prompt"]
    # Seeding again must not duplicate.
    scheduler._seed_default()
    assert len(scheduler._load()) == 3


def test_legacy_briefing_upgraded_in_place(jobs_file):
    """Existing installs carry the old prompt in jobs.json — the seed path
    must upgrade it to the ranked card format, but ONLY if uncustomized."""
    scheduler._save([{"id": "morning-briefing",
                      "prompt": scheduler._LEGACY_BRIEFING_PROMPT,
                      "cron": "0 9 * * 1-5", "enabled": True, "last_run": ""}])
    scheduler._seed_default()
    job = scheduler._load()[0]
    assert job["deliver"] == "card"
    assert "RANKED briefing" in job["prompt"]


def test_customized_briefing_never_touched(jobs_file):
    scheduler._save([{"id": "morning-briefing",
                      "prompt": "my own special briefing",
                      "cron": "0 8 * * *", "enabled": True, "last_run": ""}])
    scheduler._seed_default()
    job = scheduler._load()[0]
    assert job["prompt"] == "my own special briefing"
    assert "deliver" not in job


async def test_card_jobs_broadcast_briefing_card(monkeypatch):
    """deliver='card' jobs must emit a silent briefing_card, not a spoken
    response; legacy jobs keep the response shape."""
    import asyncio
    from services import agent, system_control

    async def fake_run_task(*a, **kw):
        return "## Needs you today\n- review PR #42"

    async def fake_notify(*a, **kw):
        return True, "ok"

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    monkeypatch.setattr(system_control, "notify", fake_notify)
    sent = []

    async def broadcast(event):
        sent.append(event)

    lock = asyncio.Lock()
    await scheduler._run_job({"id": "morning-briefing", "prompt": "brief me",
                              "cron": "0 9 * * 1-5", "deliver": "card"},
                             broadcast, lock)
    assert sent[0]["type"] == "briefing_card"
    assert "review PR #42" in sent[0]["markdown"]

    sent.clear()
    await scheduler._run_job({"id": "r1", "prompt": "remind me",
                              "when": "2026-07-08T09:00"}, broadcast, lock)
    assert sent[0]["type"] == "response"
    assert sent[0]["text"].startswith("[Reminder]")


def test_meeting_prep_seeded_once(jobs_file, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "_PREP_SEED_MARKER", tmp_path / ".prep-seeded")
    scheduler._seed_default()
    jobs = scheduler._load()
    ids = [j["id"] for j in jobs]
    assert "meeting-prep-scan" in ids
    prep = next(j for j in jobs if j["id"] == "meeting-prep-scan")
    assert prep["deliver"] == "card"
    assert scheduler.NOTHING_SENTINEL in prep["prompt"]
    # User cancels → marker prevents re-seed on next boot.
    scheduler.cancel_job("meeting-prep-scan")
    scheduler._seed_default()
    assert "meeting-prep-scan" not in [j["id"] for j in scheduler._load()]


async def test_nothing_sentinel_is_silent(monkeypatch):
    import asyncio
    from services import agent, system_control

    async def fake_run_task(*a, **kw):
        return scheduler.NOTHING_SENTINEL

    notified = []

    async def fake_notify(*a, **kw):
        notified.append(a)
        return True, "ok"

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    monkeypatch.setattr(system_control, "notify", fake_notify)
    sent = []

    async def broadcast(event):
        sent.append(event)

    await scheduler._run_job({"id": "meeting-prep-scan", "prompt": "scan",
                              "cron": "*/30 * * * *", "deliver": "card",
                              "quiet": True},
                             broadcast, asyncio.Lock())
    assert sent == [] and notified == [], "empty scan must be completely silent"


async def test_quiet_scan_silent_on_failure(monkeypatch):
    """A quiet scan whose run FAILS (calendar unavailable) must not spam a
    card+notification every 30 min — it stays silent."""
    import asyncio
    from services import agent, system_control

    async def failing_run(*a, **kw):
        return "I ran into a problem and couldn't finish, sir — calendar unavailable"

    notified = []

    async def fake_notify(*a, **kw):
        notified.append(a)
        return True, "ok"

    monkeypatch.setattr(agent, "run_task", failing_run)
    monkeypatch.setattr(system_control, "notify", fake_notify)
    sent = []

    async def broadcast(e):
        sent.append(e)

    await scheduler._run_job({"id": "meeting-prep-scan", "prompt": "scan",
                              "deliver": "card", "quiet": True},
                             broadcast, asyncio.Lock())
    assert sent == [] and notified == []


async def test_non_quiet_job_delivers_even_with_sentinel_substring(monkeypatch):
    """The sentinel silences ONLY quiet jobs, and only when the output STARTS
    with it — a plain reminder whose text merely mentions it still fires."""
    import asyncio
    from services import agent, system_control

    async def run(*a, **kw):
        return f"Reminder: tell the team {scheduler.NOTHING_SENTINEL} is our code word"

    async def fake_notify(*a, **kw):
        return True, "ok"

    monkeypatch.setattr(agent, "run_task", run)
    monkeypatch.setattr(system_control, "notify", fake_notify)
    sent = []

    async def broadcast(e):
        sent.append(e)

    await scheduler._run_job({"id": "r1", "prompt": "x", "when": "2026-07-08T09:00"},
                             broadcast, asyncio.Lock())
    assert sent and sent[0]["type"] == "response"


async def test_headless_interaction_autodeclines():
    hi = scheduler._HeadlessInteraction()
    fut = hi.begin("confirm")
    assert (await fut) == "no"
    fut = hi.begin("ask")
    assert "headless" in (await fut)

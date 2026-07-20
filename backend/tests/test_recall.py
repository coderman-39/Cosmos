"""services.recall (FTS5 run index) + services.memory fact APIs."""

import json

import pytest

from services import memory, recall


@pytest.fixture
def recall_db(tmp_path, monkeypatch):
    monkeypatch.setattr(recall, "DB", tmp_path / "recall.db")
    return tmp_path


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "FILE", tmp_path / "memory.json")
    monkeypatch.setattr(memory, "_cache", None)
    return memory


# ─── recall ────────────────────────────────────────────────────────────────────

async def test_record_and_search(recall_db):
    recall.record_run("rotate the github api token", "Rotated and stored in vault.",
                      ["gh", "bash"])
    recall.record_run("what's the weather", "28C sunny", [])
    hits = await recall.search("github token")
    assert len(hits) == 1
    assert "rotate the github api token" in hits[0]
    assert "Rotated and stored in vault." in hits[0]


async def test_search_no_match(recall_db):
    recall.record_run("open slack", "Opened Slack.", ["open_app"])
    assert await recall.search("kubernetes deployment") == []


async def test_search_hostile_query_does_not_crash(recall_db):
    recall.record_run("check disk space", "269Gi free", ["bash"])
    # FTS5 syntax chars must be neutralized, not passed through.
    assert await recall.search('disk" OR *') != None  # noqa: E711 — just must not raise
    assert await recall.search("") == []


async def test_days_back_filter(recall_db):
    recall.record_run("ancient task", "done", [], ts="2020-01-01T10:00:00")
    recall.record_run("recent task", "done", [])
    hits = await recall.search("task", days_back=30)
    assert len(hits) == 1
    assert "recent task" in hits[0]


async def test_backfill_from_traces(recall_db, tmp_path):
    day = tmp_path / "traces" / "20260701"
    day.mkdir(parents=True)
    (day / "abc123.jsonl").write_text("\n".join([
        json.dumps({"ts": "2026-07-01T10:00:00", "type": "run_start",
                    "user_text": "restart the staging pod"}),
        json.dumps({"ts": "2026-07-01T10:00:01", "type": "tool_start", "tool": "bash"}),
        json.dumps({"ts": "2026-07-01T10:00:05", "type": "run_end",
                    "final_text": "Pod restarted, sir."}),
    ]))
    added = recall.backfill_from_traces(tmp_path / "traces")
    assert added == 1
    hits = await recall.search("staging pod", days_back=3650)
    assert hits and "restart the staging pod" in hits[0]
    # Second backfill is a no-op (DB non-empty).
    assert recall.backfill_from_traces(tmp_path / "traces") == 0


# ─── memory facts ──────────────────────────────────────────────────────────────

def test_remember_and_snapshot(mem):
    out = memory.remember("preference", "PR merges", "always squash")
    assert "Remembered" in out
    memory.remember("person", "Alice Chen", "teammate, handles deploys")
    snap = json.loads(memory.snapshot_for_prompt())
    assert snap["preferences"] == {"PR merges": "always squash"}
    assert snap["people"] == {"Alice Chen": "teammate, handles deploys"}


def test_forget(mem):
    memory.remember("preference", "editor", "vscode")
    assert "Forgot" in memory.forget("editor")
    assert "No stored fact" in memory.forget("editor")


def test_remember_bad_kind(mem):
    assert memory.remember("nonsense", "k", "v").startswith("Error")


def test_record_task_hour_histogram(mem):
    memory.record_task("morning briefing", True)
    memory.record_task("morning briefing", True)
    memory.record_task("failed thing", False)      # failures not recorded
    tasks = memory.load()["frequent_tasks"]
    assert len(tasks) == 1
    assert tasks[0]["count"] == 2
    assert sum(tasks[0]["hours"]) == 2

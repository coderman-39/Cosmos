"""services.learning: lessons dedup, tool-health stats, learned routing."""

import pytest

from services import learning


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(learning, "STATS_FILE", tmp_path / "tool_stats.json")
    monkeypatch.setattr(learning, "ROUTES_FILE", tmp_path / "route_memory.json")
    monkeypatch.setattr(learning, "_stats", {})
    monkeypatch.setattr(learning, "_last_error", {})
    monkeypatch.setattr(learning, "_stats_loaded", False)
    monkeypatch.setattr(learning, "_routes", None)


# ─── Lessons ───────────────────────────────────────────────────────────────────

def test_lesson_stored_and_topped():
    learning.add_lesson("WHEN clicking web links DO use click_web not mouse")
    assert learning.top_lessons() == ["WHEN clicking web links DO use click_web not mouse"]


def test_near_duplicate_bumps_count_instead_of_duplicating():
    learning.add_lesson("WHEN gmail attach fails DO use gmail_attach tool")
    learning.add_lesson("WHEN gmail attach fails DO use the gmail_attach tool")
    learning.add_lesson("WHEN reading PDFs DO use read_document")
    lessons = learning.top_lessons(10)
    assert len(lessons) == 2
    # The duplicated lesson ranks first (count 2).
    assert "gmail" in lessons[0].lower()


def test_empty_and_oversized_lessons_rejected():
    learning.add_lesson("")
    learning.add_lesson("x" * 300)
    assert learning.top_lessons() == []


# ─── Tool health ───────────────────────────────────────────────────────────────

def test_degraded_tool_flagged_after_failures():
    for _ in range(6):
        learning.record_tool("click_ui", False, "osascript error -1743 not authorised")
    flagged = learning.degraded_tools()
    assert len(flagged) == 1
    assert "click_ui" in flagged[0]
    assert "TCC" in flagged[0], "permission-shaped errors must name the fix"


def test_healthy_tool_not_flagged():
    for _ in range(10):
        learning.record_tool("bash", True)
    assert learning.degraded_tools() == []


def test_too_few_samples_not_flagged():
    learning.record_tool("terraform", False, "500")
    learning.record_tool("terraform", False, "500")
    assert learning.degraded_tools() == []


def test_stats_persist_and_reload(tmp_path, monkeypatch):
    for _ in range(6):
        learning.record_tool("mouse", False, "boom")
    # Simulate restart
    monkeypatch.setattr(learning, "_stats", {})
    monkeypatch.setattr(learning, "_stats_loaded", False)
    assert any("mouse" in d for d in learning.degraded_tools())


# ─── Route memory ──────────────────────────────────────────────────────────────

def test_route_recorded_and_hinted():
    assert learning.route_hint("check ci status") is None
    learning.record_route("Check CI status?", True)
    assert learning.route_hint("check ci status") is True
    learning.record_route("check ci status", False)   # later run used writes
    assert learning.route_hint("check ci status") is False


def test_route_lru_bounded(monkeypatch):
    monkeypatch.setattr(learning, "_MAX_ROUTES", 5)
    for i in range(10):
        learning.record_route(f"task number {i}", True)
    assert learning.route_hint("task number 0") is None     # evicted
    assert learning.route_hint("task number 9") is True

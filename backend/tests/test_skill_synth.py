"""Skill synthesis (F4): pattern detection + save_skill self-modification.

Contracts:
  - The 3rd similar multi-step run produces exactly ONE suggestion, ever.
  - Trivial/uniform runs (few tools, one tool repeated) never count.
  - save_skill validates hard (kebab-case, size, protected names), writes
    under backend/skills/, and busts the prompt caches immediately.
  - save_skill confirms in BOTH permission modes (self-modification).
"""

import pytest

from services import agent, skill_synth


@pytest.fixture
def synth(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_synth, "CANDIDATES", tmp_path / "cands.json")
    monkeypatch.setattr(skill_synth, "SKILLS_DIR", tmp_path / "skills")
    return tmp_path


SEQ = ["gh", "docker", "slack_dm"]


def test_third_similar_run_suggests_once(synth):
    assert skill_synth.observe("check alice's pr", SEQ) is None
    assert skill_synth.observe("check bob's pr", SEQ) is None
    tip = skill_synth.observe("check carol's pr", SEQ)
    assert tip and "save it as a reusable skill" in tip
    assert "check alice's pr" in tip
    # Never nags again for the same pattern.
    assert skill_synth.observe("check someone else's pr", SEQ) is None


def test_trivial_runs_never_count(synth):
    assert skill_synth._signature(["say", "ask_user"]) is None       # ignored tools
    assert skill_synth._signature(["bash", "bash", "bash"]) is None  # uniform
    assert skill_synth._signature(["bash", "web_search"]) is None    # too short
    assert skill_synth._signature(["gh", "docker", "slack_dm"]) == "gh,docker,slack_dm"
    # Consecutive duplicates collapse into the same signature.
    assert (skill_synth._signature(["gh", "gh", "docker", "slack_dm"])
            == "gh,docker,slack_dm")


def test_different_patterns_tracked_separately(synth):
    other = ["web_search", "fetch_url", "write_file"]
    for i in range(2):
        skill_synth.observe(f"review {i}", SEQ)
        skill_synth.observe(f"research {i}", other)
    assert skill_synth.observe("review 3", SEQ)      # 3rd of pattern A
    assert skill_synth.observe("research 3", other)  # 3rd of pattern B


def test_corrupt_candidates_degrade(synth):
    skill_synth.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    skill_synth.CANDIDATES.write_text("{broken")
    assert skill_synth.observe("x", SEQ) is None     # count restarts, no crash


# ─── save_skill ────────────────────────────────────────────────────────────────

_GOOD = ("# Review a PR\n\n1. gh: fetch the PR diff and checks.\n"
         "2. docker: run the test container.\n3. slack_dm the requester the result.\n")


def test_save_skill_writes_and_busts_caches(synth, monkeypatch):
    monkeypatch.setattr(agent, "_skills_cache", "stale")
    monkeypatch.setattr(agent, "_stable_prompt_cache", "stale")
    out = skill_synth.save_skill("review-a-pr", _GOOD)
    assert out.startswith("Saved new skill 'review-a-pr'")
    assert (skill_synth.SKILLS_DIR / "review-a-pr.md").read_text().startswith("# Review")
    assert agent._skills_cache is None
    assert agent._stable_prompt_cache is None
    # Overwriting your own saved skill is an update, not an error.
    assert skill_synth.save_skill("review-a-pr", _GOOD).startswith("Updated skill")


def test_save_skill_validation(synth):
    assert skill_synth.save_skill("Bad Name!", _GOOD).startswith("Error")
    assert skill_synth.save_skill("", _GOOD).startswith("Error")
    assert skill_synth.save_skill("ok-name", "too short").startswith("Error")
    assert skill_synth.save_skill("ok-name", "x" * 30_000).startswith("Error")
    assert skill_synth.save_skill("research", _GOOD).startswith("Error")  # protected
    assert not (skill_synth.SKILLS_DIR / "ok-name.md").exists()


def test_save_skill_gated_in_both_modes():
    args = {"name": "review-a-pr", "content": _GOOD}
    assert agent.needs_confirmation("save_skill", args, "ask")
    assert agent.needs_confirmation("save_skill", args, "full")
    # Self-modification is 'destructive-class': never batched or pre-approved.
    assert agent._is_destructive("save_skill", args)

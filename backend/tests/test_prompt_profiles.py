"""Two-tier prompt architecture (SPEED_PLAN 3.3).

Read lookups get a slim stable prefix (own prompt-cache entry); the full tier
keeps the complete doctrine + skills. read_skill loads playbooks on demand
(the index mode is opt-in via FRIDAY_SKILLS_INDEX until cache telemetry says
the inline block is actually being re-paid).
"""

from pathlib import Path

from services import agent


def test_read_profile_is_dramatically_smaller():
    full = agent._system_stable()
    slim = agent._system_read_stable()
    assert len(slim) < len(full) / 2
    assert "SKILLS" not in slim          # no playbooks in the read tier
    assert "sir" in slim                 # persona survives


def test_system_blocks_profile_selects_prefix():
    full_blocks = agent._system_blocks(profile="full")
    read_blocks = agent._system_blocks(profile="read")
    assert full_blocks[0]["text"] == agent._system_stable()
    assert read_blocks[0]["text"] == agent._system_read_stable()
    # both stable blocks carry the prompt-cache breakpoint
    assert full_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert read_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # the volatile tail is present either way
    assert "LIVE CONTEXT" in full_blocks[1]["text"]
    assert "LIVE CONTEXT" in read_blocks[1]["text"]


def test_skills_index_lists_every_playbook():
    idx = agent._load_skills_index()
    skills_dir = Path(agent.__file__).parent.parent / "skills"
    for f in skills_dir.glob("*.md"):
        assert f"- {f.stem}" in idx
    # index must be a fraction of the full inline block
    assert len(idx) < len(agent._load_skills()) / 4


async def test_read_skill_loads_by_name():
    skills_dir = Path(agent.__file__).parent.parent / "skills"
    name = sorted(f.stem for f in skills_dir.glob("*.md"))[0]
    out = await agent._tool_read_skill({"name": name}, None)
    assert not out.startswith("Error")
    assert out.strip()


async def test_read_skill_rejects_traversal_and_unknown():
    out = await agent._tool_read_skill({"name": "../../backend/.env"}, None)
    assert out.startswith("Error")
    assert "Available:" in out
    out = await agent._tool_read_skill({"name": "no-such-skill"}, None)
    assert out.startswith("Error")


def test_read_skill_is_read_only_and_verify_skipped():
    assert "read_skill" in agent._READ_ONLY_TOOLS
    assert "read_skill" in agent._VERIFY_SKIP_TOOLS
    assert any(t.get("name") == "read_skill" for t in agent.TOOLS)

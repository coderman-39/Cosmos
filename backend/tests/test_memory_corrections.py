"""Speech-correction application (main._apply_memory_corrections) and the
rolling conversation summary (agent._remember)."""

import pytest

import main
from services import agent, llm


@pytest.fixture
def corrections(monkeypatch):
    store = {}
    monkeypatch.setattr(main, "memory_get_corrections", lambda: store)
    return store


def test_multiword_correction_matches(corrections):
    corrections["alice chan"] = "Alice Chen"
    out = main._apply_memory_corrections("message alice chan about the sprint")
    assert out == "message Alice Chen about the sprint"


def test_longest_key_wins(corrections):
    corrections["alice"] = "Alice Chen"
    corrections["alice smith"] = "Alice Smith"
    out = main._apply_memory_corrections("dm alice smith please")
    assert "Alice Smith" in out
    assert "Alice Chen" not in out


def test_case_preserved_in_replacement(corrections):
    corrections["jhon"] = "John"
    assert main._apply_memory_corrections("ping Jhon now") == "ping John now"


def test_no_substring_false_positive(corrections):
    corrections["ram"] = "Rama"
    out = main._apply_memory_corrections("check the program logs")
    assert out == "check the program logs"     # "ram" inside "program" untouched


def test_punctuation_boundary(corrections):
    corrections["dokker"] = "Docker"
    assert main._apply_memory_corrections("open dokker, please") == "open Docker, please"


def test_record_correction_keeps_target_case(tmp_path, monkeypatch):
    from services import memory
    monkeypatch.setattr(memory, "FILE", tmp_path / "memory.json")
    monkeypatch.setattr(memory, "_cache", None)
    main.memory_record_correction("Alice Chan ", "Alice Chen")
    stored = main._load_memory()["corrections"]
    assert stored == {"alice chan": "Alice Chen"}


# ─── Rolling summary in agent._remember ────────────────────────────────────────

class _FakeText:
    type = "text"
    def __init__(self, text): self.text = text


class _FakeResp:
    def __init__(self, text): self.content = [_FakeText(text)]


async def test_remember_rolls_summary_pair(monkeypatch):
    async def fake_acreate(**kw):
        return _FakeResp("Summary of the early conversation.")
    monkeypatch.setattr(llm, "acreate", fake_acreate)

    history = []
    for i in range(8):   # fills to the 16-message cap
        await agent._remember(history, f"question {i}", f"answer {i}")
    assert len(history) == 16

    # The 9th exchange appends immediately; the eviction roll is DETACHED
    # (the answer must not wait on a summarizer LLM call). Run the roll to
    # completion explicitly — the background task it spawned is a no-op
    # afterwards (roll lock + re-check).
    await agent._remember(history, "question 8", "answer 8")
    assert len(history) == 18      # appended, not yet rolled
    await agent._roll_history(history)
    assert len(history) == 16      # summary pair + 14 verbatim
    assert history[0]["role"] == "user"
    assert history[0]["content"].startswith(agent._SUMMARY_PREFIX)
    assert "Summary of the early conversation." in history[0]["content"]
    assert history[1] == {"role": "assistant", "content": agent._SUMMARY_ACK}
    # Alternation preserved for claude-* fallbacks
    roles = [e["role"] for e in history]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    # Newest turn kept verbatim
    assert history[-1]["content"] == "answer 8"


async def test_roll_preserves_turns_appended_mid_summary(monkeypatch):
    """A run finishing while the previous run's roll is still summarizing must
    not lose its exchange when the roll rewrites the list."""
    import asyncio

    gate = asyncio.Event()

    async def slow_acreate(**kw):
        await gate.wait()
        return _FakeResp("Early context, condensed.")
    monkeypatch.setattr(llm, "acreate", slow_acreate)

    history = []
    for i in range(9):
        await agent._remember(history, f"q{i}", f"a{i}")

    roll = asyncio.ensure_future(agent._roll_history(history))
    await asyncio.sleep(0)                       # roll is now parked on the LLM
    history.append({"role": "user", "content": "q-late"})
    history.append({"role": "assistant", "content": "a-late"})
    gate.set()
    await roll

    contents = [e["content"] for e in history]
    assert "a-late" in contents                   # the mid-roll turn survived
    assert history[0]["content"].startswith(agent._SUMMARY_PREFIX)


async def test_remember_hard_truncates_on_llm_failure(monkeypatch):
    async def broken_acreate(**kw):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(llm, "acreate", broken_acreate)

    history = []
    for i in range(9):
        await agent._remember(history, f"q{i}", f"a{i}")
    await agent._roll_history(history)
    assert len(history) == agent._HISTORY_CAP     # old behavior: hard cap
    assert not history[0]["content"].startswith(agent._SUMMARY_PREFIX)

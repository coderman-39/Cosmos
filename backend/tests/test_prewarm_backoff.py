"""Prewarm failure handling (observed live: gpt-5.4 429 every 4 minutes).

A background cache ping must never make real runs worse. Two rules:
  - a RATE LIMIT is a property of the key, so acreate's cooldown must STAND
    (real runs back off to their fallback instead of re-paying a doomed call);
  - any other ping failure clears only the cooldown it introduced, and after
    a few tries the tier is dropped from prewarm instead of being hammered.
"""

import time

import pytest

from services import agent, llm


class _FakeRateLimit(Exception):
    pass


_FakeRateLimit.__name__ = "RateLimitError"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(agent, "_prewarm_fails", {})
    monkeypatch.setattr(llm, "_cooldown_until", {})
    monkeypatch.setattr(agent, "FAST_MODEL", "fast-model")
    monkeypatch.setattr(agent, "AGENT_MODEL", "agent-model")


def _acreate_that(exc_for: dict):
    async def fake(**kw):
        m = kw.get("model")
        exc = exc_for.get(m)
        if exc:
            # Mirror acreate's real contract: it sets a cooldown, then raises.
            llm._cooldown_until[m] = time.monotonic() + 120
            raise exc
        return type("R", (), {"content": []})()
    return fake


def test_rate_limit_is_detected():
    assert llm.is_rate_limit(_FakeRateLimit("Error code: 429 - quota"))
    assert llm.is_rate_limit(Exception("Error code: 429"))
    assert not llm.is_rate_limit(Exception("Connection reset by peer"))


async def test_rate_limited_tier_keeps_its_cooldown_and_stops_being_pinged(monkeypatch):
    """The live bug: the 429 cooldown was popped, so real read-lookups
    re-tried the dead model AND prewarm hammered it every 4 minutes."""
    monkeypatch.setattr(llm, "acreate",
                        _acreate_that({"fast-model": _FakeRateLimit("Error code: 429")}))
    await agent.prewarm()

    # acreate's cooldown SURVIVES — real runs skip straight to the fallback.
    assert llm._cooldown_until.get("fast-model", 0) > time.monotonic()
    # ...and the tier is out of prewarm immediately, not after N more 429s.
    assert agent._prewarm_fails["fast-model"] >= agent._PREWARM_MAX_FAILS

    calls = []
    async def counting(**kw):
        calls.append(kw.get("model"))
        return type("R", (), {"content": []})()
    monkeypatch.setattr(llm, "acreate", counting)
    await agent.prewarm()
    assert "fast-model" not in calls        # no more hammering
    assert "agent-model" in calls           # healthy tier unaffected


async def test_transient_failure_clears_only_its_own_cooldown(monkeypatch):
    monkeypatch.setattr(llm, "acreate",
                        _acreate_that({"fast-model": Exception("connection reset")}))
    await agent.prewarm()
    # A synthetic 16-token ping failing says little about real runs — the
    # cooldown it introduced is cleared so a real command still tries.
    assert "fast-model" not in llm._cooldown_until
    assert agent._prewarm_fails["fast-model"] == 1


async def test_repeated_transient_failures_drop_the_tier(monkeypatch):
    monkeypatch.setattr(llm, "acreate",
                        _acreate_that({"fast-model": Exception("boom")}))
    for _ in range(agent._PREWARM_MAX_FAILS):
        await agent.prewarm()
    assert agent._prewarm_fails["fast-model"] >= agent._PREWARM_MAX_FAILS

    calls = []
    async def counting(**kw):
        calls.append(kw.get("model"))
        return type("R", (), {"content": []})()
    monkeypatch.setattr(llm, "acreate", counting)
    await agent.prewarm()
    assert "fast-model" not in calls


async def test_success_resets_the_failure_count(monkeypatch):
    monkeypatch.setattr(llm, "acreate",
                        _acreate_that({"fast-model": Exception("blip")}))
    await agent.prewarm()
    assert agent._prewarm_fails["fast-model"] == 1
    monkeypatch.setattr(llm, "acreate", _acreate_that({}))
    await agent.prewarm()
    assert "fast-model" not in agent._prewarm_fails

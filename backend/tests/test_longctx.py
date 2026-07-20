"""Long-context routing (Lever 1): the 1M-token window switch.

Contract under test:
  - llm.long_context_variant() tags a bare model, is idempotent, and never
    double-tags an already-windowed id.
  - llm.estimate_tokens() is a cheap char/4 estimate.
  - run_task() upgrades the whole chain to the [1m] variant ONCE the assembled
    context crosses the threshold, and leaves normal-sized runs alone.
"""

import pytest

from services import agent, learning, llm
from services import raptor
from services import recall as recall_svc
from services.agent import Interaction


# ─── unit: llm helpers ─────────────────────────────────────────────────────────

def test_long_context_variant_tags_bare_model(monkeypatch):
    # Plain OpenAI has no window-variant tags (default suffix "") — the tagging
    # machinery is kept for gateways that use them, so tests opt in explicitly.
    monkeypatch.setattr(llm, "LONGCTX_SUFFIX", "[1m]")
    assert llm.long_context_variant("gpt-5.5") == "gpt-5.5[1m]"


def test_long_context_variant_is_identity_without_suffix(monkeypatch):
    monkeypatch.setattr(llm, "LONGCTX_SUFFIX", "")
    assert llm.long_context_variant("gpt-5.6") == "gpt-5.6"


def test_long_context_variant_is_idempotent():
    assert llm.long_context_variant("gpt-5.5[1m]") == "gpt-5.5[1m]"


def test_long_context_variant_leaves_other_brackets_alone():
    assert llm.long_context_variant("glm5p2[1m]") == "glm5p2[1m]"
    assert llm.long_context_variant("foo[128k]") == "foo[128k]"


def test_long_context_chain_maps_all(monkeypatch):
    monkeypatch.setattr(llm, "LONGCTX_SUFFIX", "[1m]")
    assert llm.long_context_chain(["gpt-5.5", "gpt-5.6-mini"]) == \
        ["gpt-5.5[1m]", "gpt-5.6-mini[1m]"]


def test_estimate_tokens_is_char_over_four():
    assert llm.estimate_tokens("x" * 4000) == 1000
    assert llm.estimate_tokens("a" * 400, "b" * 400) == 200


def test_system_char_len_handles_blocks_and_strings():
    assert agent._system_char_len("hello") == 5
    assert agent._system_char_len(
        [{"type": "text", "text": "ab"}, {"type": "text", "text": "cde"}]) == 5


# ─── integration: run_task upgrades the chain ──────────────────────────────────

class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = None
        self.model = "model-A"


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    from services import trace as trace_mod, system_control

    async def no_focus():
        return ""

    async def no_rebuild():
        return 0

    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(system_control, "get_focus_context", no_focus)
    monkeypatch.setattr(recall_svc, "record_run", lambda *a, **k: None)
    monkeypatch.setattr(raptor, "maybe_rebuild", no_rebuild)
    monkeypatch.setattr(learning, "record_route", lambda *a, **k: None)
    monkeypatch.setattr(learning, "record_tool", lambda *a, **k: None)
    monkeypatch.setattr(learning, "route_hint", lambda t: None)
    monkeypatch.setattr(learning, "top_lessons", lambda n=5: [])
    monkeypatch.setattr(learning, "degraded_tools", lambda: [])
    monkeypatch.setattr(llm, "STREAM_ENABLED", False)
    monkeypatch.setattr(agent, "_VERIFY_ENABLED", False)


async def _run_capturing_model(monkeypatch, user_text, history):
    seen = []

    async def fake_acreate(**kwargs):
        seen.append(kwargs.get("model"))
        return _Resp([_TextBlock("Done, sir.")])

    monkeypatch.setattr(llm, "acreate", fake_acreate)

    async def emit(_ev):
        pass

    await agent.run_task(user_text, emit, Interaction(), history=history, mode="full")
    return seen


async def test_upgrades_to_1m_when_context_large(monkeypatch, quiet):
    monkeypatch.setattr(llm, "LONGCTX_ENABLED", True)
    monkeypatch.setattr(llm, "LONGCTX_SUFFIX", "[1m]")
    monkeypatch.setattr(llm, "LONGCTX_THRESHOLD_TOKENS", 100)   # ~400 chars
    big = "x" * 6000
    history = [{"role": "user", "content": big},
               {"role": "assistant", "content": big}]
    # "send ..." has an action verb → routes to the AGENT model (model-A), not fast.
    seen = await _run_capturing_model(monkeypatch, "send the full report now", history)
    assert seen and seen[0] == "model-A[1m]"


async def test_no_upgrade_for_normal_context(monkeypatch, quiet):
    monkeypatch.setattr(llm, "LONGCTX_ENABLED", True)
    monkeypatch.setattr(llm, "LONGCTX_THRESHOLD_TOKENS", 180_000)
    seen = await _run_capturing_model(monkeypatch, "send the report", [])
    assert seen and seen[0] == "model-A"        # bare, un-windowed


async def test_disabled_flag_prevents_upgrade(monkeypatch, quiet):
    monkeypatch.setattr(llm, "LONGCTX_ENABLED", False)
    monkeypatch.setattr(llm, "LONGCTX_THRESHOLD_TOKENS", 100)
    big = "x" * 6000
    history = [{"role": "user", "content": big}]
    seen = await _run_capturing_model(monkeypatch, "send the report", history)
    assert seen and seen[0] == "model-A"        # not upgraded despite huge context

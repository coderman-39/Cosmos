"""Prompt-cache split in services.agent.

The stable prefix must carry NO volatile data (no clock) so the cache survives
across turns; the volatile tail carries the clock; cache_control breakpoints sit
on exactly the right blocks.
"""

import re

from services import agent


_HHMM = re.compile(r"\b\d{1,2}:\d{2}\b")


def test_stable_prompt_has_no_hhmm_timestamp():
    # A HH:MM clock in the stable block would bust the cache every minute.
    assert not _HHMM.search(agent._system_stable())


def test_volatile_prompt_has_hhmm_timestamp():
    assert _HHMM.search(agent._system_volatile())


def test_system_blocks_cache_control_on_first_block_only():
    blocks = agent._system_blocks()
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_tools_for_request_cache_breakpoint_on_last_tool():
    tools = agent._tools_for_request()
    assert len(tools) == len(agent.TOOLS)
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}
    # Only the final tool carries the breakpoint.
    assert all("cache_control" not in t for t in tools[:-1])


def test_tools_for_request_does_not_mutate_source_tools():
    # The cache marker must live on a copy, never on the shared TOOLS list.
    agent._tools_for_request()
    assert all("cache_control" not in t for t in agent.TOOLS)


# ─── Rolling message cache marker ──────────────────────────────────────────────

def test_rotate_marks_last_string_message():
    msgs = [{"role": "user", "content": "hello"}]
    agent._rotate_message_cache_marker(msgs)
    content = msgs[-1]["content"]
    assert isinstance(content, list)
    assert content[-1]["cache_control"] == {"type": "ephemeral"}
    assert content[-1]["text"] == "hello"


def test_rotate_moves_marker_forward():
    msgs = [{"role": "user", "content": "first"}]
    agent._rotate_message_cache_marker(msgs)
    msgs.append({"role": "assistant", "content": "ok"})
    msgs.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "result"}]})
    agent._rotate_message_cache_marker(msgs)
    # Old marker stripped, new one on the last tool_result
    assert "cache_control" not in msgs[0]["content"][-1]
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_rotate_ignores_non_dict_blocks():
    class FakeSdkBlock:
        type = "text"
        text = "assistant said"
    msgs = [{"role": "assistant", "content": [FakeSdkBlock()]}]
    agent._rotate_message_cache_marker(msgs)   # must not raise or mutate
    assert not hasattr(msgs[0]["content"][-1], "cache_control")


# ─── Old tool_result compaction ────────────────────────────────────────────────

def _tool_result_msg(text):
    return {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": text}]}


def test_compact_noop_under_budget():
    msgs = [_tool_result_msg("x" * 100) for _ in range(10)]
    assert agent._compact_messages(msgs, budget_chars=1_000_000) == 0


def test_compact_truncates_old_big_results_keeps_tail():
    big = "y" * 5000
    msgs = [_tool_result_msg(big) for _ in range(10)]
    n = agent._compact_messages(msgs, keep_tail=6, budget_chars=10_000)
    assert n == 4, "only the 4 messages outside the tail should compact"
    for m in msgs[:4]:
        c = m["content"][0]["content"]
        assert c.endswith(agent._COMPACT_MARKER) and len(c) < 400
    for m in msgs[4:]:
        assert m["content"][0]["content"] == big


def test_compact_idempotent():
    big = "z" * 5000
    msgs = [_tool_result_msg(big) for _ in range(10)]
    agent._compact_messages(msgs, keep_tail=6, budget_chars=10_000)
    snapshot = [m["content"][0]["content"] for m in msgs]
    agent._compact_messages(msgs, keep_tail=6, budget_chars=10_000)
    assert [m["content"][0]["content"] for m in msgs] == snapshot

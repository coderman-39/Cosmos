"""services.llm: translation layer, acreate fallback chain, sanitize_error.

No real network/LLM calls: acreate tests monkeypatch llm.get_async_client() to
return a hand-built fake whose chat.completions.create is fully under test
control, returning OpenAI-shaped responses.
"""

import asyncio
import time
import types

import pytest

from services import llm


# ─── Fake response/block plumbing ──────────────────────────────────────────────

def _block(btype, text):
    return types.SimpleNamespace(type=btype, text=text)


def _resp(*blocks):
    return types.SimpleNamespace(content=list(blocks))


def _oai_resp(text: "str | None" = "ok", finish="stop", model="A", tool_calls=None):
    """Minimal OpenAI ChatCompletion look-alike."""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=text, tool_calls=tool_calls),
            finish_reason=finish)],
        model=model, usage=None)


def _oai_tool_call(cid, name, arguments):
    return types.SimpleNamespace(
        id=cid, type="function",
        function=types.SimpleNamespace(name=name, arguments=arguments))


# ─── extract_text ──────────────────────────────────────────────────────────────

def test_extract_text_returns_first_real_text():
    resp = _resp(_block("text", "hello sir"), _block("text", "second"))
    assert llm.extract_text(resp) == "hello sir"


def test_extract_text_skips_leading_non_text_block():
    resp = _resp(_block("thinking", None), _block("text", "the answer"))
    assert llm.extract_text(resp) == "the answer"


def test_extract_text_empty_when_only_thinking_or_empty():
    resp = _resp(_block("thinking", None), _block("text", ""), _block("thinking", "x"))
    assert llm.extract_text(resp) == ""


# ─── Translation: internal shape → OpenAI ──────────────────────────────────────

def test_system_blocks_concatenated_and_cache_control_stripped():
    msgs = llm.to_openai_messages(
        [{"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
         {"type": "text", "text": "volatile"}],
        [{"role": "user", "content": "hi"}])
    assert msgs[0] == {"role": "system", "content": "stable\n\nvolatile"}
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_assistant_tool_use_becomes_tool_calls():
    blk = types.SimpleNamespace(type="tool_use", id="t1", name="bash",
                                input={"cmd": "ls"})
    msgs = llm.to_openai_messages(None, [{"role": "assistant", "content": [blk]}])
    (m,) = msgs
    assert m["role"] == "assistant"
    assert m["tool_calls"][0]["id"] == "t1"
    assert m["tool_calls"][0]["function"]["name"] == "bash"
    assert '"cmd"' in m["tool_calls"][0]["function"]["arguments"]


def test_tool_result_becomes_tool_message():
    msgs = llm.to_openai_messages(None, [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "out"}]}])
    assert msgs == [{"role": "tool", "tool_call_id": "t1", "content": "out"}]


def test_image_block_becomes_data_url_part():
    msgs = llm.to_openai_messages(None, [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": "AAA"}}]}])
    (m,) = msgs
    parts = m["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAA"


def test_tools_translated_to_function_schema():
    out = llm.to_openai_tools([{"name": "bash", "description": "run",
                                "input_schema": {"type": "object", "properties": {}}}])
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "bash"
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}


# ─── Translation: OpenAI → internal shape ──────────────────────────────────────

def test_from_openai_maps_tool_calls_and_stop_reason():
    resp = llm._from_openai(_oai_resp(
        text=None, finish="tool_calls",
        tool_calls=[_oai_tool_call("t9", "read_file", '{"path": "/x"}')]))
    assert resp.stop_reason == "tool_use"
    (blk,) = resp.content
    assert blk.type == "tool_use" and blk.name == "read_file"
    assert blk.input == {"path": "/x"}


def test_from_openai_maps_length_and_malformed_args():
    resp = llm._from_openai(_oai_resp(
        text="partial", finish="length",
        tool_calls=[_oai_tool_call("t1", "bash", "{not json")]))
    assert resp.stop_reason == "max_tokens"
    assert resp.content[0].text == "partial"
    assert resp.content[1].input == {}   # malformed args degrade to empty dict


# ─── Fake async client for acreate ─────────────────────────────────────────────

class _FakeCompletions:
    def __init__(self, behavior):
        # behavior: dict[model] -> Exception instance (raise) | response (return)
        self.behavior = behavior
        self.calls = []

    async def create(self, *, model, **kwargs):
        self.calls.append(model)
        outcome = self.behavior[model]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, behavior):
        self.completions = _FakeCompletions(behavior)
        self.chat = types.SimpleNamespace(completions=self.completions)


@pytest.fixture
def patch_client(monkeypatch):
    """Install a fake client with the given per-model behavior; hands back the
    client so tests can inspect .completions.calls."""
    def _install(behavior):
        client = _FakeClient(behavior)
        monkeypatch.setattr(llm, "get_async_client", lambda: client)
        return client
    return _install


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    llm._cooldown_until.clear()
    yield
    llm._cooldown_until.clear()


# ─── acreate: A → B fallthrough ────────────────────────────────────────────────

async def test_acreate_falls_through_to_second_model(patch_client):
    client = patch_client({"A": RuntimeError("A down"), "B": _oai_resp("from B")})
    result = await llm.acreate(model="A", fallbacks=["B"], max_tokens=10,
                               messages=[], system="s")
    assert llm.extract_text(result) == "from B"
    assert client.completions.calls == ["A", "B"]


async def test_acreate_on_fallback_called_with_failed_next_exc(patch_client):
    exc = RuntimeError("A down")
    patch_client({"A": exc, "B": _oai_resp()})
    seen = []

    async def on_fb(failed, nxt, e):
        seen.append((failed, nxt, e))

    await llm.acreate(model="A", fallbacks=["B"], on_fallback=on_fb,
                      max_tokens=10, messages=[], system="s")
    assert seen == [("A", "B", exc)]


async def test_acreate_empty_chain_raises_valueerror(patch_client):
    patch_client({})
    with pytest.raises(ValueError):
        await llm.acreate(model="", fallbacks=[], max_tokens=10,
                          messages=[], system="s")


async def test_acreate_cancelled_propagates_not_swallowed(patch_client):
    client = patch_client({"A": asyncio.CancelledError(), "B": _oai_resp()})
    with pytest.raises(asyncio.CancelledError):
        await llm.acreate(model="A", fallbacks=["B"], max_tokens=10,
                          messages=[], system="s")
    # It must NOT have fallen through to B — cancellation is not a retryable error.
    assert client.completions.calls == ["A"]


async def test_acreate_failed_model_cooled_down_and_skipped(patch_client):
    # First call: A fails, B succeeds → A goes on cooldown.
    client = patch_client({"A": RuntimeError("A down"), "B": _oai_resp()})
    await llm.acreate(model="A", fallbacks=["B"], max_tokens=10, messages=[], system="s")
    assert client.completions.calls == ["A", "B"]
    assert "A" in llm._cooldown_until and llm._cooldown_until["A"] > time.monotonic()

    # Immediate next call with the same chain: A is skipped (still cooling),
    # B is tried directly.
    client.completions.calls.clear()
    await llm.acreate(model="A", fallbacks=["B"], max_tokens=10, messages=[], system="s")
    assert client.completions.calls == ["B"], "cooled-down model A should be skipped"


async def test_acreate_success_clears_cooldown(patch_client):
    # B is cooling but is the ONLY chain member → full chain tried anyway;
    # success must clear the cooldown.
    patch_client({"B": _oai_resp()})
    llm._cooldown_until["B"] = time.monotonic() + 999
    result = await llm.acreate(model="B", fallbacks=[], max_tokens=10,
                               messages=[], system="s")
    assert llm.extract_text(result) == "ok"
    assert "B" not in llm._cooldown_until, "success must clear the cooldown"


# ─── sanitize_error ────────────────────────────────────────────────────────────

def test_sanitize_error_redacts_sk_key():
    exc = RuntimeError("auth failed for key sk-abcdEFGH1234567890 rejected")
    out = llm.sanitize_error(exc, cap=200)
    assert "sk-abcdEFGH1234567890" not in out
    assert "[redacted]" in out
    assert "RuntimeError" in out  # type name preserved


def test_sanitize_error_redacts_long_hex_token():
    hexhash = "a" * 40  # >= 32 hex chars
    exc = ValueError(f"virtual key {hexhash} over budget")
    out = llm.sanitize_error(exc, cap=200)
    assert hexhash not in out
    assert "[redacted]" in out
    assert "ValueError" in out


def test_sanitize_error_respects_cap():
    exc = RuntimeError("x" * 500)
    assert len(llm.sanitize_error(exc, cap=50)) == 50

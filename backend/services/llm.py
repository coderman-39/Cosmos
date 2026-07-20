"""LLM service — OpenAI API (async client), GPT-5.6 by default.

The agent loop speaks an Anthropic-style message shape internally (content
blocks, tool_use / tool_result, system= as a separate argument) — it predates
this port and every service is written against it. This module is the ONLY
place that shape meets the wire: acreate()/astream() translate to OpenAI
Chat Completions on the way out and back into block-shaped responses on the
way in, so the 3.6k-line agent loop needed zero changes for the port.

Translation contract:
  system (str | [text blocks])       → one "system" message (cache_control
                                       markers stripped — OpenAI caches
                                       prompts automatically)
  {"type":"text"} / {"type":"image"} → content parts (images become data: URLs)
  assistant tool_use blocks          → tool_calls (arguments JSON-encoded)
  {"type":"tool_result"}             → "tool" role messages (images inside a
                                       tool result ride in a follow-up user
                                       message — tool messages are text-only)
  tools [{name, input_schema}]       → [{"type":"function", ...}]
  max_tokens                         → max_completion_tokens
  finish_reason tool_calls/length    → stop_reason "tool_use"/"max_tokens"

Model fallback chains (env-overridable):
  agent : gpt-5.6 → gpt-5.6-mini   (primary quality → same-family fast twin)
  fast  : gpt-5.6-mini             (quick reads / routing / summaries)
"""

import asyncio
import json
import os
import re
import time
from types import SimpleNamespace
from typing import Awaitable, Callable

import httpx
import openai

_client: "openai.AsyncOpenAI | None" = None

# reasoning_effort keeps GPT-5.x's thinking pass proportionate: 'low' preserves
# correct tool-selection/planning while cutting hidden-reasoning latency on
# every call. Set FRIDAY_REASONING_EFFORT=default to send nothing.
REASONING_EFFORT = os.getenv("FRIDAY_REASONING_EFFORT", "low")
STREAM_ENABLED   = os.getenv("FRIDAY_STREAM", "1").lower() not in ("0", "false", "no")


def reasoning_kwargs(model: str = "") -> dict:
    """extra_body for chat.completions.create() to control thinking depth."""
    if REASONING_EFFORT and REASONING_EFFORT != "default":
        return {"extra_body": {"reasoning_effort": REASONING_EFFORT}}
    return {}


def _split_env(name: str, default: str) -> list[str]:
    return [m.strip() for m in os.getenv(name, default).split(",") if m.strip()]


DEFAULT_MODEL   = os.getenv("FRIDAY_DEFAULT_MODEL", "gpt-5.6")
AGENT_FALLBACKS = _split_env("FRIDAY_AGENT_FALLBACKS", "gpt-5.6-mini")
FAST_FALLBACKS  = _split_env("FRIDAY_FAST_FALLBACKS", "gpt-5.6-mini")


# ─── Long-context routing ──────────────────────────────────────────────────────
# OpenAI models expose their full context window on the base id — there is no
# gateway-style "[1m]" variant to switch to, so the variant hooks are identity
# by default. FRIDAY_LONGCTX_SUFFIX stays for gateways that DO use id tags.
LONGCTX_ENABLED   = os.getenv("FRIDAY_LONGCTX", "1").lower() not in ("0", "false", "no")
LONGCTX_SUFFIX    = os.getenv("FRIDAY_LONGCTX_SUFFIX", "")
LONGCTX_THRESHOLD_TOKENS = int(os.getenv("FRIDAY_LONGCTX_THRESHOLD", "180000"))
_CHARS_PER_TOKEN = 4


def long_context_variant(model: str) -> str:
    """Idempotently tag a model id for a gateway's long-window variant. With
    the default empty suffix (plain OpenAI) this is the identity function."""
    m = (model or "").strip()
    if not m or not LONGCTX_SUFFIX or LONGCTX_SUFFIX in m or m.endswith("]"):
        return m
    return m + LONGCTX_SUFFIX


def long_context_chain(models: list[str]) -> list[str]:
    """Apply long_context_variant across a whole (model + fallbacks) chain."""
    return [long_context_variant(m) for m in models]


def estimate_tokens(*text_parts: str) -> int:
    """Cheap char-based token estimate for the long-context switch decision."""
    total = sum(len(p) for p in text_parts if p)
    return total // _CHARS_PER_TOKEN


# API errors can embed the key id; anything shown on the HUD, spoken, or stored
# in history goes through this scrub first.
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}|[a-f0-9]{32,}")


def sanitize_error(exc: Exception, cap: int = 120) -> str:
    return _SECRET_RE.sub("[redacted]", f"{type(exc).__name__}: {exc}")[:cap]


def is_rate_limit(exc: Exception) -> bool:
    """429 / quota exhaustion. Unlike a transient error this is a property of
    the KEY, not of the caller — so a background ping that hits it must let
    the cooldown stand rather than clearing it for real runs."""
    if type(exc).__name__ == "RateLimitError":
        return True
    s = str(exc)[:300].lower()
    return "429" in s or "rate limit" in s or "rate_limit" in s


# A model that just failed sits out for a cooldown window instead of being
# re-attempted (and re-timing-out) on every one of up to 40 agent turns.
# Success clears it. If EVERY chain member is cooling, the full chain is
# tried anyway — cooldown must degrade latency, never availability.
_COOLDOWN_S = float(os.getenv("FRIDAY_MODEL_COOLDOWN", "120"))
_cooldown_until: dict[str, float] = {}

# Callback signature: (failed_model, next_model, exception) — awaited before
# the chain moves on, so the agent can surface the switch on the HUD.
OnFallback = Callable[[str, str, Exception], Awaitable[None]]


def chain_status() -> dict:
    """Honest health snapshot: which models are cooling and for how long."""
    now = time.monotonic()
    return {"cooldowns": {m: round(t - now, 1)
                          for m, t in _cooldown_until.items() if t > now}}


def all_models_cooling(chain: list[str]) -> bool:
    """True when EVERY chain member recently failed — the HUD should warn the
    user instead of letting them wait through a doomed-looking stall."""
    now = time.monotonic()
    return bool(chain) and all(_cooldown_until.get(m, 0.0) > now for m in chain)


# ─── Anthropic-shape ⇄ OpenAI translation ──────────────────────────────────────

def _battr(b, key, default=None):
    """Read a field off a block that may be a dict OR an object (our response
    blocks, routines' SimpleNamespace stand-ins, compaction-built dicts)."""
    if isinstance(b, dict):
        return b.get(key, default)
    return getattr(b, key, default)


def _system_text(system) -> str:
    """system= may be a plain string or a list of text blocks carrying
    cache_control markers (harmless here — OpenAI caches automatically)."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(
            _battr(b, "text", "") or "" for b in system
            if _battr(b, "type") == "text")
    return str(system or "")


def _image_part(block) -> dict | None:
    src = _battr(block, "source") or {}
    data = _battr(src, "data", "")
    if not data:
        return None
    media = _battr(src, "media_type", "image/png")
    return {"type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"}}


def _result_to_text_and_images(content) -> tuple[str, list[dict]]:
    """A tool_result's content: str, or a list of text/image blocks."""
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for b in content or []:
        btype = _battr(b, "type")
        if btype == "text":
            texts.append(_battr(b, "text", "") or "")
        elif btype == "image":
            part = _image_part(b)
            if part:
                images.append(part)
    return "\n".join(t for t in texts if t), images


def to_openai_messages(system, messages: list[dict]) -> list[dict]:
    """Translate the internal Anthropic-shaped conversation to OpenAI format."""
    out: list[dict] = []
    sys_text = _system_text(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, str):
            if content or role == "assistant":
                out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            texts, tool_calls = [], []
            for b in content:
                btype = _battr(b, "type")
                if btype == "text" and (_battr(b, "text") or ""):
                    texts.append(_battr(b, "text"))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": _battr(b, "id", ""),
                        "type": "function",
                        "function": {
                            "name": _battr(b, "name", ""),
                            "arguments": json.dumps(_battr(b, "input") or {},
                                                    ensure_ascii=False),
                        }})
            msg: dict = {"role": "assistant",
                         "content": "\n".join(texts) if texts else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        # user turn: tool_results must land as "tool" messages IMMEDIATELY
        # after the assistant tool_calls turn; remaining parts follow as one
        # ordinary user message (images inside tool results ride here too —
        # tool messages are text-only in Chat Completions).
        pending_parts: list[dict] = []
        for b in content:
            btype = _battr(b, "type")
            if btype == "tool_result":
                text, images = _result_to_text_and_images(_battr(b, "content"))
                out.append({"role": "tool",
                            "tool_call_id": _battr(b, "tool_use_id", ""),
                            "content": text or "(no output)"})
                if images:
                    pending_parts.append(
                        {"type": "text", "text": "Image output of the tool call above:"})
                    pending_parts.extend(images)
            elif btype == "text":
                if _battr(b, "text"):
                    pending_parts.append({"type": "text", "text": _battr(b, "text")})
            elif btype == "image":
                part = _image_part(b)
                if part:
                    pending_parts.append(part)
        if pending_parts:
            # Collapse a lone text part to a plain string (cheaper, canonical).
            if len(pending_parts) == 1 and pending_parts[0]["type"] == "text":
                out.append({"role": "user", "content": pending_parts[0]["text"]})
            else:
                out.append({"role": "user", "content": pending_parts})
    return out


def to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    return [{"type": "function",
             "function": {"name": t["name"],
                          "description": t.get("description", ""),
                          "parameters": t.get("input_schema",
                                              {"type": "object", "properties": {}})}}
            for t in tools]


_STOP_MAP = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}


def _parse_args(raw: str) -> dict:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _from_openai(resp) -> SimpleNamespace:
    """OpenAI ChatCompletion → block-shaped response (.content/.stop_reason)."""
    choice = resp.choices[0]
    msg = choice.message
    blocks = []
    if getattr(msg, "content", None):
        blocks.append(SimpleNamespace(type="text", text=msg.content))
    for tc in getattr(msg, "tool_calls", None) or []:
        blocks.append(SimpleNamespace(
            type="tool_use", id=tc.id, name=tc.function.name,
            input=_parse_args(tc.function.arguments)))
    stop = _STOP_MAP.get(getattr(choice, "finish_reason", "") or "", "end_turn")
    return SimpleNamespace(content=blocks, stop_reason=stop,
                           model=getattr(resp, "model", ""),
                           usage=getattr(resp, "usage", None))


def _request_kwargs(kwargs: dict) -> dict:
    """Adapt an Anthropic-shaped call's kwargs to chat.completions.create()."""
    kw = dict(kwargs)
    system = kw.pop("system", None)
    messages = kw.pop("messages", [])
    tools = to_openai_tools(kw.pop("tools", None))
    out: dict = {"messages": to_openai_messages(system, messages)}
    if tools:
        out["tools"] = tools
    if "max_tokens" in kw:
        out["max_completion_tokens"] = kw.pop("max_tokens")
    for k in ("temperature", "top_p", "stop", "seed"):
        if k in kw:
            out[k] = kw.pop(k)
    # Anything left is Anthropic-only (metadata, stop_sequences …) — drop it
    # rather than 400 the request.
    return out


# ─── Public entry points ───────────────────────────────────────────────────────

async def acreate(*, model: str, fallbacks: list[str] | None = None,
                  on_fallback: OnFallback | None = None, **kwargs):
    """Non-streaming completion with an automatic model-fallback chain.

    Tries `model`, then each entry of `fallbacks` in order. Falls through on
    ANY error except cancellation — timeouts, 429s (budget/rate), 5xx, parse
    errors. CancelledError always propagates: a user "stop" must never be
    absorbed into a retry. Raises the last error if the whole chain fails.
    """
    client = get_async_client()
    chain: list[str] = []
    for m in [model, *(fallbacks if fallbacks is not None else [])]:
        if m and m not in chain:
            chain.append(m)
    if not chain:
        raise ValueError(
            "acreate: no models configured — check FRIDAY_*_MODEL / "
            "FRIDAY_*_FALLBACKS env vars (set-but-empty values yield an empty chain)")

    req = _request_kwargs(kwargs)

    # Skip members in cooldown from a recent failure — unless that would
    # leave nothing to try.
    now = time.monotonic()
    active = [m for m in chain if _cooldown_until.get(m, 0.0) <= now]
    attempt_chain = active or chain

    last_exc: Exception | None = None
    for i, m in enumerate(attempt_chain):
        try:
            # Flat per-request timeout: non-streaming responses buffer server-
            # side, so the client-default 15s read gap would kill healthy long
            # generations. REQUEST_TIMEOUT caps the whole call instead.
            resp = await client.chat.completions.create(
                model=m, timeout=REQUEST_TIMEOUT, **req, **reasoning_kwargs(m))
            _cooldown_until.pop(m, None)
            return _from_openai(resp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            _cooldown_until[m] = time.monotonic() + _COOLDOWN_S
            nxt = attempt_chain[i + 1] if i + 1 < len(attempt_chain) else None
            print(f"[LLM] {m} failed ({sanitize_error(exc)})"
                  + (f" → falling back to {nxt}" if nxt else " — chain exhausted"))
            if nxt and on_fallback:
                try:
                    await on_fallback(m, nxt, exc)
                except Exception:
                    pass  # a HUD-notify failure must not kill the fallback
    if last_exc is None:  # unreachable: attempt_chain is never empty
        raise RuntimeError("acreate: no attempt was made")
    raise last_exc


async def astream(*, model: str, fallbacks: list[str] | None = None,
                  on_fallback: OnFallback | None = None,
                  on_delta: Callable[[str], Awaitable[None]] | None = None,
                  **kwargs):
    """Streaming completion with the same model-fallback chain as acreate().
    Calls `on_delta(text_chunk)` for each streamed text delta and returns the
    fully-assembled final response (tool_use blocks intact).

    Fallback rule: a model that errors BEFORE emitting any text is retried on
    the next model. Once ANY delta has been delivered we can't un-emit it, so a
    mid-stream failure re-raises (the caller falls back to non-streaming).
    CancelledError always propagates.
    """
    client = get_async_client()
    chain: list[str] = []
    for m in [model, *(fallbacks if fallbacks is not None else [])]:
        if m and m not in chain:
            chain.append(m)
    if not chain:
        raise ValueError("astream: no models configured")

    req = _request_kwargs(kwargs)

    now = time.monotonic()
    active = [m for m in chain if _cooldown_until.get(m, 0.0) <= now]
    attempt_chain = active or chain

    last_exc: Exception | None = None
    for i, m in enumerate(attempt_chain):
        delivered = False
        text_parts: list[str] = []
        # tool calls stream as fragments keyed by index: id/name arrive once,
        # arguments accumulate across chunks.
        calls: dict[int, dict] = {}
        finish = ""
        try:
            stream = await client.chat.completions.create(
                model=m, stream=True, **req, **reasoning_kwargs(m))
            async with stream:
                async for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    ch = chunk.choices[0]
                    delta = getattr(ch, "delta", None)
                    if delta is not None:
                        piece = getattr(delta, "content", None)
                        if piece:
                            delivered = True
                            text_parts.append(piece)
                            if on_delta:
                                await on_delta(piece)
                        for tc in getattr(delta, "tool_calls", None) or []:
                            slot = calls.setdefault(tc.index, {"id": "", "name": "",
                                                               "args": ""})
                            if getattr(tc, "id", None):
                                slot["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    slot["name"] = fn.name
                                if getattr(fn, "arguments", None):
                                    slot["args"] += fn.arguments
                    if getattr(ch, "finish_reason", None):
                        finish = ch.finish_reason
            _cooldown_until.pop(m, None)
            blocks = []
            if text_parts:
                blocks.append(SimpleNamespace(type="text", text="".join(text_parts)))
            for idx in sorted(calls):
                c = calls[idx]
                blocks.append(SimpleNamespace(type="tool_use", id=c["id"],
                                              name=c["name"],
                                              input=_parse_args(c["args"])))
            return SimpleNamespace(content=blocks,
                                   stop_reason=_STOP_MAP.get(finish, "end_turn"),
                                   model=m, usage=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            _cooldown_until[m] = time.monotonic() + _COOLDOWN_S
            if delivered:
                raise  # partial text already on the wire — don't switch models
            nxt = attempt_chain[i + 1] if i + 1 < len(attempt_chain) else None
            print(f"[LLM] {m} stream failed ({sanitize_error(exc)})"
                  + (f" → falling back to {nxt}" if nxt else " — chain exhausted"))
            if nxt and on_fallback:
                try:
                    await on_fallback(m, nxt, exc)
                except Exception:
                    pass
    assert last_exc is not None
    raise last_exc


def extract_text(resp) -> str:
    """First real text block's content — skips any non-text block."""
    for block in resp.content:
        if block.type == "text" and block.text:
            return block.text
    return ""


REQUEST_TIMEOUT = float(os.getenv("FRIDAY_REQUEST_TIMEOUT", "45"))


def get_async_client() -> "openai.AsyncOpenAI":
    global _client
    if _client is None:
        # Recovery is the acreate()/astream() MODEL fallback chain, not
        # same-model retries (max_retries=0 — retrying a stalled request
        # wastes the whole budget on it).
        #
        # CLIENT DEFAULT (used by streaming): fail-fast split timeouts. For a
        # streaming response the read timeout applies BETWEEN chunks, so a
        # stalled model is abandoned in ~15s and the chain moves on. Healthy
        # models emit events well inside it.
        #
        # NON-STREAMING (acreate) passes a per-request flat timeout instead:
        # its read timeout caps END-TO-END generation time, and 45s is what
        # healthy long turns (big write_file payloads, vision uploads) need.
        timeout = httpx.Timeout(
            connect=float(os.getenv("FRIDAY_CONNECT_TIMEOUT", "5")),
            read=float(os.getenv("FRIDAY_STREAM_READ_TIMEOUT", "15")),
            write=30.0, pool=5.0,
        )
        _client = openai.AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "sk-placeholder"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=timeout,
            max_retries=0,
            # httpx expires idle keepalive sockets after 5s by default — every
            # turn following a >5s tool call re-paid the TCP+TLS handshake.
            # 120s covers real gaps.
            http_client=httpx.AsyncClient(
                limits=httpx.Limits(max_keepalive_connections=20,
                                    keepalive_expiry=120),
                timeout=timeout,
            ),
        )
    return _client

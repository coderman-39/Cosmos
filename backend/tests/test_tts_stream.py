"""TTS streaming path — phrase-cache integration (SPEED_PLAN Wave 1.2).

The /api/tts/stream generator must serve short phrases from the phrase cache
(canned acks become ~1ms instead of a curl+TLS+ElevenLabs round-trip), tee live
renders INTO the cache only on a clean EOF, and never poison the cache with a
torn stream.
"""

import asyncio

import httpx
import pytest

from services import http_pool, tts

MP3 = b"\xff\xfb" + b"\x00" * 600  # valid mp3 magic, > 500-byte floor


@pytest.fixture
def el_env(tmp_path, monkeypatch):
    """Pretend ElevenLabs is configured; isolate the cache to tmp_path.
    Pins the transport gate to the curl fallback — these tests drive the
    stream via a fake curl subprocess (no network); the httpx transport is
    exercised separately below with a MockTransport."""
    monkeypatch.setattr(tts, "EL_AVAILABLE", True)
    monkeypatch.setattr(tts, "EL_KEY", "test-key")
    monkeypatch.setattr(tts, "EL_VOICE", "test-voice")
    monkeypatch.setattr(tts, "_DISK_DIR", tmp_path / "tts-cache")
    monkeypatch.setattr(tts, "_cache", type(tts._cache)())
    monkeypatch.setattr(tts, "_el_httpx", False)
    return tmp_path


def _mock_el_client(monkeypatch, handler):
    """Route the pooled 'elevenlabs' client through an httpx.MockTransport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setitem(http_pool._clients, "elevenlabs", client)
    return client


class FakeProc:
    """Stands in for the curl subprocess: replays chunks, then EOF."""

    def __init__(self, chunks, returncode=0):
        self._chunks = list(chunks)
        self.returncode = returncode
        self.stdout = self

    async def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


async def _collect(text):
    return [c async for c in tts.stream(text)]


async def test_stream_serves_cache_hit_without_curl(el_env, monkeypatch):
    tts._cache_put(tts._cache_key("el", "On it, sir."), MP3, tts.CONTENT_TYPE_EL)

    def boom(*a, **k):
        raise AssertionError("cache hit must not spawn curl")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    chunks = await _collect("On it, sir.")
    assert chunks == [MP3]


async def test_stream_tees_into_cache_on_clean_eof(el_env, monkeypatch):
    half = len(MP3) // 2
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _fake(FakeProc([MP3[:half], MP3[half:]])))
    chunks = await _collect("Done, sir.")
    assert b"".join(chunks) == MP3
    assert tts._cache_get(tts._cache_key("el", "Done, sir.")) == (MP3, tts.CONTENT_TYPE_EL)


async def test_torn_stream_never_cached(el_env, monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _fake(FakeProc([MP3], returncode=56)))
    await _collect("Done, sir.")
    assert tts._cache_get(tts._cache_key("el", "Done, sir.")) is None


async def test_long_text_streams_but_skips_cache(el_env, monkeypatch):
    long_text = "sir " * 100  # > _CACHE_MAX_TEXT after cleaning
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _fake(FakeProc([MP3])))
    chunks = await _collect(long_text)
    assert b"".join(chunks) == MP3
    assert tts._cache_get(tts._cache_key("el", tts._clean(long_text))) is None


async def _fake(proc):
    return proc


# ─── httpx transport (primary path — SPEED_PLAN 3.2) ──────────────────────────


async def test_httpx_stream_tees_into_cache(el_env, monkeypatch):
    monkeypatch.setattr(tts, "_el_httpx", None)     # untried → httpx first

    def handler(request):
        assert request.headers["xi-api-key"] == "test-key"   # header, not argv
        return httpx.Response(200, content=MP3)

    _mock_el_client(monkeypatch, handler)

    def boom(*a, **k):
        raise AssertionError("httpx path must not spawn curl")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    chunks = await _collect("Done, sir.")
    assert b"".join(chunks) == MP3
    assert tts._el_httpx is True                    # cutover confirmed
    assert tts._cache_get(tts._cache_key("el", "Done, sir.")) == (MP3, tts.CONTENT_TYPE_EL)


async def test_httpx_ssl_error_falls_back_to_curl_permanently(el_env, monkeypatch):
    """Zscaler-style certificate failure on the FIRST call trips the gate:
    the same call is served via curl, and the process stays on curl."""
    monkeypatch.setattr(tts, "_el_httpx", None)

    def handler(request):
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    _mock_el_client(monkeypatch, handler)
    half = len(MP3) // 2
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _fake(FakeProc([MP3[:half], MP3[half:]])))
    chunks = await _collect("On it, sir.")
    assert b"".join(chunks) == MP3                  # served by the curl fallback
    assert tts._el_httpx is False                   # permanent for the process


async def test_httpx_non_ssl_failure_does_not_trip_gate(el_env, monkeypatch):
    """A transient network error must NOT lock the process onto curl."""
    monkeypatch.setattr(tts, "_el_httpx", None)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    _mock_el_client(monkeypatch, handler)
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        lambda *a, **k: _fake(FakeProc([MP3])))
    chunks = await _collect("Done, sir.")
    assert chunks == []                             # caller falls back to buffered
    assert tts._el_httpx is None                    # gate untouched — httpx retried next call

"""Secrets must never ride on a subprocess argv (visible in ps/Activity
Monitor) — curlutil.SecretFiles moves them into 0600 files; the TTS phrase
cache never crosses engines."""

import os
import stat

from services import tts
from services.curlutil import SecretFiles


# ─── SecretFiles ───────────────────────────────────────────────────────────────

def test_header_goes_to_0600_file_not_argv():
    sf = SecretFiles()
    try:
        args = sf.header("Authorization: Bearer sk-supersecret-token")
        assert args[0] == "-H" and args[1].startswith("@")
        path = args[1][1:]
        assert "sk-supersecret-token" not in " ".join(args)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)
        with open(path) as f:
            assert f.read() == "Authorization: Bearer sk-supersecret-token\n"
    finally:
        sf.cleanup()
        assert not os.path.exists(path), "cleanup must remove the secret file"


def test_data_preserves_newlines():
    sf = SecretFiles()
    try:
        payload = '{"a": 1,\n "secret": "x"}'
        args = sf.data(payload)
        assert args[0] == "--data-binary"
        with open(args[1][1:]) as f:
            assert f.read() == payload
    finally:
        sf.cleanup()


def test_multiple_secrets_isolated():
    sf = SecretFiles()
    try:
        h1 = sf.header("X-One: a")
        h2 = sf.header("X-Two: b")
        assert h1[1] != h2[1]
    finally:
        sf.cleanup()


# ─── TTS phrase cache ──────────────────────────────────────────────────────────

def test_tts_cache_keys_are_engine_scoped():
    el_key = tts._cache_key("el", "On it, sir.")
    say_key = tts._cache_key("say", "On it, sir.")
    assert el_key != say_key
    assert el_key[0] == "el" and say_key[0] == "say"


def test_tts_cache_lru_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_DISK_DIR", tmp_path)
    monkeypatch.setattr(tts, "_CACHE_MAX_ENTRIES", 5)
    tts._cache.clear()
    for i in range(10):
        tts._cache_put(tts._cache_key("say", f"phrase {i}"), b"audio", tts.CONTENT_TYPE_SAY)
    assert len(tts._cache) == 5
    # Newest survive, oldest evicted from memory…
    assert tts._cache_get(tts._cache_key("say", "phrase 9")) is not None
    # …but disk still serves an evicted phrase (restart survival).
    assert tts._cache_get(tts._cache_key("say", "phrase 0")) is not None


def test_tts_cache_roundtrip_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_DISK_DIR", tmp_path)
    tts._cache.clear()
    key = tts._cache_key("say", "Done, sir.")
    tts._cache_put(key, b"m4a-bytes", tts.CONTENT_TYPE_SAY)
    tts._cache.clear()                      # simulate a restart
    hit = tts._cache_get(key)
    assert hit == (b"m4a-bytes", tts.CONTENT_TYPE_SAY)

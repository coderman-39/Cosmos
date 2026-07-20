"""Natural TTS — ElevenLabs (primary) with a macOS-native `say` fallback.

If ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID are set, `synthesize()` renders text
with ElevenLabs (high-quality neural mp3). Otherwise (or if the API call fails)
it falls back to the macOS `say` engine → AAC/M4A. If neither is available the
route 503s and the frontend uses the browser Web Speech API.

`synthesize()` returns (audio_bytes, content_type) or None.
"""

import os
import re
import ssl
import json
import shutil
import asyncio
import hashlib
import tempfile
import subprocess
from collections import OrderedDict
from pathlib import Path

import httpx

from services import http_pool

# ─── Config ──────────────────────────────────────────────────────────────────────

_MAX_CHARS = 1000          # cap model-generated text before synthesis
_SYNTH_TIMEOUT = 20.0      # seconds for the say+afconvert pipeline

# ── ElevenLabs (primary if configured) ──
# eleven_turbo_v2_5 = lowest latency; eleven_multilingual_v2 = highest quality.
EL_KEY   = os.getenv("ELEVENLABS_API_KEY", "").strip()
EL_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
EL_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5").strip()
EL_STABILITY  = float(os.getenv("ELEVENLABS_STABILITY", "0.5"))
EL_SIMILARITY = float(os.getenv("ELEVENLABS_SIMILARITY", "0.8"))
EL_STYLE      = float(os.getenv("ELEVENLABS_STYLE", "0.0"))
EL_AVAILABLE  = bool(EL_KEY and EL_VOICE)

# Content types per engine. AAC/M4A (say) and MP3 (ElevenLabs) both play natively
# in every modern browser via <audio>/Audio().
CONTENT_TYPE_SAY = "audio/mp4"
CONTENT_TYPE_EL  = "audio/mpeg"
# Back-compat alias (older callers referenced tts.CONTENT_TYPE).
CONTENT_TYPE = CONTENT_TYPE_EL if EL_AVAILABLE else CONTENT_TYPE_SAY

# Populated at import time (say-engine probe).
CHOSEN_VOICE: str | None = None
AVAILABLE: bool = False


# ─── Voice selection (runs once at import) ───────────────────────────────────────

def _list_voices() -> list[tuple[str, str]]:
    """Return [(name, locale), ...] from `say -v '?'`. Empty on any failure."""
    try:
        out = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    voices: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        # Format: "Name (with spaces)   en_US    # sample sentence"
        m = re.match(r"^(.+?)\s{2,}([a-z]{2}_[A-Z]{2})\s", line)
        if m:
            voices.append((m.group(1).strip(), m.group(2)))
    return voices


def _pick_voice() -> str | None:
    """Best available English voice, in preference order."""
    voices = _list_voices()
    if not voices:
        return None

    english = [(n, loc) for n, loc in voices if loc in ("en_US", "en_GB")]

    # 1. Premium / Enhanced neural English voices ("Ava (Premium)", ...).
    for name, _ in english:
        if "premium" in name.lower() or "enhanced" in name.lower():
            return name
    # 2. Samantha — the classic default macOS assistant voice.
    for name, _ in english:
        if name == "Samantha":
            return name
    # 3. Any en_US / en_GB voice. Skip the joke/novelty voices so we don't
    #    end up with "Bad News" or "Bells" reading Cosmos's replies.
    novelty = {
        "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos",
        "Wobble", "Good News", "Jester", "Organ", "Superstar", "Trinoids",
        "Whisper", "Zarvox", "Fred", "Junior", "Kathy", "Ralph", "Deranged",
        "Hysterical", "Pipe Organ",
    }
    for name, _ in english:
        if name not in novelty:
            return name
    # 4. Last resort — anything English at all.
    return english[0][0] if english else None


def _detect() -> tuple[str | None, bool]:
    if shutil.which("say") is None or shutil.which("afconvert") is None:
        return None, False
    voice = _pick_voice()
    return voice, voice is not None


# The `say -v '?'` probe costs ~0.7s — LAZY, not at import (boot latency).
# None = not probed yet; ensure_say() fills these on first need (or from the
# lifespan TTS prewarm, off-thread).
_SAY_VOICE: str | None = None
_SAY_AVAILABLE: bool | None = None

# Optimistic until probed: with EL configured this is simply true; without it,
# macOS always ships `say` — and if it's genuinely absent, synthesize() returns
# None and the route 503s to the browser fallback exactly as before.
AVAILABLE = True
CHOSEN_VOICE = f"ElevenLabs:{EL_VOICE}" if EL_AVAILABLE else None


def ensure_say() -> None:
    """Run the say-engine probe once (idempotent, ~0.7s, blocking — callers
    off the event loop or via asyncio.to_thread)."""
    global _SAY_VOICE, _SAY_AVAILABLE, AVAILABLE, CHOSEN_VOICE
    if _SAY_AVAILABLE is not None:
        return
    _SAY_VOICE, _SAY_AVAILABLE = _detect()
    AVAILABLE = EL_AVAILABLE or _SAY_AVAILABLE
    if not EL_AVAILABLE:
        CHOSEN_VOICE = _SAY_VOICE


# ─── Phrase cache ────────────────────────────────────────────────────────────────
# Canned lines ("On it, sir.", greetings, "Stopped, sir.") are re-synthesized
# dozens of times a day at 0.5-2s (and ElevenLabs credits) each. A small LRU +
# disk cache makes repeats ~0ms and survives backend restarts (dev reload!).
# Keyed on the engine ACTUALLY used + its voice/model, so a say-engine fallback
# render can never be served later as the ElevenLabs voice.

_CACHE_MAX_ENTRIES = 40
_CACHE_MAX_TEXT    = 200          # only short phrases are worth caching
_cache: "OrderedDict[tuple, tuple[bytes, str]]" = OrderedDict()
_DISK_DIR = Path.home() / ".friday" / "tts-cache"


def _cache_key(engine: str, clean: str) -> tuple:
    if engine == "el":
        return ("el", EL_VOICE, EL_MODEL, clean)
    return ("say", _SAY_VOICE or "", clean)


def _disk_path(key: tuple) -> Path:
    ext = ".mp3" if key[0] == "el" else ".m4a"
    return _DISK_DIR / (hashlib.sha1(repr(key).encode()).hexdigest() + ext)


def _cache_get(key: tuple) -> tuple[bytes, str] | None:
    hit = _cache.get(key)
    if hit:
        _cache.move_to_end(key)
        return hit
    try:
        p = _disk_path(key)
        if p.exists():
            audio = p.read_bytes()
            ctype = CONTENT_TYPE_EL if key[0] == "el" else CONTENT_TYPE_SAY
            _cache_put(key, audio, ctype, persist=False)
            return audio, ctype
    except Exception:
        pass
    return None


def _cache_put(key: tuple, audio: bytes, content_type: str, persist: bool = True) -> None:
    _cache[key] = (audio, content_type)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
    if persist:
        try:
            _DISK_DIR.mkdir(parents=True, exist_ok=True)
            _disk_path(key).write_bytes(audio)
        except Exception:
            pass


# ─── Text sanitisation ───────────────────────────────────────────────────────────

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: str) -> str:
    """Strip control chars, collapse whitespace, cap length. Never raises."""
    if not text:
        return ""
    text = _CONTROL_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]
    return text


# ─── Synthesis ───────────────────────────────────────────────────────────────────

# httpx cutover gate. None = untried; True = httpx transport confirmed working;
# False = httpx hit an SSL error (Zscaler / trust-store issue) on the first EL
# call → permanently fall back to the proven curl path for this process.
_el_httpx: bool | None = None

# First streamed chunk must arrive within this or we give up (caller falls back
# to the buffered path). Cache hits absorb the short phrases, so this only
# guards genuine ElevenLabs stalls.
_FIRST_CHUNK_DEADLINE = 4.0

# Sentinel yielded by a stream transport AFTER a verified full-body delivery —
# the outer stream() only tees into the phrase cache when it sees this.
_CLEAN_EOF = object()


def _scrub_el(s: str) -> str:
    """Strip the API key from any error text before it's printed/surfaced."""
    return s.replace(EL_KEY, "[redacted]") if EL_KEY else s


def _is_ssl_error(exc: BaseException) -> bool:
    """True if `exc` (or anything in its cause chain) is a TLS/certificate
    failure — the class of error the Zscaler MITM produces with certifi."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLError):
            return True
        msg = str(e)
        if "SSL" in msg or "certificate verify" in msg.lower():
            return True
        e = e.__cause__ or e.__context__
    return False


def _el_payload(clean: str) -> dict:
    return {
        "text": clean,
        "model_id": EL_MODEL,
        "voice_settings": {
            "stability": EL_STABILITY,
            "similarity_boost": EL_SIMILARITY,
            "style": EL_STYLE,
            "use_speaker_boost": True,
        },
    }


async def _elevenlabs_synth(clean: str) -> bytes | None:
    """Render `clean` text via the ElevenLabs API → mp3 bytes. Persistent
    keep-alive httpx transport (key rides an in-process header dict — never
    argv, never disk); permanent curl fallback if httpx hits an SSL error.
    None on any other failure so the caller falls back to `say`."""
    global _el_httpx
    if not EL_AVAILABLE:
        return None
    if _el_httpx is False:
        return await _curl_elevenlabs_synth(clean)
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}"
           "?output_format=mp3_44100_128")
    try:
        client = http_pool.get_client("elevenlabs")
        r = await client.post(url, json=_el_payload(clean),
                              headers={"xi-api-key": EL_KEY},
                              timeout=httpx.Timeout(_SYNTH_TIMEOUT, connect=5.0))
        _el_httpx = True
        if r.status_code != 200 or len(r.content) < 500:
            err = _scrub_el(r.text[:200]) if r.content else ""
            print(f"[TTS] ElevenLabs failed (HTTP {r.status_code}): {err} — "
                  "falling back to say")
            return None
        return r.content
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if _el_httpx is None and _is_ssl_error(e):
            _el_httpx = False
            print("[TTS] httpx→ElevenLabs SSL failure — using curl for the "
                  "process lifetime")
            return await _curl_elevenlabs_synth(clean)
        print(f"[TTS] ElevenLabs error: {type(e).__name__} — falling back to say")
        return None


async def _curl_elevenlabs_synth(clean: str) -> bytes | None:
    """curl fallback (SSL-gate tripped): proven reliable behind the Zscaler
    MITM. None on any failure so the caller falls back to `say`."""
    if not EL_AVAILABLE:
        return None
    payload = json.dumps({
        "text": clean,
        "model_id": EL_MODEL,
        "voice_settings": {
            "stability": EL_STABILITY,
            "similarity_boost": EL_SIMILARITY,
            "style": EL_STYLE,
            "use_speaker_boost": True,
        },
    })
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}"
           "?output_format=mp3_44100_128")
    tmpdir = tempfile.mkdtemp(prefix="cosmos_el_")
    os.chmod(tmpdir, 0o700)
    body_f = Path(tmpdir) / "body.json"
    hdr_f  = Path(tmpdir) / "hdr"
    out_f  = Path(tmpdir) / "speech.mp3"
    try:
        body_f.write_text(payload)
        # API key goes via a 0600 header FILE (-H @file), never on argv where
        # it's visible in `ps`/Activity Monitor.
        hdr_f.write_text(f"xi-api-key: {EL_KEY}\n")
        os.chmod(hdr_f, 0o600)
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-X", "POST", url,
            "-H", f"@{hdr_f}",
            "-H", "Content-Type: application/json",
            "--data", f"@{body_f}",
            "-o", str(out_f),
            "-w", "%{http_code}",
            "--max-time", str(int(_SYNTH_TIMEOUT)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        code_out, _ = await asyncio.wait_for(proc.communicate(),
                                             timeout=_SYNTH_TIMEOUT + 3)
        code = (code_out or b"").decode().strip()
        if code != "200" or not out_f.exists() or out_f.stat().st_size < 500:
            # Non-200 → out_f holds the JSON error; surface it (scrubbed of key).
            err = ""
            try: err = out_f.read_text(errors="replace")[:200]
            except Exception: pass
            print(f"[TTS] ElevenLabs failed (HTTP {code}): {err} — falling back to say")
            return None
        return out_f.read_bytes()
    except Exception as e:
        print(f"[TTS] ElevenLabs error: {type(e).__name__} — falling back to say")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def stream(text: str):
    """Async generator of mp3 chunks from ElevenLabs' /stream endpoint —
    first audio in ~300ms instead of waiting for the whole render. Yields
    nothing if EL is unconfigured or the request fails (caller 503s and the
    frontend falls back to the buffered path).

    Transport: persistent keep-alive httpx (no per-call process spawn + TLS);
    if httpx hits an SSL error on the first call the process permanently falls
    back to the proven curl path. Short phrases are served from / teed into the
    phrase cache (clean EOF only — a torn stream must never poison it)."""
    clean = _clean(text)
    if not clean or not EL_AVAILABLE:
        return
    # Cache hit → serve the whole phrase as one chunk (~1ms instead of a fresh
    # TLS+EL round-trip). Canned lines and repeated short sentences hit this
    # on nearly every spoken utterance.
    cacheable = len(clean) <= _CACHE_MAX_TEXT
    if cacheable:
        hit = _cache_get(_cache_key("el", clean))
        if hit:
            yield hit[0]
            return
    factories = ([_curl_stream] if _el_httpx is False
                 else [_httpx_stream, _curl_stream])
    chunks: list[bytes] = []
    emitted = clean_eof = False
    for factory in factories:
        chunks, emitted, clean_eof = [], False, False
        inner = factory(clean)
        try:
            async for item in inner:
                if item is _CLEAN_EOF:
                    clean_eof = True
                    continue
                emitted = True
                if cacheable:
                    chunks.append(item)
                yield item
        finally:
            await inner.aclose()
        if emitted:
            break
        if factory is _httpx_stream and _el_httpx is False:
            continue        # SSL gate tripped before any audio → retry via curl
        break               # genuine EL failure/stall → caller falls back to buffered
    # Clean EOF only (transport confirmed full delivery): tee the render into
    # the phrase cache so the next utterance of this line is ~1ms. Never on
    # exception/cancel — a torn stream must not poison it.
    if cacheable and clean_eof:
        audio = b"".join(chunks)
        if len(audio) >= 500:
            _cache_put(_cache_key("el", clean), audio, CONTENT_TYPE_EL)


async def _httpx_stream(clean: str):
    """Streaming transport over the pooled httpx client. Yields mp3 chunks,
    then the _CLEAN_EOF sentinel iff the full body arrived (httpx raises on a
    truncated/torn body, so loop completion == clean EOF). Trips the module
    SSL gate (permanent curl fallback) on a certificate failure."""
    global _el_httpx
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}/stream"
           "?output_format=mp3_44100_128")
    cm = None
    opened = False
    try:
        client = http_pool.get_client("elevenlabs")
        cm = client.stream("POST", url, json=_el_payload(clean),
                           headers={"xi-api-key": EL_KEY},
                           timeout=httpx.Timeout(60.0, connect=5.0))
        # First chunk within ~4s or give up — one deadline spanning connect,
        # response headers AND the first audio bytes, matching the old curl
        # spawn→first-read budget.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FIRST_CHUNK_DEADLINE
        resp = await asyncio.wait_for(cm.__aenter__(),
                                      timeout=_FIRST_CHUNK_DEADLINE)
        opened = True
        _el_httpx = True                     # TLS handshake succeeded — cutover holds
        if resp.status_code != 200:
            return
        it = resp.aiter_bytes(4096)
        try:
            first = await asyncio.wait_for(
                it.__anext__(), timeout=max(0.25, deadline - loop.time()))
        except StopAsyncIteration:
            return
        if not first or first[:2] not in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID"):
            return
        yield first
        async for chunk in it:
            if chunk:
                yield chunk
        yield _CLEAN_EOF
    except asyncio.CancelledError:
        raise
    except Exception as e:
        if not opened and _el_httpx is None and _is_ssl_error(e):
            _el_httpx = False
            print("[TTS] httpx→ElevenLabs SSL failure — using curl for the "
                  "process lifetime")
        return
    finally:
        if cm is not None and opened:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


async def _curl_stream(clean: str):
    """curl streaming transport (SSL-gate fallback). Yields mp3 chunks, then
    the _CLEAN_EOF sentinel iff curl exited 0 (full body delivered)."""
    tmpdir = tempfile.mkdtemp(prefix="cosmos_el_stream_")
    os.chmod(tmpdir, 0o700)
    hdr_f = Path(tmpdir) / "hdr"
    body_f = Path(tmpdir) / "body.json"
    proc = None
    try:
        # API key goes via a 0600 header FILE (-H @file), never on argv where
        # it's visible in `ps`/Activity Monitor.
        hdr_f.write_text(f"xi-api-key: {EL_KEY}\n")
        os.chmod(hdr_f, 0o600)
        body_f.write_text(json.dumps(_el_payload(clean)))
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}/stream"
               "?output_format=mp3_44100_128")
        proc = await asyncio.create_subprocess_exec(
            "curl", "-sN", "--fail", "-X", "POST", url,
            "-H", f"@{hdr_f}", "-H", "Content-Type: application/json",
            "--data", f"@{body_f}",
            "--max-time", "60",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        # First chunk within ~4s or give up (caller falls back to buffered).
        first = await asyncio.wait_for(proc.stdout.read(4096),
                                       timeout=_FIRST_CHUNK_DEADLINE)
        if not first or not first[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID"):
            return
        yield first
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            yield chunk
        rc = await asyncio.wait_for(proc.wait(), timeout=5)
        if rc == 0:
            yield _CLEAN_EOF
    except asyncio.CancelledError:
        raise
    except Exception:
        return
    finally:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def synthesize(text: str) -> tuple[bytes, str] | None:
    """Render `text` to (audio_bytes, content_type).

    Tries ElevenLabs first (mp3) when configured; on any failure falls back to
    the macOS `say` engine (AAC/M4A). Returns None only if BOTH are unavailable
    or fail — then the caller 503s and the frontend uses Web Speech.
    """
    clean = _clean(text)
    if not clean:
        return None
    cacheable = len(clean) <= _CACHE_MAX_TEXT

    # 1. ElevenLabs (best quality).
    if EL_AVAILABLE:
        if cacheable:
            hit = _cache_get(_cache_key("el", clean))
            if hit:
                return hit
        audio = await _elevenlabs_synth(clean)
        if audio:
            if cacheable:
                _cache_put(_cache_key("el", clean), audio, CONTENT_TYPE_EL)
            return audio, CONTENT_TYPE_EL

    # 2. macOS `say` fallback.
    if _SAY_AVAILABLE is None:
        await asyncio.to_thread(ensure_say)
    if not _SAY_AVAILABLE or not _SAY_VOICE:
        return None
    if cacheable:
        hit = _cache_get(_cache_key("say", clean))
        if hit:
            return hit

    tmpdir = tempfile.mkdtemp(prefix="cosmos_tts_")
    aiff = Path(tmpdir) / "speech.aiff"
    m4a = Path(tmpdir) / "speech.m4a"
    try:
        # 1. say → AIFF. Let `say` pick the container from the .aiff extension
        # (an explicit --data-format fails as "Opening output file failed:
        # fmt?"). `--` guards against text that begins with a dash.
        say_proc = await asyncio.create_subprocess_exec(
            "say", "-v", _SAY_VOICE, "-o", str(aiff), "--", clean,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            rc = await asyncio.wait_for(say_proc.wait(), timeout=_SYNTH_TIMEOUT)
        except asyncio.TimeoutError:
            say_proc.kill()
            return None
        if rc != 0 or not aiff.exists() or aiff.stat().st_size == 0:
            return None

        # 2. afconvert AIFF → M4A/AAC (small, browser-playable).
        conv_proc = await asyncio.create_subprocess_exec(
            "afconvert", str(aiff), str(m4a),
            "-f", "m4af", "-d", "aac",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            rc = await asyncio.wait_for(conv_proc.wait(), timeout=_SYNTH_TIMEOUT)
        except asyncio.TimeoutError:
            conv_proc.kill()
            return None
        if rc != 0 or not m4a.exists() or m4a.stat().st_size == 0:
            return None

        audio = m4a.read_bytes()
        if cacheable:
            _cache_put(_cache_key("say", clean), audio, CONTENT_TYPE_SAY)
        return audio, CONTENT_TYPE_SAY
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

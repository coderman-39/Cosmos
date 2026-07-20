"""Text embeddings for semantic recall — provider chain, never raises.

  1. OpenAI /embeddings (FRIDAY_EMBED_MODEL, default text-embedding-3-small,
     1536-dim) — best quality, needs network + OPENAI_API_KEY.
  2. Apple NLEmbedding via pyobjc (512-dim, on-device) — offline fallback.
  3. None — callers degrade gracefully to lexical (FTS5) search.

Vectors are tagged with the model that produced them; only same-model vectors
are ever compared (a 512-dim NL vector must never meet a 1536-dim API one).
A failing API goes on cooldown so searches don't pay its timeout twice.

Calibrated for text-embedding-3-small: related pairs score 0.5–0.7,
unrelated <0.25 → threshold 0.35. NLEmbedding separates far less (0.42 vs
0.31) → threshold 0.40.
"""

import asyncio
import math
import os
import struct
import time

import httpx

EMBED_MODEL = os.getenv("FRIDAY_EMBED_MODEL", "text-embedding-3-small")
ENABLED = os.getenv("FRIDAY_EMBED", "1").lower() not in ("0", "false", "no")
_TIMEOUT_S = float(os.getenv("FRIDAY_EMBED_TIMEOUT", "8"))
_COOLDOWN_S = 120.0

_NL_MODEL = "apple-nl-512"        # tag for on-device vectors
_THRESHOLDS = {_NL_MODEL: 0.40}   # anything else (API models): 0.35

_async_client: httpx.AsyncClient | None = None


def _get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        # keepalive_expiry: don't re-pay TLS to the gateway for every
        # recall/index embed — httpx's 5s default expires between uses.
        _async_client = httpx.AsyncClient(
            timeout=_TIMEOUT_S,
            limits=httpx.Limits(max_keepalive_connections=5,
                                keepalive_expiry=120))
    return _async_client


async def close_client() -> None:
    global _async_client
    if _async_client is not None and not _async_client.is_closed:
        await _async_client.aclose()
        _async_client = None


def threshold_for(model: str) -> float:
    return _THRESHOLDS.get(model, 0.35)


def _gateway() -> tuple[str, str]:
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    key = os.getenv("OPENAI_API_KEY", "")
    return base, key


_gateway_dead_until = 0.0


def _gateway_ok() -> bool:
    base, key = _gateway()
    return bool(base and key) and time.monotonic() >= _gateway_dead_until


def _mark_gateway_dead() -> None:
    global _gateway_dead_until
    _gateway_dead_until = time.monotonic() + _COOLDOWN_S


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ─── Apple NLEmbedding fallback ────────────────────────────────────────────────

_nl = None          # None = untried, False = unavailable


def _nl_embedding():
    global _nl
    if _nl is None:
        try:
            from NaturalLanguage import NLEmbedding
            _nl = NLEmbedding.sentenceEmbeddingForLanguage_("en") or False
        except Exception:
            _nl = False
    return _nl or None


def _nl_embed(texts: list[str]) -> list[list[float]] | None:
    emb = _nl_embedding()
    if emb is None:
        return None
    out = []
    for t in texts:
        try:
            vec = emb.vectorForString_((t or " ")[:1000])
            out.append([float(x) for x in vec] if vec is not None else None)
        except Exception:
            out.append(None)
    if any(v is None for v in out):
        return None
    return out


# ─── Public API ────────────────────────────────────────────────────────────────
# Both entry points return (model_tag, vectors) or None. Sync flavor is for
# threads (boot backfill); async flavor keeps the event loop unblocked.

def embed_sync(texts: list[str]) -> tuple[str, list[list[float]]] | None:
    if not ENABLED or not texts:
        return None
    if _gateway_ok():
        base, key = _gateway()
        try:
            r = httpx.post(f"{base}/v1/embeddings", headers=_headers(key),
                           json={"model": EMBED_MODEL, "input": texts},
                           timeout=_TIMEOUT_S)
            if r.status_code == 200:
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                return EMBED_MODEL, [d["embedding"] for d in data]
            _mark_gateway_dead()
        except Exception:
            _mark_gateway_dead()
    nl = _nl_embed(texts)
    return (_NL_MODEL, nl) if nl else None


async def aembed(texts: list[str]) -> tuple[str, list[list[float]]] | None:
    if not ENABLED or not texts:
        return None
    if _gateway_ok():
        base, key = _gateway()
        try:
            client = _get_async_client()
            r = await client.post(f"{base}/v1/embeddings",
                                  headers=_headers(key),
                                  json={"model": EMBED_MODEL, "input": texts})
            if r.status_code == 200:
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                return EMBED_MODEL, [d["embedding"] for d in data]
            _mark_gateway_dead()
        except asyncio.CancelledError:
            raise
        except Exception:
            _mark_gateway_dead()
    # NLEmbedding is a few ms per text — run off-loop anyway to stay clean.
    nl = await asyncio.to_thread(_nl_embed, texts)
    return (_NL_MODEL, nl) if nl else None


# ─── Vector helpers ────────────────────────────────────────────────────────────

def to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def from_blob(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)

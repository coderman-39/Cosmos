"""Query-aware prompt compression (LLMLingua-style, dependency-free default).

The blunt fix for an oversized tool_result / retrieved block is head-truncation
— but that throws away the middle, which is often exactly where the answer to
THIS run's query lives. This module keeps the parts most relevant to the query
and drops the rest, so a 4 kB tool output collapses toward a few hundred
query-relevant chars without losing the needle.

Three backends, chosen in order, all guarded (compression must NEVER raise or
break a run — on any failure the ORIGINAL text is returned):

  1. LLMLingua (optional, off by default) — the real distilled compressor, used
     only when the `llmlingua` package is importable AND FRIDAY_LLMLINGUA=1.
     Heavy (downloads a small LM), so opt-in.
  2. Embedding-scored extraction (async) — score each unit by cosine similarity
     to the query via services.embeddings (Apple NLEmbedding on-device is free),
     keep the top units within a char budget.
  3. Lexical extraction (sync) — score units by query-term overlap. Zero deps,
     deterministic, fast enough for the hot compaction path.

Every path preserves a HEAD and TAIL slice (structure + where errors live) and
appends `marker` so the caller can detect an already-compressed block and skip
re-compressing it.
"""

import os
import re

from services import embeddings

# LLMLingua is heavy (pulls torch + a small LM). Opt-in only.
LLMLINGUA_ENABLED = os.getenv("FRIDAY_LLMLINGUA", "0").lower() in ("1", "true", "yes")

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
# Tokens too common to signal relevance — dropped from the query term set.
_STOP = frozenset(
    "the a an of to in on at for and or but is are was were be been being this "
    "that these those it its as by with from into out up down we you i he she "
    "they them his her their our your my me do does did done have has had will "
    "would can could should may might must not no yes if then than so".split())


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _query_terms(query: str) -> set[str]:
    return {t for t in _tokens(query) if len(t) > 2 and t not in _STOP}


def _split_units(text: str, max_unit: int = 400) -> list[str]:
    """Break text into scorable units: lines, with over-long lines further split
    on sentence-ish boundaries so one giant line can't defeat the budget."""
    units: list[str] = []
    for line in (text or "").split("\n"):
        line = line.rstrip()
        if not line:
            continue
        if len(line) <= max_unit:
            units.append(line)
            continue
        for piece in re.split(r"(?<=[.!?;])\s+", line):
            piece = piece.strip()
            if not piece:
                continue
            # Still too long (no sentence breaks) → hard-slice.
            for i in range(0, len(piece), max_unit):
                units.append(piece[i:i + max_unit])
    return units


def _assemble(head: str, mid: str, tail: str, marker: str) -> str:
    body = " … ".join(p for p in (head, mid, tail) if p)
    return body + marker


# ─── LLMLingua backend (optional) ───────────────────────────────────────────────

_llmlingua = None    # None = untried, False = unavailable


def _llmlingua_compress(text: str, query: str, target_chars: int) -> str | None:
    global _llmlingua
    if _llmlingua is False:
        return None
    try:
        if _llmlingua is None:
            from llmlingua import PromptCompressor
            _llmlingua = PromptCompressor(
                model_name=os.getenv("FRIDAY_LLMLINGUA_MODEL",
                                     "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"),
                use_llmlingua2=True)
        res = _llmlingua.compress_prompt(
            text, question=query or "", target_token=max(64, target_chars // 4))
        out = (res or {}).get("compressed_prompt")
        return out if isinstance(out, str) and out else None
    except Exception:
        _llmlingua = False       # don't retry a broken/absent backend every call
        return None


# ─── Public API ─────────────────────────────────────────────────────────────────

def compress_sync(text: str, query: str = "", target_chars: int = 600, *,
                  head_chars: int = 220, tail_chars: int = 180,
                  marker: str = "") -> str:
    """Query-aware compress `text` toward `target_chars`. Returns the ORIGINAL
    when it's already small enough or on any failure. Never raises.

    Lexical scoring by default (fast, deterministic); LLMLingua when enabled.
    With no query, degrades to a head+tail keep (the old truncation behaviour,
    but keeping the tail where build/errors live)."""
    text = text or ""
    if len(text) <= target_chars:
        return text
    if LLMLINGUA_ENABLED:
        out = _llmlingua_compress(text, query, target_chars)
        if out is not None:
            return out + marker

    # Clamp head+tail so they never eat the whole budget — the query-relevant
    # MIDDLE (the whole point) must keep room. head ≤ ⅓, tail ≤ ¼ of target.
    head_chars = min(head_chars, max(0, target_chars // 3))
    tail_chars = min(tail_chars, max(0, target_chars // 4))
    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars else ""
    qterms = _query_terms(query)
    if not qterms:
        return _assemble(head, "", tail, marker)

    budget = max(0, target_chars - len(head) - len(tail))
    scored = []
    for idx, unit in enumerate(_split_units(text)):
        ut = _tokens(unit)
        if not ut:
            continue
        hits = sum(1 for t in ut if t in qterms)
        if hits:
            scored.append((hits / (len(ut) ** 0.5), idx, unit))
    scored.sort(key=lambda x: x[0], reverse=True)
    kept: list[tuple[int, str]] = []
    used = 0
    for _, idx, unit in scored:
        if used + len(unit) + 1 > budget:
            continue
        kept.append((idx, unit))
        used += len(unit) + 1
    kept.sort()
    mid = " … ".join(u for _, u in kept)
    return _assemble(head, mid, tail, marker)


async def acompress(text: str, query: str = "", target_chars: int = 600, *,
                    head_chars: int = 220, tail_chars: int = 180,
                    marker: str = "") -> str:
    """Embedding-scored query-aware compression — keeps units by cosine
    similarity to the query (semantic, so it catches relevant text that shares
    no literal words). Falls back to compress_sync (lexical) when embeddings
    are unavailable. Never raises."""
    text = text or ""
    if len(text) <= target_chars or not (query or "").strip():
        return compress_sync(text, query, target_chars, head_chars=head_chars,
                             tail_chars=tail_chars, marker=marker)
    try:
        head = text[:head_chars]
        tail = text[-tail_chars:] if tail_chars else ""
        units = _split_units(text)
        res = await embeddings.aembed([query[:500]] + [u[:500] for u in units])
        if not res:
            return compress_sync(text, query, target_chars, head_chars=head_chars,
                                 tail_chars=tail_chars, marker=marker)
        _model, vecs = res
        qvec, uvecs = vecs[0], vecs[1:]
        budget = max(0, target_chars - len(head) - len(tail))
        scored = sorted(
            ((embeddings.cosine(qvec, uvecs[i]), i, u) for i, u in enumerate(units)),
            key=lambda x: x[0], reverse=True)
        kept: list[tuple[int, str]] = []
        used = 0
        for _sim, idx, unit in scored:
            if used + len(unit) + 1 > budget:
                continue
            kept.append((idx, unit))
            used += len(unit) + 1
        kept.sort()
        mid = " … ".join(u for _, u in kept)
        return _assemble(head, mid, tail, marker)
    except Exception:
        return compress_sync(text, query, target_chars, head_chars=head_chars,
                             tail_chars=tail_chars, marker=marker)

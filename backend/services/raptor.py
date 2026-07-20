"""RAPTOR-style hierarchical summaries over the recall corpus.

recall.py stores one row per completed run (the LEAF level). That's great for
pinpoint recall ("when did we rotate the CI token") but useless for global
questions ("what have we been doing about test flakiness lately") — those
need retrieval at a HIGHER abstraction than a single run.

This module builds that abstraction: it clusters related leaf runs by embedding
similarity, LLM-summarises each cluster into a level-1 node, then clusters and
summarises THOSE into level-2 nodes — a small tree of summaries. search() then
retrieves at whichever level matches the query, so "1M raw tokens of history"
becomes "a queryable hierarchy of themes".

Reuses recall.db (same file) and the vectors recall already stored — no
re-embedding of leaves. Pure-Python clustering (greedy cosine threshold, no
numpy/sklearn). Every path is guarded: RAPTOR must never break a run, and it
degrades to nothing when embeddings or the LLM are unavailable.
"""

import asyncio
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from services import embeddings, llm, recall

ENABLED = os.getenv("FRIDAY_RAPTOR", "1").lower() not in ("0", "false", "no")

# Summaries use the fast model tier. Read env directly (NOT from services.agent,
# which imports THIS module — that would be a circular import).
_FAST_MODEL = (os.getenv("FRIDAY_FAST_MODEL") or os.getenv("FRIDAY_AGENT_MODEL")
               or llm.DEFAULT_MODEL)

DB = recall.DB
_STATE = Path.home() / ".friday" / ".raptor_build"   # last-built leaf count

# Tunables.
_TAU = float(os.getenv("FRIDAY_RAPTOR_TAU", "0.55"))     # cluster cosine floor
_MIN_CLUSTER = 2          # clusters smaller than this aren't worth summarising
_LEVELS = 2               # depth of the summary tree
_MAX_LEAVES = 1500        # cap the corpus we cluster in one rebuild
_REBUILD_DELTA = int(os.getenv("FRIDAY_RAPTOR_REBUILD_EVERY", "25"))  # new leaves

# ── Cost controls (learned the hard way) ──
# One fast-model call per cluster is unbounded work on a shared, rate-limited
# key: 311 leaves → 50 clusters → 50 sequential gpt-5.4 calls + 50 embeddings,
# which 429'd the gateway and (via the cooldown map) knocked the user's own
# interactive chain down to its third fallback. Background enrichment gets a
# hard ceiling, a pace, and an early abort.
_MAX_SUMMARIES = int(os.getenv("FRIDAY_RAPTOR_MAX_SUMMARIES", "12"))
_PACE_S = float(os.getenv("FRIDAY_RAPTOR_PACE", "1.0"))   # gap between LLM calls
_MAX_CONSECUTIVE_FAILURES = 3    # summariser failing → stop, don't grind

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS raptor USING fts5(
    level UNINDEXED, ts UNINDEXED, members UNINDEXED, text
);
CREATE TABLE IF NOT EXISTS raptor_vec (
    rowid INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);
"""

_rebuild_lock = threading.Lock()   # one rebuild at a time across the process


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    return conn


# ─── Clustering (pure Python) ───────────────────────────────────────────────────

def _cluster(items: list[tuple], tau: float) -> list[list[tuple]]:
    """Greedy cosine-threshold clustering. items: [(payload, vec)]. Each seed
    pulls in every not-yet-used item within `tau`. O(n²) — fine at recall scale
    (bounded by _MAX_LEAVES). Returns groups of the original tuples."""
    n = len(items)
    used = [False] * n
    clusters = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        group = [items[i]]
        for j in range(i + 1, n):
            if used[j]:
                continue
            try:
                if embeddings.cosine(items[i][1], items[j][1]) >= tau:
                    used[j] = True
                    group.append(items[j])
            except Exception:
                continue
        clusters.append(group)
    return clusters


async def _summarise(texts: list[str], level: int) -> str:
    """One fast-model summary of a cluster. '' on any failure."""
    joined = "\n---\n".join(t[:300] for t in texts[:20])[:4000]
    kind = "past task records" if level == 1 else "theme summaries"
    prompt = (
        "You are COSMOS's memory summariser. Below are related "
        f"{kind}. Write ONE dense paragraph (max ~90 words) capturing the shared "
        "theme: the entities, decisions, and outcomes they have in common. No "
        "preamble, no bullet points — just the summary.\n\n" + joined)
    try:
        resp = await asyncio.wait_for(
            llm.acreate(model=_FAST_MODEL, fallbacks=llm.FAST_FALLBACKS,
                        max_tokens=220,
                        messages=[{"role": "user", "content": prompt}]),
            timeout=30)
        return llm.extract_text(resp).strip()
    except Exception:
        return ""


# ─── Leaf loading (reuse recall's stored vectors) ───────────────────────────────

def _leaf_model() -> str | None:
    """The embedding model most leaves are stored under — we cluster within a
    single model's vector space (dims/threshold differ per model)."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT model, count(*) c FROM runs_vec GROUP BY model "
                "ORDER BY c DESC LIMIT 1").fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _load_leaves(model: str, limit: int) -> list[tuple]:
    """[(text, vec)] for leaf runs embedded under `model`, newest first."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT r.user_text, r.outcome, v.embedding FROM runs r "
                "JOIN runs_vec v ON v.rowid = r.rowid WHERE v.model = ? "
                "ORDER BY r.ts DESC LIMIT ?", (model, limit)).fetchall()
    except Exception:
        return []
    out = []
    for asked, outcome, blob in rows:
        try:
            out.append((f"{asked} → {outcome}", embeddings.from_blob(blob)))
        except Exception:
            continue
    return out


def _leaf_count() -> int:
    try:
        with _conn() as conn:
            return conn.execute("SELECT count(*) FROM runs").fetchone()[0]
    except Exception:
        return 0


# ─── Rebuild ────────────────────────────────────────────────────────────────────

def _mark_built() -> None:
    """Record the leaf count this tree was built at. Called BEFORE the work
    starts: an interrupted rebuild must NOT hot-loop (see rebuild())."""
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(str(_leaf_count()))
    except Exception:
        pass


def _swap_nodes(nodes: list[tuple]) -> int:
    """Replace the tree with `nodes` in ONE transaction. The old tree survives
    until the new one is ready, so an aborted rebuild never leaves the board
    wiped or half-built. nodes: [(level, text, members, vec, vmodel)]."""
    if not nodes:
        return 0
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM raptor")
            conn.execute("DELETE FROM raptor_vec")
            for level, text, members, vec, vmodel in nodes:
                cur = conn.execute(
                    "INSERT INTO raptor (level, ts, members, text) VALUES (?,?,?,?)",
                    (level, datetime.now().isoformat(timespec="seconds"),
                     members, text))
                if vec is not None and vmodel:
                    conn.execute(
                        "INSERT OR REPLACE INTO raptor_vec (rowid, model, embedding) "
                        "VALUES (?,?,?)",
                        (cur.lastrowid, vmodel, embeddings.to_blob(vec)))
        return len(nodes)
    except Exception as e:
        print(f"[raptor] swap failed (non-fatal): {e}")
        return 0


async def rebuild() -> int:
    """(Re)build the summary tree from the current leaf corpus. Returns the
    number of summary nodes written. Never raises.

    HARD-BOUNDED on purpose. The first version was unbounded and ran one
    fast-model call per cluster — on a 311-run corpus that is ~50 sequential
    gpt-5.4 calls plus ~50 embedding calls, which rate-limited the shared
    gateway key (429s) and, via the model cooldown map, degraded the user's
    INTERACTIVE chain too. Background enrichment must never outbid the user for
    their own quota."""
    if not ENABLED or not embeddings.ENABLED:
        return 0
    model = _leaf_model()
    if not model:
        return 0
    leaves = _load_leaves(model, _MAX_LEAVES)
    if len(leaves) < _MIN_CLUSTER * 2:      # too little to abstract over
        return 0

    # Mark BEFORE the work. If this rebuild is interrupted (backend restart, run
    # cancelled, rate limit), the state file must already say "attempted at N
    # leaves" — otherwise is_stale() stays True and every subsequent run kicks
    # off another full rebuild forever. Backoff is the +_REBUILD_DELTA threshold.
    _mark_built()

    nodes: list[tuple] = []                 # staged; swapped in atomically at the end
    current = leaves                        # [(text, vec)]
    budget = _MAX_SUMMARIES
    consecutive_failures = 0
    for level in range(1, _LEVELS + 1):
        if len(current) < _MIN_CLUSTER or budget <= 0:
            break
        clusters = [g for g in _cluster(current, _TAU) if len(g) >= _MIN_CLUSTER]
        next_level: list[tuple] = []
        skipped = 0
        for group in clusters:
            if budget <= 0:
                skipped += 1
                continue
            summary = await _summarise([g[0] for g in group], level)
            budget -= 1
            if not summary:
                # The summariser swallows errors and returns "" — during a 429
                # storm that means grinding through every cluster producing
                # nothing. Bail out early instead of burning the quota.
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    print("[raptor] aborting rebuild — summariser failing "
                          "(rate limited?); keeping the existing tree")
                    budget = 0
                    break
                continue
            consecutive_failures = 0
            emb = await embeddings.aembed([summary])
            vec = emb[1][0] if emb else None
            nodes.append((level, summary, len(group), vec,
                          emb[0] if emb else None))
            if vec is not None:
                next_level.append((summary, vec))
            if _PACE_S:
                await asyncio.sleep(_PACE_S)   # never burst the shared key
        if skipped:
            # No silent caps: say what was left out.
            print(f"[raptor] level {level}: hit the {_MAX_SUMMARIES}-summary cap "
                  f"— {skipped} cluster(s) not summarised this round")
        current = next_level

    if not nodes:
        return 0                            # keep whatever tree we already had
    written = _swap_nodes(nodes)
    if written:
        print(f"[raptor] built {written} summary nodes over {len(leaves)} runs")
    return written


def is_stale() -> bool:
    """Cheap check: have enough new leaves accrued since the last build to
    justify rebuilding? True when never built or the delta crosses the floor."""
    if not ENABLED:
        return False
    try:
        last = int(_STATE.read_text().strip()) if _STATE.exists() else -1
    except Exception:
        last = -1
    return _leaf_count() - last >= _REBUILD_DELTA


async def maybe_rebuild() -> int:
    """Rebuild only if stale, and only one at a time. Cheap no-op otherwise —
    safe to fire-and-forget at the end of every run."""
    if not is_stale() or not _rebuild_lock.acquire(blocking=False):
        return 0
    try:
        return await rebuild()
    finally:
        _rebuild_lock.release()


# ─── Search ─────────────────────────────────────────────────────────────────────

def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9]+", query or "")
    return " OR ".join(f'"{t}"' for t in terms[:12])


def _fts_rows(query: str, limit: int) -> list[tuple]:
    q = _fts_query(query)
    if not q:
        return []
    try:
        with _conn() as conn:
            return conn.execute(
                "SELECT rowid, level, members, text FROM raptor "
                "WHERE raptor MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    except Exception:
        return []


def _vec_rows(qvec: list[float], model: str, limit: int) -> list[tuple]:
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT n.rowid, n.level, n.members, n.text, v.embedding "
                "FROM raptor n JOIN raptor_vec v ON v.rowid = n.rowid "
                "WHERE v.model = ?", (model,)).fetchall()
    except Exception:
        return []
    floor = embeddings.threshold_for(model)
    scored = []
    for rowid, level, members, text, blob in rows:
        try:
            sim = embeddings.cosine(qvec, embeddings.from_blob(blob))
        except Exception:
            continue
        if sim >= floor:
            scored.append((sim, rowid, level, members, text))
    scored.sort(reverse=True)
    return [(rowid, level, members, text) for _, rowid, level, members, text
            in scored[:limit]]


async def search(query: str, limit: int = 3,
                 qvec: tuple[str, list[float]] | None = None) -> list[str]:
    """Ranked summary lines fusing lexical + semantic hits over the tree.
    Each line notes its abstraction level and how many runs it covers. []
    on no match / disabled / error. Never raises.

    `qvec` is an optional precomputed (model, vector) for the query — callers
    that also query recall embed once and pass it to both searches."""
    if not ENABLED or not (query or "").strip():
        return []
    limit = max(1, min(limit, 10))
    fts = await asyncio.to_thread(_fts_rows, query, limit)
    vec: list[tuple] = []
    try:
        if qvec is not None:
            model, vecs = qvec[0], [qvec[1]]
        else:
            res = await embeddings.aembed([query[:500]])
            if not res:
                model, vecs = "", []
            else:
                model, vecs = res
        if vecs:
            vec = await asyncio.to_thread(_vec_rows, vecs[0], model, limit)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    scores: dict[int, float] = {}
    rows: dict[int, tuple] = {}
    for ranked in (fts, vec):
        for i, row in enumerate(ranked):
            rowid = row[0]
            scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (60 + i)
            rows.setdefault(rowid, row)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out = []
    for rowid, _ in ordered:
        _id, level, members, text = rows[rowid]
        out.append(f"LEVEL {level} theme (covers {members} runs): {text}")
    return out

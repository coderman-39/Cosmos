"""Searchable memory of past runs — SQLite at ~/.friday/recall.db.

Every completed agent run is indexed (what was asked, the outcome, which
tools ran, when) TWICE: an FTS5 table for lexical match and a parallel
`runs_vec` table holding embedding vectors for semantic match. search()
fuses both with Reciprocal Rank Fusion, so "what did we decide about the
CI token rotation" finds "rotate the ci api token" even though they
share almost no words — and unrelated queries still return [] (vector hits
must clear a per-model cosine threshold).

stdlib sqlite3 only; embeddings via services.embeddings (gateway → Apple
NLEmbedding → none). Every path is guarded — recall must never break a run,
and a missing embedding provider degrades silently to pure FTS5.
"""

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from services import embeddings

DB = Path.home() / ".friday" / "recall.db"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS runs USING fts5(
    ts UNINDEXED, user_text, outcome, tools
);
CREATE TABLE IF NOT EXISTS runs_vec (
    rowid INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5)
    # WAL + busy_timeout: the fire-and-forget embed writes and a boot-time
    # backfill thread must never trade "database is locked" errors.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    conn.executescript(_SCHEMA)
    return conn


def _embed_text(user_text: str, outcome: str) -> str:
    return f"{(user_text or '')[:500]}\n{(outcome or '')[:500]}"


def _store_vec(rowid: int, text: str) -> None:
    """Embed + store one row's vector. Sync, guarded, never raises."""
    try:
        res = embeddings.embed_sync([text])
        if not res:
            return
        model, vecs = res
        with _conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs_vec (rowid, model, embedding) "
                "VALUES (?,?,?)",
                (rowid, model, embeddings.to_blob(vecs[0])))
    except Exception as e:
        print(f"[recall] embed failed (non-fatal): {e}")


def record_run(user_text: str, outcome: str, tools: list[str] | None = None,
               ts: str | None = None) -> None:
    """Index one completed run. Never raises. The FTS row lands immediately;
    the embedding is computed off-loop (or inline when no loop is running)."""
    try:
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (ts, user_text, outcome, tools) VALUES (?,?,?,?)",
                (ts or datetime.now().isoformat(timespec="seconds"),
                 (user_text or "")[:500], (outcome or "")[:500],
                 " ".join(tools or [])[:200]))
            rowid = cur.lastrowid
    except Exception as e:
        print(f"[recall] record failed (non-fatal): {e}")
        return
    if not embeddings.ENABLED:
        return
    text = _embed_text(user_text, outcome)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _store_vec(rowid, text)              # thread/backfill path: inline
        return
    # Agent-loop path: never block the event loop on an embedding network call.
    loop.create_task(asyncio.to_thread(_store_vec, rowid, text))


def _fts_query(query: str) -> str:
    """Quote each term so user text can't break FTS5 syntax; OR them so any
    matching word ranks results rather than requiring all."""
    terms = re.findall(r"[A-Za-z0-9]+", query or "")
    return " OR ".join(f'"{t}"' for t in terms[:12])


def _fts_rows(query: str, cutoff: str, limit: int) -> list[tuple]:
    q = _fts_query(query)
    if not q:
        return []
    try:
        with _conn() as conn:
            return conn.execute(
                "SELECT rowid, ts, user_text, outcome FROM runs "
                "WHERE runs MATCH ? AND ts >= ? ORDER BY rank LIMIT ?",
                (q, cutoff, limit)).fetchall()
    except Exception as e:
        print(f"[recall] fts search failed (non-fatal): {e}")
        return []


def _vec_rows(qvec: list[float], model: str, cutoff: str,
              limit: int) -> list[tuple]:
    """Same-model rows within the window, cosine-ranked, thresholded."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT r.rowid, r.ts, r.user_text, r.outcome, v.embedding "
                "FROM runs r JOIN runs_vec v ON v.rowid = r.rowid "
                "WHERE v.model = ? AND r.ts >= ?",
                (model, cutoff)).fetchall()
    except Exception as e:
        print(f"[recall] vec search failed (non-fatal): {e}")
        return []
    floor = embeddings.threshold_for(model)
    scored = []
    for rowid, ts, asked, outcome, blob in rows:
        try:
            sim = embeddings.cosine(qvec, embeddings.from_blob(blob))
        except Exception:
            continue
        if sim >= floor:
            scored.append((sim, rowid, ts, asked, outcome))
    scored.sort(reverse=True)
    return [(rowid, ts, asked, outcome) for _, rowid, ts, asked, outcome
            in scored[:limit]]


async def search(query: str, days_back: int = 90, limit: int = 8,
                 qvec: tuple[str, list[float]] | None = None) -> list[str]:
    """Ranked 'DATE — asked — outcome' lines fusing lexical + semantic hits
    (Reciprocal Rank Fusion). Empty list on no match/error.

    `qvec` is an optional precomputed (model, vector) for the query — callers
    that also query RAPTOR embed once and pass it to both searches."""
    if not (query or "").strip():
        return []
    limit = max(1, min(limit, 25))
    cutoff = (datetime.now() - timedelta(days=max(1, days_back))).isoformat()

    # Both DB reads run off the event loop: sqlite (connect + WAL pragma +
    # FTS MATCH) can block, and the same diff added concurrent writer threads
    # (fire-and-forget embeds, boot re-embed) that contend for the lock.
    fts = await asyncio.to_thread(_fts_rows, query, cutoff, limit)
    vec: list[tuple] = []
    try:
        if qvec is not None:
            model, v = qvec
            vec = await asyncio.to_thread(_vec_rows, v, model, cutoff, limit)
        else:
            res = await embeddings.aembed([query[:500]])
            if res:
                model, vecs = res
                vec = await asyncio.to_thread(_vec_rows, vecs[0], model, cutoff, limit)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[recall] query embed failed (non-fatal): {e}")

    # RRF: score = Σ 1/(60+rank) over each list a row appears in.
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
        _, ts, asked, outcome = rows[rowid]
        day = (ts or "")[:16].replace("T", " ")
        out.append(f"{day} — asked: {asked} — outcome: {outcome}")
    return out


def ensure_embeddings(batch: int = 64, max_rows: int = 4000) -> int:
    """Embed rows that predate semantic recall, were written while the provider
    was down, OR were embedded under a DIFFERENT model than the one now active
    (e.g. the Apple-NL fallback while the gateway was on cooldown — those rows
    are invisible to gateway-model queries until upgraded). Sync — call from a
    thread. Returns vectors (re-)embedded."""
    # Probe the current provider once so we know which model is live.
    probe = embeddings.embed_sync(["_probe_"])
    current_model = probe[0] if probe else None
    # Only UPGRADE toward the preferred gateway model — if the probe returned
    # the on-device fallback (gateway down), just fill missing rows; re-embedding
    # gateway rows under NL would be a downgrade and churn every boot.
    upgrade = bool(current_model) and current_model == embeddings.EMBED_MODEL
    try:
        with _conn() as conn:
            if upgrade:
                rows = conn.execute(
                    "SELECT r.rowid, r.user_text, r.outcome FROM runs r "
                    "LEFT JOIN runs_vec v ON v.rowid = r.rowid "
                    "WHERE v.rowid IS NULL OR v.model != ? LIMIT ?",
                    (current_model, max_rows)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT r.rowid, r.user_text, r.outcome FROM runs r "
                    "LEFT JOIN runs_vec v ON v.rowid = r.rowid "
                    "WHERE v.rowid IS NULL LIMIT ?", (max_rows,)).fetchall()
    except Exception:
        return 0
    added = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        res = embeddings.embed_sync([_embed_text(u, o) for _, u, o in chunk])
        if not res:
            break                      # provider down — retry next boot
        model, vecs = res
        try:
            with _conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO runs_vec (rowid, model, embedding) "
                    "VALUES (?,?,?)",
                    [(rowid, model, embeddings.to_blob(v))
                     for (rowid, _, _), v in zip(chunk, vecs)])
            added += len(chunk)
        except Exception as e:
            print(f"[recall] re-embed failed (non-fatal): {e}")
            break
    if added:
        print(f"[recall] embedded {added} historical runs")
    return added


def backfill_from_traces(trace_dir: Path | None = None) -> int:
    """One-time import of historical runs from ~/.friday/traces/*/*.jsonl.
    Idempotent-ish: runs only when the DB is empty. Returns rows added."""
    trace_dir = trace_dir or (Path.home() / ".friday" / "traces")
    try:
        with _conn() as conn:
            if conn.execute("SELECT count(*) FROM runs").fetchone()[0] > 0:
                return 0
    except Exception:
        return 0
    added = 0
    try:
        for f in sorted(trace_dir.glob("*/*.jsonl")):
            start, end, tools, ts = None, None, [], None
            try:
                for line in f.read_text(errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    t = rec.get("type")
                    if t == "run_start":
                        start = rec.get("user_text")
                        ts = rec.get("ts")
                    elif t == "tool_start" and rec.get("tool"):
                        tools.append(rec["tool"])
                    elif t == "run_end":
                        end = rec.get("final_text")
                if start and end:
                    record_run(start, end, sorted(set(tools)), ts=ts)
                    added += 1
            except Exception:
                continue
    except Exception:
        pass
    return added


def bootstrap() -> None:
    """Boot-time (thread) init: backfill an empty DB from traces, then embed
    any rows still missing vectors. Both steps are cheap no-ops once done."""
    backfill_from_traces()
    ensure_embeddings()

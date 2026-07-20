"""RAPTOR hierarchical summaries (services.raptor).

Contract under test:
  - _cluster() groups items by cosine similarity (pure, deterministic).
  - search() degrades to [] when disabled / empty / on error (never raises).
  - rebuild() no-ops without an embedding provider.
  - end-to-end: seed leaf runs → rebuild builds summary nodes → search retrieves
    them at the theme level (with fake embeddings + a fake summariser).
"""

import asyncio
import sqlite3
import string

import pytest

from services import embeddings, llm, raptor
from services import recall as recall_svc


# ─── fakes ─────────────────────────────────────────────────────────────────────

def _char_vec(text: str) -> list[float]:
    """26-dim letter-count embedding — deterministic, so texts sharing letters
    cluster and a keyword query matches its summary."""
    v = [0.0] * 26
    for c in (text or "").lower():
        if c in string.ascii_lowercase:
            v[ord(c) - 97] += 1.0
    return v


async def _fake_aembed(texts):
    return ("fake", [_char_vec(t) for t in texts])


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, content):
        self.content = content


async def _fake_acreate(**kwargs):
    msg = str((kwargs.get("messages") or [{}])[0].get("content", "")).lower()
    theme = "apples" if "apple" in msg else ("docker" if "docker" in msg else "misc")
    return _Resp([_TextBlock(f"recurring theme about {theme}")])


# ─── unit: clustering ──────────────────────────────────────────────────────────

def test_cluster_groups_by_similarity():
    a1, a2 = _char_vec("apple apple"), _char_vec("apple apples")
    b1 = _char_vec("docker token zzzz")
    groups = raptor._cluster([("a1", a1), ("a2", a2), ("b1", b1)], tau=0.5)
    # The two apple vectors land together; docker is its own group.
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]


# ─── graceful degradation ──────────────────────────────────────────────────────

async def test_search_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(raptor, "ENABLED", False)
    assert await raptor.search("anything") == []


async def test_search_empty_on_blank_query():
    assert await raptor.search("   ") == []


async def test_rebuild_noop_without_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "ENABLED", False)
    assert await raptor.rebuild() == 0


# ─── end-to-end ────────────────────────────────────────────────────────────────

def _seed(dbpath, rows):
    con = sqlite3.connect(dbpath)
    con.executescript(recall_svc._SCHEMA)
    for i, text in enumerate(rows):
        con.execute("INSERT INTO runs (ts, user_text, outcome, tools) VALUES (?,?,?,?)",
                    (f"2026-07-1{i % 9}T09:00:00", text, "ok", ""))
        rid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO runs_vec (rowid, model, embedding) VALUES (?,?,?)",
                    (rid, "fake", embeddings.to_blob(_char_vec(text))))
    con.commit()
    con.close()


@pytest.fixture
def raptor_db(monkeypatch, tmp_path):
    db = tmp_path / "recall.db"
    monkeypatch.setattr(raptor, "DB", db)
    monkeypatch.setattr(raptor, "_STATE", tmp_path / ".raptor_build")
    monkeypatch.setattr(raptor, "ENABLED", True)
    monkeypatch.setattr(raptor, "_TAU", 0.3)
    monkeypatch.setattr(raptor, "_LEVELS", 1)
    monkeypatch.setattr(raptor, "_REBUILD_DELTA", 2)   # small corpus in tests
    monkeypatch.setattr(embeddings, "ENABLED", True)
    monkeypatch.setattr(embeddings, "aembed", _fake_aembed)
    monkeypatch.setattr(llm, "acreate", _fake_acreate)
    return db


async def test_rebuild_then_search(raptor_db):
    _seed(raptor_db, [
        "rotate the apples orchard apples", "harvest apples apples orchard",
        "count the apples apples in store",
        "docker token rotation zzzz", "docker policy zzzz update", "docker image zzzz prune",
    ])
    written = await raptor.rebuild()
    assert written >= 1                       # at least one theme summarised

    hits = await raptor.search("apples orchard")
    assert hits and any("apple" in h.lower() for h in hits)
    assert any(h.startswith("LEVEL 1 theme") for h in hits)


# ─── cost controls: this burst 429'd the real gateway ──────────────────────────

def _distinct_clusters(n: int, per: int = 2) -> list[str]:
    """n groups of `per` identical rows, each group using a DISJOINT letter set.
    With the char-vector fake embedding that gives cosine≈1 within a group and 0
    across groups — so _cluster genuinely produces n clusters (and therefore n
    would-be LLM calls). Seeding every row with the same words instead collapses
    everything into ONE cluster and makes a cap test vacuous."""
    rows = []
    for k in range(n):
        a = string.ascii_lowercase[(2 * k) % 26]
        b = string.ascii_lowercase[(2 * k + 1) % 26]
        rows.extend([f"{a * 6} {b * 6}"] * per)
    return rows


async def test_rebuild_is_capped(raptor_db, monkeypatch):
    """Unbounded, one LLM call per cluster is what rate-limited the shared key
    (311 leaves → ~50 sequential fast-model calls). Must honour the cap."""
    calls = {"n": 0}

    async def counting_acreate(**kw):
        calls["n"] += 1
        return await _fake_acreate(**kw)

    monkeypatch.setattr(llm, "acreate", counting_acreate)
    monkeypatch.setattr(raptor, "_MAX_SUMMARIES", 3)
    monkeypatch.setattr(raptor, "_PACE_S", 0)
    _seed(raptor_db, _distinct_clusters(8))      # 8 real clusters, cap of 3
    await raptor.rebuild()
    assert calls["n"] <= 3, f"cap ignored — made {calls['n']} LLM calls"


async def test_rebuild_aborts_when_summariser_keeps_failing(raptor_db, monkeypatch):
    """A 429 storm makes _summarise return '' every time. Grinding through every
    cluster burns the quota for nothing — bail out early."""
    calls = {"n": 0}

    async def always_failing(**kw):
        calls["n"] += 1
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(llm, "acreate", always_failing)
    monkeypatch.setattr(raptor, "_PACE_S", 0)
    monkeypatch.setattr(raptor, "_MAX_SUMMARIES", 99)   # cap must not mask the abort
    _seed(raptor_db, _distinct_clusters(8))             # 8 clusters available
    assert await raptor.rebuild() == 0
    assert calls["n"] <= raptor._MAX_CONSECUTIVE_FAILURES, \
        f"kept grinding after repeated failures ({calls['n']} calls)"


async def test_interrupted_rebuild_does_not_hot_loop(raptor_db, monkeypatch):
    """THE regression: the state marker was only written on the success path, so
    an interrupted rebuild left is_stale() True and EVERY later run kicked off
    another full rebuild — a permanent LLM burn."""
    async def boom(**kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(llm, "acreate", boom)
    monkeypatch.setattr(raptor, "_PACE_S", 0)
    _seed(raptor_db, [f"apples orchard {i//2} zz{i//2}" for i in range(12)])
    assert raptor.is_stale() is True
    with pytest.raises(asyncio.CancelledError):
        await raptor.rebuild()
    assert raptor.is_stale() is False, \
        "interrupted rebuild left is_stale() True → rebuilds forever"


async def test_failed_rebuild_keeps_the_existing_tree(raptor_db, monkeypatch):
    """The old code wiped the tree BEFORE rebuilding, so a rebuild that produced
    nothing destroyed a perfectly good tree."""
    _seed(raptor_db, [f"apples orchard {i//2} zz{i//2}" for i in range(12)])
    monkeypatch.setattr(raptor, "_PACE_S", 0)
    assert await raptor.rebuild() >= 1
    before = await raptor.search("apples orchard")
    assert before

    async def always_failing(**kw):
        raise RuntimeError("429")

    monkeypatch.setattr(llm, "acreate", always_failing)
    monkeypatch.setattr(raptor, "_STATE", raptor_db.parent / ".raptor_build2")
    assert await raptor.rebuild() == 0
    assert await raptor.search("apples orchard") == before, \
        "a failed rebuild wiped the existing tree"


async def test_is_stale_tracks_build_marker(raptor_db):
    _seed(raptor_db, ["apples apples orchard", "apples apples store",
                      "docker zzzz token", "docker zzzz policy"])
    monkeypatch_delta = raptor._REBUILD_DELTA
    assert raptor.is_stale() is True          # never built → stale
    await raptor.rebuild()                     # writes the marker with current count
    assert raptor.is_stale() is False          # freshly built → not stale
    _ = monkeypatch_delta

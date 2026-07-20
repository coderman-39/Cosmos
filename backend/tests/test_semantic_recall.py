"""Semantic recall (F3): embedding-vector search fused with FTS5.

Uses a deterministic fake provider (fixed text→vector table) so no network is
touched. The contracts under test:
  - A paraphrased query with ZERO lexical overlap still finds the run.
  - Unrelated queries stay [] (the cosine threshold holds the line).
  - Vectors are tagged per-model and never compared across models.
  - Provider failure degrades to pure FTS5, never breaks search.
"""

import asyncio

import pytest

from services import embeddings, recall


# Hand-built corpus: q/doc pairs share NO tokens where the test needs that.
_DOC = "rotate the github api token\nRotated and stored in vault."
_VECS = {
    _DOC:                                     [1.0, 0.0, 0.0],
    "credential refresh policy decision":     [0.9, 0.1, 0.0],   # paraphrase
    "kubernetes cluster status":              [0.0, 0.0, 1.0],   # unrelated
}


def _fake_provider(model="fake-3"):
    def embed_sync(texts):
        return model, [_VECS.get(t, [0.0, 0.01, 0.0]) for t in texts]

    async def aembed(texts):
        return embed_sync(texts)

    return embed_sync, aembed


@pytest.fixture
def semantic_db(tmp_path, monkeypatch):
    monkeypatch.setattr(recall, "DB", tmp_path / "recall.db")
    monkeypatch.setattr(embeddings, "ENABLED", True)
    embed_sync, aembed = _fake_provider()
    monkeypatch.setattr(embeddings, "embed_sync", embed_sync)
    monkeypatch.setattr(embeddings, "aembed", aembed)
    return tmp_path


def _populate():
    # Runs in a worker thread (no event loop) → record_run embeds INLINE,
    # so the vector is committed before the test proceeds. Deterministic.
    recall.record_run("rotate the github api token", "Rotated and stored in vault.",
                      ["gh"])


async def test_paraphrase_hits_without_shared_words(semantic_db):
    await asyncio.to_thread(_populate)
    hits = await recall.search("credential refresh policy decision")
    assert len(hits) == 1
    assert "rotate the github api token" in hits[0]


async def test_unrelated_query_still_empty(semantic_db):
    await asyncio.to_thread(_populate)
    assert await recall.search("kubernetes cluster status") == []


async def test_lexical_and_semantic_fuse_to_one_row(semantic_db):
    """A query matching BOTH ways must return the row once, not twice."""
    await asyncio.to_thread(_populate)
    monkey_query = "credential refresh policy decision"
    _VECS[monkey_query + " github"] = _VECS[monkey_query]  # same vector, adds a lexical token
    hits = await recall.search(monkey_query + " github")
    assert len(hits) == 1


async def test_model_mismatch_never_compared(semantic_db, monkeypatch):
    await asyncio.to_thread(_populate)                    # stored under "fake-3"
    embed_sync, aembed = _fake_provider(model="other-2")  # queries under "other-2"
    monkeypatch.setattr(embeddings, "aembed", aembed)
    # No same-model vectors and no lexical overlap → clean empty, no crash.
    assert await recall.search("credential refresh policy decision") == []


async def test_provider_failure_degrades_to_fts(semantic_db, monkeypatch):
    await asyncio.to_thread(_populate)

    async def broken(texts):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(embeddings, "aembed", broken)
    hits = await recall.search("github token")            # lexical still works
    assert len(hits) == 1


def test_ensure_embeddings_backfills_missing(semantic_db, monkeypatch):
    # Write a row with embeddings off → no vector.
    monkeypatch.setattr(embeddings, "ENABLED", False)
    recall.record_run("rotate the github api token", "Rotated and stored in vault.", [])
    monkeypatch.setattr(embeddings, "ENABLED", True)
    assert recall.ensure_embeddings() == 1
    assert recall.ensure_embeddings() == 0                # idempotent


def test_blob_roundtrip_and_cosine():
    v = [0.25, -1.5, 3.0]
    assert embeddings.from_blob(embeddings.to_blob(v)) == pytest.approx(v)
    assert embeddings.cosine(v, v) == pytest.approx(1.0)
    assert embeddings.cosine([1, 0], [0, 1]) == 0.0
    assert embeddings.cosine([1, 0], [1]) == 0.0          # dim mismatch → 0

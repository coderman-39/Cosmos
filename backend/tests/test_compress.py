"""Query-aware prompt compression (services.compress).

Contract under test:
  - Small text passes through untouched.
  - Oversized text is shrunk toward the target and KEEPS the query-relevant
    parts (the needle survives, the filler doesn't).
  - With no query it degrades to a head+tail keep (still shrinks, keeps the tail).
  - The marker is appended so callers can detect an already-compressed block.
  - Never raises.
"""

from services import compress


def test_small_text_is_untouched():
    assert compress.compress_sync("short", "query", target_chars=400) == "short"


def test_shrinks_oversized_text():
    big = "filler " * 400
    out = compress.compress_sync(big, "", target_chars=300, marker=" [C]")
    assert len(out) < len(big)
    assert out.endswith(" [C]")


def test_keeps_the_query_relevant_needle():
    filler = "\n".join(f"unrelated log line number {i} about weather" for i in range(300))
    needle = "the GITHUB api token was rotated at 14:32 by alice"
    big = filler + "\n" + needle + "\n" + ("tail noise " * 20)
    out = compress.compress_sync(big, "github token rotation", target_chars=400,
                                 marker=" [C]")
    assert len(out) < len(big)
    assert "github" in out.lower()        # the relevant unit was retained


def test_no_query_keeps_head_and_tail():
    text = "HEAD-MARKER" + ("m" * 3000) + "TAIL-MARKER"
    out = compress.compress_sync(text, "", target_chars=300, head_chars=180,
                                 tail_chars=80, marker=" [C]")
    assert out.startswith("HEAD-MARKER")
    assert "TAIL-MARKER" in out
    assert len(out) < 400                  # honours the prompt-cache invariant


def test_never_raises_on_bad_input():
    # None / weird types must degrade, not explode.
    assert compress.compress_sync(None, None, 100) == (None or "")


async def test_acompress_falls_back_without_embeddings(monkeypatch):
    async def no_embed(texts):
        return None
    monkeypatch.setattr(compress.embeddings, "aembed", no_embed)
    big = "apples " * 400
    out = await compress.acompress(big, "apples", target_chars=200, marker=" [C]")
    assert len(out) < len(big) and out.endswith(" [C]")

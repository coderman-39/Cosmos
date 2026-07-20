"""Context ledger (services.ledger) — the evidence set a run stood on.

Contract under test:
  - cite() dedupes by (kind, ref).
  - snapshot()/lines() render entries with version + summary.
  - ref_for() is a stable content hash (same text → same ref).
  - Never raises on odd input.
"""

from services import ledger


def test_cite_dedupes_by_kind_and_ref():
    lg = ledger.ContextLedger()
    lg.cite("recall", "r1", "asked X")
    lg.cite("recall", "r1", "asked X again")   # same (kind, ref) → ignored
    lg.cite("recall", "r2", "asked Y")
    assert len(lg) == 2


def test_lines_render_version_and_summary():
    lg = ledger.ContextLedger()
    lg.cite("raptor", "t1", "compliance theme", version="2")
    line = lg.lines()[0]
    assert "raptor" in line and "t1" in line and "v2" in line and "compliance" in line


def test_snapshot_is_a_copy():
    lg = ledger.ContextLedger()
    lg.cite("artifact", "a1", "wrote /x")
    snap = lg.snapshot()
    snap.append({"kind": "x", "ref": "y"})
    assert len(lg) == 1                     # mutating the snapshot doesn't leak back


def test_ref_for_is_stable():
    a = ledger.ref_for("some recall line", "recall")
    b = ledger.ref_for("some recall line", "recall")
    c = ledger.ref_for("a different line", "recall")
    assert a == b and a != c and a.startswith("recall:")


def test_cite_never_raises_on_bad_input():
    lg = ledger.ContextLedger()
    lg.cite(None, None)                     # must not raise
    lg.cite("k", 123, version=0)
    assert len(lg) == 2

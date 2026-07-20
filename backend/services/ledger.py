"""Context ledger — the typed record of what evidence a run is standing on.

SCBench's core finding is "consistency drift": the retriever, summariser and
generator are each internally correct but looking at different compressed views
of the same source. The fix is to make the evidence set EXPLICIT — every run
carries a ledger of the exact artefacts it used (past-run row, RAPTOR summary
node + level, tool artefact, compressed block, document version). That makes
retries idempotent (same evidence in → same answer) and lets the HUD / trace
show precisely what a given answer was grounded on.

Deliberately tiny and dependency-free. Like every other COSMOS bookkeeping
surface, it must never raise — a ledger failure can't be allowed to break a run.
"""

import hashlib

# Recognised evidence kinds (free-form is allowed; these are the common ones).
KINDS = ("recall", "raptor", "artifact", "compressed", "document", "skill", "memory")


def ref_for(text: str, prefix: str = "") -> str:
    """Stable short id for a piece of evidence that has no natural id (e.g. a
    recall line) — a content hash, so the same evidence always cites the same
    ref across runs and retries."""
    h = hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:10]
    return f"{prefix}:{h}" if prefix else h


class ContextLedger:
    """Append-only, deduped list of evidence entries for one run."""

    def __init__(self):
        self.entries: list[dict] = []
        self._seen: set[tuple[str, str]] = set()

    def cite(self, kind: str, ref, summary: str = "", version: str = "") -> None:
        """Record one evidence entry. Deduped by (kind, ref); never raises."""
        try:
            k, r = str(kind), str(ref)
            if (k, r) in self._seen:
                return
            self._seen.add((k, r))
            self.entries.append({
                "kind": k, "ref": r,
                "summary": (summary or "")[:160],
                "version": str(version) if version != "" else "",
            })
        except Exception:
            pass

    def snapshot(self) -> list[dict]:
        return list(self.entries)

    def lines(self) -> list[str]:
        out = []
        for e in self.entries:
            v = f" v{e['version']}" if e.get("version") else ""
            s = f" — {e['summary']}" if e.get("summary") else ""
            out.append(f"[{e['kind']}] {e['ref']}{v}{s}")
        return out

    def __len__(self) -> int:
        return len(self.entries)

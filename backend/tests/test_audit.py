"""services.audit: snapshot() backups + record() append-only log.

All module-level paths (_DIR, _AUDIT, _UNDO_DIR) are monkeypatched into tmp_path
so the real ~/.friday tree is never touched.
"""

import json
from pathlib import Path

import pytest

from services import audit


@pytest.fixture
def audit_paths(tmp_path, monkeypatch):
    d = tmp_path / ".friday"
    monkeypatch.setattr(audit, "_DIR", d)
    monkeypatch.setattr(audit, "_AUDIT", d / "audit.jsonl")
    monkeypatch.setattr(audit, "_UNDO_DIR", d / "undo")
    return d


# ─── snapshot ──────────────────────────────────────────────────────────────────

def test_snapshot_backs_up_existing_file(audit_paths, tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("original contents")
    backup = audit.snapshot(src)
    assert backup is not None
    assert Path(backup).read_text() == "original contents"
    # Backup lives under the (patched) undo dir.
    assert Path(backup).parent == audit._UNDO_DIR


def test_snapshot_missing_file_returns_none(audit_paths, tmp_path):
    assert audit.snapshot(tmp_path / "nope.txt") is None


def test_snapshot_never_raises_on_failure(audit_paths, tmp_path, monkeypatch):
    src = tmp_path / "doc.txt"
    src.write_text("x")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(audit.shutil, "copy2", boom)
    # Must swallow and return None, not raise.
    assert audit.snapshot(src) is None


# ─── record ────────────────────────────────────────────────────────────────────

def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_appends_one_json_line_per_call(audit_paths):
    audit.record("bash", "ls -la", ok=True)
    audit.record("open_app", "Slack", ok=True)
    lines = _read_lines(audit._AUDIT)
    assert len(lines) == 2
    assert lines[0]["tool"] == "bash"
    assert lines[1]["tool"] == "open_app"


def test_record_has_expected_keys(audit_paths):
    audit.record("bash", "rm -rf x", ok=False, danger="Destructive", confirmed=True)
    entry = _read_lines(audit._AUDIT)[0]
    assert set(["ts", "tool", "ok", "summary"]).issubset(entry.keys())
    assert entry["tool"] == "bash"
    assert entry["ok"] is False
    assert entry["danger"] == "Destructive"
    assert entry["confirmed"] is True


def test_record_omits_danger_and_confirmed_when_not_provided(audit_paths):
    audit.record("date", "date", ok=True)
    entry = _read_lines(audit._AUDIT)[0]
    assert "danger" not in entry
    assert "confirmed" not in entry


def test_record_never_raises_on_failure(audit_paths, monkeypatch):
    # Force json.dumps to blow up inside record() — it must be swallowed.
    def boom(*a, **k):
        raise ValueError("cannot serialize")

    monkeypatch.setattr(audit.json, "dumps", boom)
    audit.record("bash", "ls", ok=True)  # no exception escapes

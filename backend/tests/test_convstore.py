"""services.convstore: per-conversation persistence + hardening.

convstore.DIR is monkeypatched to a tmp dir so tests never touch the real
~/.friday/conversations/.
"""

import json
import os
import time

import pytest

from services import convstore


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    d = tmp_path / "conversations"
    monkeypatch.setattr(convstore, "DIR", d)
    monkeypatch.setattr(convstore, "LEGACY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(convstore, "_migrated", False)
    return d


def test_save_load_roundtrip(store_dir):
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello sir"},
    ]
    convstore.save("conv-1", history)
    assert convstore.load("conv-1") == history


def test_conversations_are_isolated(store_dir):
    convstore.save("conv-a", [{"role": "user", "content": "about docker"}])
    convstore.save("conv-b", [{"role": "user", "content": "about lunch"}])
    assert convstore.load("conv-a") == [{"role": "user", "content": "about docker"}]
    assert convstore.load("conv-b") == [{"role": "user", "content": "about lunch"}]
    assert convstore.load("conv-c") == []


def test_conv_id_sanitized_no_path_escape(store_dir):
    convstore.save("../../evil", [{"role": "user", "content": "x"}])
    # Whatever file was written must live INSIDE the conversations dir.
    written = list(store_dir.rglob("*.json"))
    assert written, "nothing was written"
    for f in written:
        assert store_dir in f.parents


def test_legacy_history_migrates_to_default(store_dir, tmp_path):
    legacy = tmp_path / "history.json"
    legacy.write_text(json.dumps([{"role": "user", "content": "old chat"}]))
    assert convstore.load(convstore.DEFAULT_ID) == [{"role": "user", "content": "old chat"}]
    assert not legacy.exists(), "legacy file must be moved, not copied"


def test_corrupt_bytes_yield_empty(store_dir):
    store_dir.mkdir(parents=True)
    (store_dir / "bad.json").write_bytes(b"\x00\x01not json at all")
    assert convstore.load("bad") == []


def test_non_list_json_yields_empty(store_dir):
    store_dir.mkdir(parents=True)
    (store_dir / "x.json").write_text(json.dumps({"role": "user", "content": "x"}))
    assert convstore.load("x") == []


def test_over_cap_truncated_to_last_16(store_dir):
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(40)
    ]
    convstore.save("big", history)
    loaded = convstore.load("big")
    assert len(loaded) == 16
    # Kept the MOST RECENT 16.
    assert loaded[0]["content"] == "m24"
    assert loaded[-1]["content"] == "m39"


def test_invalid_entries_dropped(store_dir):
    raw = [
        {"role": "user", "content": "keep me"},
        {"role": "user", "content": 123},          # non-str content → drop
        {"role": "system", "content": "nope"},     # bad role → drop
        {"role": "assistant", "content": "keep 2"},
        "not even a dict",                          # → drop
    ]
    store_dir.mkdir(parents=True)
    (store_dir / "mixed.json").write_text(json.dumps(raw))
    assert convstore.load("mixed") == [
        {"role": "user", "content": "keep me"},
        {"role": "assistant", "content": "keep 2"},
    ]


def _backdate(path, seconds):
    """Age a file's mtime by `seconds` to simulate an idle conversation."""
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_slack_conversation_flushed_after_ttl(store_dir):
    convstore.save("slack-C1-1720000000", [{"role": "user", "content": "old"}])
    path = store_dir / "slack-C1-1720000000.json"
    _backdate(path, convstore._SLACK_TTL_S + 60)   # 3 days + a minute idle
    # Context is forgotten and the stale file is removed on load.
    assert convstore.load("slack-C1-1720000000") == []
    assert not path.exists()


def test_slack_conversation_kept_within_ttl(store_dir):
    convstore.save("slack-C1-2", [{"role": "user", "content": "recent"}])
    _backdate(store_dir / "slack-C1-2.json", convstore._SLACK_TTL_S - 3600)  # 1h short
    assert convstore.load("slack-C1-2") == [{"role": "user", "content": "recent"}]


def test_non_slack_conversation_never_expires(store_dir):
    convstore.save("hud-chat", [{"role": "user", "content": "keep"}])
    _backdate(store_dir / "hud-chat.json", convstore._SLACK_TTL_S * 10)
    assert convstore.load("hud-chat") == [{"role": "user", "content": "keep"}]


def test_exists_recognizes_stale_thread_for_fresh_run(store_dir):
    # A flushed thread is still recognized (exists) so a reply runs fresh,
    # even though its loaded context is empty.
    convstore.save("slack-C1-3", [{"role": "user", "content": "old"}])
    _backdate(store_dir / "slack-C1-3.json", convstore._SLACK_TTL_S + 60)
    assert convstore.exists("slack-C1-3") is True
    assert convstore.load("slack-C1-3") == []


def test_delete_removes_file_and_is_idempotent(store_dir):
    convstore.save("gone", [{"role": "user", "content": "x"}])
    assert (store_dir / "gone.json").exists()
    convstore.delete("gone")
    assert not (store_dir / "gone.json").exists()
    convstore.delete("gone")   # second delete must not raise

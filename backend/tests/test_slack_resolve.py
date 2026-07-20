"""services.slack — DM-partner name matching (the fix for resolving people who
are in your DMs but past the company directory's page cap)."""

import pytest

from services import slack


@pytest.fixture(autouse=True)
def seed(monkeypatch):
    # A tiny fake DM roster: user_id -> {name, real_name}
    monkeypatch.setattr(slack, "_dm_by_user", {
        "U1": "d1", "U2": "d2", "U3": "d3", "U4": "d4",
    })
    monkeypatch.setattr(slack, "_user_cache", {
        "U1": {"name": "carol.jones",  "real_name": "Mary Carol Jones"},
        "U2": {"name": "alice.h",      "real_name": "Alice H"},
        "U3": {"name": "alice.smith",  "real_name": "J Alice Smith"},
        "U4": {"name": "bobby",        "real_name": "Bob Martin"},
    })


def test_partial_matches_full_name():
    uid, cands = slack._match_dm_partner("Carol")
    assert uid == "U1" and not cands


def test_handle_matches():
    uid, _ = slack._match_dm_partner("bobby")
    assert uid == "U4"


def test_exact_beats_contains():
    # "Alice H" is exact; "Alice" alone is ambiguous (H vs Smith).
    uid, _ = slack._match_dm_partner("Alice H")
    assert uid == "U2"


def test_ambiguous_returns_candidates():
    uid, cands = slack._match_dm_partner("Alice")
    assert uid is None
    assert set(cands) == {"Alice H", "J Alice Smith"}


def test_unknown_returns_nothing():
    uid, cands = slack._match_dm_partner("Napoleon")
    assert uid is None and cands == []

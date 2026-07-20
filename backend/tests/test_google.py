"""Google Workspace integration (Gmail/Calendar/Docs/Sheets/Meet).

Network is faked: _access_token is monkeypatched and httpx is intercepted via
a stub AsyncClient, so no real Google call is made. Covers token caching,
request routing, the read-vs-write risk gate, gmail send journaling, the
API→Chrome→vision fallback hint, and calendar-event undo.
"""

import json
import time

import pytest

from services import agent, google as g, outbox


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.content = b"x" if (payload is not None or text) else b""

    def json(self):
        return self._payload


class _StubClient:
    """Records requests; replays queued responses by (method, url-substring)."""

    routes = []          # list of (method, url_substr, _Resp)
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, params=None, json=None):
        _StubClient.calls.append({"method": method, "url": url, "json": json,
                                  "params": params})
        for m, sub, resp in _StubClient.routes:
            if m == method and sub in url:
                return resp
        return _Resp(404, {"error": "no stub route"})

    async def post(self, url, data=None, json=None, headers=None):
        return await self.request("POST", url, headers, None, json or data)


@pytest.fixture
def gapi(monkeypatch):
    monkeypatch.setattr(g, "AVAILABLE", True)
    monkeypatch.setattr(g, "httpx", type("h", (), {"AsyncClient": _StubClient}))

    async def fake_token():
        return "fake-access-token"

    monkeypatch.setattr(g, "_access_token", fake_token)
    _StubClient.routes = []
    _StubClient.calls = []
    return _StubClient


# ─── Token cache ───────────────────────────────────────────────────────────────

async def test_token_cached_until_expiry(monkeypatch):
    calls = {"n": 0}

    class TokClient(_StubClient):
        async def post(self, url, data=None, json=None, headers=None):
            calls["n"] += 1
            return _Resp(200, {"access_token": f"tok{calls['n']}", "expires_in": 3600})

    monkeypatch.setattr(g, "httpx", type("h", (), {"AsyncClient": TokClient}))
    monkeypatch.setattr(g, "AVAILABLE", True)
    monkeypatch.setattr(g, "CLIENT_ID", "x")
    monkeypatch.setattr(g, "CLIENT_SECRET", "y")
    monkeypatch.setattr(g, "REFRESH_TOKEN", "z")
    g._token.update({"value": None, "exp": 0.0})
    t1 = await g._access_token()
    t2 = await g._access_token()
    assert t1 == t2 == "tok1"
    assert calls["n"] == 1, "second call must use the cache"
    # Force expiry → refreshes.
    g._token["exp"] = time.time() - 1
    t3 = await g._access_token()
    assert t3 == "tok2" and calls["n"] == 2


# ─── Gmail ─────────────────────────────────────────────────────────────────────

async def test_gmail_search(gapi):
    gapi.routes = [
        ("GET", "/users/me/messages", _Resp(200, {"messages": [{"id": "m1"}]})),
    ]
    # search first hits the list endpoint, then a metadata GET per id (same substr);
    # queue the metadata response to be returned for the per-message call too.
    gapi.routes.append(("GET", "/users/me/messages/m1",
                        _Resp(200, {"snippet": "hi there",
                                    "payload": {"headers": [
                                        {"name": "From", "value": "alice@example.com"},
                                        {"name": "Subject", "value": "PR review"}]}})))
    # route order matters: more-specific /m1 must win → put it first.
    gapi.routes.reverse()
    ok, out = await g.gmail_search("is:unread", 5)
    assert ok and "PR review" in out and "alice@example.com" in out


async def test_gmail_send_journals(gapi, monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "FILE", tmp_path / "outbox.jsonl")
    gapi.routes = [("POST", "/messages/send", _Resp(200, {"id": "sent1"}))]
    ok, out = await g.gmail_send("alice@example.com", "hello", "body text")
    assert ok and "sent to alice@example.com" in out
    rec = outbox.recent(n=1)[0]
    assert rec["tool"] == "google" and rec["action"] == "gmail_send"
    assert rec["undoable"] is False          # email can't be unsent
    # The RFC822 was base64url-encoded into a raw field.
    body = _StubClient.calls[-1]["json"]
    assert "raw" in body


# ─── Calendar / undo ───────────────────────────────────────────────────────────

async def test_calendar_create_journals_undoable(gapi, monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "FILE", tmp_path / "outbox.jsonl")
    gapi.routes = [("POST", "/events", _Resp(200, {"id": "ev1",
                                                   "hangoutLink": "https://meet/x"}))]
    ok, out = await g.calendar_create("Standup", "2026-07-09T15:00:00+05:30",
                                      with_meet=True)
    assert ok and "meet/x" in out.lower().replace("meet: ", "meet/") or "ev1" in out
    rec = outbox.recent(n=1)[0]
    assert rec["action"] == "calendar_create" and rec["undoable"] is True
    assert rec["handle"]["event_id"] == "ev1"


async def test_calendar_undo_deletes_event(gapi):
    gapi.routes = [("DELETE", "/events/ev1", _Resp(204))]
    ok, msg = await g.undo_action({"tool": "google", "action": "calendar_create",
                                   "handle": {"calendar_id": "primary", "event_id": "ev1"}})
    assert ok and "deleted" in msg
    assert _StubClient.calls[-1]["method"] == "DELETE"


async def test_undo_no_inverse_for_email(gapi):
    ok, msg = await g.undo_action({"tool": "google", "action": "gmail_send",
                                   "handle": {}})
    assert not ok and "no inverse" in msg


# ─── Error / fallback hint ─────────────────────────────────────────────────────

async def test_disabled_api_gives_actionable_hint(gapi):
    gapi.routes = [("GET", "/calendarList", _Resp(
        403, text='{"error":{"message":"Calendar API has not been used in project"}}'))]
    ok, out = await g._request("calendar", "GET", "/users/me/calendarList")
    assert not ok
    assert "not enabled" in out and "[FALLBACK]" in out and "Chrome" in out


async def test_not_configured_hint(monkeypatch):
    monkeypatch.setattr(g, "AVAILABLE", False)
    ok, out = await g.gmail_search("x")
    assert not ok and "isn't configured" in out and "[FALLBACK]" in out


def test_secret_scrub(monkeypatch):
    monkeypatch.setattr(g, "_secret_re", __import__("re").compile("SUPERSECRET"))
    assert "SUPERSECRET" not in g._scrub("token=SUPERSECRET failed")
    assert "[redacted]" in g._scrub("token=SUPERSECRET failed")


# ─── Risk gate + label (in agent) ──────────────────────────────────────────────

def test_gate_reads_free_writes_confirm():
    assert agent.needs_confirmation("google", {"service": "gmail", "action": "search",
                                               "query": "x"}, "ask") is None
    assert agent.needs_confirmation("google", {"service": "calendar", "action": "list"},
                                    "ask") is None
    assert agent.needs_confirmation("google", {"service": "docs", "action": "read",
                                               "id": "d"}, "ask") is None
    # Writes confirm in ask mode…
    assert agent.needs_confirmation("google", {"service": "gmail", "action": "send",
                                               "to": "v@x.com"}, "ask")
    assert agent.needs_confirmation("google", {"service": "calendar", "action": "create",
                                               "summary": "x"}, "ask")
    assert agent.needs_confirmation("google", {"service": "sheets", "action": "write"},
                                    "ask")
    # …but not in full mode (outward-but-recoverable, like Slack sends).
    assert agent.needs_confirmation("google", {"service": "gmail", "action": "send",
                                               "to": "v@x.com"}, "full") is None


def test_confirm_summary_shows_recipient_and_body():
    s = agent._confirm_summary("google", {"service": "gmail", "action": "send",
                                          "to": "alice@example.com", "subject": "Hi",
                                          "body": "the full body"})
    assert "alice@example.com" in s and "Hi" in s and "the full body" in s


def test_worker_google_reads_ok_writes_blocked():
    # Sub-agent workers may do Google READS (research) but never sends/writes.
    assert agent._read_only_block("google", {"service": "gmail", "action": "search"}) is None
    assert agent._read_only_block("google", {"service": "calendar", "action": "list"}) is None
    assert agent._read_only_block("google", {"service": "docs", "action": "read"}) is None
    assert agent._read_only_block("google", {"service": "gmail", "action": "send"})
    assert agent._read_only_block("google", {"service": "calendar", "action": "create"})
    assert agent._read_only_block("google", {"service": "meet", "action": "create"})

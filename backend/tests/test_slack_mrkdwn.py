"""services.slack.to_mrkdwn — standard/GitHub markdown → Slack mrkdwn, so a
structured reply renders as one properly-formatted message."""

from services import slack


def test_bold_double_to_single():
    assert slack.to_mrkdwn("**bold**") == "*bold*"
    assert slack.to_mrkdwn("__bold__") == "*bold*"


def test_bullets_to_slack_dots():
    out = slack.to_mrkdwn("- one\n- two\n* three\n+ four")
    assert out == "• one\n• two\n• three\n• four"


def test_headings_become_bold():
    assert slack.to_mrkdwn("## Phase 1") == "*Phase 1*"
    assert slack.to_mrkdwn("# Title") == "*Title*"


def test_links_rewritten():
    assert slack.to_mrkdwn("[the doc](https://x.com/d)") == "<https://x.com/d|the doc>"


def test_italic_and_strike():
    assert slack.to_mrkdwn("*note*") == "_note_"
    assert slack.to_mrkdwn("~~old~~") == "~old~"


def test_bold_not_mangled_into_italic():
    # The italic pass must not touch text already claimed by bold.
    assert slack.to_mrkdwn("**done** and *soon*") == "*done* and _soon_"


def test_newlines_preserved_one_message():
    src = "**Highlights**\n- a\n- b"
    out = slack.to_mrkdwn(src)
    assert out == "*Highlights*\n• a\n• b"
    assert "\n" in out          # stays one multi-line message, not stacked


def test_numbered_list_preserved():
    assert slack.to_mrkdwn("1. do X\n2. do Y") == "1. do X\n2. do Y"

# ─── list_mentions: per-question answered-detection, incl. threads ─────────────

import asyncio as _asyncio


def _mock_slack(monkeypatch, history, threads=None):
    """history: top-level messages. threads: {root_ts: [messages...]}."""
    from services import slack
    threads = threads or {}

    async def fake_api(method, params=None, timeout=20):
        if method == "conversations.history":
            return {"ok": True, "messages": history}
        if method == "conversations.replies":
            return {"ok": True, "messages": threads.get(params["ts"], [])}
        if method == "auth.test":
            return {"ok": True, "user_id": "MEID"}
        return {"ok": True}

    monkeypatch.setattr(slack, "_api", fake_api)
    monkeypatch.setattr(slack, "_self_id_cache", "MEID")
    async def fake_resolve(t): return "C1", []
    monkeypatch.setattr(slack, "_resolve_channel", fake_resolve)
    async def noop(*a, **k): return "someone"
    monkeypatch.setattr(slack, "_name_for", noop)
    async def passthrough(hits, me_name): return hits, 0
    monkeypatch.setattr(slack, "_filter_directed", passthrough)
    return slack


def _q(ts, text, user="U_OTHER", **extra):
    return {"ts": ts, "user": user, "text": text, **extra}


def test_unanswered_root_surfaces(monkeypatch):
    sl = _mock_slack(monkeypatch, [_q("100", "<@MEID> open question", reply_count=0)])
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "ts=100" in out and "open question" in out


def test_answered_root_skipped(monkeypatch):
    sl = _mock_slack(monkeypatch,
        [_q("100", "<@MEID> q", reply_count=1)],
        {"100": [_q("100", "<@MEID> q"), _q("101", "sure, here you go")]})
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "No unanswered mentions" in out and "1 already answered" in out


def test_two_questions_in_thread_one_open(monkeypatch):
    # Q1 asked, answered, THEN Q2 asked in the same thread — only Q2 is open.
    sl = _mock_slack(monkeypatch,
        [_q("100", "<@MEID> question one", reply_count=2)],
        {"100": [
            _q("100", "<@MEID> question one"),
            _q("101", "here is the answer to one", user="U_OTHER"),
            _q("102", "<@MEID> question two"),          # last, no answer after
        ]})
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "question two" in out
    assert "question one" not in out                    # Q1 was answered
    assert "ts=100" in out                              # reply attaches to thread root


def test_two_questions_both_open(monkeypatch):
    # Two @me questions back to back, nothing answered either.
    sl = _mock_slack(monkeypatch,
        [_q("100", "<@MEID> q one", reply_count=1)],
        {"100": [_q("100", "<@MEID> q one"), _q("101", "<@MEID> q two")]})
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "q one" in out and "q two" in out            # neither answered → both surface


def test_mention_only_inside_thread(monkeypatch):
    # Root doesn't mention me; a thread reply does and is unanswered.
    sl = _mock_slack(monkeypatch,
        [_q("100", "hey folks", reply_count=1)],
        {"100": [_q("100", "hey folks"), _q("101", "<@MEID> can you check this")]})
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "can you check this" in out and "ts=100" in out


def test_answered_by_someone_else_skipped(monkeypatch):
    sl = _mock_slack(monkeypatch,
        [_q("100", "<@MEID> q", reply_count=1)],
        {"100": [_q("100", "<@MEID> q"), _q("101", "I got this one", user="U_X")]})
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "No unanswered mentions" in out              # anyone answering counts


def test_my_own_and_system_ignored(monkeypatch):
    sl = _mock_slack(monkeypatch, [
        _q("100", "<@MEID> mine", user="MEID", reply_count=0),
        _q("101", "<@MEID> joined", subtype="channel_join"),
    ])
    ok, out = _asyncio.run(sl.list_mentions("c"))
    assert "No unanswered mentions" in out

# ─── _filter_directed: LLM triage of directed vs passing mentions ──────────────

def _mock_llm(monkeypatch, reply_text):
    from services import slack, llm
    class _Blk:
        type = "text"
        def __init__(self, t): self.text = t
    class _Resp:
        def __init__(self, t): self.content = [_Blk(t)]
    async def fake_acreate(**kw): return _Resp(reply_text)
    monkeypatch.setattr(llm, "acreate", fake_acreate)
    monkeypatch.setattr(llm, "extract_text", lambda r: r.content[0].text)


def _hit(text):
    return ({"user": "U_X", "text": text, "ts": "1"}, "root")


def test_filter_keeps_indicated(monkeypatch):
    from services import slack
    monkeypatch.setattr(slack, "_user_cache", {"U_X": {"name": "x", "real_name": "X"}})
    _mock_llm(monkeypatch, "0, 2")
    hits = [_hit("q for me"), _hit("passing"), _hit("also for me")]
    kept, dropped = _asyncio.run(slack._filter_directed(hits, "Alice"))
    assert [h[0]["text"] for h in kept] == ["q for me", "also for me"]
    assert dropped == 1


def test_filter_none(monkeypatch):
    from services import slack
    monkeypatch.setattr(slack, "_user_cache", {"U_X": {"name": "x", "real_name": "X"}})
    _mock_llm(monkeypatch, "none")
    kept, dropped = _asyncio.run(slack._filter_directed([_hit("passing")], "Alice"))
    assert kept == [] and dropped == 1


def test_filter_unparseable_keeps_all(monkeypatch):
    # Model returns junk → never silently drop a real question.
    from services import slack
    monkeypatch.setattr(slack, "_user_cache", {"U_X": {"name": "x", "real_name": "X"}})
    _mock_llm(monkeypatch, "hmm not sure")
    hits = [_hit("a"), _hit("b")]
    kept, dropped = _asyncio.run(slack._filter_directed(hits, "Alice"))
    assert kept == hits and dropped == 0


def test_filter_llm_failure_keeps_all(monkeypatch):
    from services import slack, llm
    monkeypatch.setattr(slack, "_user_cache", {"U_X": {"name": "x", "real_name": "X"}})
    async def boom(**kw): raise RuntimeError("gateway down")
    monkeypatch.setattr(llm, "acreate", boom)
    hits = [_hit("a")]
    kept, dropped = _asyncio.run(slack._filter_directed(hits, "Alice"))
    assert kept == hits and dropped == 0

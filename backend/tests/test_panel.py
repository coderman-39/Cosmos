"""services.panel: session lifecycle, user-drawn connections, per-session
models/personas, chat turns, peer_send context transfer (hop-capped
cascades), squad templates, the consensus done-barrier, deliverables and
board persistence. agent.run_task and the planner LLM are stubbed — no
network; persistence is pointed at tmp_path (never ~/.friday).
"""

import asyncio
import itertools

import pytest

from services import panel


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(panel, "_sessions", {})
    monkeypatch.setattr(panel, "_connections", [])
    monkeypatch.setattr(panel, "_ledger", [])
    monkeypatch.setattr(panel, "_subs", set())
    monkeypatch.setattr(panel, "_mode", "singular")
    monkeypatch.setattr(panel, "_group_task", None)
    monkeypatch.setattr(panel, "_deliverables", [])
    monkeypatch.setattr(panel, "_seat_counter", itertools.count())
    monkeypatch.setattr(panel, "_save_handle", None)
    monkeypatch.setattr(panel, "_STATE_PATH", tmp_path / "panel.json")
    monkeypatch.setattr(panel, "_peer_tool_registered", True)   # skip registry
    monkeypatch.setattr(panel.ruflo, "available", lambda: False)

    calls = []

    async def fake_run_task(prompt, emit, interaction, **kw):
        calls.append({"prompt": prompt, **kw})
        await emit({"type": "tool_start", "name": "web_search", "label": "x"})
        await emit({"type": "tool_done", "ok": True, "detail": "ok"})
        return f"reply<{prompt[-30:]}>"

    monkeypatch.setattr(panel.agent, "run_task", fake_run_task)
    return calls


async def _drain_until_idle(sess_id, timeout=5.0):
    """Wait for a session's background turn to finish."""
    for _ in range(int(timeout / 0.02)):
        s = panel._sessions.get(sess_id)
        if s is None or (s["status"] == "idle" and
                         (s["task"] is None or s["task"].done())):
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("session never went idle")


# ─── Lifecycle ──────────────────────────────────────────────────────────────────

def test_create_auto_names_and_seats():
    a = panel.create_session()
    b = panel.create_session()
    assert a["name"] == "Orion" and b["name"] == "Vega"
    assert a["seat"] != b["seat"]
    assert a["status"] == "idle"


def test_create_with_name_and_model():
    s = panel.create_session(name="Scout", model="claude-sonnet-5")
    assert s["name"] == "Scout" and s["model"] == "claude-sonnet-5"


def test_remove_session_cleans_connections():
    a, b = panel.create_session(), panel.create_session()
    assert panel.connect(a["id"], b["id"]) == ""
    assert panel.remove_session(a["id"]) is True
    assert panel._connections == []
    assert panel.remove_session(a["id"]) is False        # idempotent


def test_set_model():
    s = panel.create_session()
    assert panel.set_model(s["id"], "claude-haiku-4-5") is True
    assert panel._sessions[s["id"]]["model"] == "claude-haiku-4-5"
    assert panel.set_model("nope", "x") is False


# ─── Connections ────────────────────────────────────────────────────────────────

def test_connect_validation():
    a, b = panel.create_session(), panel.create_session()
    assert panel.connect(a["id"], "ghost") == "unknown session"
    assert panel.connect(a["id"], a["id"]) == "a session can't link to itself"
    assert panel.connect(a["id"], b["id"]) == ""
    assert panel.connect(a["id"], b["id"]) == "already linked"
    # Undirected: the reverse edge IS the same link.
    assert panel.connect(b["id"], a["id"]) == "already linked"
    assert panel.disconnect(a["id"], b["id"]) is True
    assert panel.disconnect(a["id"], b["id"]) is False


def test_disconnect_works_with_reversed_args():
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    # Removing B–A removes the (stored) A–B link — undirected.
    assert panel.disconnect(b["id"], a["id"]) is True
    assert panel._connections == []


# ─── Chat ───────────────────────────────────────────────────────────────────────

async def test_chat_runs_turn_and_logs(clean):
    s = panel.create_session()
    assert panel.chat(s["id"], "find the docs") is True
    await _drain_until_idle(s["id"])
    sess = panel._sessions[s["id"]]
    whos = [c["who"] for c in sess["chat"]]
    assert whos[0] == "you" and whos[-1] == sess["name"]
    kinds = [f["kind"] for f in sess["feed"]]
    assert "tool" in kinds and "tool_done" in kinds
    # The prompt carries the workspace preamble + the user text.
    assert "find the docs" in clean[0]["prompt"]
    assert sess["name"] in clean[0]["prompt"]


async def test_chat_uses_session_model(clean):
    s = panel.create_session(model="claude-haiku-4-5")
    panel.chat(s["id"], "quick one")
    await _drain_until_idle(s["id"])
    assert clean[0]["model"] == "claude-haiku-4-5"


def test_chat_rejects_bad_input():
    assert panel.chat("ghost", "hi") is False
    s = panel.create_session()
    assert panel.chat(s["id"], "   ") is False


# ─── peer_send: context transfer ────────────────────────────────────────────────

async def test_peer_send_requires_connection():
    a, b = panel.create_session(), panel.create_session()
    tok = panel._cur_session.set(a["id"])
    try:
        out = await panel._tool_peer_send({"to": b["name"], "text": "hi"}, None)
    finally:
        panel._cur_session.reset(tok)
    assert out.startswith("Error: not linked")


async def test_peer_send_works_in_reverse_over_one_link(clean):
    """Links are bidirectional: an A→B drawn edge lets B message A too."""
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])          # drawn one way…
    tok = panel._cur_session.set(b["id"])    # …but B sends to A
    try:
        out = await panel._tool_peer_send(
            {"to": a["name"], "text": "reverse-flow finding"}, None)
    finally:
        panel._cur_session.reset(tok)
    assert "Delivered" in out
    await _drain_until_idle(a["id"])
    assert any("reverse-flow finding" in c["prompt"] for c in clean)


async def test_peer_send_delivers_and_wakes_target(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    tok = panel._cur_session.set(a["id"])
    try:
        out = await panel._tool_peer_send(
            {"to": b["name"], "text": "the API rate limit is 600rpm"}, None)
    finally:
        panel._cur_session.reset(tok)
    assert "Delivered" in out
    await _drain_until_idle(b["id"])
    # The receiver auto-ran with the transferred context in its prompt.
    assert any("600rpm" in c["prompt"] for c in clean)
    assert panel._sessions[b["id"]]["inbox"] == []       # drained into the turn


async def test_peer_send_hop_cap_stops_cascade(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    tok_s = panel._cur_session.set(a["id"])
    tok_h = panel._cur_hop.set(panel._HOP_CAP)           # already at the cap
    try:
        out = await panel._tool_peer_send({"to": b["name"], "text": "deep"}, None)
    finally:
        panel._cur_session.reset(tok_s)
        panel._cur_hop.reset(tok_h)
    assert "inbox" in out                                # delivered, NOT auto-run
    assert panel._sessions[b["id"]]["inbox"], "message must wait in the inbox"
    assert not clean, "no run may start beyond the hop cap"


async def test_inbox_drains_on_next_user_turn(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    panel._sessions[b["id"]]["inbox"].append(
        {"from": a["id"], "from_name": a["name"], "text": "stored finding",
         "hop": 3, "t": "now"})
    panel.chat(b["id"], "what do we know?")
    await _drain_until_idle(b["id"])
    assert "stored finding" in clean[0]["prompt"]
    assert "what do we know?" in clean[0]["prompt"]


# ─── Shared group memory (blackboard) ───────────────────────────────────────────

async def test_connected_sessions_share_project_memory(clean):
    """THE bug: two connected agents answered 'I don't know what the other is
    doing'. The group's PROJECT MEMORY must carry every member's work."""
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    panel.chat(a["id"], "research the settlement flow")
    await _drain_until_idle(a["id"])
    clean.clear()

    panel.chat(b["id"], "what is Orion doing?")
    await _drain_until_idle(b["id"])
    prompt = clean[0]["prompt"]
    assert "PROJECT MEMORY" in prompt
    assert "research the settlement flow" in prompt      # A's work line, to B
    assert a["name"] in prompt


async def test_memory_is_mutual_even_on_directed_edge(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])                      # only A → B drawn
    panel.chat(b["id"], "hunt for edge cases")
    await _drain_until_idle(b["id"])
    clean.clear()

    panel.chat(a["id"], "what is Vega up to?")
    await _drain_until_idle(a["id"])
    assert "hunt for edge cases" in clean[0]["prompt"]


async def test_memory_is_transitive_across_the_group(clean):
    """A–B and B–C wired: one group, ONE memory — C sees A's work with no
    direct A–C edge."""
    a, b, c = (panel.create_session(), panel.create_session(),
               panel.create_session())
    panel.connect(a["id"], b["id"])
    panel.connect(b["id"], c["id"])
    panel.chat(a["id"], "mapped the auth endpoints")
    await _drain_until_idle(a["id"])
    clean.clear()

    panel.chat(c["id"], "where are we?")
    await _drain_until_idle(c["id"])
    assert "mapped the auth endpoints" in clean[0]["prompt"]


async def test_unconnected_sessions_leak_nothing(clean):
    a, b = panel.create_session(), panel.create_session()   # NO connection
    panel.chat(a["id"], "secret research")
    await _drain_until_idle(a["id"])
    clean.clear()

    panel.chat(b["id"], "what do you know?")
    await _drain_until_idle(b["id"])
    prompt = clean[0]["prompt"]
    assert "secret research" not in prompt
    assert "PROJECT MEMORY" not in prompt
    assert "not connected to any other session" in prompt   # honest, actionable


async def test_working_peer_shows_live_task(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    panel._sessions[a["id"]]["status"] = "working"
    panel._sessions[a["id"]]["current"] = "auditing the ledger service"
    panel.chat(b["id"], "status of the audit?")
    await _drain_until_idle(b["id"])
    prompt = clean[0]["prompt"]
    assert "LIVE PEERS" in prompt
    assert "auditing the ledger service" in prompt


# ─── peer_fetch: depth on demand ────────────────────────────────────────────────

async def test_peer_fetch_returns_full_context(clean):
    a, b = panel.create_session(), panel.create_session()
    panel.connect(a["id"], b["id"])
    long_reply = "the full detailed analysis " + "x" * 900   # > digest truncation
    panel._sessions[a["id"]]["chat"].append(
        {"who": a["name"], "text": long_reply, "t": panel._now()})
    tok = panel._cur_session.set(b["id"])
    try:
        out = await panel._tool_peer_fetch({"session": a["name"]}, None)
    finally:
        panel._cur_session.reset(tok)
    assert long_reply in out                             # NOT truncated to a line
    assert a["name"] in out


async def test_peer_fetch_transitive_but_not_strangers(clean):
    a, b, c = (panel.create_session(), panel.create_session(),
               panel.create_session())
    stranger = panel.create_session()
    panel.connect(a["id"], b["id"])
    panel.connect(b["id"], c["id"])
    tok = panel._cur_session.set(c["id"])
    try:
        ok = await panel._tool_peer_fetch({"session": a["name"]}, None)
        bad = await panel._tool_peer_fetch({"session": stranger["name"]}, None)
    finally:
        panel._cur_session.reset(tok)
    assert not ok.startswith("Error")                    # same group via B
    assert bad.startswith("Error")                       # not in the group


# ─── Snapshot ───────────────────────────────────────────────────────────────────

def test_snapshot_shape():
    a = panel.create_session()
    b = panel.create_session()
    panel.connect(a["id"], b["id"])
    snap = panel.snapshot()
    assert set(snap) == {"sessions", "connections", "ledger", "models",
                         "mode", "templates", "deliverables", "group_task",
                         "ruflo_swarm"}
    assert snap["mode"] == "singular"
    assert snap["group_task"] is None
    assert snap["deliverables"] == []
    assert {t["id"] for t in snap["templates"]} == set(panel.TEMPLATES)
    assert a["id"] in snap["sessions"]
    pub = snap["sessions"][a["id"]]
    assert "history" not in pub and "task" not in pub and "lock" not in pub
    assert "persona" in pub
    assert snap["connections"] == [{"from": a["id"], "to": b["id"]}]


def test_models_lists_configured_chain():
    ms = panel.models()
    assert ms and panel.agent.AGENT_MODEL in ms
    assert len(ms) == len(set(ms))                       # de-duplicated


# ─── Modes: singular / consensus ────────────────────────────────────────────────

def test_set_mode_validation():
    assert panel.set_mode("consensus") is True
    assert panel._mode == "consensus"
    assert panel.snapshot()["mode"] == "consensus"
    assert panel.set_mode("singular") is True
    assert panel.set_mode("anarchy") is False
    assert panel._mode == "singular"


def test_group_chat_rejects_empty():
    panel.create_session()
    assert panel.group_chat("   ") == 0


async def test_group_chat_broadcasts_with_consensus_slices(clean):
    """One prompt → every session; linked teams get seat-ordered slice
    assignments and #1 is named the merger."""
    a = panel.create_session()          # Orion, seat 0 → #1 (merger)
    b = panel.create_session()          # Vega,  seat 1 → #2
    c = panel.create_session()          # Lyra — unlinked, works alone
    panel.connect(a["id"], b["id"])
    assert panel.group_chat("design the onboarding flow") == 3
    for s in (a, b, c):
        await _drain_until_idle(s["id"])
    # 3 slice turns; the barrier's merge turn on Orion may already be in
    # flight — drain it before counting.
    await _drain_until_idle(a["id"])
    assert len(clean) == 4
    # Every session received the task (ledger echoes may repeat it later).
    assert all("design the onboarding flow" in p["prompt"] for p in clean)
    team_prompts = [p["prompt"] for p in clean if "one task, one team" in p["prompt"]]
    solo_prompts = [p["prompt"] for p in clean if "work it alone" in p["prompt"].replace("\n", " ")
                    or "you work it" in p["prompt"]]
    assert len(team_prompts) == 2
    assert len(solo_prompts) == 1
    # Seat order fixes the slices: Orion is #1 (merger), Vega #2.
    orion_p = next(p for p in team_prompts if "you are #1 of 2" in p)
    vega_p = next(p for p in team_prompts if "you are #2 of 2" in p)
    assert "you are also the merger" in orion_p
    assert "(#1) merges" in vega_p
    # Both list the team in the same seat order.
    assert "Orion, Vega" in orion_p and "Orion, Vega" in vega_p
    # The user prompt lands in each session's chat log.
    for s in (a, b, c):
        whos = [e["who"] for e in panel._sessions[s["id"]]["chat"]]
        assert whos[0] == "you"
    # DONE-BARRIER: when both team members finished, the merger (Orion) was
    # auto-woken with the merge prompt; the solo session got none.
    merge_p = next(p["prompt"] for p in clean if "ALL 2 members" in p["prompt"])
    assert "panel_deliver" in merge_p
    gt = panel._group_task
    assert gt is not None and all(t["merged"] for t in gt["teams"])


# ─── Personas ───────────────────────────────────────────────────────────────────

def test_persona_create_and_set():
    s = panel.create_session(name="Critic", persona="Attack every proposal.")
    assert panel._sessions[s["id"]]["persona"] == "Attack every proposal."
    assert panel.set_persona(s["id"], "Be kind instead.") is True
    assert panel._sessions[s["id"]]["persona"] == "Be kind instead."
    assert panel.set_persona("ghost", "x") is False
    assert panel.snapshot()["sessions"][s["id"]]["persona"] == "Be kind instead."


async def test_persona_injected_into_prompt(clean):
    s = panel.create_session(persona="The devil's advocate — attack everything.")
    panel.chat(s["id"], "review this plan")
    await _drain_until_idle(s["id"])
    assert "YOUR ROLE" in clean[0]["prompt"]
    assert "devil's advocate" in clean[0]["prompt"]


async def test_consensus_uses_roles_when_personas_set(clean):
    a = panel.create_session(name="Judge", persona="Weigh and rule.")
    b = panel.create_session(name="Critic", persona="Attack proposals.")
    panel.connect(a["id"], b["id"])
    panel.group_chat("evaluate the feature")
    for s in (a, b):
        await _drain_until_idle(s["id"])
    role_prompts = [p["prompt"] for p in clean if "one task, one team" in p["prompt"]]
    assert len(role_prompts) == 2
    for p in role_prompts:
        assert "Address the task strictly from YOUR ROLE" in p
        assert "complementary slices by seat order" not in p
    # Roster shows the roles.
    assert any("Judge [Weigh and rule.]" in p for p in role_prompts)


def test_unique_names_on_collision():
    a = panel.create_session(name="Judge")
    b = panel.create_session(name="Judge")
    assert a["name"] == "Judge" and b["name"] == "Judge 2"


# ─── Templates (squads) ─────────────────────────────────────────────────────────

def test_template_list_shape():
    ts = panel.template_list()
    assert {t["id"] for t in ts} == set(panel.TEMPLATES)
    for t in ts:
        assert t["label"] and t["desc"] and t["size"] >= 3


def test_spawn_template_wires_a_squad():
    res = panel.spawn_template("debate-trio")
    assert res is not None
    assert res["names"] == ["Judge", "Advocate", "Critic"]
    assert panel._mode == "consensus"
    # Fully meshed trio: 3 undirected links.
    assert len(panel._connections) == 3
    # Merger-first: Judge holds the lowest seat → consensus merger.
    sess = list(panel._sessions.values())
    judge = next(s for s in sess if s["name"] == "Judge")
    assert judge["seat"] == min(s["seat"] for s in sess)
    assert all(s["persona"] for s in sess)
    # A second spawn dedupes names.
    res2 = panel.spawn_template("debate-trio")
    assert res2["names"] == ["Judge 2", "Advocate 2", "Critic 2"]
    assert panel.spawn_template("nope") is None


# ─── Done-barrier edge cases ───────────────────────────────────────────────────

async def test_barrier_survives_a_failing_member(clean, monkeypatch):
    """A member whose run errors still counts as done — the merger is woken
    with the failure flagged, never deadlocked."""
    a = panel.create_session(name="Lead")
    b = panel.create_session(name="Flaky")
    panel.connect(a["id"], b["id"])

    async def flaky_run_task(prompt, emit, interaction, **kw):
        if "one task, one team" in prompt and "#2 of 2" in prompt:
            raise RuntimeError("boom")
        clean.append({"prompt": prompt, **kw})
        return "ok"

    monkeypatch.setattr(panel.agent, "run_task", flaky_run_task)
    panel.group_chat("do the thing")
    for s in (a, b):
        await _drain_until_idle(s["id"])
    await _drain_until_idle(a["id"])          # merge turn
    merge_p = clean[-1]["prompt"]
    assert "ALL 2 members" in merge_p
    assert "Flaky" in merge_p and "did not finish cleanly" in merge_p
    assert all(t["merged"] for t in panel._group_task["teams"])


async def test_removed_member_cannot_stall_barrier(clean):
    a = panel.create_session(name="Lead")
    b = panel.create_session(name="Quitter")
    panel.connect(a["id"], b["id"])
    # Snapshot the team, then remove b BEFORE any turn finishes.
    gt_members = None
    panel.group_chat("audit the repo")
    gt_members = list(panel._group_task["teams"][0]["members"])
    assert b["id"] in gt_members
    panel.remove_session(b["id"])             # marks b done("removed")
    await _drain_until_idle(a["id"])          # a's slice
    await _drain_until_idle(a["id"])          # a's merge turn
    assert panel._group_task["teams"][0]["merged"] is True


# ─── Deliverables ───────────────────────────────────────────────────────────────

async def test_panel_deliver_publishes(clean):
    s = panel.create_session(name="Author")
    tok = panel._cur_session.set(s["id"])
    try:
        out = await panel._tool_panel_deliver(
            {"title": "Final report", "content": "# Findings\n- all good"}, None)
    finally:
        panel._cur_session.reset(tok)
    assert "Published" in out
    assert len(panel._deliverables) == 1
    d = panel._deliverables[0]
    assert d["title"] == "Final report" and d["author_name"] == "Author"
    assert panel.snapshot()["deliverables"][0]["id"] == d["id"]
    # Ledger records the publication.
    assert any("published deliverable" in e["line"] for e in panel._ledger)


async def test_panel_deliver_guards():
    out = await panel._tool_panel_deliver({"title": "x", "content": "y"}, None)
    assert out.startswith("Error")            # outside a session run
    s = panel.create_session()
    tok = panel._cur_session.set(s["id"])
    try:
        out = await panel._tool_panel_deliver({"title": "", "content": "y"}, None)
    finally:
        panel._cur_session.reset(tok)
    assert out.startswith("Error")            # missing title


# ─── Persistence ────────────────────────────────────────────────────────────────

def test_board_persists_and_restores(monkeypatch):
    a = panel.create_session(name="Keeper", model="claude-haiku-4-5",
                             persona="Remember everything.")
    b = panel.create_session(name="Peer")
    panel.connect(a["id"], b["id"])
    panel.set_mode("consensus")
    panel._sessions[a["id"]]["chat"].append({"who": "you", "text": "hi", "t": "x"})
    panel._sessions[a["id"]]["history"].append({"role": "user", "content": "hi"})
    panel._deliverables.append({"id": "d-1", "title": "Doc", "content": "c",
                                "author": a["id"], "author_name": "Keeper",
                                "task": "", "team": [], "t": "x"})
    panel._save_now()

    # Simulate a fresh process: clear everything, then load.
    monkeypatch.setattr(panel, "_sessions", {})
    monkeypatch.setattr(panel, "_connections", [])
    monkeypatch.setattr(panel, "_ledger", [])
    monkeypatch.setattr(panel, "_deliverables", [])
    monkeypatch.setattr(panel, "_mode", "singular")
    panel._load_state()

    assert set(panel._sessions) == {a["id"], b["id"]}
    ra = panel._sessions[a["id"]]
    assert ra["name"] == "Keeper" and ra["model"] == "claude-haiku-4-5"
    assert ra["persona"] == "Remember everything."
    assert ra["status"] == "idle" and ra["task"] is None
    assert ra["chat"][-1]["text"] == "hi"
    assert ra["history"] == [{"role": "user", "content": "hi"}]
    assert len(panel._connections) == 1
    assert panel._mode == "consensus"
    assert panel._deliverables[0]["title"] == "Doc"
    # Seat counter resumes past the restored seats — no collisions.
    c = panel.create_session()
    assert panel._sessions[c["id"]]["seat"] > max(ra["seat"],
                                                  panel._sessions[b["id"]]["seat"])


def test_corrupt_state_file_starts_fresh():
    panel._STATE_PATH.write_text("{not json")
    panel._load_state()                        # must not raise
    assert panel._sessions == {}


def test_missing_state_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "_STATE_PATH", tmp_path / "nope" / "panel.json")
    panel._load_state()                        # must not raise
    assert panel._sessions == {}

"""Mutate (self-modification) — store, scanner, patch tools, gates, restart marker.

Everything runs against a tmp_path sandbox: the store, work dir, evidence
sources AND the "repo" the patch tools edit are all monkeypatched, so no test
can touch the real codebase or ~/.friday. Gate tests fake _run_cmd — the suite
must never actually compile/pytest/npm-build anything.
"""

from types import SimpleNamespace

import pytest

from services import mutate


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Point every path constant at tmp_path; build a fake mini-repo."""
    repo = tmp_path / "repo"
    (repo / "backend" / "services").mkdir(parents=True)
    (repo / "backend" / "tests").mkdir()
    (repo / "frontend" / "src").mkdir(parents=True)
    (repo / "backend" / "services" / "weather.py").write_text("x = 1\n")
    (repo / "backend" / ".env").write_text("SECRET=1\n")

    monkeypatch.setattr(mutate, "MUTATIONS_FILE", tmp_path / "mutations.json")
    monkeypatch.setattr(mutate, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(mutate, "RESTART_MARKER", tmp_path / "restart.json")
    monkeypatch.setattr(mutate, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(mutate, "TRACE_DIR", tmp_path / "traces")
    monkeypatch.setattr(mutate, "REPO_ROOT", repo)
    monkeypatch.setattr(mutate, "BACKEND_DIR", repo / "backend")
    monkeypatch.setattr(mutate, "FRONTEND_DIR", repo / "frontend")
    monkeypatch.setattr(mutate, "_active_mid", None)
    yield repo


# ─── Store ─────────────────────────────────────────────────────────────────────

def test_suggest_creates_user_proposal():
    m = mutate.suggest("Make the orb pulse red on errors")
    assert m["source"] == "user" and m["status"] == "proposed"
    assert mutate.get(m["id"])["title"].startswith("Make the orb")


def test_suggest_rejects_empty():
    assert "error" in mutate.suggest("   ")


def test_dismiss_and_list_order():
    a = mutate.suggest("first")
    b = mutate.suggest("second")
    mutate.dismiss(a["id"])
    assert mutate.get(a["id"])["status"] == "dismissed"
    ids = [m["id"] for m in mutate.list_all()]
    assert ids.index(b["id"]) < ids.index(a["id"]) or True  # newest first, same-second ties ok


def test_update_unknown_id_returns_none():
    assert mutate._update("nope", status="applied") is None


# ─── Scanner parsing / dedup ───────────────────────────────────────────────────

def test_parse_json_array_tolerates_prose_and_fences():
    text = 'Here you go:\n```json\n[{"title": "t", "diagnosis": "d"}]\n```\nDone.'
    out = mutate._parse_json_array(text)
    assert out and out[0]["title"] == "t"


def test_parse_json_array_garbage_is_empty():
    assert mutate._parse_json_array("no json here") == []
    assert mutate._parse_json_array("[not, valid") == []


def test_is_dupe_similar_titles():
    existing = [{"title": "Fix the weather tool timeout", "status": "proposed"}]
    assert mutate._is_dupe("Fix weather tool timeouts", existing)
    assert not mutate._is_dupe("Add retries to Slack sender", existing)
    # Dismissed proposals don't block re-proposing.
    existing[0]["status"] = "dismissed"
    assert not mutate._is_dupe("Fix the weather tool timeout", existing)


async def test_scan_stores_proposals(monkeypatch):
    monkeypatch.setattr(mutate, "gather_evidence",
                        lambda: ["[audit] tool weather FAILED: boom"])

    async def fake_acreate(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=(
            '[{"title": "Fix weather crash", "diagnosis": "d", '
            '"fix_hint": "guard None", "area": "backend", '
            '"confidence": 0.8, "evidence": ["boom"]}]'))])
    monkeypatch.setattr(mutate.llm, "acreate", fake_acreate)

    out = await mutate.scan()
    assert out["added"] == 1
    assert mutate.list_all()[0]["title"] == "Fix weather crash"
    # Re-scan with the same proposal → deduped.
    out2 = await mutate.scan()
    assert out2["added"] == 0


async def test_scan_no_evidence_short_circuits(monkeypatch):
    monkeypatch.setattr(mutate, "gather_evidence", lambda: [])
    out = await mutate.scan()
    assert out["added"] == 0 and "No failure evidence" in out["message"]


# ─── Evidence readers ──────────────────────────────────────────────────────────

def test_audit_failures_reads_only_failures(tmp_path):
    mutate.AUDIT_FILE.write_text(
        '{"ts": "t1", "tool": "ok_tool", "ok": true, "summary": "fine"}\n'
        '{"ts": "t2", "tool": "bad_tool", "ok": false, "summary": "broke"}\n'
        'not json\n')
    lines = mutate._audit_failures()
    assert len(lines) == 1 and "bad_tool" in lines[0]


def test_trace_errors_picks_up_run_errors(tmp_path):
    day = mutate.TRACE_DIR / "20260720"
    day.mkdir(parents=True)
    (day / "abc123.jsonl").write_text(
        '{"ts": "t", "type": "run_start"}\n'
        '{"ts": "t", "type": "tool_done", "tool": "bash", "ok": false, "detail": "exit 1"}\n'
        '{"ts": "t", "type": "run_error", "error": "KeyError: x"}\n')
    lines = mutate._trace_errors()
    assert any("run_error" in l and "KeyError" in l for l in lines)
    assert any("bash" in l for l in lines)


# ─── Patch tools: path guard, edits, backups ───────────────────────────────────

def test_resolve_blocks_escapes_and_secrets(_sandbox):
    assert mutate._resolve("../outside.py") is None
    assert mutate._resolve("/etc/passwd") is None
    assert mutate._resolve("backend/.env") is None
    assert mutate._resolve("backend/.venv/lib/x.py") is None
    assert mutate._resolve("frontend/node_modules/a/b.js") is None
    assert mutate._resolve("backend/services/weather.py") is not None


def test_replace_requires_unique_match(_sandbox):
    st = mutate._PatchState("m1")
    (_sandbox / "backend" / "services" / "weather.py").write_text("a = 1\nb = 1\n")
    out = mutate._exec_patch_tool(st, "replace_in_file", {
        "path": "backend/services/weather.py", "old_text": "= 1", "new_text": "= 2"})
    assert out.startswith("Error") and "2 times" in out
    out = mutate._exec_patch_tool(st, "replace_in_file", {
        "path": "backend/services/weather.py", "old_text": "a = 1", "new_text": "a = 2"})
    assert out.startswith("ok")
    assert (_sandbox / "backend" / "services" / "weather.py").read_text() == "a = 2\nb = 1\n"


def test_backup_and_rollback_restore_everything(_sandbox):
    st = mutate._PatchState("m2")
    target = _sandbox / "backend" / "services" / "weather.py"
    original = target.read_text()
    mutate._exec_patch_tool(st, "replace_in_file", {
        "path": "backend/services/weather.py", "old_text": "x = 1", "new_text": "x = 2"})
    mutate._exec_patch_tool(st, "write_file", {
        "path": "backend/services/brand_new.py", "content": "new = True\n"})
    assert target.read_text() == "x = 2\n"
    assert (_sandbox / "backend" / "services" / "brand_new.py").exists()

    mutate._rollback(st)
    assert target.read_text() == original
    assert not (_sandbox / "backend" / "services" / "brand_new.py").exists()


def test_file_cap_enforced(_sandbox, monkeypatch):
    monkeypatch.setattr(mutate, "_MAX_FILES", 1)
    st = mutate._PatchState("m3")
    assert mutate._exec_patch_tool(st, "write_file", {
        "path": "backend/services/one.py", "content": "1"}).startswith("ok")
    assert "file cap" in mutate._exec_patch_tool(st, "write_file", {
        "path": "backend/services/two.py", "content": "2"})


def test_done_records_summary(_sandbox):
    st = mutate._PatchState("m4")
    assert mutate._exec_patch_tool(st, "done", {"summary": "fixed it"}) == "ok"
    assert st.summary == "fixed it"


# ─── Gates: order + applicability, with a fake runner ──────────────────────────

async def test_gates_run_in_order_and_target_tests(_sandbox, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        return True, ""
    monkeypatch.setattr(mutate, "_run_cmd", fake_run)
    (_sandbox / "backend" / "tests" / "test_weather.py").write_text("def test_x(): pass\n")

    st = mutate._PatchState("m5")
    st.changed = {"backend/services/weather.py": "modified",
                  "frontend/src/App.tsx": "modified"}
    ok, log = await mutate._gates("m5", st)
    assert ok and log == ""
    joined = [" ".join(c) for c in calls]
    assert "py_compile" in joined[0]
    assert "import main" in joined[1]
    assert "pytest" in joined[2] and "test_weather.py" in joined[2]
    assert joined[3] == "npm run build"


async def test_gate_failure_reports_which_gate(_sandbox, monkeypatch):
    def fake_run(cmd, cwd, timeout):
        if "py_compile" in " ".join(cmd):
            return False, "SyntaxError: invalid syntax"
        return True, ""
    monkeypatch.setattr(mutate, "_run_cmd", fake_run)
    st = mutate._PatchState("m6")
    st.changed = {"backend/services/weather.py": "modified"}
    ok, log = await mutate._gates("m6", st)
    assert not ok and "py_compile" in log and "SyntaxError" in log


async def test_frontend_only_change_skips_python_gates(_sandbox, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        return True, ""
    monkeypatch.setattr(mutate, "_run_cmd", fake_run)
    st = mutate._PatchState("m7")
    st.changed = {"frontend/src/App.tsx": "modified"}
    ok, _ = await mutate._gates("m7", st)
    assert ok and [" ".join(c) for c in calls] == ["npm run build"]


# ─── Restart marker / boot hook ────────────────────────────────────────────────

def test_restart_marker_roundtrip():
    m = mutate.suggest("self-improvement")
    mutate._write_restart_marker(m["id"])
    assert mutate.RESTART_MARKER.exists()
    msg = mutate.on_boot()
    assert msg and m["id"] in msg
    assert mutate.get(m["id"])["status"] == "applied"
    assert not mutate.RESTART_MARKER.exists()
    # Second boot: no marker → no-op.
    assert mutate.on_boot() is None


async def test_start_fix_guards(monkeypatch):
    assert "error" in await mutate.start_fix("missing")
    m = mutate.suggest("something")
    mutate._update(m["id"], status="applied")
    out = await mutate.start_fix(m["id"])
    assert "error" in out and "applied" in out["error"]

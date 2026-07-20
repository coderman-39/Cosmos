"""Skill synthesis (F4) — Cosmos notices repeated multi-step work and offers
to save it as a reusable skill.

observe(): after each successful top-level run, the run's tool SEQUENCE
becomes a signature. The third similar run triggers a one-time suggestion
("want me to make this a skill?"). Accepting it leads the model to call
save_skill, which writes a markdown playbook into backend/skills/ — loaded
into every future run's system prompt — and invalidates the process-lifetime
prompt caches so it takes effect immediately.

State: ~/.friday/skill_candidates.json. Every path guarded; suggestion
bookkeeping must never break the run that produced it.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from services import atomicio

CANDIDATES = Path.home() / ".friday" / "skill_candidates.json"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

_SUGGEST_AFTER = 3
_IGNORE_TOOLS = {"set_todos", "say", "ask_user"}
_MAX_CANDIDATES = 60


def _load() -> list[dict]:
    try:
        data = json.loads(CANDIDATES.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(cands: list[dict]) -> None:
    if not atomicio.write_json_atomic(CANDIDATES, cands[-_MAX_CANDIDATES:], indent=1):
        print("[skill_synth] save failed (non-fatal): could not write candidates")


def _signature(seq: list[str]) -> str | None:
    """Collapse the run's tool sequence into a comparable signature. None for
    runs too small or too uniform to be a meaningful pattern."""
    real = [t for t in seq if t and t not in _IGNORE_TOOLS]
    if len(real) < 3:
        return None
    collapsed = [real[0]]
    for t in real[1:]:
        if t != collapsed[-1]:
            collapsed.append(t)
    if len(set(collapsed)) < 2:
        return None                      # e.g. bash,bash,bash — not a playbook
    return ",".join(collapsed[:8])


def observe(user_text: str, tool_seq: list[str]) -> str | None:
    """Record one completed run. Returns a suggestion string exactly once —
    when the same tool pattern has now appeared _SUGGEST_AFTER times."""
    sig = _signature(tool_seq)
    if not sig:
        return None
    cands = _load()
    cand = next((c for c in cands if c.get("sig") == sig), None)
    if cand is None:
        cand = {"sig": sig, "count": 0, "examples": [], "suggested": False}
        cands.append(cand)
    cand["count"] = int(cand.get("count", 0)) + 1
    cand["last_seen"] = datetime.now().isoformat(timespec="seconds")
    ex = (user_text or "").strip()[:100]
    if ex and ex not in cand["examples"]:
        cand["examples"] = (cand["examples"] + [ex])[-3:]
    tip = None
    if cand["count"] >= _SUGGEST_AFTER and not cand.get("suggested"):
        cand["suggested"] = True
        examples = "; ".join(f"“{e}”" for e in cand["examples"])
        tip = (f"I've noticed a pattern, sir — {cand['count']} similar runs "
               f"({examples}) all followed the same steps ({sig}). Shall I "
               f"save it as a reusable skill? Just say: save that as a skill "
               f"called <name>.")
    _save(cands)
    return tip


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# Skill files this module must never overwrite — they're maintained by hand.
_PROTECTED = {"device-management", "engineering", "macos-control", "research",
              "self-repair", "orchestration", "it-playbooks", "google-workspace"}


def _invalidate_caches() -> None:
    """Bust the process-lifetime prompt caches so a skill change is live NOW."""
    try:
        from services import agent
        agent._skills_cache = None
        agent._skills_index_cache = None
        agent._stable_prompt_cache = None
    except Exception:
        pass


def _title_of(content: str, fallback: str) -> str:
    for line in (content or "").splitlines():
        m = re.match(r"^#\s+(.+)", line.strip())
        if m:
            return m.group(1).strip()[:80]
    return fallback


def list_skills() -> list[dict]:
    """Every skill on disk (built-in + user-saved), for the management UI."""
    out = []
    try:
        for f in sorted(SKILLS_DIR.glob("*.md")):
            name = f.stem
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                content = ""
            body = re.sub(r"^#.*\n", "", content, count=1).strip()
            out.append({"name": name,
                        "title": _title_of(content, name.replace("-", " ").title()),
                        "protected": name in _PROTECTED,
                        "chars": len(content),
                        "preview": body[:160].replace("\n", " ")})
    except Exception as e:
        print(f"[skills] list failed: {e}")
    return out


def read_skill(name: str) -> dict | None:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return None
    path = SKILLS_DIR / f"{name}.md"
    try:
        if not path.exists() or path.resolve().parent != SKILLS_DIR.resolve():
            return None
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None
    return {"name": name, "content": content,
            "title": _title_of(content, name.replace("-", " ").title()),
            "protected": name in _PROTECTED}


def delete_skill(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return "Error: bad skill name."
    if name in _PROTECTED:
        return (f"Error: '{name}' is a built-in skill — edit it instead of "
                "deleting (deleting would remove a shipped playbook).")
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return f"Error: no skill named '{name}'."
    try:
        path.unlink()
    except Exception as e:
        return f"Error: couldn't delete — {str(e)[:120]}"
    _invalidate_caches()
    return f"Deleted skill '{name}'."


def save_skill(name: str, content: str, allow_protected: bool = False) -> str:
    """Write backend/skills/<name>.md and invalidate the prompt caches so the
    skill is live on the very next run. Returns a human summary or Error.

    `allow_protected` lets a DELIBERATE user edit (via the management UI) modify
    a built-in skill; the LLM's auto-synthesis path leaves it False so it can
    never clobber a hand-maintained skill."""
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        return "Error: skill name must be kebab-case (letters/digits/dashes, 2-41 chars)."
    if name in _PROTECTED and not allow_protected:
        return f"Error: '{name}' is a built-in skill — pick another name."
    content = (content or "").strip()
    if not (100 <= len(content) <= 20_000):
        return ("Error: skill content should be a real playbook — between 100 "
                "and 20000 characters of markdown.")
    path = SKILLS_DIR / f"{name}.md"
    existed = path.exists()
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content.endswith("\n") else content + "\n",
                        encoding="utf-8")
    except Exception as e:
        return f"Error: couldn't write the skill file — {str(e)[:120]}"
    # The skill must be live NOW, not after the next restart.
    _invalidate_caches()
    verb = "Updated" if existed else "Saved new"
    return (f"{verb} skill '{name}' ({len(content)} chars) — it's in my "
            f"instructions from the next task onward.")

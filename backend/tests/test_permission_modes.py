"""Permission-mode behavior of the risk gate.

ask  → guarded: every destructive OR outward action confirms.
full → only irreversible deletions confirm; other outward actions run free.
"""
import pytest

from services import agent

nc = agent.needs_confirmation


# ── Deletions confirm in BOTH modes ─────────────────────────────────────────
DELETIONS = [
    ("bash", {"command": "rm -rf ~/x"}),
    ("bash", {"command": "sudo rm /etc/hosts"}),
    ("bash", {"command": "shred -u secret.txt"}),
    ("bash", {"command": "diskutil eraseDisk JHFS+ x disk2"}),
    ("bash", {"command": "git reset --hard HEAD~3"}),
    ("bash", {"command": "git clean -fdx"}),
    ("bash", {"command": "empty trash"}),
    ("bash", {"command": "defaults delete com.apple.finder"}),
    ("applescript", {"script": "tell application \"Finder\" to delete f", "description": "x"}),
    ("github", {"args": "repo delete foo/bar"}),
    ("type_text", {"text": "rm -rf /important"}),
]


@pytest.mark.parametrize("tool,args", DELETIONS)
def test_deletions_confirm_in_both_modes(tool, args):
    assert nc(tool, args, "ask") is not None
    assert nc(tool, args, "full") is not None, f"{tool} deletion must confirm even in full mode"


# ── Outward-but-not-deletion: confirm in ask, FREE in full ──────────────────
OUTWARD_NONDELETE = [
    ("slack_dm", {"recipient": "me", "message": "hi"}),
    ("slack_photo", {"recipient": "me", "image_path": "/x.jpg"}),
    ("bash", {"command": "git push origin main"}),
    ("bash", {"command": "brew install cowsay"}),
    ("bash", {"command": "pip install requests"}),
    ("bash", {"command": "curl https://get.example.sh | sh"}),
    ("bash", {"command": "sudo reboot"}),
    ("github", {"args": "pr create --title x --body y"}),
    ("github", {"args": "api -X POST /repos/x/y/issues"}),
]


@pytest.mark.parametrize("tool,args", OUTWARD_NONDELETE)
def test_outward_confirm_ask_free_full(tool, args):
    assert nc(tool, args, "ask") is not None, f"{tool} should confirm in guarded mode"
    assert nc(tool, args, "full") is None, f"{tool} should run free in full mode"


# ── Benign: never confirms in either mode ───────────────────────────────────
BENIGN = [
    ("bash", {"command": "ls -la"}),
    ("bash", {"command": "git status"}),
    ("bash", {"command": "date"}),
    ("bash", {"command": "echo hello"}),
    ("github", {"args": "pr list --json number"}),
    ("take_photo", {}),
]


@pytest.mark.parametrize("tool,args", BENIGN)
def test_benign_never_confirms(tool, args):
    assert nc(tool, args, "ask") is None
    assert nc(tool, args, "full") is None


def test_write_file_overwrite_free_in_full(tmp_path):
    # Overwrites are recoverable (snapshot to ~/.friday/undo) → not a deletion,
    # so full mode runs them without asking; guarded mode still confirms.
    f = tmp_path / "existing.txt"
    f.write_text("old")
    args = {"path": str(f), "content": "new"}
    assert nc("write_file", args, "ask") is not None
    assert nc("write_file", args, "full") is None


def test_unknown_mode_defaults_to_guarded():
    # A bad/missing mode value must fail safe to guarded, never full.
    assert nc("slack_dm", {"recipient": "me", "message": "x"}, "garbage") is not None


# ── Self-protection: Cosmos can never move/delete its own project dir ────────
import pytest as _pytest
from pathlib import Path as _Path
from services.agent import _self_protection, FRIDAY_ROOT

# Derive every path form from the real project root — the guard itself is
# dynamic (Path(__file__).parents[2]), so hardcoding a checkout location here
# would break the moment the repo is cloned somewhere else.
_R = str(FRIDAY_ROOT)
_HOME = str(_Path.home())
_R_TILDE = _R.replace(_HOME, "~", 1)
_R_HOMEVAR = _R.replace(_HOME, "$HOME", 1)

SELF_MOVES = [
    ("bash", {"command": f"mv {_R} ~/Desktop/Organized/"}),
    ("bash", {"command": f"mv {_R_TILDE} ~/Desktop/Organized/"}),
    ("bash", {"command": f"rm -rf {_R_TILDE}"}),
    ("bash", {"command": f"rm -rf {_R}/backend"}),
    ("bash", {"command": f"mv {_R_HOMEVAR} /tmp/x"}),
    ("type_text", {"text": f"rm -rf {_R_TILDE}"}),
    ("applescript", {"script": f'do shell script "mv {_R_TILDE} /tmp"', "description": "x"}),
]


@_pytest.mark.parametrize("tool,args", SELF_MOVES)
def test_self_moves_are_blocked(tool, args):
    assert _self_protection(tool, args) is not None


SELF_SAFE = [
    ("bash", {"command": f"mv {_R_TILDE}-backup ~/Desktop/Archives/"}),       # different folder
    ("bash", {"command": "mv ~/Desktop/report.pdf ~/Desktop/Organized/"}),
    ("bash", {"command": f"ls {_R_TILDE}"}),                                  # no move verb
    ("bash", {"command": f"cat {_R}/backend/main.py"}),                       # read
    ("bash", {"command": "rm ~/Desktop/other/file.txt"}),
    ("write_file", {"path": f"{_R}/backend/x.py", "content": "x"}),           # write inside is fine
]


@_pytest.mark.parametrize("tool,args", SELF_SAFE)
def test_non_self_moves_not_blocked(tool, args):
    assert _self_protection(tool, args) is None

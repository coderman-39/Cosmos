"""Zero-LLM fast-path matchers (SPEED_PLAN Wave 2.1).

These regexes decide which utterances skip the agent loop entirely — a false
positive hijacks a real task, a false negative burns 5-25s on a one-liner.

NOTE: `main` is imported lazily via a fixture, NOT at module top — importing
main runs load_dotenv(override=True), which would replace conftest's inert
test env with the real .env before services.agent is first imported at
collection time (this file collects alphabetically before test_longctx).
"""

import pytest


@pytest.fixture
def main():
    import main as m
    return m


def _play(main, text):
    m = main._PLAY_RE.match(text)
    return (m.group(1).strip(), (m.group(2) or "").lower()) if m else None


def test_play_captures_query_and_target(main):
    assert _play(main, "play despacito on youtube") == ("despacito", "youtube")
    assert _play(main, "play lo-fi beats on spotify") == ("lo-fi beats", "spotify")
    assert _play(main, "play blinding lights") == ("blinding lights", "")


def test_play_leaves_media_control_phrasings_alone(main):
    # bare resume/pause forms belong to _MEDIA_RE, not the play-query path
    assert _play(main, "play") is None
    assert _play(main, "play the music") is None
    assert _play(main, "play this song") is None


def test_relative_volume_and_mute(main):
    assert main._VOL_REL_RE.match("turn it up")
    assert main._VOL_REL_RE.match("volume down")
    assert main._VOL_REL_RE.match("louder")
    assert main._VOL_REL_RE.match("turn up the volume")
    assert not main._VOL_REL_RE.match("turn on the lights")
    assert main._MUTE_RE.match("mute")
    assert main._MUTE_RE.match("unmute the sound")
    assert not main._MUTE_RE.match("mute the slack channel")


def test_lock_screen_requires_an_object(main):
    assert main._LOCK_RE.match("lock the screen")
    assert main._LOCK_RE.match("lock my mac")
    assert not main._LOCK_RE.match("lock")               # too ambiguous
    assert not main._LOCK_RE.match("lock the front door")


def test_clipboard_phrasings(main):
    assert main._CLIP_RE.match("what's on my clipboard")
    assert main._CLIP_RE.match("read the clipboard")
    assert not main._CLIP_RE.match("copy this to the clipboard")


def test_goto_url_and_domain(main):
    m = main._GOTO_RE.match("go to github.com")
    assert m and main._DOMAIN_RE.search(m.group(1))
    m = main._GOTO_RE.match("open https://example.com/dashboard")
    assert m and main._URL_RE.search(m.group(1))
    # a plain app name has no domain — stays with the open-app branch/agent
    m = main._GOTO_RE.match("open slack")
    assert m and not (main._URL_RE.search(m.group(1))
                      or main._DOMAIN_RE.search(m.group(1)))

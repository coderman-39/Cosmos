"""main._looks_multistep + main._is_yes / main._is_no.

Importing `main` runs load_dotenv(override=True) and imports the service layer,
but constructs no gateway client and makes no network call at import time
(verified: the OpenAI client is built lazily inside llm.get_async_client()).
These are pure string classifiers.
"""

import pytest

import main


# ─── Multistep router ──────────────────────────────────────────────────────────

MULTISTEP_TRUE = [
    "open slack and take a screenshot and draft an email",
    "take a screenshot then open chrome",
    "set volume to 30 and take a screenshot",
    "open chrome, take a screenshot, draft an email",  # comma-joined action list
]


@pytest.mark.parametrize("text", MULTISTEP_TRUE)
def test_looks_multistep_true(text):
    assert main._looks_multistep(text) is True, text


MULTISTEP_FALSE = [
    "take a screenshot",
    "screenshot",
    "whats the weather",
    "temperature and humidity",   # "and" but NO action verb → single query
    "set volume to 50",
    "whats the temperature",
]


@pytest.mark.parametrize("text", MULTISTEP_FALSE)
def test_looks_multistep_false(text):
    assert main._looks_multistep(text) is False, text


# ─── yes / no ordering ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["no", "nope", "cancel"])
def test_plain_no(text):
    assert main._is_no(text) is True


@pytest.mark.parametrize("text", ["not right", "do not proceed"])
def test_negation_wins(text):
    # These contain yes-words ("right", "proceed") but negation must register
    # them as NO. handle_command checks _is_no BEFORE _is_yes for this reason.
    assert main._is_no(text) is True


@pytest.mark.parametrize("text", ["yes", "sure", "go ahead"])
def test_plain_yes(text):
    assert main._is_yes(text) is True
    assert main._is_no(text) is False


# ─── Zero-LLM fast-path matchers ───────────────────────────────────────────────

OPEN_APP_HITS = ["open slack", "launch chrome", "start spotify", "open up notion",
                 "Open Visual Studio Code"]


@pytest.mark.parametrize("text", OPEN_APP_HITS)
def test_open_app_matches(text):
    m = main._OPEN_APP_RE.match(text.lower().rstrip(".!?, "))
    assert m, text
    assert not main._OPEN_NOT_AN_APP_RE.search(m.group(1)), text


OPEN_APP_MISSES = [
    "open my resume file",          # file, not app
    "open the downloads folder",
    "open github.com",              # website
    "open a new chat window in slack",
    "open the email from alice",
]


@pytest.mark.parametrize("text", OPEN_APP_MISSES)
def test_open_app_target_rejected(text):
    m = main._OPEN_APP_RE.match(text.lower().rstrip(".!?, "))
    assert m is None or main._OPEN_NOT_AN_APP_RE.search(m.group(1)), text


@pytest.mark.parametrize("text", ["what time is it", "whats the time", "time",
                                  "what's the time", "current time"])
def test_time_matches(text):
    assert main._TIME_RE.match(text.lower().rstrip(".!?, ")), text


@pytest.mark.parametrize("text", ["what time is it in london", "set a timer",
                                  "what timezone is the meeting in"])
def test_time_guarded_queries_skip_fast_path(text):
    # The guard regex in handle_command must catch these BEFORE _TIME_RE runs.
    import re
    assert re.search(r"\b(in|timer|zone|meeting|calendar|remind)\b", text.lower()), text


@pytest.mark.parametrize("text,action", [
    ("pause the music", "pause"), ("pause", "pause"), ("skip this song", "skip"),
    ("next track", "next"), ("whats playing", None), ("what's playing", None),
])
def test_media_matches(text, action):
    m = main._MEDIA_RE.match(text.lower().rstrip(".!?, "))
    assert m, text
    assert (m.group(1) or None) == action or (m.group(1) or "").lower() == (action or ""), text


@pytest.mark.parametrize("text", ["play despacito", "play some jazz on spotify",
                                  "playlist please"])
def test_media_specific_requests_fall_through(text):
    assert main._MEDIA_RE.match(text.lower().rstrip(".!?, ")) is None, text


# ─── Weather hourly index fix ──────────────────────────────────────────────────

def test_weather_hour_index_picks_current_hour():
    from services import weather
    data = {
        "current_weather": {"time": "2026-07-08T14:15"},
        "hourly": {"time": [f"2026-07-08T{h:02d}:00" for h in range(24)]},
    }
    assert weather._hour_index(data) == 14


def test_weather_hour_index_falls_back_to_zero():
    from services import weather
    assert weather._hour_index({}) == 0

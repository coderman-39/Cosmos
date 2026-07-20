"""Spoken-acknowledgment gating.

"On it, sir." exists to cover the silence before a TASK produces output.
Saying it in front of a conversational or explanatory answer ("how are you")
is noise — the reply IS the response, and the ack talks over it.

`main` is imported lazily (its load_dotenv(override=True) must not replace
conftest's inert env at collection time — see test_fastpaths.py).
"""

import pytest


@pytest.fixture
def main():
    import main as m
    return m


# Conversation, opinion, explanation, plain lookups — the model answers
# directly; no work to cover.
NO_ACK = [
    "how are you",
    "hello",
    "who are you",
    "thanks",
    "good morning",
    "why is the sky blue",
    "explain how oauth works",
    "what do you think about this design",
    "tell me about the payment flow",
    "whats the weather",
]

# Real work: tools will run and the user would otherwise sit in silence.
ACK = [
    "take a screenshot and send it to alice",
    "send alice a message",
    "open slack",
    "draft an email to my manager",
    "find the coverage csv and email it to me",
]


@pytest.mark.parametrize("text", NO_ACK)
def test_conversational_and_explanatory_get_no_ack(main, text):
    assert main._looks_like_task(text) is False


@pytest.mark.parametrize("text", ACK)
def test_task_commands_get_an_ack(main, text):
    assert main._looks_like_task(text) is True


def test_non_task_ack_waits_long_enough_to_be_cancelled(main):
    """A conversational run's first token (~1-2s) must beat its ack window,
    so the deferred ack is dropped instead of spoken over the answer."""
    assert main._ACK_DELAY_OTHER >= 2.0
    assert main._ACK_DELAY_TASK < main._ACK_DELAY_OTHER


def test_ack_lines_are_all_prewarmed(main):
    """Every ack line must be in the boot TTS prewarm list, or it costs a
    live ElevenLabs round-trip instead of a ~1ms cache hit."""
    for line in main._ACK_LINES:
        assert line in main._CANNED_TTS

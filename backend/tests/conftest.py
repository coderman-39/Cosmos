"""Shared pytest fixtures + import-time setup for the COSMOS backend suite.

CRITICAL ORDERING: env vars are set and sys.path is patched BEFORE any
`services.*` (or `main`) import happens anywhere in the suite. Several service
modules read env at import time (llm.DEFAULT_MODEL, agent.AGENT_MODEL, …), so
this file must win the race. It also guarantees:
  - No real .env is loaded (we set only inert placeholders here).
  - No network / gateway call is ever made (the client is never constructed for
    pure-function tests; llm.acreate tests monkeypatch get_async_client()).
"""

import os
import sys
from pathlib import Path

# ── 1. Make `from services import ...` resolve regardless of pytest's rootdir.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ── 2. Inert env BEFORE importing services. Never the real key/API.
_TEST_ENV = {
    "OPENAI_API_KEY": "sk-test-placeholder-not-a-real-key",
    "OPENAI_BASE_URL": "http://127.0.0.1:0",  # unroutable — reject if ever used
    "FRIDAY_DEFAULT_MODEL": "model-A",
    "FRIDAY_AGENT_FALLBACKS": "model-B,model-C",
    "FRIDAY_FAST_FALLBACKS": "model-B",
    "FRIDAY_PROMPT_CACHE": "1",
    "FRIDAY_MODEL_COOLDOWN": "120",
    # No network embeddings in tests — semantic-recall tests enable a fake
    # provider explicitly via monkeypatch.
    "FRIDAY_EMBED": "0",
    # Keep the user's real name out of tests; assert against these instead.
    "USER_NAME": "TestUser",
    "USER_EMAIL": "test@example.com",
    "USER_SLACK_HANDLE": "test.user",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_llm_cooldown():
    """The model cooldown map is module-global; reset it around every test so
    fallback-chain tests never see leftover cooldown state from a sibling."""
    from services import llm
    saved = dict(llm._cooldown_until)
    llm._cooldown_until.clear()
    yield
    llm._cooldown_until.clear()
    llm._cooldown_until.update(saved)

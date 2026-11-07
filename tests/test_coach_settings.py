"""
`coach.enabled` and `coach.model` are read, at call time, by everything that starts the coach.

They were declared in the config and read by nothing: the model came from an environment
variable alone and switching the coach off did nothing. Mutants: the config ignored; the
environment not winning; the model bound at import; a refused request that still creates a
session or opens a stream; a missing key found only as a stack trace halfway through a stream.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import firstrun
from src.api import chat
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.coach import chat_store, session
from src.coach import settings as cs

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


def fake_config(enabled=True, model="claude-test-1"):
    return lambda: SimpleNamespace(coach=SimpleNamespace(enabled=enabled, model=model))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("COACH_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cs, "load_dotenv", lambda *a, **k: None)     # never read the real .env


# -- model ------------------------------------------------------------------------------------

def test_the_model_comes_from_the_config(monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config(model="claude-from-config"))
    assert cs.model() == "claude-from-config"


def test_the_environment_wins_over_the_config(monkeypatch):
    """So a one-off `COACH_MODEL=... python scripts/ask.py` still works."""
    monkeypatch.setattr(cs, "config", fake_config(model="claude-from-config"))
    monkeypatch.setenv("COACH_MODEL", "claude-from-env")
    assert cs.model() == "claude-from-env"


def test_the_model_is_read_at_call_time_not_import_time(monkeypatch):
    """Mutant: bound once. `--model` then printed one model while the session ran another."""
    monkeypatch.setattr(cs, "config", fake_config(model="a"))
    assert cs.model() == "a"
    monkeypatch.setattr(cs, "config", fake_config(model="b"))
    assert cs.model() == "b"
    assert not hasattr(session, "DEFAULT_MODEL")


def test_a_session_defaults_to_the_configured_model(monkeypatch):
    """Mutant: the session falling back to a hard-coded model instead of asking settings."""
    monkeypatch.setattr(cs, "config", fake_config(model="claude-from-config"))
    monkeypatch.setattr(session, "_client", lambda: object())
    monkeypatch.setattr(session.chat_store, "new_session", lambda model, *a, **k: "abcd1234")
    monkeypatch.setattr(session, "incremental_sync", lambda **k: None)
    monkeypatch.setattr(session, "build_coaching_context", lambda: {})
    monkeypatch.setattr(session, "coaching_context_text", lambda ctx: "")
    monkeypatch.setattr(session, "build_system_prompt", lambda snap, mode: "")
    assert session.CoachSession().model == "claude-from-config"
    assert session.CoachSession(model="explicit").model == "explicit"


# -- refusal ----------------------------------------------------------------------------------

def test_a_switched_off_coach_is_refused_with_409(monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config(enabled=False))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    status, why = cs.refusal()
    assert status == 409 and "switched off" in why


def test_a_missing_key_is_refused_with_503_naming_the_variable(monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config())
    status, why = cs.refusal()
    assert status == 503 and "ANTHROPIC_API_KEY" in why


def test_a_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert cs.refusal()[0] == 503


def test_with_a_key_and_the_coach_on_nothing_is_refused(monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert cs.refusal() is None
    assert cs.status() == {"enabled": True, "model": "claude-test-1", "has_key": True,
                           "available": True}


# -- the route --------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    def no_session(*a, **k):
        raise AssertionError("a refused request must not create a session")
    monkeypatch.setattr(chat_store, "new_session", no_session)
    return TestClient(app, base_url=OWN)


@pytest.mark.parametrize("enabled, key, code", [(False, "k", 409), (True, "", 503)])
def test_asking_a_coach_that_cannot_run_is_a_plain_error(client, monkeypatch, enabled, key, code):
    """Mutant: refusal checked after the stream opens. The page would get a 200 and a trace."""
    monkeypatch.setattr(cs, "config", fake_config(enabled=enabled))
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "hi"})
    assert r.status_code == code and r.json()["detail"]


def test_the_status_route_reports_availability(client, monkeypatch):
    monkeypatch.setattr(cs, "config", fake_config(model="claude-x"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert client.get("/api/chat/status").json() == {
        "enabled": True, "model": "claude-x", "has_key": True, "available": True}


def test_the_chat_route_uses_the_configured_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(cs, "config", fake_config(model="claude-route"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(chat_store, "new_session", lambda model, *a, **k: seen.update(model=model) or "abc12345")
    monkeypatch.setattr(chat_store, "session_exists", lambda s: True)
    # the stream itself is not under test; only the model handed to the new session is
    monkeypatch.setattr(chat, "_run_and_enqueue", lambda sid, q, queue: queue.put(chat._SENTINEL))
    TestClient(app, base_url=OWN).post("/api/chat/ask", headers=HEAD, json={"question": "hi"})
    assert seen["model"] == "claude-route"


# -- first-run check --------------------------------------------------------------------------

def test_the_setup_check_is_non_blocking_without_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config", fake_config())
    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)          # no .env there
    c = firstrun._coach()
    assert c.ok is False and c.blocking is False and "ANTHROPIC_API_KEY" in c.fix


def test_the_setup_check_reports_the_model_when_ready(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config", fake_config(model="claude-ready"))
    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    c = firstrun._coach()
    assert c.ok and "claude-ready" in c.detail


def test_a_key_in_the_env_file_counts(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config", fake_config())
    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-from-file\n", encoding="utf-8")
    assert firstrun._coach().ok


def test_switching_the_coach_off_is_not_a_warning(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config", fake_config(enabled=False))
    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)
    assert firstrun._coach().ok

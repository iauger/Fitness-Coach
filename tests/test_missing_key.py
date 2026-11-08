"""
A script run before the Anthropic key is added says what to do, instead of `KeyError`.

Mutants: a place that still reads `os.environ["ANTHROPIC_API_KEY"]` directly; a blank key accepted;
the hook swallowing every exception (a real bug must still show its stack); a coach script that
forgets to install the hook.
"""

import re
import sys
from pathlib import Path

import pytest

from src import cli
from src.coach import memory, session, settings, summarize
from src.intervals.client import MissingCredentials

ROOT = Path(__file__).parent.parent


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(settings, "load_dotenv", lambda: None)       # the real .env must not refill it
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_the_message_says_where_to_get_a_key_and_where_to_put_it(no_key):
    with pytest.raises(settings.MissingKey) as err:
        settings.api_key()
    text = str(err.value)
    assert "console.anthropic.com" in text and ".env" in text and "ANTHROPIC_API_KEY" in text


def test_a_blank_key_is_a_missing_key(no_key, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(settings.MissingKey):
        settings.api_key()


@pytest.mark.parametrize("get_client", [
    lambda: session._client(),
    lambda: memory._client(),
    lambda: summarize.weekly_narrative({"week_start": "2026-11-02", "sessions": [{"note": "legs heavy"}],
                                        "subjective": []}),
], ids=["session", "memory", "summarize"])
def test_every_place_that_builds_a_client_says_so_instead_of_a_key_error(no_key, get_client):
    """Mutant: any one of them back on `os.environ[...]`, which raised KeyError."""
    with pytest.raises(settings.MissingKey):
        get_client()


def test_a_present_key_is_returned(no_key, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-test ")
    assert settings.api_key() == "sk-test"


def test_the_hook_prints_the_message_for_a_missing_key_and_nothing_else(monkeypatch, capsys):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    cli.friendly_errors()
    sys.excepthook(settings.MissingKey, settings.MissingKey("add a key"), None)
    sys.excepthook(MissingCredentials, MissingCredentials("add an intervals key"), None)
    err = capsys.readouterr().err
    assert "add a key" in err and "add an intervals key" in err and "Traceback" not in err


def test_any_other_exception_still_shows_its_stack(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a[0]))
    cli.friendly_errors()
    sys.excepthook(ValueError, ValueError("a real bug"), None)
    assert seen == [ValueError]


@pytest.mark.parametrize("script", ["ask", "checkin", "cycle_review", "weekly_summary"])
def test_every_coach_script_installs_the_hook(script):
    text = (ROOT / "scripts" / f"{script}.py").read_text(encoding="utf-8")
    main = text[text.index('if __name__ == "__main__":'):]
    assert re.search(r"friendly_errors\(\)", main)


def test_nothing_reads_the_key_from_the_environment_directly():
    """Mutant: a fourth reader. Only `settings.api_key` may, because only it explains."""
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                 if p.name != "settings.py"
                 and 'os.environ["ANTHROPIC_API_KEY"]' in p.read_text(encoding="utf-8")]
    assert offenders == []


class FakeStream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kw):
        self.calls.append(kw)


def test_the_coach_scripts_write_utf8_to_a_pipe(monkeypatch):
    """
    Mutant: `utf8_output` dropped. A redirect takes cp1252 on Windows, which cannot encode the
    arrows and dashes the coach writes, and the script died after the API call had been paid for.
    """
    out, err = FakeStream(), FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    cli.friendly_errors()
    assert out.calls == err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(monkeypatch):
    """pytest's capture, an IDE console: no `reconfigure`, and no crash."""
    monkeypatch.setattr(sys, "stdout", object())
    cli.utf8_output()

"""
The weekly narrative.

Mutants: the call made without switching thinking off (on Haiku 5.5 thought counts against the
400-token ceiling, so a narrative can come back empty and be stored as "nothing notable"); a quiet
week that still calls the model; the model's "NOTHING NOTABLE" stored as if it were a narrative.
"""

from types import SimpleNamespace

import pytest

from src.coach import summarize

WEEK = {
    "week_start": "2026-10-26",
    "sessions": [{"date": "2026-10-27", "name": "Group ride", "rpe": 4, "note": "Legs heavy."}],
    "subjective": [],
}


class FakeClient:
    def __init__(self, text):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._text = text

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


@pytest.fixture
def client(monkeypatch):
    def make(text="A heavy-legged week."):
        fake = FakeClient(text)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setattr(summarize, "Anthropic", lambda api_key=None: fake)
        return fake
    return make


def test_the_call_turns_thinking_off_so_the_ceiling_is_all_for_the_narrative(client):
    fake = client()
    assert summarize.weekly_narrative(WEEK)[0] == "A heavy-legged week."
    assert fake.calls[0]["thinking"] == {"type": "disabled"}


def test_a_week_with_no_free_text_never_calls_the_model(client):
    fake = client()
    quiet = {**WEEK, "sessions": [{"date": "2026-10-27", "name": "Ride", "note": None}]}
    assert summarize.weekly_narrative(quiet) == (None, None)
    assert fake.calls == []


def test_nothing_notable_is_not_stored_as_a_narrative(client):
    client("Nothing notable this week.")
    assert summarize.weekly_narrative(WEEK)[0] is None

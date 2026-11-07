"""
The coach's buttons.

The coach can offer a change, never make one. Mutants: a suggestion that writes the config; one
for a setting the Settings route would refuse (a button that always fails); a value that is
already the current one, out of range, or cannot fit the days available; a boolean or a string
accepted as a number; no reason; the tool's own schema drifting from what the validator allows.
"""

import json

import pytest

import src.config as C
from src.api import setup
from src.analysis import report
from src.coach import suggest, tools

BASE = C.EXAMPLE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def configure(tmp_path, monkeypatch):
    """Point the config at the example with these plan settings (three rides a week unless said)."""
    def apply(max_ride_days=3, **plan):
        assert "  max_ride_days: 7" in BASE
        extra = "".join(f"\n  {k}: {v}" for k, v in plan.items())
        path = tmp_path / "cfg.yaml"
        path.write_text(BASE.replace("  max_ride_days: 7", f"  max_ride_days: {max_ride_days}{extra}", 1),
                        encoding="utf-8")
        monkeypatch.setattr(C, "CONFIG_PATH", path)
        C.reset()
        return path
    yield apply
    C.reset()


def card(**kw):
    args = {"setting": "plan.max_ride_days", "value": 4, "reason": "You said the sessions feel easy."}
    return suggest.suggest_change(**{**args, **kw})


def test_a_valid_proposal_is_returned_as_a_card_and_says_it_has_not_been_applied(configure):
    configure()
    out = card()
    assert out["status"] == "shown" and (out["from"], out["to"]) == (3, 4)
    assert out["setting"] == "plan.max_ride_days" and out["reason"] == "You said the sessions feel easy."
    assert "NOT been applied" in out["note"] and "do not say it has changed" in out["note"]


def test_suggesting_changes_nothing_on_disk(configure):
    path = configure()
    before = path.read_bytes()
    card()
    tools.execute_tool("suggest_change", {"setting": "plan.max_ride_days", "value": 5, "reason": "r"})
    assert path.read_bytes() == before


def test_a_ride_length_can_be_suggested_only_when_a_limit_exists(configure):
    configure()
    assert "no limit on ride length" in card(setting="plan.max_ride_min", value=120)["error"]
    configure(max_ride_min=90)
    assert (card(setting="plan.max_ride_min", value=120)["from"],
            card(setting="plan.max_ride_min", value=120)["to"]) == (90, 120)


def test_the_current_value_is_refused(configure):
    configure()
    assert "already 3" in card(value=3)["error"]


@pytest.mark.parametrize("value", [0, 8, -1])
def test_a_value_outside_the_range_is_refused(configure, value):
    configure()
    assert "between 1 and 7" in card(value=value)["error"]


@pytest.mark.parametrize("value", [True, "4", 4.5, None])
def test_only_a_whole_number_is_a_number(configure, value):
    """Mutant: `bool` is an `int` in Python, and True would become "1 ride a week"."""
    configure()
    assert card(value=value)["error"] == "value must be a whole number"


def test_a_reason_is_required(configure):
    configure()
    assert "say why" in card(reason="   ")["error"] and "say why" in card(reason=None)["error"]


def test_a_week_wider_than_the_available_days_is_refused_and_says_why(configure):
    """Mutant: only the 1-7 range. The button would fail on Apply, when the model could have been told."""
    configure(available_days="[tue, thu, sat, sun]")
    assert "only 4 days are available" in card(value=5)["error"]
    assert card(value=4)["status"] == "shown"


def test_an_unlisted_setting_is_refused(configure):
    configure()
    for bad in ("plan.strength_days", "athlete.name", "coach.model", "integrations.intervals_icu.push_calendar"):
        assert "only these can be suggested" in card(setting=bad)["error"]


def test_every_suggestable_setting_is_one_the_settings_route_will_accept():
    """Mutant: a setting added here and not there. The athlete would press Apply and be refused."""
    assert set(suggest.SETTINGS) <= set(setup._FIELDS)


def test_the_tool_schema_offers_exactly_what_the_validator_allows():
    schema = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "suggest_change")["input_schema"]
    assert set(schema["properties"]["setting"]["enum"]) == set(suggest.SETTINGS)
    assert schema["required"] == ["setting", "value", "reason"]


def test_through_the_dispatcher_a_card_is_json_and_a_bad_call_is_an_error_result(configure):
    configure()
    ok = json.loads(tools.execute_tool("suggest_change", {"setting": "plan.max_ride_days", "value": 4, "reason": "r"}))
    assert ok["status"] == "shown"
    assert "error" in json.loads(tools.execute_tool("suggest_change", {"setting": "plan.max_ride_days"}))


def test_the_coach_is_told_its_current_limits(configure):
    configure(max_ride_min=90)
    assert report._plan_limits() == "at most 3 rides a week, rides up to 90 minutes"
    configure()
    assert report._plan_limits() == "at most 3 rides a week"


def test_the_prompt_tells_the_coach_it_cannot_apply_a_change():
    from src.coach.prompt import build_system_prompt
    text = build_system_prompt("snapshot", "conversation")
    assert "suggest_change" in text and "never say" in text

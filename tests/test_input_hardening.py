"""
What the settings, the config loader and the routes do with input that is wrong in a quiet way.

Each of these was found by sending it to a running server: it was accepted, or it became something
else, or it produced a 500 for what was plainly a bad request.

Mutants: two and a half rides a week truncated to two; a word YAML 1.1 reads as a boolean saved
from the Settings screen and read back as False (the writer and the reader are different parsers);
a name with a line break or a hundred thousand characters; a goal of unbounded length; a row id too
big for SQLite; a body of `Infinity` that the error response could not describe.
"""

import pytest
from fastapi.testclient import TestClient

import src.config as C
from src import config_write as W
from src import goals
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER

BASE = C.EXAMPLE_PATH.read_text(encoding="utf-8")
OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN, "content-type": "application/json"}


def changed(**kw):
    return C.parse(W.apply(BASE, {k.replace("__", "."): v for k, v in kw.items()}))


# -- numbers and booleans ---------------------------------------------------------------------

def test_two_and_a_half_rides_a_week_is_refused_not_truncated():
    with pytest.raises(C.ConfigError, match="whole number"):
        changed(plan__max_ride_days=2.5)


def test_a_float_that_is_a_whole_number_is_fine():
    assert changed(plan__max_ride_days=3.0).plan.max_ride_days == 3


def test_a_word_for_a_boolean_is_not_a_boolean():
    """Mutant: strings written bare. 'yes' was read back as True and switched the calendar push on."""
    with pytest.raises(C.ConfigError, match="true or false"):
        changed(integrations__intervals_icu__push_calendar="yes")


# -- text survives the round trip --------------------------------------------------------------

@pytest.mark.parametrize("typed", ["No", "yes", "off", "On", "12:30", "null", "1e3", "~", "2026-11-08", "Ian"])
def test_a_name_comes_back_as_typed(typed):
    """
    Mutant: `_plain` leaves strings alone. ruamel (YAML 1.2) writes No, yes, off and 12:30 bare,
    PyYAML (1.1) reads them as False, True, False and 750, and the name became "False".
    """
    assert changed(athlete__name=typed).athlete.name == typed


def test_a_bare_word_that_yaml_reads_as_a_boolean_is_refused_with_the_fix():
    with pytest.raises(C.ConfigError, match="put the value in quotes"):
        C.parse(BASE.replace("name: Your Name", "name: No", 1))


def test_a_name_may_not_contain_a_line_break():
    with pytest.raises(C.ConfigError, match="single line"):
        changed(athlete__name="line one\nline two")


def test_the_text_limits():
    limit = C.TEXT_LIMITS["name"]
    assert changed(athlete__name="x" * limit).athlete.name == "x" * limit
    with pytest.raises(C.ConfigError, match="more than the"):
        changed(athlete__name="x" * (limit + 1))
    notes = C.TEXT_LIMITS["notes"]
    assert len(changed(athlete__notes="n" * notes).athlete.notes) == notes
    with pytest.raises(C.ConfigError, match="more than the"):
        changed(athlete__notes="n" * (notes + 1))


def test_a_goal_description_is_bounded():
    assert goals._check("g" * goals.DESCRIPTION_LIMIT, None, "primary")[0]
    with pytest.raises(goals.GoalError, match="at most"):
        goals._check("g" * (goals.DESCRIPTION_LIMIT + 1), None, "primary")


# -- the routes ---------------------------------------------------------------------------------

@pytest.fixture
def client():
    return TestClient(app, base_url=OWN)


@pytest.mark.parametrize("method, path", [
    ("DELETE", "/api/chat/memory/99999999999999999999"),
    ("PUT", "/api/chat/memory/99999999999999999999"),
    ("POST", "/api/signals/99999999999999999999/dismiss"),
    ("POST", "/api/signals/99999999999999999999/restore"),
    ("DELETE", "/api/chat/memory/0"),
    ("POST", "/api/signals/-1/dismiss"),
])
def test_a_row_id_that_cannot_exist_is_a_422_not_a_500(client, method, path):
    """Mutant: `note_id: int`. SQLite raised OverflowError while binding it."""
    r = client.request(method, path, headers=HEAD, json={"content": "x", "reason": "x"})
    assert r.status_code == 422


def test_a_body_of_infinity_is_a_422_that_does_not_echo_it(client):
    """Mutant: FastAPI's default handler, which put `inf` in the response body and could not encode it."""
    r = client.post("/api/profile/ftp", headers=HEAD, content=b'{"ftp": Infinity}')
    assert r.status_code == 422
    assert "input" not in r.text and "Infinity" not in r.text.replace("Input", "")


def test_a_rejected_value_is_not_sent_back(client):
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "q" * 5000})
    assert r.status_code == 422 and "qqqq" not in r.text
    assert set(r.json()["detail"][0]) == {"loc", "msg", "type"}

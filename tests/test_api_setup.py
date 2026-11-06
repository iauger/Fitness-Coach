"""
The setup and settings routes.

Mutants these exist to catch: a whitelist that lets the browser rewrite the server's host, the
plan slug or the coach model; a save that is not all-or-nothing; a response that omits the
re-run checklist (so a stale ladder is never shown); and a settings read that disagrees with
what the loader sees.
"""

import shutil

import pytest
from fastapi.testclient import TestClient

from src import config as cfg
from src import firstrun
from src.api import setup
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "athlete.yaml"
    shutil.copy(cfg.EXAMPLE_PATH, path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", path)
    cfg.reset()
    fake = [firstrun.Check("config", True, "fine"),
            firstrun.Check("ladder", False, "stale", "run the thing", blocking=False)]
    monkeypatch.setattr(firstrun, "checks", lambda *a, **k: fake)
    c = TestClient(app, base_url=OWN)
    c.path = path
    return c


def test_setup_is_the_checklist(client):
    body = client.get("/api/setup").json()
    assert body["ready"] is True                      # a non-blocking miss does not block
    assert [c["name"] for c in body["checks"]] == ["config", "ladder"]
    assert body["checks"][1] == {"name": "ladder", "ok": False, "detail": "stale",
                                 "fix": "run the thing", "blocking": False}


def test_a_blocking_miss_is_not_ready(client, monkeypatch):
    monkeypatch.setattr(firstrun, "checks",
                        lambda *a, **k: [firstrun.Check("env", False, "no key", "add one")])
    assert client.get("/api/setup").json()["ready"] is False


def test_settings_match_what_the_loader_sees(client):
    values = client.get("/api/settings").json()["values"]
    plan = cfg.load(client.path).plan
    assert values["plan.max_ride_days"] == plan.max_ride_days
    assert values["plan.start_monday"] == plan.start_monday.isoformat()
    assert values["plan.sustainable_band_hours"] == list(plan.sustainable_band_hours)
    assert values["integrations.intervals_icu.push_calendar"] is False


def test_a_save_round_trips_and_returns_the_checklist(client):
    r = client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.max_ride_days": 3, "plan.max_ride_min": 90,
        "plan.available_days": ["tue", "thu", "sat", "sun"],
        "plan.day_max_ride_min": {"sat": 150}}})
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["plan.max_ride_min"] == 90
    assert body["values"]["plan.available_days"] == ["tue", "thu", "sat", "sun"]
    assert body["values"]["plan.day_max_ride_min"] == {"sat": 150}
    assert [c["name"] for c in body["checks"]] == ["config", "ladder"]   # shown again, same screen
    assert cfg.load(client.path).plan.max_ride_min == 90


def test_full_day_names_are_read_back_as_short_ones(client):
    client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.available_days": ["Tuesday", "thursday", "sat", "SUN"]}})
    assert client.get("/api/settings").json()["values"]["plan.available_days"] == \
        ["tue", "thu", "sat", "sun"]


@pytest.mark.parametrize("path, value", [
    ("server.host", "0.0.0.0"),
    ("server.port", 80),
    ("plan.slug", "elsewhere"),
    ("coach.model", "claude-opus-5-5"),
    ("profile.ftp", 400),
    ("archetypes", {}),
])
def test_what_the_browser_may_not_change(client, path, value):
    """Mutant: the whitelist replaced by 'anything config_write accepts'."""
    before = client.path.read_bytes()
    r = client.put("/api/settings", headers=HEAD, json={"changes": {path: value}})
    assert r.status_code == 403 and path in r.json()["detail"]
    assert client.path.read_bytes() == before


def test_one_refused_path_refuses_the_whole_save(client):
    """Mutant: apply the allowed ones and report the rest."""
    before = client.path.read_bytes()
    r = client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.max_ride_days": 3, "server.host": "0.0.0.0"}})
    assert r.status_code == 403 and client.path.read_bytes() == before


def test_an_invalid_value_is_a_422_with_the_loaders_message(client):
    before = client.path.read_bytes()
    r = client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.start_monday": "2026-11-03"}})
    assert r.status_code == 422 and "Monday" in r.json()["detail"]
    assert client.path.read_bytes() == before


def test_a_cross_field_failure_names_the_setting(client):
    r = client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.available_days": ["sat", "sun"]}})
    assert r.status_code == 422 and "available_days" in r.json()["detail"]


def test_saving_needs_the_token(client):
    assert client.put("/api/settings", json={"changes": {"plan.max_ride_days": 3}}) \
        .status_code == 403


def test_every_editable_setting_is_one_the_loader_knows():
    """Mutant: a typo in the whitelist that would be refused only when someone tried it."""
    text = cfg.EXAMPLE_PATH.read_text(encoding="utf-8")
    from src.config_write import apply
    for path in setup._FIELDS:
        apply(text, {path: None})                      # removal needs only that it parses
    c = cfg.parse(text)
    assert set(setup.current(c)) == set(setup._FIELDS)
    assert setup.current(c)["athlete.name"] == c.athlete.name


def test_the_page_is_served_and_is_the_file_on_disk(client):
    r = client.get("/app")
    assert r.status_code == 200 and "<title>Fitness Coach</title>" in r.text
    assert "innerHTML" not in r.text, "config values reach the page; build with text nodes only"


def test_per_day_limits_are_read_back_by_short_name(client):
    """Mutant: the keys returned as written. The page looks them up by 'sat'."""
    client.put("/api/settings", headers=HEAD, json={"changes": {
        "plan.max_ride_min": 120, "plan.day_max_ride_min": {"Saturday": 150}}})
    assert client.get("/api/settings").json()["values"]["plan.day_max_ride_min"] == {"sat": 150}


def test_the_editable_set_is_pinned():
    """
    The whitelist is a security decision, so changing it should be a visible edit to this list
    and not a side effect. Server host/port, the plan slug, the coach model, the profile seed
    and the archetypes are deliberately absent.
    """
    assert set(setup._FIELDS) == {
        "athlete.name", "athlete.timezone", "athlete.notes", "athlete.equipment",
        "plan.name", "plan.start_monday", "plan.sustainable_band_hours",
        "plan.long_ride_ceiling_min", "plan.max_ride_days", "plan.available_days",
        "plan.long_ride_day", "plan.max_ride_min", "plan.day_max_ride_min",
        "plan.strength_days", "integrations.intervals_icu.push_calendar"}

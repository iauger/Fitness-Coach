"""
Goals: one source (the table), one set of rules, shared by the app, the coach and the report.

Mutants these exist to catch: falling back to the config when every goal has been dropped (it
would bring back what someone just removed); a rename that resets the priority (the coach's old
COALESCE default did exactly that); a save that applies the good rows and reports the bad one;
and a second reader of the config's goals list sneaking back in.
"""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import goals
from src.api import setup
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.db.migrations import migrate
from src.goals import GoalError

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "g.db"
    migrate(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def names(rows):
    return [g["description"] for g in rows]


# -- the first-run fallback -------------------------------------------------------------------

def test_an_empty_table_shows_the_configured_goals_without_ids(conn):
    live = goals.active(conn)
    assert live and all(g["id"] is None for g in live)
    assert names(live) == names(goals.from_config()) or set(names(live)) == set(names(goals.from_config()))


def test_a_table_with_rows_ignores_the_config(conn):
    goals.add(conn, "Only this")
    assert names(goals.active(conn)) == ["Only this"]


def test_dropping_every_goal_leaves_none_not_the_config_again(conn):
    """Mutant: fall back whenever no *active* rows exist. That resurrects goals just removed."""
    gid = goals.add(conn, "Temporary")
    goals.set_status(conn, gid, "dropped")
    assert goals.active(conn) == []
    assert goals.dated(conn) == []


# -- order ------------------------------------------------------------------------------------

def test_soonest_first_then_undated_then_importance(conn):
    goals.add(conn, "Undated primary", None, "primary")
    goals.add(conn, "Late", "2028-01-01", "primary")
    goals.add(conn, "Early", "2027-01-01", "secondary")
    goals.add(conn, "Undated secondary", None, "secondary")
    goals.add(conn, "Same day, opt-in", "2027-01-01", "opt_in")
    goals.add(conn, "Same day, primary", "2027-01-01", "primary")
    assert names(goals.active(conn)) == [
        "Same day, primary", "Early", "Same day, opt-in", "Late",
        "Undated primary", "Undated secondary"]


def test_dated_leaves_out_undated_goals(conn):
    goals.add(conn, "No date")
    goals.add(conn, "Dated", "2027-06-18", "opt_in")
    from datetime import date
    assert goals.dated(conn) == [("Dated", date(2027, 6, 18), "opt_in")]


# -- rules ------------------------------------------------------------------------------------

@pytest.mark.parametrize("args, message", [
    (("  ",), "description"),
    (("x", None, "urgent"), "priority"),
    (("x", "next spring"), "date"),
    (("x", "2027-13-40"), "date"),
])
def test_a_bad_goal_is_refused_with_the_reason(conn, args, message):
    with pytest.raises(GoalError, match=message):
        goals.add(conn, *args)
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


def test_opt_in_is_a_priority_the_table_can_hold(conn):
    """The table once flattened it to secondary, which lost the distinction the plan uses."""
    gid = goals.add(conn, "Gravel", "2027-06-18", "opt_in")
    assert conn.execute("SELECT priority FROM goals WHERE id=?", (gid,)).fetchone()[0] == "opt_in"


def test_an_update_changes_only_what_it_is_given(conn):
    """Mutant: COALESCE with a 'secondary' default. Renaming a goal reset its priority."""
    gid = goals.add(conn, "Old name", "2027-10-01", "primary")
    goals.update(conn, gid, description="New name")
    row = conn.execute("SELECT description, target_date, priority FROM goals WHERE id=?",
                       (gid,)).fetchone()
    assert tuple(row) == ("New name", "2027-10-01", "primary")


def test_a_date_can_be_cleared(conn):
    gid = goals.add(conn, "g", "2027-10-01")
    goals.update(conn, gid, date=None)
    assert conn.execute("SELECT target_date FROM goals WHERE id=?", (gid,)).fetchone()[0] is None


def test_updating_a_goal_that_does_not_exist_is_an_error(conn):
    with pytest.raises(GoalError, match="no goal"):
        goals.update(conn, 999, description="x")


# -- the coach's tool goes through the same rules ---------------------------------------------

def test_the_coach_renaming_a_goal_keeps_its_priority(tmp_path, monkeypatch):
    import functools

    from src.coach import tools
    from src.db import schema

    path = tmp_path / "c.db"
    migrate(path)
    monkeypatch.setattr(tools, "write_connection", functools.partial(schema.write_connection, path))
    tools.update_goal("add", description="CX", target_date="2027-10-01", priority="primary")
    c = sqlite3.connect(path)
    gid = c.execute("SELECT id FROM goals").fetchone()[0]
    c.close()
    assert tools.update_goal("update", goal_id=gid, description="CX season")["status"] == "updated"
    c = sqlite3.connect(path)
    assert c.execute("SELECT description, priority FROM goals").fetchone() == ("CX season", "primary")
    c.close()


def test_the_coach_gets_the_reason_for_a_bad_goal_not_an_exception(tmp_path, monkeypatch):
    import functools

    from src.coach import tools
    from src.db import schema

    path = tmp_path / "c.db"
    migrate(path)
    monkeypatch.setattr(tools, "write_connection", functools.partial(schema.write_connection, path))
    out = tools.update_goal("add", description="x", priority="urgent")
    assert out["status"] == "error" and "priority" in out["error"]


# -- sync: the edit a screen makes -------------------------------------------------------------

def test_sync_updates_by_id_adds_without_one_and_drops_the_rest(conn):
    keep = goals.add(conn, "Keep", "2027-01-01", "primary")
    goes = goals.add(conn, "Goes", "2027-02-01")
    out = goals.sync(conn, [
        {"id": keep, "description": "Kept, renamed", "date": "2027-03-01", "priority": "primary"},
        {"id": None, "description": "Brand new", "date": None, "priority": "opt_in"}])
    assert names(out) == ["Kept, renamed", "Brand new"]
    status = dict(conn.execute("SELECT id, status FROM goals").fetchall())
    assert status[goes] == "dropped"                          # dropped, not deleted
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 3


def test_a_bad_row_means_nothing_is_written(conn):
    """Mutant: write as you go. The first row would land and the second would be reported."""
    gid = goals.add(conn, "Original", "2027-01-01")
    with pytest.raises(GoalError, match="goal 2"):
        goals.sync(conn, [{"id": gid, "description": "Changed"},
                          {"id": None, "description": "", "priority": "primary"}])
    assert names(goals.active(conn)) == ["Original"]


def test_an_unknown_id_is_refused(conn):
    goals.add(conn, "a")
    with pytest.raises(GoalError, match="no live goal with id 77"):
        goals.sync(conn, [{"id": 77, "description": "ghost"}])


def test_the_first_save_moves_the_configured_goals_into_the_table(conn):
    """The screen shows the fallback goals with no ids; saving them adds each as a row."""
    shown = goals.active(conn)
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0
    goals.sync(conn, shown)
    assert conn.execute("SELECT COUNT(*) FROM goals WHERE status='active'").fetchone()[0] == len(shown)
    assert all(g["id"] is not None for g in goals.active(conn))


def test_an_empty_save_drops_everything_and_stays_empty(conn):
    goals.add(conn, "a")
    assert goals.sync(conn, []) == []


# -- the routes -------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import functools

    from src.db import schema
    path = tmp_path / "api.db"
    migrate(path)
    monkeypatch.setattr(setup, "read_connection", functools.partial(schema.read_connection, path))
    monkeypatch.setattr(setup, "write_connection", functools.partial(schema.write_connection, path))
    return TestClient(app, base_url=OWN)


def test_the_goals_round_trip_through_the_api(client):
    shown = client.get("/api/goals").json()
    assert shown["priorities"] == ["primary", "secondary", "opt_in"]
    r = client.put("/api/goals", headers=HEAD, json={"goals": [
        {"id": None, "description": "Ride the Dolomites", "date": "2027-07-01", "priority": "primary"}]})
    assert r.status_code == 200
    assert [g["description"] for g in r.json()["goals"]] == ["Ride the Dolomites"]
    assert client.get("/api/goals").json()["goals"][0]["id"] is not None


def test_a_bad_goal_is_a_422_naming_the_row_and_saves_nothing(client):
    r = client.put("/api/goals", headers=HEAD, json={"goals": [
        {"description": "fine"}, {"description": "bad", "date": "soon"}]})
    assert r.status_code == 422 and "goal 2" in r.json()["detail"]
    assert all(g["id"] is None for g in client.get("/api/goals").json()["goals"])   # still the fallback


def test_saving_goals_needs_the_token(client):
    assert client.put("/api/goals", json={"goals": []}).status_code == 403


# -- one reader of the config's goals list -----------------------------------------------------

def test_nothing_else_reads_the_configs_goals_list():
    """
    The config's `goals:` is a first-run seed read in `goals.from_config` and nowhere else. A
    second reader is how the plan screen and the coach ended up counting down to different days.
    """
    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"config\(\)\.goals|\bcfg\.goals|\bcfg\.goals\b|\bconf(?:ig)?\.goals")
    allowed = {root / "src" / "config.py", root / "src" / "goals.py"}
    offenders = []
    for folder in ("src", "scripts"):
        for path in (root / folder).rglob("*.py"):
            if path in allowed:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)

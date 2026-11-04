"""
Tests for the tools the coaching model calls.

377 lines with no coverage, which matters more here than in most modules: these functions
return JSON straight into a model's context. A wrong number in a chart is visible; a wrong
number in a tool result becomes a confident sentence of advice with nothing to contradict it.

The case that motivated these is in `test_a_truncated_window_says_so`. Asking about the last
three months returned the *first* 20 of 32 activities — five weeks of thirteen, stopping
immediately before the September dropout — and reported `count: 20` as though that were all
there was.
"""

import json
import sqlite3
from datetime import date

import pytest

from src.coach import tools
from src.db.migrations import migrate


@pytest.fixture
def db(tmp_path, monkeypatch):
    """
    Point the tools at a temporary database.

    Patching `schema.DB_PATH` does not work, and the reason is worth knowing:
    `read_connection(db_path=DB_PATH)` binds that default **when the function is defined**, so
    rebinding the module attribute afterwards changes nothing. The first version of this
    fixture did exactly that and the tests silently ran against the real database — reporting
    the athlete's actual goal count rather than the fixture's.

    So the context managers are replaced in the tools module's own namespace, bound to the
    temp path.
    """
    import functools

    from src.db import schema

    path = tmp_path / "c.db"
    migrate(path)
    monkeypatch.setattr(tools, "read_connection",
                        functools.partial(schema.read_connection, path))
    monkeypatch.setattr(tools, "write_connection",
                        functools.partial(schema.write_connection, path))

    conn = sqlite3.connect(path)
    for i in range(40):
        day = date(2026, 9, 1).replace(day=1).toordinal() + i
        d = date.fromordinal(day).isoformat()
        conn.execute("INSERT INTO activities (id, date, type, moving_time, distance) "
                     "VALUES (?,?,?,?,?)", (f"a{i}", d, "Ride", 3600, 30000))
        conn.execute("INSERT INTO wellness (date, ctl, atl, hrv, rhr) VALUES (?,?,?,?,?)",
                     (d, 10 + i * 0.2, 12.0, 50, 48))
    conn.commit()
    conn.close()
    return path


def first(result):
    return result["rows"][0]["date"]


def last(result):
    return result["rows"][-1]["date"]


# ── the truncation ────────────────────────────────────────────────────────────

def test_a_truncated_window_says_so(db):
    """
    The silent failure this file exists for.

    A model given 20 rows and `count: 20` has no way to know 12 more were dropped, or that the
    ones dropped were the recent ones. It would describe a window it never saw.
    """
    r = tools.query_history("activities_in_period", "2026-09-01", "2026-12-31", limit=20)
    assert r["count"] == 20
    assert r["total"] == 40
    assert r["truncated"] is True
    assert "20 most recent of 40" in r["note"]


@pytest.mark.parametrize("query_type", ["ctl_range", "activities_in_period",
                                        "wellness_in_period"])
def test_truncation_keeps_the_most_recent_not_the_oldest(db, query_type):
    """
    Which end gets dropped is the whole point, and it has to hold for all three.

    The old code ordered oldest-first and took the head, so a question about recent training
    was answered with the start of the window — in the real database, a span that ended before
    the dropout being asked about. Parametrised because an earlier version checked only
    activities, and a mutant that reverted ctl_range alone passed the whole suite.
    """
    r = tools.query_history(query_type, "2026-09-01", "2026-12-31", limit=5)
    all_rows = tools.query_history(query_type, "2026-09-01", "2026-12-31", limit=1000)
    assert last(r) == last(all_rows), "the newest row was dropped"
    assert first(r) > first(all_rows), "the window was not trimmed from the old end"


def test_rows_come_back_oldest_first(db):
    """Trimmed from the new end, read from the old: the order a human or model expects."""
    r = tools.query_history("wellness_in_period", "2026-09-01", "2026-12-31", limit=10)
    dates = [row["date"] for row in r["rows"]]
    assert dates == sorted(dates)


def test_an_untruncated_window_makes_no_claim_about_truncation(db):
    r = tools.query_history("activities_in_period", "2026-09-01", "2026-12-31", limit=500)
    assert r["count"] == 40
    assert "truncated" not in r and "note" not in r


@pytest.mark.parametrize("query_type", ["ctl_range", "activities_in_period",
                                        "wellness_in_period"])
def test_every_period_query_reports_its_total(db, query_type):
    r = tools.query_history(query_type, "2026-09-01", "2026-12-31", limit=3)
    assert r["total"] == 40 and r["truncated"] is True


def test_an_unknown_query_type_returns_nothing_rather_than_raising(db):
    r = tools.query_history("no_such_query")
    assert r["rows"] == [] and r["count"] == 0


# ── goals ─────────────────────────────────────────────────────────────────────

def test_dropping_a_goal_stores_dropped_not_dropd(db):
    """`action + "d"` gave "achieved" and "dropd". Anything filtering on the real word
    would have missed every dropped goal."""
    tools.update_goal("add", description="CX season", target_date="2027-10-01")
    conn = sqlite3.connect(db)
    goal_id = conn.execute("SELECT id FROM goals").fetchone()[0]
    conn.close()

    assert tools.update_goal("drop", goal_id=goal_id)["status"] == "dropped"

    conn = sqlite3.connect(db)
    status = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0]
    conn.close()
    assert status == "dropped"


def test_achieving_a_goal_stores_achieved(db):
    tools.update_goal("add", description="ride 100km")
    conn = sqlite3.connect(db)
    goal_id = conn.execute("SELECT id FROM goals").fetchone()[0]
    conn.close()

    assert tools.update_goal("achieve", goal_id=goal_id)["status"] == "achieved"


def test_an_unrecognised_goal_action_is_a_no_op(db):
    assert tools.update_goal("explode")["status"] == "no_op"


# ── logging ───────────────────────────────────────────────────────────────────

def test_a_life_event_is_logged(db):
    out = tools.log_life_event("illness", "2026-11-01", note="flu")
    assert out["status"] == "logged"
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT type, start_date, note FROM life_events").fetchall()
    conn.close()
    assert rows == [("illness", "2026-11-01", "flu")]


def test_subjective_feel_defaults_to_the_athletes_today_not_utc(db):
    """
    `utc_now().date()` files an 8pm US Eastern check-in under tomorrow, where it is then
    compared against activity dates reported in local time.
    """
    from src.clock import today_local_iso

    out = tools.log_subjective_feel(7)
    assert out["date"] == today_local_iso()


# ── dispatch ──────────────────────────────────────────────────────────────────

def test_an_unknown_tool_name_returns_an_error_not_an_exception(db):
    assert "unknown tool" in json.loads(tools.execute_tool("nope", {}))["error"]


def test_a_tool_that_raises_is_reported_as_an_error(db):
    """The model has to be told; an exception escaping here kills the whole turn."""
    assert "error" in json.loads(tools.execute_tool("log_life_event", {"bad": "args"}))


def test_every_advertised_tool_has_an_implementation():
    """A schema with no function behind it fails only once the model decides to call it."""
    advertised = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert advertised == set(tools.TOOL_FN_MAP)

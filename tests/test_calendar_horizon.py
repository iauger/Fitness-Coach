"""
The calendar shows the plan's committed horizon, not just the current week.

It ended on this Sunday, so the two further weeks the roller has already scheduled (and
mirrored into `planned_workouts`, where the calendar reads them) never appeared. Mutants: the
range still ending this Sunday; `ahead` counting days instead of weeks; the default not
extending it.
"""

import functools
import sqlite3
from datetime import date

import pytest

from src.db import schema
from src.db.migrations import migrate
from src.visualizations import calendar_view as cal

TODAY = date(2026, 11, 3)          # a Tuesday; this week is Nov 2-8


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "h.db"
    migrate(path)
    conn = sqlite3.connect(path)
    for day, name in (("2026-11-05", "ThisWeek"), ("2026-11-12", "NextWeek"), ("2026-11-21", "WeekAfter"),
                      ("2026-11-23", "TooFar")):
        conn.execute("INSERT INTO planned_workouts (date, name, planned_tss, planned_duration_min) "
                     "VALUES (?, ?, 40, 60)", (day, name))
    conn.commit()
    conn.close()
    monkeypatch.setattr(cal, "read_connection", functools.partial(schema.read_connection, path))
    monkeypatch.setattr(cal, "today_local", lambda: TODAY)


def test_bounds_end_on_the_sunday_of_the_last_week_shown():
    assert cal._week_bounds(1) == (date(2026, 11, 2), date(2026, 11, 8))
    assert cal._week_bounds(1, ahead=2) == (date(2026, 11, 2), date(2026, 11, 22))
    assert cal._week_bounds(3, ahead=1) == (date(2026, 10, 19), date(2026, 11, 15))


def test_the_default_shows_this_week_and_the_two_after(db):
    html = cal.build_calendar_html(weeks=1)
    for name in ("ThisWeek", "NextWeek", "WeekAfter"):
        assert name in html, name
    assert "TooFar" not in html
    assert "Nov 02 to Nov 22, 2026" in html


def test_ahead_zero_is_the_old_range(db):
    html = cal.build_calendar_html(weeks=1, ahead=0)
    assert "ThisWeek" in html and "NextWeek" not in html and "Nov 02 to Nov 08, 2026" in html


def test_ahead_is_in_whole_weeks(db):
    html = cal.build_calendar_html(weeks=1, ahead=1)
    assert "NextWeek" in html and "WeekAfter" not in html

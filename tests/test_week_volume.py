"""
Tests for week boundaries and measured weekly volume.

These sit under the rolling plan generator: it grades a week on `week_volume`'s `ride_min` and
`ride_days`, and decides re-entry on weeks that have no activity at all. Both of those are easy
to get subtly wrong in ways nothing downstream would flag, which is the same reason
test_planning.py exists.

Unlike test_planning.py these need a connection, so they build a two-column in-memory `activities`
table rather than touching data/fitness.db — the assertions are about arithmetic and boundaries,
not about the athlete's history.
"""

import sqlite3
from datetime import date

import pytest

from src.analysis.activities import week_volume
from src.analysis.dates import last_completed_week, monday_of, mondays_between, sunday_of


# ── week boundaries ───────────────────────────────────────────────────────────

def test_monday_of_is_idempotent_on_a_monday():
    monday = date(2026, 11, 2)
    assert monday.weekday() == 0
    assert monday_of(monday) == monday
    assert monday_of(monday_of(monday)) == monday


@pytest.mark.parametrize("day", range(7))
def test_monday_of_maps_every_day_of_a_week_to_the_same_monday(day):
    monday = date(2026, 11, 2)
    assert monday_of(monday.replace(day=2 + day)) == monday


def test_sunday_of_closes_the_same_week():
    assert sunday_of(date(2026, 11, 2)) == date(2026, 11, 8)
    assert sunday_of(date(2026, 11, 8)) == date(2026, 11, 8)


def test_last_completed_week_is_the_previous_monday():
    # On a Monday the current week has not elapsed, so the answer is seven days back.
    assert last_completed_week(date(2026, 11, 2)) == date(2026, 10, 26)
    assert last_completed_week(date(2026, 11, 8)) == date(2026, 10, 26)


def test_mondays_between_is_inclusive_at_both_ends():
    weeks = mondays_between(date(2026, 11, 2), date(2026, 11, 16))
    assert weeks == [date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)]


def test_mondays_between_normalises_mid_week_bounds():
    # A Thursday-to-Wednesday span still yields whole weeks from each containing Monday.
    assert mondays_between(date(2026, 11, 5), date(2026, 11, 11)) == [
        date(2026, 11, 2), date(2026, 11, 9)]


def test_mondays_between_handles_a_single_week():
    assert mondays_between(date(2026, 11, 4), date(2026, 11, 6)) == [date(2026, 11, 2)]


# ── measured weekly volume ────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE activities (date TEXT, type TEXT, moving_time INTEGER)")
    return c


def add(conn, day: str, type_: str, minutes: float | None):
    conn.execute("INSERT INTO activities VALUES (?,?,?)",
                 (day, type_, None if minutes is None else int(minutes * 60)))


def test_an_empty_week_returns_zeroes_rather_than_nothing(conn):
    """
    The case the whole re-entry rule turns on. A week with no rows produces no GROUP BY row, so
    12 of the 62 weeks in the real history would simply vanish from a row-driven iterator — and
    those weeks are the signal, not missing data.
    """
    week = week_volume(conn, date(2026, 11, 2))
    assert week == {
        "week_start": "2026-11-02", "ride_min": 0.0, "ride_days": 0,
        "longest_ride_min": 0.0, "strength_sessions": 0, "training_days": 0,
        "any_activity": False,
    }


def test_cycling_and_strength_are_counted_separately(conn):
    add(conn, "2026-11-03", "VirtualRide", 60)
    add(conn, "2026-11-05", "Ride", 45)
    add(conn, "2026-11-07", "GravelRide", 90)
    add(conn, "2026-11-04", "WeightTraining", 40)
    add(conn, "2026-11-06", "Workout", 30)
    add(conn, "2026-11-06", "Yoga", 25)          # neither a ride nor strength

    week = week_volume(conn, date(2026, 11, 2))
    assert week["ride_min"] == pytest.approx(195.0)
    assert week["ride_days"] == 3
    assert week["longest_ride_min"] == pytest.approx(90.0)
    assert week["strength_sessions"] == 2
    assert week["training_days"] == 5            # yoga day counts as a training day
    assert week["any_activity"] is True


def test_two_rides_in_one_day_count_as_one_ride_day(conn):
    add(conn, "2026-11-03", "Ride", 40)
    add(conn, "2026-11-03", "VirtualRide", 50)
    week = week_volume(conn, date(2026, 11, 2))
    assert week["ride_days"] == 1
    assert week["ride_min"] == pytest.approx(90.0)
    assert week["longest_ride_min"] == pytest.approx(50.0)


def test_the_week_excludes_the_next_weeks_monday(conn):
    add(conn, "2026-11-08", "Ride", 60)      # Sunday, in
    add(conn, "2026-11-09", "Ride", 999)     # next Monday, out
    add(conn, "2026-11-01", "Ride", 999)     # previous Sunday, out
    week = week_volume(conn, date(2026, 11, 2))
    assert week["ride_min"] == pytest.approx(60.0)
    assert week["ride_days"] == 1


def test_any_date_in_the_week_gives_the_same_answer(conn):
    add(conn, "2026-11-05", "Ride", 75)
    monday = week_volume(conn, date(2026, 11, 2))
    assert week_volume(conn, date(2026, 11, 5)) == monday
    assert week_volume(conn, date(2026, 11, 8)) == monday


def test_rides_without_a_duration_are_excluded(conn):
    """`moving_time IS NOT NULL` is the repo-wide "is this a real session" filter."""
    add(conn, "2026-11-03", "Ride", 60)
    add(conn, "2026-11-04", "Ride", None)
    week = week_volume(conn, date(2026, 11, 2))
    assert week["ride_days"] == 1
    assert week["ride_min"] == pytest.approx(60.0)


def test_unknown_activity_types_count_as_training_but_not_as_rides(conn):
    add(conn, "2026-11-03", "Kayaking", 60)
    week = week_volume(conn, date(2026, 11, 2))
    assert week["ride_days"] == 0
    assert week["ride_min"] == 0.0
    assert week["training_days"] == 1
    assert week["any_activity"] is True

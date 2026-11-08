"""
The incremental sync: what it asks for, and what it is allowed to decide from what it got.

`incremental_sync` had no tests. Two properties were found missing by reading it:

1. **Each kind of data resumes from its own last success.** The window was anchored on the newest
   wellness date for all three kinds, so a run where activities failed and wellness succeeded moved
   the anchor to today and the days in between were never asked for again.
2. **A failed activities fetch must not close a week.** The roller ran regardless, and a closed week
   is graded once and for good, so a permanent decision could be made from data the run knew was
   stale.

Mutants: one shared anchor; the roll always allowed to record decisions; a malformed activity
record costing the batch (or going unreported).
"""

import functools
import sqlite3
from datetime import date

import pytest

from src import sync
from src.db import schema, store
from src.db.migrations import migrate

TODAY = date(2026, 11, 7)


class FakeClient:
    athlete_id = "i1"

    def __init__(self, activities=None, fail_activities=False):
        self._activities = activities or []
        self._fail = fail_activities
        self.asked: dict[str, tuple] = {}

    def get_activities(self, start, end):
        self.asked.setdefault("activities", (start, end))
        if self._fail:
            raise RuntimeError("intervals.icu is down")
        return self._activities

    def get_wellness(self, start, end):
        self.asked["wellness"] = (start, end)
        return [{"id": "2026-11-06", "ctl": 20, "hrv": 50}]

    def get_events(self, start, end):
        self.asked["events"] = (start, end)
        return []

    def get_activity_messages(self, activity_id):
        return []


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    migrate(path)
    for name in ("read_connection", "write_connection", "get_connection"):
        monkeypatch.setattr(sync, name, functools.partial(getattr(schema, name), path))
    for name in ("upsert_activities", "upsert_wellness", "upsert_events", "update_activity_notes"):
        monkeypatch.setattr(sync, name, functools.partial(getattr(store, name), db_path=path))
    monkeypatch.setattr(sync, "today_local", lambda: TODAY)
    monkeypatch.setattr(sync, "match_planned_workouts", lambda *a, **k: {})
    rolled: list[dict] = []

    def fake_roll(conn, today, **kw):
        rolled.append(kw)
        return {"tier": 2, "week_type": "normal", "written": 0, "cutoff": "x", "weeks": 0, "backfilled": 0}

    monkeypatch.setattr(sync, "store_horizon", fake_roll)

    class World:
        db = path
        roll_calls = rolled

        def client(self, **kw):
            fake = FakeClient(**kw)
            monkeypatch.setattr(sync, "IntervalsClient", lambda: fake)
            return fake

        def logged(self, data_type, end, status="ok"):
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO sync_log (synced_at, data_type, start_date, end_date, records, status)"
                         " VALUES ('x', ?, ?, ?, 1, ?)", (data_type, end, end, status))
            conn.commit()
            conn.close()

        def stored(self):
            conn = sqlite3.connect(path)
            try:
                return {r[0] for r in conn.execute("SELECT id FROM activities")}
            finally:
                conn.close()

    return World()


def ride(ident, day="2026-11-05", minutes=60):
    """Different lengths, because two same-day rides of near-equal length are treated as one."""
    return {"id": ident, "start_date_local": f"{day}T07:00:00", "type": "Ride", "moving_time": minutes * 60}


def test_each_kind_of_data_resumes_from_its_own_last_success(world):
    """
    Activities last succeeded on the 20th, wellness on the 6th. Asking for activities from two days
    before the *wellness* date would skip 20 Oct to 4 Nov for good.
    """
    world.logged("activities", "2026-10-20")
    world.logged("wellness", "2026-11-06")
    fake = world.client()
    sync.incremental_sync(silent=True)
    assert fake.asked["activities"][0] == date(2026, 10, 18)
    assert fake.asked["wellness"][0] == date(2026, 11, 4)


def test_a_failed_run_does_not_move_the_activities_anchor(world):
    """The failed attempt writes no 'ok' row for activities, so the next run asks for the same days."""
    world.logged("activities", "2026-10-20")
    world.client(fail_activities=True)
    result = sync.incremental_sync(silent=True)
    assert any(e.startswith("activities:") for e in result["errors"])
    fake = world.client()
    sync.incremental_sync(silent=True)
    assert fake.asked["activities"][0] == date(2026, 10, 18)


def test_a_database_with_no_sync_record_falls_back_to_the_wellness_date(world):
    conn = sqlite3.connect(world.db)
    conn.execute("INSERT INTO wellness (date, ctl) VALUES ('2026-11-01', 10)")
    conn.commit()
    conn.close()
    fake = world.client()
    sync.incremental_sync(silent=True)
    assert fake.asked["activities"][0] == date(2026, 10, 30)


def test_an_error_row_is_not_a_success(world):
    world.logged("activities", "2026-11-06", status="error: timeout")
    world.logged("activities", "2026-10-20")
    fake = world.client()
    sync.incremental_sync(silent=True)
    assert fake.asked["activities"][0] == date(2026, 10, 18)


def test_a_failed_activities_fetch_does_not_let_the_roll_record_a_decision(world):
    world.client(fail_activities=True)
    sync.incremental_sync(silent=True)
    assert world.roll_calls == [{"persist_state": False}]


def test_a_good_fetch_lets_the_roll_record_decisions(world):
    world.client(activities=[ride("a1")])
    sync.incremental_sync(silent=True)
    assert world.roll_calls == [{"persist_state": True}]
    assert world.stored() == {"a1"}


def test_one_malformed_record_costs_only_itself_and_is_reported(world):
    broken = {"id": "bad", "start_date_local": None, "type": "Ride"}
    world.client(activities=[ride("a1"), broken, ride("a2", minutes=30)])
    result = sync.incremental_sync(silent=True)
    assert world.stored() == {"a1", "a2"}
    assert result["activities"] == 2
    assert any("1 record(s) skipped" in e and "bad (no usable date)" in e for e in result["errors"])
    assert world.roll_calls == [{"persist_state": True}], "a skipped record is reported, not a failed fetch"

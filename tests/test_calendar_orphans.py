"""
A session that leaves the plan must take its calendar event with it.

The roller deleted the row, and the row held the only record of the event's id. The push deletes
only the ids of rows that still exist, so lowering `max_ride_days`, stepping down a rung or a
recovery week dropping a ride left the old event on the athlete's watch for good. `reconcile` now
names every remote event that has this plan's exact id shape and matches no local session, and the
maintenance cycle deletes those.

Mutants: orphans named but never deleted; a looser match that claims another plan's events (or an
event with no external id); an event that a local session still owns named as an orphan; the
roll-then-reconcile path not finding what the roll removed.
"""

import sqlite3
from datetime import date

import pytest

from conftest import seed_ladder
from test_maintenance import RecordingClient, _const_conn

from src import maintenance
from src.db.migrations import migrate
from src.planning import roller
from src.planning.export import is_ours, pending, reconcile
from src.planning.roller import store_horizon
from test_roller import CUTOFF, PLAN, TODAY, force_state


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    yield conn
    conn.close()


class Remote:
    """A calendar that holds whatever events it is given, and records what is deleted."""

    def __init__(self, events):
        self.events = events
        self.deleted: list[int] = []

    def get_events(self, start, end):
        return self.events

    def delete_events_by_id(self, ids):
        self.deleted += list(ids)
        return len(ids)


def ours(day, role, event_id):
    return {"id": event_id, "external_id": roller.external_id(PLAN, day, role)}


def test_the_shape_of_an_id_this_plan_could_have_written():
    assert is_ours("test-rolling-2026-11-09-long", PLAN)
    assert not is_ours("test-rolling-2-2026-11-09-long", PLAN), "another plan whose slug starts with ours"
    assert not is_ours("test-rolling-notes-2026-11-09-long", PLAN)
    assert not is_ours("test-rolling-2026-11-09", PLAN), "no role"
    assert not is_ours("test-rolling-2026-11-09-long extra", PLAN), "match, not fullmatch: trailing text"
    assert not is_ours("", PLAN) and not is_ours(None, PLAN)
    assert not is_ours("someone-elses-event", PLAN)


def test_an_event_for_a_session_that_no_longer_exists_is_named(db):
    store_horizon(db, TODAY, PLAN)
    row = pending(db, date.fromisoformat(CUTOFF), PLAN)[0]
    gone = ours("2026-11-20", "removed", 777)
    kept = {"id": 500, "external_id": row["external_id"]}
    out = reconcile(db, Remote([gone, kept]), date.fromisoformat(CUTOFF), PLAN)
    assert out["orphan_ids"] == [777], "the event a local session owns must not be named"


def test_events_that_are_not_this_plans_are_never_named(db):
    store_horizon(db, TODAY, PLAN)
    strangers = [
        {"id": 1, "external_id": None},
        {"id": 2},
        {"id": 3, "external_id": "someone-elses-event"},
        {"id": 4, "external_id": "test-rolling-2-2026-11-20-long"},     # another plan
        {"id": 5, "external_id": "test-rolling-2026-11-20"},            # no role: not our shape
    ]
    out = reconcile(db, Remote(strangers), date.fromisoformat(CUTOFF), PLAN)
    assert out["orphan_ids"] == []


def test_a_roll_that_drops_sessions_leaves_orphans_that_are_found(db, monkeypatch):
    """
    The real path, end to end. Rich then poor: the second roll removes sessions, each of which had
    an event on the calendar. Every one of those events must be named, and no other.
    """
    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'evt-' || id, pushed_at = 'x' "
               "WHERE plan_name = ?", (PLAN,))
    on_calendar = [{"id": int(r["id"]), "external_id": r["external_id"]} for r in db.execute(
        "SELECT id, external_id FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (PLAN, CUTOFF))]

    monkeypatch.setattr(roller, "current_state", force_state(tier=0, block_index=0))
    store_horizon(db, TODAY, PLAN)
    still = {r["external_id"] for r in db.execute(
        "SELECT external_id FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (PLAN, CUTOFF))}
    removed = {e["id"] for e in on_calendar if e["external_id"] not in still}
    assert removed, "no session left the horizon, so this proves nothing"

    out = reconcile(db, Remote(on_calendar), date.fromisoformat(CUTOFF), PLAN)
    assert set(out["orphan_ids"]) == removed


def test_the_cycle_deletes_the_orphans_and_says_so(db, monkeypatch):
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()
    client = RecordingClient()
    client.get_events = lambda s, e: [ours("2026-11-20", "removed", 777)]
    deleted: list[list[int]] = []
    client.delete_events_by_id = lambda ids: (deleted.append(list(ids)), len(ids))[1]
    monkeypatch.setattr("src.intervals.client.IntervalsClient", lambda: client)
    monkeypatch.setattr(maintenance, "write_connection", _const_conn(db))
    monkeypatch.setattr(maintenance, "read_connection", _const_conn(db))
    monkeypatch.setattr(maintenance, "configured_plan_name", lambda: PLAN, raising=False)

    out = maintenance._reconcile_and_push(TODAY, PLAN)

    assert deleted == [[777]]
    assert out["orphans_removed"] == 1 and "orphan_ids" not in out
    line = maintenance.summarise({"sync": {}, "push": out})
    assert "removed 1 calendar events" in line


def test_a_cycle_with_nothing_orphaned_deletes_nothing(db, monkeypatch):
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()
    client = RecordingClient()
    monkeypatch.setattr("src.intervals.client.IntervalsClient", lambda: client)
    monkeypatch.setattr(maintenance, "write_connection", _const_conn(db))
    monkeypatch.setattr(maintenance, "read_connection", _const_conn(db))
    out = maintenance._reconcile_and_push(TODAY, PLAN)
    assert client.deletes == 0 and out["orphans_removed"] == 0

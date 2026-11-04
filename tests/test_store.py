"""
Tests for the write path into SQLite.

These exist because of a gap the review found rather than a bug: `src/db/store.py` had no
coverage at all. Four `NameError`s were introduced into it during the connection cleanup and
the full suite still reported 394 passing — every sync would have failed on the first write,
and nothing would have said so until a sync ran.

They also pin the behaviour the cleanup could have changed. The upserts used to open a
connection, do their work inside `with conn:` and close afterwards; they now run inside
`write_connection`, which commits on success, rolls back on error and always closes. That
widens the transaction from the loop to the whole function, so the things worth asserting are
that a write is visible afterwards, that a failure leaves nothing behind, and that the
duplicate check still sees rows written earlier in the same call.
"""

import sqlite3

import pytest

from src.db.migrations import migrate
from src.db import store


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    migrate(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    return path


def rows(path, table):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def activity(ident, date="2026-11-03", minutes=60, kind="Ride"):
    return {"id": ident, "start_date_local": f"{date}T07:00:00", "type": kind,
            "moving_time": minutes * 60, "name": f"ride {ident}"}


# ── activities ────────────────────────────────────────────────────────────────

def test_activities_are_written_and_visible_after_the_call(db):
    assert store.upsert_activities([activity("a1"), activity("a2", minutes=30)], db) == 2
    assert {r["id"] for r in rows(db, "activities")} == {"a1", "a2"}


def test_upserting_the_same_activity_twice_updates_rather_than_duplicates(db):
    store.upsert_activities([activity("a1", minutes=60)], db)
    store.upsert_activities([activity("a1", minutes=75)], db)
    got = rows(db, "activities")
    assert len(got) == 1
    assert got[0]["moving_time"] == 75 * 60


def test_the_duplicate_check_sees_rows_written_earlier_in_the_same_call(db):
    """
    The one thing widening the transaction could have broken.

    `_has_near_duplicate` queries the same connection mid-loop. If the earlier inserts were
    not visible to it, a Strava import and an intervals.icu sync in one batch would both land.
    """
    store.upsert_activities([activity("a1", minutes=60), activity("a2", minutes=60)], db)
    got = rows(db, "activities")
    assert len(got) == 1, "the second near-duplicate should have been skipped"


def test_a_failure_part_way_through_leaves_nothing_behind(db):
    """
    write_connection rolls back on an exception.

    Previously `with conn:` did the same for the loop and the close was skipped entirely on
    the error path, leaking the handle. The rollback behaviour has to survive the change.
    """
    bad = [activity("a1"), {"id": object()}]     # unencodable -> sqlite raises
    with pytest.raises(Exception):
        store.upsert_activities(bad, db)
    assert rows(db, "activities") == []


# ── wellness, events, notes ───────────────────────────────────────────────────

def test_wellness_is_written_and_upserted_by_date(db):
    assert store.upsert_wellness([{"id": "2026-11-03", "ctl": 15.0, "hrv": 50}], db) == 1
    store.upsert_wellness([{"id": "2026-11-03", "ctl": 16.0, "hrv": 52}], db)
    got = rows(db, "wellness")
    assert len(got) == 1 and got[0]["ctl"] == 16.0


def test_events_are_written_and_upserted_by_id(db):
    ev = {"id": "e1", "start_date_local": "2026-11-03T00:00:00", "name": "A race",
          "category": "RACE_A"}
    assert store.upsert_events([ev], db) == 1
    store.upsert_events([{**ev, "name": "A race, renamed"}], db)
    got = rows(db, "events")
    assert len(got) == 1 and got[0]["name"] == "A race, renamed"


def test_notes_are_written_only_for_the_activities_named(db):
    """
    A targeted UPDATE, not part of the upsert.

    Notes are fetched only for activities carrying an icu_chat_id, so folding them into the
    main upsert would null out athlete_note on every other activity in the sync window.
    """
    store.upsert_activities([activity("a1"), activity("a2", date="2026-10-01")], db)
    assert store.update_activity_notes({"a1": "felt strong"}, db) == 1

    by_id = {r["id"]: r for r in rows(db, "activities")}
    assert by_id["a1"]["athlete_note"] == "felt strong"
    assert by_id["a2"]["athlete_note"] is None


def test_writing_no_notes_touches_nothing(db):
    store.upsert_activities([activity("a1")], db)
    store.update_activity_notes({"a1": "kept"}, db)
    assert store.update_activity_notes({}, db) == 0
    assert rows(db, "activities")[0]["athlete_note"] == "kept"


def test_a_note_can_be_cleared(db):
    """An emptied chat thread should clear the note, not leave a stale one."""
    store.upsert_activities([activity("a1")], db)
    store.update_activity_notes({"a1": "something"}, db)
    store.update_activity_notes({"a1": None}, db)
    assert rows(db, "activities")[0]["athlete_note"] is None

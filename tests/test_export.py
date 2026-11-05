"""
Tests for the intervals.icu push.

This writes to a real person's calendar, so the tests concentrate on the ways it could do
damage rather than on happy-path shape:

1. **Nothing is deleted that we did not create.** Deletion goes by stored `remote_event_id`
   only — never a range delete, which would remove the athlete's own events in the window.
2. **Re-pushing does not duplicate.** `upsert` is documented as matching `external_id` only for
   events "created by the same OAuth application", and this client uses an API key. Identity is
   tracked locally instead, so the local bookkeeping has to be right.
3. **Remote ids are matched back by `external_id`, not by position.** One reordering would
   otherwise stamp the wrong remote id onto a session, and the *next* push would delete an
   event belonging to something else.
4. **dry_run is the default.** A caller that forgets the flag must not write.

No test touches the network: the client is a fake that records calls.
"""

import json
import re
import sqlite3
from datetime import date

import pytest

from conftest import seed_ladder

from src.db.migrations import migrate
from src.planning.export import (
    CATEGORY, MIDNIGHT, build_payload, event_for, pending, push,
)
from src.planning.roller import store_horizon

PLAN = "test rolling"
TODAY = date(2026, 11, 3)


class FakeClient:
    """Records what would have been sent, and hands back plausible created events."""

    def __init__(self, drop_external_ids: bool = False, deleted: int = 0):
        self.created_payloads: list[list[dict]] = []
        self.deleted_ids: list[list[int]] = []
        self._next_id = 1000
        self._drop = drop_external_ids
        self._deleted = deleted

    def delete_events_by_id(self, ids):
        self.deleted_ids.append(list(ids))
        return self._deleted or len(ids)

    def create_events(self, events):
        self.created_payloads.append(events)
        out = []
        for e in events:
            self._next_id += 1
            created = {"id": self._next_id}
            if not self._drop:
                created["external_id"] = e["external_id"]
            out.append(created)
        return out


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn, name=PLAN)
    store_horizon(conn, TODAY, PLAN)
    conn.commit()
    yield conn
    conn.close()


# ── the payload ───────────────────────────────────────────────────────────────

def test_dates_are_sent_at_midnight(db):
    """intervals.icu rejects a calendar date carrying a time of day."""
    for event in build_payload(pending(db, TODAY, PLAN)):
        assert event["start_date_local"].endswith(MIDNIGHT)
        assert event["start_date_local"].count("T") == 1


def test_every_event_is_a_workout_with_a_sport(db):
    events = build_payload(pending(db, TODAY, PLAN))
    assert events
    assert {e["category"] for e in events} == {CATEGORY}
    assert {e["type"] for e in events} <= {"Ride", "WeightTraining"}


def test_strength_days_are_pushed_too(db):
    """Strength is the habit the plan is anchored on. Leaving it off the calendar would make
    the calendar disagree with the plan about what the week asks for."""
    events = build_payload(pending(db, TODAY, PLAN))
    assert any(e["type"] == "WeightTraining" for e in events)


def test_rides_carry_the_zwo_and_strength_days_do_not(db):
    """
    The .zwo doubles as the structured workout, so there is one definition rather than two.

    A strength day has no power target and no .zwo to send; attaching an empty file would be
    worse than attaching nothing.
    """
    events = build_payload(pending(db, TODAY, PLAN))
    rides = [e for e in events if e["type"] == "Ride"]
    strength = [e for e in events if e["type"] == "WeightTraining"]

    assert rides and all("<workout_file>" in e["file_contents"] for e in rides)
    assert all(e["filename"].endswith(".zwo") for e in rides)
    assert strength and all("file_contents" not in e for e in strength)


def test_duration_and_load_are_integers_in_the_units_the_api_wants(db):
    for event in build_payload(pending(db, TODAY, PLAN)):
        assert isinstance(event["moving_time"], int)
        assert isinstance(event["icu_training_load"], int)
    ride = next(e for e in build_payload(pending(db, TODAY, PLAN)) if e["type"] == "Ride")
    assert ride["moving_time"] >= 45 * 60      # seconds, not minutes


def test_past_sessions_are_never_pushed(db):
    """They are the record of what was asked, and pushing them risks overwriting a day the
    athlete has already logged an activity against."""
    rows = pending(db, TODAY, PLAN)
    assert rows
    assert min(r["date"] for r in rows) >= TODAY.isoformat()


def test_the_name_drops_the_parenthetical_metrics(db):
    """The prescription string carries "(45min, 26 TSS, IF 0.59)" for the terminal; the
    calendar already shows duration and load in their own fields."""
    for event in build_payload(pending(db, TODAY, PLAN)):
        assert "TSS" not in event["name"]


# ── safety ────────────────────────────────────────────────────────────────────

def test_push_is_a_dry_run_unless_asked(db):
    client = FakeClient()
    result = push(db, client, TODAY, PLAN)
    assert result["dry_run"] is True
    assert client.created_payloads == [] and client.deleted_ids == []
    assert result["payload"], "a dry run should still show what it would send"


def test_a_first_push_deletes_nothing(db):
    """There is nothing of ours on the calendar yet, so there is nothing to clean up."""
    client = FakeClient()
    result = push(db, client, TODAY, PLAN, dry_run=False)
    assert result["to_delete"] == 0
    assert client.deleted_ids == [] or client.deleted_ids == [[]]


def test_re_pushing_deletes_exactly_what_it_created_before(db):
    """
    The property that stands in for upsert.

    Identity is tracked locally because the API's own `upsert` only matches `external_id` for
    OAuth applications, and this client uses an API key.
    """
    client = FakeClient()
    first = push(db, client, TODAY, PLAN, dry_run=False)
    db.commit()
    stamped = [r["remote_event_id"] for r in pending(db, TODAY, PLAN)]
    assert all(stamped) and len(set(stamped)) == len(stamped)

    second = push(db, client, TODAY, PLAN, dry_run=False)
    db.commit()
    assert second["to_delete"] == first["created"]
    assert sorted(client.deleted_ids[-1]) == sorted(int(s) for s in stamped)


def test_remote_ids_are_matched_by_external_id_not_position(db):
    """
    One reordering would otherwise stamp the wrong remote id onto a session, and the *next*
    push would delete an event belonging to something else.
    """
    class Reordering(FakeClient):
        def create_events(self, events):
            return list(reversed(super().create_events(events)))

    client = Reordering()
    push(db, client, TODAY, PLAN, dry_run=False)
    db.commit()

    sent = {e["external_id"]: e for e in client.created_payloads[0]}
    for row in pending(db, TODAY, PLAN):
        assert row["external_id"] in sent
        assert row["remote_event_id"] is not None
    ids = [r["remote_event_id"] for r in pending(db, TODAY, PLAN)]
    assert len(set(ids)) == len(ids), "two sessions ended up pointing at one remote event"


def test_a_response_without_external_ids_stamps_nothing_rather_than_guessing(db):
    """
    If the server does not echo `external_id`, we cannot tell which event is which.

    Stamping by position would be a guess, and a wrong guess makes the next push delete the
    wrong event. Recording nothing just means the next push creates duplicates, which is
    visible and recoverable.
    """
    client = FakeClient(drop_external_ids=True)
    result = push(db, client, TODAY, PLAN, dry_run=False)
    db.commit()
    assert result["stamped"] == 0
    assert result["unmatched"] == result["created"]
    assert all(r["remote_event_id"] is None for r in pending(db, TODAY, PLAN))


def test_nothing_is_sent_when_there_is_nothing_to_push(db):
    client = FakeClient()
    result = push(db, client, date(2030, 1, 1), PLAN, dry_run=False)
    assert result["sessions"] == 0
    assert client.created_payloads == []


def test_external_ids_are_unique_across_the_payload(db):
    events = build_payload(pending(db, TODAY, PLAN))
    ids = [e["external_id"] for e in events]
    assert len(set(ids)) == len(ids)
    assert all(ids)


def test_event_for_handles_a_row_with_no_tier_recorded():
    """Rows written before migration 5 have no volume_tier; the description must not crash."""
    row = {"date": "2026-11-03", "archetype": "lit", "prescription": "60min @ 62% FTP",
           "minutes": 60.0, "tss": 38.0, "external_id": "x", "block_name": "Sprints",
           "week_in_block": 1, "volume_tier": None, "week_type": None,
           "plan_name": "Sustainable rolling", "role": "lit"}
    event = event_for(row)
    assert "T" not in event["description"].split("\n")[1].replace("Sprints", "")
    assert event["moving_time"] == 3600


def test_a_junk_remote_id_is_skipped_rather_than_crashing_the_push(db):
    """
    Should be impossible, but a crash mid-push is the worst outcome available.

    Deletion runs before creation, so raising partway through would leave the calendar with
    some events removed and none written back.
    """
    client = FakeClient()
    push(db, client, TODAY, PLAN, dry_run=False)
    db.commit()
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'not-a-number' "
               "WHERE plan_name = ? AND date = (SELECT MIN(date) FROM prescribed_sessions "
               "WHERE plan_name = ?)", (PLAN, PLAN))
    db.commit()

    result = push(db, client, TODAY, PLAN, dry_run=False)
    assert result["created"] > 0
    assert all(isinstance(i, int) for i in client.deleted_ids[-1])


# ── the description must not read as a workout ────────────────────────────────

REP_COUNT = re.compile(r"^\s*\d+\s*x", re.IGNORECASE)


def zwo_description(event: dict) -> str:
    return re.search(r"<description>(.*?)</description>",
                     event["file_contents"], re.S).group(1)


def test_no_attached_workout_carries_a_rep_count_in_its_description(db):
    """
    The bug that put a 45-minute session on the calendar as 362 minutes.

    intervals.icu lifts the attached .zwo's own <description> into the event and then parses
    the result as a workout in its native format. Our prescriptions begin "8x 10s all-out,
    3min recovery", and that leading "8x" was read as a repeat count over the entire attached
    workout: 2720 seconds became 21760, exactly eight times too long, with a training load of
    212 instead of 26. "6x 20s" did it six times over.

    Both descriptions are checked, because fixing only the event's left the one inside the file
    still doing it — the first fix changed nothing observable.
    """
    for event in build_payload(pending(db, TODAY, PLAN)):
        assert not REP_COUNT.match(event["description"]), event["description"]
        if "file_contents" in event:
            assert not REP_COUNT.match(zwo_description(event)), zwo_description(event)


def test_an_attached_workout_describes_only_where_it_sits_in_the_plan(db):
    """Where a workout_doc carries the session, the description carries prose the parser
    cannot mistake for a step list."""
    for event in build_payload(pending(db, TODAY, PLAN)):
        if "file_contents" not in event:
            continue
        assert "TSS" not in event["description"]
        assert "TSS" not in zwo_description(event)
        assert PLAN in event["description"]


def test_strength_days_keep_their_full_text(db):
    """They have no file attached and no leading rep count, and the text is all there is."""
    strength = [e for e in build_payload(pending(db, TODAY, PLAN))
                if "file_contents" not in e]
    assert strength
    assert all("min" in e["description"] for e in strength)
    assert all(not REP_COUNT.match(e["description"]) for e in strength)


# ── the read path must never write ────────────────────────────────────────────

def test_reading_the_plan_state_records_no_decision(db):
    """
    A GET must not file a training decision.

    `current_state` runs the gate's catch-up, which normally persists — a report or an API
    call that triggered that would write a permanent, unique-constrained row for a week, from
    a code path nobody thinks of as a writer. Every read-side caller passes persist=False.
    """
    from src.planning.progression_store import current_state, rows as progression_rows

    before = len(progression_rows(db, PLAN))
    for _ in range(3):
        current_state(db, date(2026, 12, 14), PLAN, persist=False)
    assert len(progression_rows(db, PLAN)) == before

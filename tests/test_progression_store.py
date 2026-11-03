"""
Tests for the gate's persistent half, and for migration 4.

Four things here would change a prescription silently if they broke, which is the bar this file
holds to rather than covering every function:

1. **The graded-week round trip.** `record_week` writes a `classify_week` dict and
   `_graded_from_row` reads it back. Those two have to agree exactly, because the rebuilt
   history is what `trailing_gap` and the met-streak rule run on — a field written to the wrong
   column would move a tier with nothing to show for it.
2. **Decide-once.** A closed week must be decided one time and never re-decided, whatever a sync
   schedule does. Without that, re-running a catch-up after a late-syncing ride would re-grade a
   week the athlete has already been prescribed against.
3. **Mondays, not rows.** A week with zero activity produces no rows at all, and those are
   precisely the weeks the re-entry rule exists to catch.
4. **The plan_name filter.** Two plans covering one week double-counted every weekly and cycle
   rollup. That was live, and it is the kind of thing that reads as a plausible wrong number.

These need a database, so they migrate a temp file rather than touching data/fitness.db.
"""

import sqlite3
from datetime import date

import pytest

from src.analysis.cycle import _weeks_in_cycle
from src.analysis.derive import weekly_load
from src.db.migrations import LATEST_VERSION, current_version, migrate
from src.planning.blocks import ROTATION, ROTATION_LOOP_FROM, advance_position
from src.planning.progression import TIERS, classify_week, next_state
from src.planning.progression_store import (
    PLAN_START_MONDAY, _graded_from_row, catch_up, record_week, rows,
)

PLAN = "test plan"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def ride(conn, day: str, minutes: int, kind: str = "Ride") -> None:
    conn.execute(
        "INSERT INTO activities (id, date, type, sport, moving_time) VALUES (?, ?, ?, ?, ?)",
        (f"{day}-{kind}-{minutes}", day, kind, kind, minutes * 60),
    )


# ── migration 4 ───────────────────────────────────────────────────────────────

def test_migration_replays_from_empty_and_is_idempotent(tmp_path):
    path = tmp_path / "fresh.db"
    applied = migrate(path)
    assert [v for v, _ in applied] == list(range(1, LATEST_VERSION + 1))
    # A second run must apply nothing. The runner's whole contract is that a database at any
    # version converges on the same schema, so "already applied" and "broken" must not look alike.
    assert migrate(path) == []
    conn = sqlite3.connect(path)
    assert current_version(conn) == LATEST_VERSION
    conn.close()


def test_migration_adds_the_export_columns_the_push_needs(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(prescribed_sessions)")}
    assert {"external_id", "remote_event_id", "pushed_at"} <= cols


def test_a_superseded_plan_keeps_its_past_and_loses_its_future(tmp_path):
    """
    The cutover deletes forward, never backward.

    Elapsed rows are the record of what was asked, and `cycle_reviews` holds a stored review
    whose boundaries `cycles()` derives from those weeks — deleting them would leave a review
    that cannot reproduce its own range.
    """
    path = tmp_path / "cutover.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 3")
    for week, name in [("2026-10-26", "TrainerRoad 2026-27"),
                       (PLAN_START_MONDAY, "TrainerRoad 2026-27")]:
        conn.execute(
            "INSERT INTO training_plan_weeks (plan_name, plan_start_date, plan_end_date, "
            "week_start_date, week_end_date, week_type, phase, phase_number, "
            "phase_week_number, plan_week_number) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, week, week, week, week, "base", "base", 1, 1, 1))
    conn.commit()
    conn.close()

    migrate(path)
    conn = sqlite3.connect(path)
    kept = [r[0] for r in conn.execute(
        "SELECT week_start_date FROM training_plan_weeks ORDER BY week_start_date")]
    conn.close()
    assert kept == ["2026-10-26"]


# ── the round trip ────────────────────────────────────────────────────────────

def test_a_stored_week_reads_back_as_the_dict_that_graded_it(db):
    """
    The one test that would catch a field written to the wrong column.

    `classify_week`'s output is the gate's own input on the next evaluation, so it has to
    survive the database unchanged. Compared field by field rather than by shape.
    """
    week = {"week_start": "2026-11-02", "ride_min": 190.0, "ride_days": 3,
            "strength_sessions": 2, "longest_ride_min": 95.0, "training_days": 5,
            "any_activity": True}
    graded = classify_week(week, 2)
    decision = next_state([graded], 2)

    assert record_week(db, graded, decision, 1, 2, week["strength_sessions"], PLAN) is True
    db.commit()

    stored = rows(db, PLAN)
    assert len(stored) == 1
    assert _graded_from_row(stored[0]) == graded


def test_the_tier_a_week_was_graded_against_survives_a_later_promotion(db):
    """
    `tier_applied` is stored, not inferred.

    A week is graded against the tier prescribed *for it*. If the stored row carried only the
    next tier, replaying history after a promotion would re-grade old weeks against the new
    one — and a week that was `met` at T1 is `missed` at T4.
    """
    week = {"week_start": "2026-11-02", "ride_min": 190.0, "ride_days": 3,
            "strength_sessions": 2, "longest_ride_min": 95.0, "training_days": 5,
            "any_activity": True}
    graded = classify_week(week, 1)
    assert graded["outcome"] == "met"
    decision = next_state([graded], 1)
    assert decision.tier == 2                      # promoted

    record_week(db, graded, decision, 1, 2, 2, PLAN)
    db.commit()
    stored = rows(db, PLAN)[0]
    assert stored["tier_applied"] == 1
    assert stored["tier_next"] == 2
    assert _graded_from_row(stored)["tier"] == 1


# ── decide once ───────────────────────────────────────────────────────────────

def test_a_closed_week_is_decided_once_however_often_the_sync_runs(db):
    ride(db, "2026-11-03", 60)
    ride(db, "2026-11-05", 60)
    ride(db, "2026-11-07", 90)
    db.commit()

    first = catch_up(db, date(2026, 11, 9), PLAN)
    db.commit()
    assert len(first["recorded"]) == 1

    for _ in range(3):
        again = catch_up(db, date(2026, 11, 9), PLAN)
        db.commit()
        assert again["recorded"] == []
        assert again["tier"] == first["tier"]
    assert len(rows(db, PLAN)) == 1


def test_record_week_reports_that_it_did_nothing_the_second_time(db):
    graded = classify_week({"week_start": "2026-11-02", "ride_min": 0.0, "ride_days": 0,
                            "strength_sessions": 0, "longest_ride_min": 0.0,
                            "training_days": 0, "any_activity": False}, 2)
    decision = next_state([graded], 2)
    assert record_week(db, graded, decision, 1, 1, 0, PLAN) is True
    assert record_week(db, graded, decision, 1, 1, 0, PLAN) is False


def test_a_what_if_writes_nothing(db):
    """`persist=False` has to give the same answer and leave no trace — otherwise asking
    'where would March put me' files March's decisions, and the unique constraint makes them
    permanent."""
    ride(db, "2026-11-03", 60)
    db.commit()
    dry = catch_up(db, date(2026, 11, 16), PLAN, persist=False)
    assert rows(db, PLAN) == []
    wet = catch_up(db, date(2026, 11, 16), PLAN)
    db.commit()
    assert (wet["tier"], wet["block_index"], wet["week_in_block"]) == (
        dry["tier"], dry["block_index"], dry["week_in_block"])


# ── Mondays, not rows ─────────────────────────────────────────────────────────

def test_a_week_with_no_activity_is_still_decided(db):
    """
    The weeks that produce no rows are the signal, not noise.

    Twelve of the 62 weeks in the reference history are empty, and they are what the re-entry
    rule exists to detect. Anything iterating the data rather than the calendar omits them.
    """
    state = catch_up(db, date(2026, 11, 30), PLAN)
    db.commit()
    decided = [r["week_start"] for r in rows(db, PLAN)]
    assert decided == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"]
    assert all(r["outcome"] == "missed" for r in rows(db, PLAN))
    assert state["week_type"] == "reentry"


def test_nothing_before_the_plan_started_is_ever_recorded(db):
    """Replaying the gate over earlier history is a counterfactual — no gate was running
    then — so it informs the starting tier and never enters the decision log."""
    ride(db, "2026-06-03", 120)
    db.commit()
    catch_up(db, date(2026, 11, 30), PLAN)
    db.commit()
    assert min(r["week_start"] for r in rows(db, PLAN)) >= PLAN_START_MONDAY


# ── rotation position ─────────────────────────────────────────────────────────

def test_continue_walks_the_block_then_rolls_into_the_next():
    block = ROTATION[ROTATION_LOOP_FROM]
    at_end = (ROTATION_LOOP_FROM, block.weeks)
    assert advance_position(ROTATION_LOOP_FROM, 1, "continue") == (ROTATION_LOOP_FROM, 2)
    nxt, week = advance_position(*at_end, "continue")
    assert week == 1 and nxt != ROTATION_LOOP_FROM


def test_restart_block_keeps_the_emphasis_and_drops_the_progression():
    """A fortnight off loses the block's accumulated position but not its emphasis — resuming
    at week 5 of a 6-week block asks for a position that was never earned."""
    assert advance_position(2, 5, "restart_block") == (2, 1)


def test_restart_rotation_goes_back_to_the_block_with_no_hard_session():
    """Four weeks off is a cold start, and block 0 is the only one carrying no hard session
    at any tier."""
    assert advance_position(3, 4, "restart_rotation") == (0, 1)
    assert ROTATION[0].block_type == "base"


# ── the plan_name filter ──────────────────────────────────────────────────────

def seed_week(conn, plan_name: str, monday: str) -> None:
    conn.execute(
        "INSERT INTO training_plan_weeks (plan_name, plan_start_date, plan_end_date, "
        "week_start_date, week_end_date, week_type, phase, phase_number, phase_week_number, "
        "plan_week_number) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (plan_name, monday, monday, monday, "2026-11-08", "base", "base", 1, 1, 1))


def test_two_plans_covering_one_week_do_not_double_count_it(db):
    """
    The live bug these filters fix.

    `cycle._weeks_in_cycle`, `derive.weekly_load` and `weekly.week_metrics` all read
    `training_plan_weeks` without a plan filter, so any date range two seeded plans both cover
    returned each week twice — and cycle length, planned TSS and the per-week breakdown all
    double off the back of it.
    """
    seed_week(db, "plan A", "2026-11-02")
    seed_week(db, "plan B", "2026-11-02")
    db.commit()

    cycle = {"plan_name": "plan A", "start_date": "2026-11-02", "end_date": "2026-11-08"}
    weeks = _weeks_in_cycle(db, cycle)
    assert [w["plan_name"] for w in weeks] == ["plan A"]


def test_weekly_load_reads_the_week_type_of_one_plan_only(db):
    # The *wrong* plan is inserted first on purpose. `weekly_load` takes `fetchone()`, so an
    # unfiltered query returns whichever row SQLite orders first — which is the lower rowid.
    # Seeding plan A first made this test pass with or without the filter.
    db.execute(
        "INSERT INTO training_plan_weeks (plan_name, plan_start_date, plan_end_date, "
        "week_start_date, week_end_date, week_type, phase, phase_number, phase_week_number, "
        "plan_week_number) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("plan B", "2026-11-02", "2026-11-02", "2026-11-02", "2026-11-08",
         "rest", "taper", 9, 9, 99))
    seed_week(db, "plan A", "2026-11-02")
    db.commit()

    week = next(w for w in weekly_load(db, date(2026, 11, 8), weeks=1, plan_name="plan A")
                if w["week_start"] == "2026-11-02")
    assert (week["week_type"], week["plan_week"]) == ("base", 1)


def test_week_metrics_reads_the_week_type_of_one_plan_only(db, monkeypatch):
    """
    Same bug, third reader. `week_metrics` stores what it reads into `weekly_summaries`
    alongside a narrative, so an unfiltered read does not just display the wrong week type —
    it freezes it next to prose describing a different week.

    Patched rather than parameterised because this function opens its own connection.
    """
    import src.analysis.weekly as W

    db.execute(
        "INSERT INTO training_plan_weeks (plan_name, plan_start_date, plan_end_date, "
        "week_start_date, week_end_date, week_type, phase, phase_number, phase_week_number, "
        "plan_week_number) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("plan B", "2026-11-02", "2026-11-02", "2026-11-02", "2026-11-08",
         "rest", "taper", 9, 9, 99))
    seed_week(db, "plan A", "2026-11-02")
    db.commit()
    monkeypatch.setattr(W, "get_connection", lambda *a, **k: db)

    m = W.week_metrics(date(2026, 11, 2), plan_name="plan A")
    assert (m["week_type"], m["phase"], m["plan_week_number"]) == ("base", "base", 1)

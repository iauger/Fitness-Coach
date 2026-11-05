"""
Tests for the rolling horizon.

The roller is where a wrong gate becomes a hundred wrong prescriptions, so these concentrate on
the two properties that make storing a horizon safe at all:

1. **The freeze.** Regeneration must not touch today or tomorrow. A test that only asserts
   "frozen rows did not change" passes trivially when nothing changed at all, so every freeze
   test here also asserts that the *future* moved — otherwise it proves nothing. That mistake
   was made once while writing these, with `start_tier`, which turns out only to seed the
   pre-plan replay and so changed no prescription.
2. **Idempotence.** Syncing twice must leave the database identical, and must never duplicate
   `(plan_name, date, role)`.

Plus the one behavioural claim the rebuild rests on: after a gap, the athlete gets a re-entry
week, not week 4 of a block they were not there for.
"""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from conftest import seed_ladder

from src.db.migrations import migrate
from src.planning import roller
from src.planning.constants import plan_start_monday

PLAN_START_MONDAY = plan_start_monday()
from src.planning.progression import TIERS
from src.planning.roller import FREEZE_DAYS, external_id, horizon, store_horizon

PLAN = "test rolling"
# A Tuesday inside the plan's first week, so there is both a frozen and a future side.
TODAY = date(2026, 11, 3)
CUTOFF = (TODAY + timedelta(days=FREEZE_DAYS)).isoformat()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    yield conn
    conn.close()


def sessions(conn, since: str = "0000-00-00") -> dict:
    """Stored sessions keyed by (date, role), with the volatile columns dropped."""
    return {(r["date"], r["role"]): {k: r[k] for k in r.keys()
                                     if k not in ("id", "created_at")}
            for r in conn.execute(
                "SELECT * FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
                (PLAN, since))}


def force_state(tier: int, block_index: int, week_in_block: int = 1,
                week_type: str = "normal"):
    """Hand the roller a specific gate decision, so the freeze can be tested against a real
    change rather than against an accidental no-op."""
    return lambda conn, *a, **k: {
        "tier": tier, "block_index": block_index, "week_in_block": week_in_block,
        "week_type": week_type, "reason": "forced", "decided_through": None,
        "seeded_from": "forced", "recent": [], "decided_weeks": 0, "recorded": [],
    }


# ── idempotence ───────────────────────────────────────────────────────────────

def test_storing_the_horizon_twice_changes_nothing(db):
    first = store_horizon(db, TODAY, PLAN)
    before = sessions(db)
    assert before, "nothing was stored"

    second = store_horizon(db, TODAY, PLAN)
    assert sessions(db) == before
    # The second run updates the future in place and backfills nothing, because the frozen
    # rows already exist. It deletes nothing at all: a roll removes only sessions the new
    # horizon no longer contains, which is what keeps remote_event_id attached to the rows
    # that survive.
    assert second["backfilled"] == 0
    assert second["deleted"] == 0
    assert first["written"] == second["written"]


def test_the_horizon_never_duplicates_a_session(db):
    for _ in range(3):
        store_horizon(db, TODAY, PLAN)
    dupes = db.execute(
        "SELECT COUNT(*) FROM (SELECT plan_name, date, role, COUNT(*) n "
        "FROM prescribed_sessions GROUP BY 1, 2, 3 HAVING n > 1)").fetchone()[0]
    assert dupes == 0


def test_a_later_run_does_not_resurrect_a_week_that_has_rolled_out(db):
    """The horizon moves with the calendar; it does not accumulate."""
    store_horizon(db, TODAY, PLAN)
    first_week = {k for k in sessions(db) if k[0] < "2026-11-09"}
    assert first_week

    # Four weeks on, the original first week is behind the horizon entirely.
    store_horizon(db, TODAY + timedelta(weeks=4), PLAN)
    kept = sessions(db)
    # It is *kept* (it is the record of what was asked) but not rewritten or extended.
    assert first_week <= set(kept)
    assert max(k[0] for k in kept) >= "2026-12-14"


# ── the freeze ────────────────────────────────────────────────────────────────

def test_regeneration_cannot_change_today_or_tomorrow(db, monkeypatch):
    """
    The property the whole design rests on.

    A Friday-evening sync must not be able to alter Saturday's long ride, and a past week must
    stay as the record of what was asked — "you were asked for 3 rides and 205 minutes at T2,
    you rode 2 and 124" is only answerable if the ask was frozen when it was made.
    """
    store_horizon(db, TODAY, PLAN)
    before = sessions(db)

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    after = sessions(db)

    frozen = {k for k in before if k[0] < CUTOFF and before[k] != after.get(k)}
    future = {k for k in set(before) | set(after)
              if k[0] >= CUTOFF and before.get(k) != after.get(k)}

    assert not frozen, f"the freeze leaked on {frozen}"
    # Without this the test passes when nothing changed at all.
    assert future, "nothing moved, so the freeze was never under test"


def test_the_frozen_side_keeps_the_tier_it_was_prescribed_at(db, monkeypatch):
    store_horizon(db, TODAY, PLAN)
    was = {k: v["volume_tier"] for k, v in sessions(db).items() if k[0] < CUTOFF}
    assert was

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    now = sessions(db)

    assert {k: now[k]["volume_tier"] for k in was} == was
    assert any(v["volume_tier"] == 4 for k, v in now.items() if k[0] >= CUTOFF)


def test_a_first_run_backfills_the_current_week_without_editing_it(db, monkeypatch):
    """
    Before the cutoff the rule is insert-if-missing, not upsert.

    On a first run the current week has to be filled in or the week is half-missing. On every
    later run those same rows must be left alone — using DO UPDATE here would defeat the freeze
    while looking like it works, which is the easiest way to get this wrong.
    """
    first = store_horizon(db, TODAY, PLAN)
    assert first["backfilled"] > 0

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    second = store_horizon(db, TODAY, PLAN)
    assert second["backfilled"] == 0


def test_past_prescriptions_from_before_the_horizon_survive(db):
    """The scoped delete. An unconditional DELETE ... WHERE plan_name = ? would wipe the record
    of every past ask on every sync, which is what this replaced."""
    db.execute("""
        INSERT INTO prescribed_sessions (plan_name, date, weekday, block_index, block_name,
            week_in_block, is_recovery_week, role, archetype, variant, prescription,
            params_json, created_at)
        VALUES (?, '2026-09-22', 1, 0, 'Old', 1, 0, 'hard', 'threshold', 'continuous',
                'something we asked for once', '{}', '2026-09-20')
    """, (PLAN,))
    db.commit()

    store_horizon(db, TODAY, PLAN)
    kept = db.execute("SELECT prescription FROM prescribed_sessions WHERE date = '2026-09-22'"
                      ).fetchone()
    assert kept["prescription"] == "something we asked for once"


# ── what the horizon contains ─────────────────────────────────────────────────

def test_the_horizon_is_three_weeks_and_holds_one_tier(db):
    """
    Later weeks are built at the *same* tier, because the gate only moves when a week closes
    and those weeks have not happened. A horizon that pre-ramped its later weeks would be the
    calendar plan again, wearing a smaller number.
    """
    built = horizon(db, TODAY, PLAN)
    weeks = {w["week_start_date"] for w in built["week_rows"]}
    assert len(weeks) == roller.HORIZON_WEEKS
    assert len({s["tier"] for s in built["sessions"]}) == 1


def test_the_rotation_advances_across_the_horizon(db):
    built = horizon(db, TODAY, PLAN)
    weeks_in_block = [w["phase_week_number"] for w in built["week_rows"]]
    assert weeks_in_block == sorted(weeks_in_block)
    assert weeks_in_block[0] != weeks_in_block[-1]


def test_only_the_first_week_can_be_a_reentry_week(db, monkeypatch):
    """The gate decided that about the week now starting; it has said nothing about the two
    after it."""
    monkeypatch.setattr(roller, "current_state",
                        force_state(tier=1, block_index=0, week_type="reentry"))
    built = horizon(db, TODAY, PLAN)
    types = [w["week_type"] for w in built["week_rows"]]
    assert types[0] == "reentry"
    assert "reentry" not in types[1:]


def test_a_gap_produces_a_reentry_week_not_week_four_of_a_block(db):
    """
    The claim the rebuild exists to make good on.

    September 2026 failed because a two-week gap left a plan marching on without the athlete.
    Here, three weeks of nothing must come back as a re-entry week at a lower tier, in week 1
    of a block, rather than at week 4 of a block that ran while nobody was riding.
    """
    # Three closed weeks with no activity at all, then roll.
    after_gap = date.fromisoformat(PLAN_START_MONDAY) + timedelta(weeks=3)
    built = horizon(db, after_gap, PLAN)

    assert built["state"]["week_type"] == "reentry"
    assert built["week_rows"][0]["phase_week_number"] == 1
    assert built["state"]["tier"] < 2
    # A re-entry week carries no hard session.
    first_week = [s for s in built["sessions"]
                  if s["date"] < (after_gap + timedelta(days=7)).isoformat()]
    assert not [s for s in first_week if s["role"] == "hard"]
    # Strength is never trimmed, not even here.
    assert len([s for s in first_week if s["role"] == "strength"]) == 2


def test_every_stored_session_round_trips_its_params(db):
    """`params_json` is what the serialisers and the watts view read back."""
    store_horizon(db, TODAY, PLAN)
    for row in db.execute("SELECT params_json, archetype FROM prescribed_sessions "
                          "WHERE plan_name = ?", (PLAN,)):
        assert isinstance(json.loads(row["params_json"]), dict)


# ── the week table ────────────────────────────────────────────────────────────

def test_the_week_rows_carry_the_tier_and_a_plan_week_number(db):
    store_horizon(db, TODAY, PLAN)
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM training_plan_weeks WHERE plan_name = ? ORDER BY week_start_date",
        (PLAN,))]
    assert len(rows) == roller.HORIZON_WEEKS
    assert [r["plan_week_number"] for r in rows] == [1, 2, 3]
    assert all(r["volume_tier"] is not None for r in rows)
    assert all(r["plan_start_date"] == PLAN_START_MONDAY for r in rows)


def test_the_current_week_row_is_frozen_too(db, monkeypatch):
    store_horizon(db, TODAY, PLAN)
    before = db.execute("SELECT volume_tier FROM training_plan_weeks WHERE plan_name = ? "
                        "AND week_start_date = '2026-11-02'", (PLAN,)).fetchone()["volume_tier"]

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    after = db.execute("SELECT volume_tier FROM training_plan_weeks WHERE plan_name = ? "
                       "AND week_start_date = '2026-11-02'", (PLAN,)).fetchone()["volume_tier"]
    later = db.execute("SELECT volume_tier FROM training_plan_weeks WHERE plan_name = ? "
                       "AND week_start_date = '2026-11-16'", (PLAN,)).fetchone()["volume_tier"]

    assert after == before
    assert later == 4


# ── the export key ────────────────────────────────────────────────────────────

def test_external_id_is_stable_and_unique_per_session(db):
    """
    Keyed on exactly what makes a session unique locally, so re-pushing a horizon updates
    remote events instead of duplicating them — the one thing an export has to get right.
    """
    a = external_id("Sustainable rolling", "2026-11-03", "hard")
    assert a == external_id("Sustainable rolling", "2026-11-03", "hard")
    assert a != external_id("Sustainable rolling", "2026-11-03", "lit")
    assert a != external_id("Sustainable rolling", "2026-11-04", "hard")
    assert " " not in a


def test_every_stored_session_gets_an_external_id(db):
    store_horizon(db, TODAY, PLAN)
    ids = [r["external_id"] for r in db.execute(
        "SELECT external_id FROM prescribed_sessions WHERE plan_name = ?", (PLAN,))]
    assert ids and all(ids)
    assert len(set(ids)) == len(ids)


def test_a_changed_block_cannot_add_a_session_to_a_frozen_day(db, monkeypatch):
    """
    The hole the per-row freeze left.

    `UNIQUE(plan_name, date, role)` means insert-if-missing only protects a row that already
    exists *under the same role*. When the rotation block changed, the new pattern put a role on
    a frozen day that day had not carried, no conflict fired, and the day quietly gained a second
    workout. Adding a session to tomorrow is as disruptive as editing one, and a frozen rest day
    has to stay a rest day.
    """
    store_horizon(db, TODAY, PLAN)
    before = {k for k in sessions(db) if k[0] < CUTOFF}

    # A different block produces a different weekday pattern.
    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)

    after = {k for k in sessions(db) if k[0] < CUTOFF}
    assert after == before, f"frozen window gained or lost sessions: {after ^ before}"


# ── the planned_workouts mirror ───────────────────────────────────────────────

def planned(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM planned_workouts ORDER BY date, id")]


def test_the_horizon_is_mirrored_into_planned_workouts(db):
    """
    Sixteen readers key on this table — compliance, the PMC forecast, the calendar view, the
    dashboard, the coach's tools, the API. Filling it is far less churn than repointing them
    all at prescribed_sessions, and it only became possible once the TrainerRoad feed that
    re-upserted the table wholesale on every sync was retired.
    """
    store_horizon(db, TODAY, PLAN)
    rows = planned(db)
    assert rows
    assert {r["date"] for r in rows} == {s[0] for s in sessions(db)}


def test_strength_days_are_mirrored_with_a_null_load_not_zero(db):
    """
    `compliance.match_planned_workouts` selects `WHERE planned_tss IS NOT NULL` as its "this is
    a bike workout with power targets" test. A zero passes that test, so every strength day
    would be scored `skipped` for having no cycling activity against it — turning the habit the
    plan is anchored on into a weekly adherence penalty.
    """
    store_horizon(db, TODAY, PLAN)
    strength = [r for r in planned(db) if r["workout_type"] == "strength"]
    assert strength
    assert all(r["planned_tss"] is None for r in strength)
    rides = [r for r in planned(db) if r["workout_type"] != "strength"]
    assert rides and all(r["planned_tss"] is not None for r in rides)


def test_the_mirror_only_deletes_rows_it_owns(db):
    """
    The delete is prefixed on our own external ids, so it cannot reach anything else in a table
    we now share — a kept TrainerRoad row, or a workout the athlete added by hand.

    Both planted rows sit **inside the rewrite window**. An earlier version of this test put
    the foreign row in the past, where an unprefixed `DELETE ... WHERE date >= cutoff` would
    not have reached it either, so it passed with the guard removed and proved nothing.
    """
    future = (date.fromisoformat(CUTOFF) + timedelta(days=1)).isoformat()
    db.execute("INSERT INTO planned_workouts (id, date, name, planned_tss) VALUES (?,?,?,?)",
               ("tr-uid-123", future, "Lazy Mountain -2", 55))
    db.execute("INSERT INTO planned_workouts (id, date, name, planned_tss) VALUES (?,?,?,?)",
               ("hand-added", future, "A ride I planned myself", 40))
    db.commit()

    store_horizon(db, TODAY, PLAN)

    kept = {r["id"]: r["name"] for r in planned(db)}
    assert kept.get("tr-uid-123") == "Lazy Mountain -2"
    assert kept.get("hand-added") == "A ride I planned myself"


def test_re_rolling_does_not_duplicate_planned_workouts(db):
    store_horizon(db, TODAY, PLAN)
    first = len(planned(db))
    for _ in range(3):
        store_horizon(db, TODAY, PLAN)
    assert len(planned(db)) == first


def test_the_mirror_respects_the_freeze(db, monkeypatch):
    """Same rule as prescribed_sessions: today and tomorrow do not move."""
    store_horizon(db, TODAY, PLAN)
    before = {r["id"]: r["name"] for r in planned(db) if r["date"] < CUTOFF}
    assert before

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    after = {r["id"]: r["name"] for r in planned(db) if r["date"] < CUTOFF}

    assert after == before


def test_the_mirror_backfills_its_own_frozen_window(db):
    """
    Each table answers the freeze question about itself.

    The mirror originally reused the answer computed for prescribed_sessions, which was wrong
    the moment the two could differ — and they differed immediately in practice, because
    prescribed_sessions had been filled by earlier runs before this mirror existed. The frozen
    side was treated as already present and never backfilled, so today's and tomorrow's
    sessions were missing from adherence, the calendar view and the forecast.
    """
    store_horizon(db, TODAY, PLAN)
    db.execute("DELETE FROM planned_workouts")      # prescribed_sessions stays populated
    db.commit()

    store_horizon(db, TODAY, PLAN)

    mirrored = {r["date"] for r in planned(db)}
    assert {k[0] for k in sessions(db)} == mirrored, "the frozen window was not backfilled"
    assert any(d < CUTOFF for d in mirrored)


# ── the link to the remote calendar ───────────────────────────────────────────

def test_rolling_preserves_the_remote_event_id(db):
    """
    The bug that would have duplicated a real calendar on every scheduled sync.

    store_horizon used to delete the whole future and re-insert it. That is correct for the
    prescription and silently destructive for everything hanging off it: remote_event_id and
    pushed_at live on these rows, and a re-inserted row is a new row with those columns empty.
    Observed live — 15 events on the calendar, 13 no longer referenced by anything, and the
    next push would have created 15 more rather than replacing them.
    """
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'evt-' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()
    before = {(r["date"], r["role"]): r["remote_event_id"] for r in sessions(db).values()
              if False} or {k: v["remote_event_id"] for k, v in sessions(db).items()}

    store_horizon(db, TODAY, PLAN)

    after = {k: v["remote_event_id"] for k, v in sessions(db).items()}
    assert after == before, "a roll dropped the link to the remote calendar"
    assert all(v for v in after.values())


def test_a_changed_session_keeps_the_id_of_the_event_it_replaces(db, monkeypatch):
    """
    Keeping the id across a *content* change is the point, not an accident.

    The push deletes by stored id and then creates. If a changed prescription arrived with an
    empty id, the event it supersedes would stay on the calendar forever beside its
    replacement.
    """
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'evt-1' "
               "WHERE plan_name = ? AND date = ?", (PLAN, "2026-11-07"))
    db.commit()
    was = sessions(db)[("2026-11-07", "long")]["prescription"]

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)

    row = sessions(db).get(("2026-11-07", "long"))
    assert row is not None
    assert row["prescription"] != was, "nothing changed, so the test proves nothing"
    assert row["remote_event_id"] == "evt-1"


def test_a_session_that_leaves_the_horizon_is_removed(db, monkeypatch):
    """
    The other half: a date/role the new horizon no longer contains must not linger.

    Rich first, then poor. The fixture database has no activity in it, so the gate already
    sits at the floor — forcing T0 from there changes nothing and the prune is never
    exercised, which is how the first version of this test passed while asserting nothing.
    """
    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=3))
    store_horizon(db, TODAY, PLAN)
    before = set(sessions(db))

    monkeypatch.setattr(roller, "current_state", force_state(tier=0, block_index=0))
    store_horizon(db, TODAY, PLAN)
    after = set(sessions(db))

    gone = {k for k in before - after if k[0] >= CUTOFF}
    assert gone, "no session left the horizon, so the prune was never exercised"
    assert not (gone & after)

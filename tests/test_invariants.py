"""
Properties that must hold for every week the generator can produce.

The unit tests check specific behaviours at specific inputs. These walk years of simulated
calendar through the real roller against a real database and assert the things that must be
true *everywhere* — the kind of violation that only appears at a combination no single test
thought to try.

The invariants are the plan's own safety rules, restated as assertions:

  - at most two structured hard sessions in a week (the source's own ceiling)
  - exactly two strength sessions, at every tier, in every week, never trimmed
  - no lower-body strength the day before or after a long ride
  - no duplicate (date, role), ever
  - tier always within the ladder, minutes always positive

One of these started out wrong and is worth recording: "one tier per week" seemed obviously
right and is not. Applying a tier change to a week that is already written leaves the frozen
days on their old tier, which is the freeze doing its job. The invariant is one tier per
*side of the freeze*.
"""

import random
import sqlite3
from datetime import date, timedelta

import pytest

from src.analysis.dates import monday_of
from src.db.migrations import migrate
from src.planning import roller
from src.planning.blocks import MAX_HARD_PER_WEEK, ROTATION
from src.planning.constants import plan_start_monday

PLAN_START_MONDAY = plan_start_monday()
from src.planning.outline import outline, summarise
from src.planning.progression import CAP_TIER, FLOOR_TIER, TIERS
from src.planning.progression_store import rows as progression_rows
from src.planning.roller import FREEZE_DAYS, store_horizon

PLAN = "invariants"
START = date.fromisoformat(PLAN_START_MONDAY)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "i.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def ride(conn, day, minutes, kind="Ride"):
    conn.execute(
        "INSERT OR REPLACE INTO activities (id, date, type, sport, moving_time) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"{day}-{kind}-{minutes}", day, kind, kind, minutes * 60))


def assert_invariants(conn, today):
    cut = (today + timedelta(days=FREEZE_DAYS)).isoformat()
    sessions = [dict(r) for r in conn.execute(
        "SELECT * FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (PLAN, monday_of(today).isoformat()))]
    assert sessions, "the roller produced nothing"

    weeks = {}
    for s in sessions:
        weeks.setdefault(monday_of(date.fromisoformat(s["date"])).isoformat(), []).append(s)

    for wk, ss in weeks.items():
        hard = [s for s in ss if s["role"] == "hard"]
        strength = [s for s in ss if s["role"] == "strength"]
        # The literal, not MAX_HARD_PER_WEEK. Comparing against the constant made this
        # tautological: raising the constant to 9 raised the bar and the assertion still
        # passed. Two is the source's own ceiling, so it is the number worth pinning.
        assert len(hard) <= 2, f"{wk}: {len(hard)} hard sessions"
        assert len(strength) == 2, f"{wk}: {len(strength)} strength sessions"

        for side, sel in (("future", [x for x in ss if x["date"] >= cut]),
                          ("frozen", [x for x in ss if x["date"] < cut])):
            assert len({x["volume_tier"] for x in sel}) <= 1, f"{wk} {side} mixes tiers"

        longs = {date.fromisoformat(s["date"]) for s in ss if s["role"] == "long"}
        for s in strength:
            d = date.fromisoformat(s["date"])
            if any(abs((d - L).days) == 1 for L in longs):
                assert "lower" not in (s["variant"] or ""), \
                    f"{s['date']}: legs loaded beside a long ride"

        for s in ss:
            assert FLOOR_TIER <= s["volume_tier"] <= CAP_TIER, f"{s['date']} off the ladder"
            assert (s["minutes"] or 0) > 0, f"{s['date']} has no duration"

    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT date, role, COUNT(*) n FROM prescribed_sessions "
        "WHERE plan_name = ? GROUP BY 1, 2 HAVING n > 1)", (PLAN,)).fetchone()[0]
    assert dupes == 0, f"{dupes} duplicate (date, role) slots"


def test_a_cold_start_produces_a_valid_week(db):
    store_horizon(db, START, PLAN)
    assert_invariants(db, START)


def test_two_years_of_random_adherence_never_breaks_an_invariant(db):
    """
    The broad sweep: 104 weeks, riding roughly two weeks in three, with the gate climbing and
    de-ramping throughout. Seeded, so a failure is reproducible.
    """
    random.seed(7)
    for w in range(104):
        monday = START + timedelta(weeks=w)
        if random.random() < 0.65:
            for off in random.sample(range(7), random.randint(1, 4)):
                ride(db, (monday + timedelta(days=off)).isoformat(), random.randint(30, 150))
            if random.random() < 0.5:
                ride(db, (monday + timedelta(days=2)).isoformat(), 40, "WeightTraining")
        store_horizon(db, monday, PLAN)
        if w % 13 == 0:
            assert_invariants(db, monday)

    decided = progression_rows(db, PLAN)
    assert len(decided) > 90
    assert {r["tier_next"] for r in decided} == set(TIERS), "not every tier was exercised"
    assert all(r["verdict"] for r in decided)
    assert all((date.fromisoformat(b["week_start"])
                - date.fromisoformat(a["week_start"])).days == 7
               for a, b in zip(decided, decided[1:])), "a week was skipped or repeated"


def test_a_year_away_returns_to_the_floor_as_a_reentry_week(db):
    ride(db, PLAN_START_MONDAY, 120)
    store_horizon(db, START, PLAN)

    far = START + timedelta(weeks=52)
    state = store_horizon(db, far, PLAN)

    assert state["tier"] == FLOOR_TIER
    assert state["week_type"] == "reentry"
    assert_invariants(db, far)


@pytest.mark.parametrize("weeks", [1, 2, 3, 5, 8])
def test_any_horizon_length_stores_exactly_that_many_weeks(db, weeks):
    store_horizon(db, START, PLAN, weeks=weeks)
    got = {monday_of(date.fromisoformat(r["date"])).isoformat() for r in db.execute(
        "SELECT date FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (PLAN, START.isoformat()))}
    assert len(got) == weeks


@pytest.mark.parametrize("weeks", [0, 1, 12, 52])
def test_the_outline_handles_any_length_including_none(weeks):
    rows = outline(START, 2, 1, 1, weeks=weeks)
    assert len(rows) == weeks
    summarise(rows)          # must not raise, including on an empty outline


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block_index", range(len(ROTATION)))
def test_every_tier_in_every_block_produces_a_valid_week(db, monkeypatch, tier, block_index):
    monkeypatch.setattr(roller, "current_state", lambda conn, *a, **k: {
        "tier": tier, "block_index": block_index, "week_in_block": 1,
        "week_type": "normal", "reason": "", "decided_through": None,
        "seeded_from": "forced", "recent": [], "decided_weeks": 0, "recorded": []})
    store_horizon(db, START, PLAN)
    assert_invariants(db, START)

"""
Deriving a tier ladder, and refusing to generate without one.

The strongest check here is the replay: derive against the frozen reference year and compare
to the ladder a human chose from that same data. It is the closest thing this step has to a
known answer. They are not required to match exactly — the question the roadmap sets is
whether the differences are defensible — but the long-ride sequence, the ride counts and the
hard-session placement all come out identical, and the one difference is a five-minute easy
ride that the derivation rounds up from a 64-minute median and the human rounded down.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from conftest import seed_ladder
from src.db.migrations import migrate
from src.planning import plans, tiers_store
from src.planning.bootstrap import (
    LONG_STEPS, MIN_RIDDEN_WEEKS, RUNGS, BootstrapError, derive, observe,
)
from src.planning.progression import TIERS
from src.planning.roller import store_horizon
from src.planning.tiers_store import NotBootstrapped

FIXTURE = Path(__file__).parent / "fixtures" / "history_2025-09_2026-11.json"
BAND = (3.5, 4.0)
CEILING = 120
MAX_RIDE_DAYS = 7


@pytest.fixture(scope="module")
def history() -> list[dict]:
    return json.load(FIXTURE.open(encoding="utf-8"))["weeks"]


@pytest.fixture
def ladder(history):
    return derive(history, BAND, CEILING, MAX_RIDE_DAYS)


def week(start="2026-01-05", ride_min=180.0, ride_days=3, longest=60.0, strength=0) -> dict:
    return {"week_start": start, "ride_min": ride_min, "ride_days": ride_days,
            "longest_ride_min": longest, "strength_sessions": strength,
            "training_days": ride_days, "any_activity": ride_min > 0}


def weeks(n: int, **kw) -> list[dict]:
    return [week(start=f"2026-{(i // 4) + 1:02d}-{((i % 4) * 7) + 1:02d}", **kw)
            for i in range(n)]


# ── the replay: a known answer ────────────────────────────────────────────────

def test_the_derived_ladder_reproduces_the_hand_written_one(ladder):
    """
    Same history, same rungs. The human and the derivation agree on everything that shapes a
    week — long ride, ride days, where the hard session appears — which is the result that
    says the derivation found the athlete rather than found the config.
    """
    hand = list(TIERS.values())
    assert [t.long_min for t in ladder.tiers] == [t.long_min for t in hand]
    assert [t.rides for t in ladder.tiers] == [t.rides for t in hand]
    assert [t.hard for t in ladder.tiers] == [t.hard for t in hand]


def test_the_only_difference_is_the_easy_ride_and_it_is_the_honest_rounding(ladder):
    """
    Derived easy ride is 65min against the hand-written 60. The athlete's median session is
    64min, so 65 is the rounded truth; 60 was a person rounding down. Asserted rather than
    waved at, because "close enough" is how a derivation drifts.
    """
    assert ladder.observed.recent_session_min == pytest.approx(64, abs=1)
    assert {t.lit_min for t in ladder.tiers[1:]} == {65}
    for derived, hand in zip(ladder.tiers, TIERS.values()):
        assert abs(derived.target_hours - hand.target_hours) <= 0.1


def test_the_entry_rung_is_not_wildly_above_measured_practice(ladder):
    """
    The roadmap's own stop condition: *"a derived T1 wildly above measured practice means the
    derivation is wrong, not the fixture."* A prescription at triple what someone is already
    doing gets ignored exactly like one at a third.
    """
    recent_hours = ladder.observed.recent_volume_min / 60
    assert ladder.tiers[1].target_hours <= recent_hours * 1.5, (
        f"T1 asks {ladder.tiers[1].target_hours}h against {recent_hours:.1f}h recent")
    assert ladder.floor.target_hours <= recent_hours


def test_the_cap_lands_in_the_configured_band(ladder):
    low, high = BAND
    assert low - 0.25 <= ladder.cap.target_hours <= high + 0.25
    assert ladder.notes == () or all("band" not in n for n in ladder.notes)


# ── no dead rungs ─────────────────────────────────────────────────────────────

def test_every_rung_asks_for_more_than_the_one_below(ladder):
    """
    A tie is a rung the gate advances into while prescribing an identical week. It reads as
    progress, changes nothing, and costs a week before the next real step.
    """
    targets = [t.target_min for t in ladder.tiers]
    assert targets == sorted(set(targets)), targets
    assert len(targets) == RUNGS


def test_a_narrow_long_ride_range_still_produces_distinct_rungs():
    """
    The regression that made `_long_sequence` necessary.

    At a fixed 15-minute step, a 75→120 range put rungs on 86.25 and 97.5, which round to 90
    and — because `round()` is banker's and 6.5 goes to 6 — also 90. Two identical rungs, no
    error, a plausible-looking ladder.
    """
    for ceiling in range(70, 181, 5):
        built = derive(weeks(12, longest=65.0), BAND, ceiling, 3)
        longs = [t.long_min for t in built.tiers]
        assert longs[1:] == sorted(set(longs[1:])), (ceiling, longs)
        assert longs[0] == longs[1], "the floor shares the first rung's long ride"


def test_the_floor_shares_the_first_rungs_long_ride(ladder):
    """What separates T0 from T1 is the hard session, not a shorter long ride."""
    assert ladder.tiers[0].long_min == ladder.tiers[1].long_min
    assert ladder.tiers[0].hard == 0
    assert all(t.hard == 1 for t in ladder.tiers[1:])


def test_a_tight_range_drops_the_step_and_says_so():
    built = derive(weeks(12, longest=75.0), BAND, 100, 3)
    assert any("step the long ride" in n for n in built.notes), built.notes
    assert max(LONG_STEPS) > 10


# ── reading the history ───────────────────────────────────────────────────────

def test_zero_weeks_count_against_sustained_volume():
    """
    Sustained means *held*. A window with one 6-hour week and seven empty ones is not a
    6-hour athlete, and a derivation that thought so would put the ladder far above anything
    reachable — the exact failure the rebuild exists to undo.
    """
    spiky = [week(ride_min=360.0, ride_days=5)] + weeks(7, ride_min=0.0, ride_days=0,
                                                        longest=0.0)
    steady = weeks(8, ride_min=200.0, ride_days=3)
    assert observe(spiky + steady).sustained_volume_min == pytest.approx(200, abs=1)


def test_a_single_huge_week_does_not_move_the_ladder():
    """Medians throughout, so one outlier week cannot set anyone's rungs."""
    normal = derive(weeks(16, ride_min=180.0, longest=60.0), BAND, CEILING, 3)
    spiked = derive(weeks(15, ride_min=180.0, longest=60.0)
                    + [week(ride_min=600.0, ride_days=6, longest=300.0)],
                    BAND, CEILING, 3)
    assert [t.target_min for t in normal.tiers] == [t.target_min for t in spiked.tiers]


def test_too_little_history_refuses_and_says_how_much_is_missing():
    with pytest.raises(BootstrapError, match=f"{MIN_RIDDEN_WEEKS} are needed"):
        derive(weeks(3) + weeks(9, ride_min=0.0, ride_days=0, longest=0.0),
               BAND, CEILING, 3)


def test_no_history_at_all_points_at_the_sync_script():
    with pytest.raises(BootstrapError, match="fetch_history"):
        derive([], BAND, CEILING, 3)


# ── intent that the history cannot reach ──────────────────────────────────────

def test_a_band_the_history_cannot_reach_is_a_note_not_an_error():
    """
    The athlete said where they want to get to; the history said what shape their riding has.
    Those disagreeing is a fact about them. Bending the ladder up to meet an aspiration is how
    the old plan reached 8.4h/week.
    """
    built = derive(weeks(12, ride_min=120.0, ride_days=2, longest=45.0), (6.0, 7.0), 60, 3)
    assert built.cap.target_hours < 6.0
    assert any("band" in n for n in built.notes), built.notes


def test_configured_availability_caps_the_ride_days_and_says_so():
    """
    `plan.max_ride_days` caps; it does not set. Someone riding five days with a ceiling of
    three gets a three-ride ladder and is told the plan is not claiming the other two.
    """
    built = derive(weeks(12, ride_min=400.0, ride_days=5, longest=90.0), BAND, CEILING, 3)
    assert all(t.rides <= 3 for t in built.tiers)
    assert any("max_ride_days" in n for n in built.notes), built.notes


def test_history_sets_the_ride_days_when_availability_allows_more():
    """
    The other half of "config caps, history sets": a ceiling of seven does not prescribe seven
    to someone who rides three. A plan asking for twice what you do is ignored exactly like one
    asking for a third.
    """
    built = derive(weeks(12, ride_min=180.0, ride_days=3, longest=60.0), BAND, CEILING, 7)
    assert built.cap.rides == 3
    assert built.notes == () or all("max_ride_days" not in n for n in built.notes)


def test_a_five_day_athlete_gets_a_five_day_week():
    """
    The limitation this replaced: the weekly template was two hardcoded lists with three ride
    slots, so three was a ceiling for everybody and the derivation was clamped to it.
    """
    from src.planning.blocks import rotation_for, rotation_rides

    built = derive(weeks(12, ride_min=400.0, ride_days=5, longest=90.0), BAND, CEILING, 7)
    assert built.cap.rides == 5
    rotation = rotation_for(built.cap.rides)
    assert rotation_rides(rotation) == 5


# ── storage, provenance and the refusal ───────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "b.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_a_plan_with_no_ladder_refuses_to_generate(db):
    """
    The point of the whole step. A fallback here would build a complete, believable plan from
    one particular athlete's rungs and report nothing — and a plan that fits badly reads as
    the plan being wrong about *you*, not as a missing setup step.
    """
    with pytest.raises(NotBootstrapped, match="bootstrap_plan.py"):
        store_horizon(db, date(2026, 11, 4))


def test_the_stored_ladder_is_what_reaches_the_prescriptions(db):
    """
    Not merely that the roller runs, but that it runs on *this* ladder. A refusal that gated
    on the table and then generated from the module constant would pass every other test here.

    Two mutants survived an earlier version of this test — one making the roller build without
    passing the ladder down, one making `expand_week` ignore the ladder it was handed. Both
    escaped because the stored ladder and the hand-written one happened to agree at the low
    tiers the gate starts a fresh database on: with no activity history the gate sits at the
    floor, and both ladders put a 75-minute long ride there. The ladder below differs at
    **every** rung, which is what makes the assertion able to fail.
    """
    plan = plans.ensure(db, "custom", "Custom", started_on="2026-11-02")
    built = derive(weeks(16, ride_min=450.0, ride_days=3, longest=140.0), BAND, 200, 3)
    tiers_store.save(db, plan.id, built, replace=True)

    stored_longs = {t.long_min for t in built.tiers}
    module_longs = {t.long_min for t in TIERS.values()}
    assert not (stored_longs & module_longs), (
        f"the two ladders share a long ride ({stored_longs & module_longs}), so a roller "
        f"ignoring the stored one could still land on a stored value by accident")

    store_horizon(db, date(2026, 11, 4), "Custom")
    longs = {r[0] for r in db.execute(
        "SELECT DISTINCT minutes FROM prescribed_sessions WHERE role = 'long'")}
    assert longs, "no long rides were written, so this proves nothing"
    # -15 is the recovery/re-entry reduction the generator applies to the long ride.
    assert longs <= stored_longs | {m - 15 for m in stored_longs}, (
        f"long rides {longs} came from neither the stored ladder {stored_longs} nor its "
        f"recovery-week reduction; the module ladder says {module_longs}")


def test_rounding_goes_half_up_not_to_even():
    """
    A direct unit test, because `_long_sequence`'s tie repair now masks this in the output.

    `round()` is banker's: `round(6.5)` is 6, so a long ride of 97.5 minutes rounded at a
    15-minute step lands on 90 rather than 105. That produced two identical rungs on the first
    run of this derivation. The repair step would now catch the tie regardless, which is
    exactly why this needs asserting here — otherwise the rounding could quietly revert and
    only show up as rungs that are subtly wrong rather than duplicated.
    """
    from src.planning.bootstrap import _round_to

    assert _round_to(97.5, 15, 45) == 105
    assert _round_to(86.25, 15, 45) == 90
    assert _round_to(62.5, 5, 30) == 65
    assert _round_to(10.0, 15, 45) == 45, "the floor still applies"


def test_the_validator_rejects_a_ladder_with_a_dead_rung():
    """
    The backstop, tested directly: `_long_sequence` is supposed to make ties unreachable, so
    nothing in normal operation exercises this. A guard that cannot be shown to fire is a
    guard nobody can trust — and this one is the last thing between a bad derivation and a
    hundred wrong prescriptions.
    """
    from src.planning.bootstrap import DerivedTier, Ladder, Observed, _validate

    blank = Observed(0, 0, "", "", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "")
    rungs = [DerivedTier(0, 2, 75, 0, 0, 45)] + [
        DerivedTier(i, 3, 90, 1, 55, 60) for i in range(1, RUNGS)]   # 1..4 identical
    with pytest.raises(BootstrapError, match="every rung must be a real step"):
        _validate(Ladder(tuple(rungs), blank, ()))


def test_the_provenance_survives_the_round_trip(db, ladder):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, ladder)
    found = tiers_store.provenance(db, plan.id)
    assert found["observed"]["recent_volume_min"] == ladder.observed.recent_volume_min
    assert found["observed"]["sustained_window_start"] == ladder.observed.sustained_window_start
    assert found["derived_at"]


def test_re_deriving_over_an_existing_ladder_needs_saying_so(db, ladder):
    """
    The tiers already recorded in `plan_progression` were graded against whatever was in force
    when each week closed. Swapping the ladder underneath them changes what `tier_applied`
    means for rows before and after, so it is a flag rather than a default.
    """
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, ladder)
    with pytest.raises(ValueError, match="--replace"):
        tiers_store.save(db, plan.id, ladder)
    assert tiers_store.save(db, plan.id, ladder, replace=True) == RUNGS


def test_a_saved_ladder_loads_back_identically(db, ladder):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, ladder)
    loaded = tiers_store.load(db, plan.id)
    assert len(loaded) == RUNGS
    for i, derived_tier in enumerate(ladder.tiers):
        assert loaded[i].long_min == derived_tier.long_min
        assert loaded[i].lit_min == derived_tier.lit_min
        assert loaded[i].rides == derived_tier.rides
        assert loaded[i].target_min == derived_tier.target_min


def test_one_plans_ladder_is_not_another_plans(db, ladder):
    a = plans.ensure(db, "a", "A", started_on="2026-11-02")
    b = plans.ensure(db, "b", "B", started_on="2026-11-02")
    tiers_store.save(db, a.id, ladder)
    assert tiers_store.load(db, b.id) is None


def test_the_gate_grades_against_the_stored_ladder_not_the_constant(db):
    """
    `classify_week` decides met/partial/missed from a tier's target. Reading the module
    constant there would grade a 4-hour athlete's week against a 3-hour ladder, and the gate
    would promote them on weeks they had not actually completed.
    """
    from src.planning.progression import classify_week

    big = derive(weeks(12, ride_min=400.0, ride_days=3, longest=120.0), (6.0, 7.0), 240, 3)
    measured = week(ride_min=240.0, ride_days=3)
    assert classify_week(measured, 2)["outcome"] == "met"
    assert classify_week(measured, 2, dict(enumerate(big.tiers)))["outcome"] != "met"


def test_seeding_a_test_database_leaves_it_able_to_generate(db):
    """The fixture other test modules rely on; broken, it would fail them all confusingly."""
    seed_ladder(db, slug="x", name="X")
    assert store_horizon(db, date(2026, 11, 4), "X")["written"] > 0


def test_what_is_reported_is_what_was_prescribed(db, monkeypatch):
    """
    The read paths and the write path must name the same ladder.

    They did not, briefly, and nothing failed: `store_horizon` generated 65-minute easy rides
    from the stored ladder while `/api/plan/state` reported the module constant's 3.42h — one
    number on the dashboard, a different one on the calendar, no error between them. It is the
    same shape of defect as CTL coming from intervals.icu while TSS is recomputed locally, and
    one of those in a project is enough.
    """
    from src.planning.outline import from_state

    plan = seed_ladder(db, slug="r", name="R")
    built = derive(weeks(16, ride_min=450.0, ride_days=3, longest=140.0), BAND, 200, 3)
    tiers_store.save(db, plan.id, built, replace=True)
    ladder = tiers_store.load(db, plan.id)

    from src.planning.progression_store import current_state

    store_horizon(db, date(2026, 11, 4), "R")
    state = current_state(db, date(2026, 11, 4), "R", persist=False, tiers=ladder)

    reported = from_state(state, date(2026, 11, 4), tiers=ladder)
    assert f"{ladder[state['tier']].target_hours}h/wk" in reported["summary"], reported["summary"]
    assert reported["weeks"][0]["hours"] == ladder[state["tier"]].target_hours

    prescribed = {r[0] for r in db.execute(
        "SELECT DISTINCT minutes FROM prescribed_sessions WHERE role = 'lit'")}
    assert prescribed == {float(ladder[state["tier"]].lit_min)}, (
        f"easy rides {prescribed} do not match the ladder being reported")


# ── a week that is actually the athlete's, 2 to 7 days ────────────────────────

@pytest.mark.parametrize("ride_days", [2, 3, 4, 5, 6, 7])
def test_a_whole_horizon_generates_at_every_ride_day_count(db, ride_days):
    """
    End to end at every width, because the safety rules were written against a three-ride week.

    The weekly template used to be two literal lists with three ride slots, and `pattern_for`
    only reduces — so three was a hard ceiling for every athlete this project would ever have,
    and `bootstrap` was clamped to it. Seven is permitted now. It is a bad idea for most
    people, and that is the athlete's judgement to make rather than the code's.

    What must hold at every width: no lower-body lift beside the long ride, at most the
    declared number of hard sessions, the full count of lifts kept, and no day given two rides.
    """
    from src.planning.blocks import rotation_for

    plan = plans.ensure(db, f"w{ride_days}", f"W{ride_days}", started_on="2026-11-02")
    built = derive(weeks(16, ride_min=90.0 * ride_days, ride_days=ride_days, longest=110.0),
                   BAND, 150, ride_days)
    assert built.cap.rides == ride_days
    tiers_store.save(db, plan.id, built, replace=True)

    rotation = rotation_for(ride_days)
    rolled = store_horizon(db, date(2026, 11, 4), f"W{ride_days}")
    assert rolled["written"] > 0

    rows = [dict(r) for r in db.execute(
        "SELECT date, weekday, role, archetype, variant, volume_tier FROM prescribed_sessions "
        "WHERE plan_name = ? ORDER BY date", (f"W{ride_days}",))]
    from src.analysis.dates import monday_of

    by_week: dict[str, list[dict]] = {}
    for r in rows:
        monday = monday_of(date.fromisoformat(r["date"])).isoformat()
        by_week.setdefault(monday, []).append(r)
    assert len(by_week) >= 2, "one week proves nothing about a weekly template"

    for sessions in by_week.values():
        long_days = {s["weekday"] for s in sessions if s["role"] == "long"}
        for s in sessions:
            if s["archetype"] == "strength" and s["variant"] == "lower":
                assert not long_days & {(s["weekday"] - 1) % 7, (s["weekday"] + 1) % 7}, (
                    f"{ride_days} ride days: lower body on weekday {s['weekday']}, "
                    f"beside a long ride")
        rides = [s for s in sessions if s["role"] != "strength"]
        assert len({s["weekday"] for s in rides}) == len(rides), "two rides on one day"
        assert sum(1 for s in sessions if s["role"] == "hard") <= 1

        assert len(rides) <= ride_days, (
            f"{len(rides)} rides in one week against a ladder asking for {ride_days}")

    # The assertion that proves the *roller* built its rotation from the ladder rather than at
    # a fixed three. `<=` alone passes trivially when the generator is narrower than the
    # ladder, which is how two mutants — the roller hardcoding `rotation_for(3)`, and `horizon`
    # ignoring the rotation it was handed — survived an earlier version of this test.
    # Against the tier the gate actually used, not the cap: a fresh database has no activity
    # history, so the pre-plan replay seeds the floor and the horizon is built there. Asserting
    # against `ride_days` instead looked right and compared the floor's rides to the cap's.
    widest = max(len([s for s in v if s["role"] != "strength"]) for v in by_week.values())
    tier_used = max(r["volume_tier"] for r in rows)
    expected = built.tiers[tier_used].rides
    assert widest == expected, (
        f"the widest week has {widest} rides; T{tier_used} of this athlete's ladder asks for "
        f"{expected}. The generator is not using their weekly template.")

    from src.planning.blocks import rotation_rides
    assert rotation_rides(rotation) == ride_days


def test_strength_is_never_trimmed_however_wide_the_week(db):
    """
    Two lifts a week at every tier, in recovery weeks and on re-entry — what
    "strength-anchored" means mechanically, and it must survive the template being generated.
    """
    from src.planning.blocks import build_pattern

    for ride_days in range(2, 8):
        pattern = build_pattern(ride_days, strength_days=2)
        assert sum(1 for _, r in pattern if r == "strength") == 2, (ride_days, pattern)


def test_nothing_reads_the_module_ladder_where_a_plan_has_its_own():
    """
    A grep, after the same defect appeared three times.

    The write path takes the plan's stored ladder; a reader falling back to
    `progression.TIERS` reports a different number for the same week — 3.42h on a dashboard
    beside 3.5h on the calendar. It was fixed in the API and the coaching report, then found
    again in `plan_build.py`, which had not been looked at. No behavioural test catches it,
    because both answers are individually plausible and the suite seeds the module ladder.

    `TIERS` is legitimate as a *fallback* (`ladder or TIERS`) and as the thing the derivation
    is compared against. It is not legitimate as a direct subscript in a reader.
    """
    import ast
    import pathlib

    # Where the module ladder is the subject rather than a shortcut: it is defined in
    # `progression`, derived against in `bootstrap`, and is the documented fallback in
    # `tiers_store`.
    allowed_files = {"progression.py", "bootstrap.py", "tiers_store.py", "safety.py"}
    offenders = []
    for root in ("src", "scripts"):
        for f in sorted(pathlib.Path(root).rglob("*.py")):
            if "__pycache__" in str(f) or f.name in allowed_files:
                continue
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # `TIERS[x]` as a direct subscript. `(ladder or TIERS)[x]` subscripts a
                # BoolOp, not a Name, so the legitimate fallback does not trip this.
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "TIERS"):
                    offenders.append(f"{f}:{node.lineno}")
    listed = "\n  ".join(offenders)
    assert not offenders, (
        "read the plan's own ladder (tiers_store.load) rather than the module constant, or "
        f"the reader and the calendar will disagree:\n  {listed}")

"""
Tests for the plan generator.

These are the project's first tests, and they are here rather than anywhere else because this
is where a review found four separate bugs that all produced *plausible* output — wrong numbers
that nothing downstream would ever flag, because `prescribed_sessions` is read by nothing. Pure
functions over fixtures, no database, no network.

Each test below names the bug it locks down.
"""

from datetime import date, timedelta

import pytest

from src.analysis.derive import EASY_IF_CEILING, HARD_IF_FLOOR
from src.planning import archetypes as A
from src.planning.blocks import (
    DEFAULT_PLAN, PLAN_NAME, Block, add_lit_day, add_second_weekend_ride, expand_plan,
    intensity_split, plan_weeks_rows, weekly_rollup,
)

START = date(2026, 11, 9)  # a Monday


# ── Step / Session arithmetic ─────────────────────────────────────────────────

def test_step_tss_matches_the_standard_formula():
    # TSS = duration_s * IF^2 / 3600 * 100
    step = A.Step(3600, 1.0)
    assert step.tss == pytest.approx(100.0)
    assert A.Step(1800, 0.5).tss == pytest.approx(12.5)


def test_session_if_is_consistent_with_its_own_tss():
    for session in A.catalogue():
        if session.archetype == "strength" or not session.seconds:
            continue
        expected = ((session.tss * 3600) / (session.seconds * 100)) ** 0.5
        assert session.intensity_factor == pytest.approx(expected, abs=0.01), session.variant


# ── structure fidelity: variants must build what they describe ────────────────

def test_thirty_fifteen_alternates_rather_than_one_long_block():
    """Was a single 9-minute block at 112% FTP, which is not ridable."""
    session = A.hiit("thirty_fifteen")
    work = [s for s in session.steps if s.pct > 1.0]
    assert len(work) > 1, "the 30/15 pattern collapsed back into one block"
    assert {s.seconds for s in work} == {30}
    recoveries = [s for s in session.steps if s.label.startswith("bout") and s.pct < 0.5]
    assert {s.seconds for s in recoveries} == {15}


def test_hard_start_opens_above_target_then_settles():
    session = A.hiit("hard_start")
    lead = next(s for s in session.steps if "hard start" in s.label)
    assert lead.seconds == 30
    assert lead.pct == pytest.approx(1.30)
    assert lead.pct > A.HIIT_VARIANTS["hard_start"]["pct"]


def test_over_unders_rolls_without_ever_crossing_threshold():
    """
    Was {0.92, 1.03} — a third of its work time above FTP, straight through the one transition
    the source is emphatic about ("better to hedge low by 10% than overshoot by even 2%",
    because a qualitatively different metabolic response develops either side of it).
    It must still roll over and under its own mean without exceeding it.
    """
    session = A.threshold("over_unders")
    work = [s for s in session.steps if s.label.startswith("effort")]
    assert len({round(s.pct, 2) for s in work}) == 2, "no longer alternates at all"
    assert max(s.pct for s in work) <= 1.00, "crosses the threshold transition"
    lo, hi = min(s.pct for s in work), max(s.pct for s in work)
    assert lo < A.THRESHOLD_VARIANTS["over_unders"]["pct"] < hi, (
        "the nominal pct should sit between the two bands, since it is what any single-number "
        "consumer (plan_build --watts) reads")


@pytest.mark.parametrize("variant", sorted(A.THRESHOLD_VARIANTS))
def test_no_threshold_step_exceeds_ftp(variant):
    assert max(s.pct for s in A.threshold(variant).steps) <= 1.00, variant


@pytest.mark.parametrize("variant", sorted(A.THRESHOLD_VARIANTS))
def test_threshold_accumulates_at_least_forty_minutes_of_work(variant):
    """The source's stated target. Easy to break later by trimming reps to shorten a session."""
    p = A.threshold(variant).params
    assert p["work_min"] * p["reps"] >= 40, variant


@pytest.mark.parametrize("variant", sorted(A.THRESHOLD_VARIANTS))
def test_threshold_rests_are_near_ten_to_one(variant):
    """
    The source asks ~10:1 and these ran 3.0-4.5:1. The rest ratio and the target move together:
    its own diagnostic is that needing long rests means the intensity is too high, so a short
    rest is only defensible alongside the hedged target.
    """
    p = A.threshold(variant).params
    if not p["rest_min"]:
        return                                   # continuous has no rest by definition
    assert p["work_min"] / p["rest_min"] >= 8, variant


def test_prescription_describes_alternation_rather_than_a_flat_target():
    """
    summary() printed `@ {pct}%` for every variant, so over_unders advertised "@ 95%" while
    building 2min/1min alternations and thirty_fifteen advertised "@ 112%" for a 30s-on bout.
    This string is what the athlete reads and what gets matched to a library workout.
    """
    assert "alternating 2min @ 88% / 1min @ 99%" in A.threshold("over_unders").summary()
    assert "alternating 30s @ 112% / 15s @ 45%" in A.hiit("thirty_fifteen").summary()
    assert "after a 30s opener at 130%" in A.hiit("hard_start").summary()
    assert "ramping 55% -> 72% FTP" in A.lit(90, "progressive").summary()
    assert "sprints every 25min" in A.lit(90, "endurance_sprints").summary()
    # a plain variant keeps the plain form
    assert A.threshold("long_reps").summary().startswith("2x 20min @ 90%,")


def test_progressive_lit_ramps():
    pcts = [s.pct for s in A.lit(90, "progressive").steps]
    assert pcts == sorted(pcts), "ramp is not monotonic"
    assert pcts[0] < pcts[-1]


def test_cadence_drills_carry_a_cadence_target():
    steps = A.lit(60, "cadence_drills").steps
    cadences = [s.cadence for s in steps]
    assert all(c is not None for c in cadences)
    assert len(set(cadences)) > 1, "the drill does not alternate"


def test_endurance_sprints_contain_real_sprints():
    steps = A.lit(90, "endurance_sprints").steps
    assert any(s.pct > 1.5 for s in steps), "the sprints exist only in prose"


def test_sit_has_no_trailing_recovery_before_the_cooldown():
    """Every SIT session used to carry one extra full recovery interval."""
    for variant in A.variant_names("sit"):
        session = A.sit(variant)
        rests = [s for s in session.steps if s.label == "full recovery"]
        assert len(rests) == A.SIT_VARIANTS[variant]["reps"] - 1, variant
        # the step before the cool-down must be work, not rest
        assert session.steps[-2].pct == A.SIT_NOMINAL_PCT, variant


@pytest.mark.parametrize("archetype", ["hiit", "threshold"])
def test_rep_count_in_summary_matches_rep_count_in_steps(archetype):
    """summary() and steps are generated separately and could disagree."""
    for variant in A.variant_names(archetype):
        session = {"hiit": A.hiit, "threshold": A.threshold}[archetype](variant)
        label = "bout" if archetype == "hiit" else "effort"
        reps = len({s.label.split("—")[0].strip() for s in session.steps
                    if s.label.startswith(label)})
        assert reps == session.params["reps"], f"{archetype}/{variant}"


# ── block expansion and its constraints ───────────────────────────────────────

def test_no_block_exceeds_the_hard_session_cap():
    for block in DEFAULT_PLAN:
        hard = sum(1 for _, role in block.pattern if role == "hard")
        assert hard <= 2, block.name


def test_block_rejects_too_many_hard_sessions():
    with pytest.raises(ValueError, match="hard sessions"):
        Block(name="bad", block_type="hiit", weeks=1,
              pattern=[(0, "hard"), (2, "hard"), (4, "hard")])


@pytest.mark.parametrize("plan_fn", [
    lambda: DEFAULT_PLAN,
    lambda: add_second_weekend_ride(DEFAULT_PLAN),
    lambda: add_lit_day(DEFAULT_PLAN),
])
def test_legs_are_never_loaded_next_to_a_long_ride(plan_fn):
    """
    The guard existed but pinned hardcoded weekdays 0 and 4, while every block schedules
    strength on 2 and 6 — so it never fired and 6 of 19 weeks put lower body on the Sunday
    after the Saturday long ride.
    """
    blocks = plan_fn()
    for block in blocks:
        long_days = {d for d, r in block.pattern if r in ("long", "long2")}
        strength_days = [d for d, r in block.pattern if r == "strength"]
        sessions = expand_plan([block], START, "t")
        by_day = {}
        for s in sessions:
            if s["archetype"] == "strength":
                by_day.setdefault(s["week_in_block"], {})[s["weekday"]] = s["variant"]
        for week, assigned in by_day.items():
            for day in strength_days:
                adjacent = long_days & {(day - 1) % 7, (day + 1) % 7}
                if adjacent:
                    assert assigned[day] == "upper", (
                        f"{block.name} week {week}: {assigned[day]} on weekday {day}, "
                        f"adjacent to a long ride")


def test_a_week_never_repeats_the_same_lift_pattern():
    for block in DEFAULT_PLAN:
        sessions = [s for s in expand_plan([block], START, "t")
                    if s["archetype"] == "strength"]
        per_week = {}
        for s in sessions:
            per_week.setdefault(s["week_in_block"], []).append(s["variant"])
        for week, variants in per_week.items():
            # two upper-body days are allowed when both abut a long ride; otherwise distinct
            if variants.count("upper") < 2:
                assert len(set(variants)) == len(variants), f"{block.name} week {week}"


def test_expansion_is_date_contiguous_across_block_boundaries():
    sessions = expand_plan(DEFAULT_PLAN, START, PLAN_NAME)
    weeks = sorted({w["week_start"] for w in weekly_rollup(sessions)})
    for earlier, later in zip(weeks, weeks[1:]):
        gap = date.fromisoformat(later) - date.fromisoformat(earlier)
        assert gap == timedelta(days=7), f"gap between {earlier} and {later}"
    assert len(weeks) == sum(b.weeks for b in DEFAULT_PLAN)


# ── the transformation helpers ────────────────────────────────────────────────

def test_second_weekend_ride_relocates_strength_instead_of_dropping_it():
    busy = add_lit_day(DEFAULT_PLAN, weekday=4)       # occupy the default target
    result = add_second_weekend_ride(busy, strength_to=4)
    for original, changed in zip(busy, result):
        before = sum(1 for _, r in original.pattern if r == "strength")
        after = sum(1 for _, r in changed.pattern if r == "strength")
        assert after == before, f"{changed.name}: strength session dropped"


def test_second_weekend_ride_refuses_rather_than_silently_doing_nothing():
    once = add_second_weekend_ride(DEFAULT_PLAN)
    with pytest.raises(ValueError, match="Sunday is already taken"):
        add_second_weekend_ride(once)


def test_sunday_ride_progresses_monotonically_across_blocks():
    """Seeding the next block with the previous block's finishing value repeated a week,
    and the recovery-week step-back silently lost a step at every boundary."""
    sessions = expand_plan(add_second_weekend_ride(DEFAULT_PLAN), START, "t")
    longs = [s["minutes"] for s in sorted(
        (x for x in sessions if x["role"] == "long2"), key=lambda x: x["date"])]
    assert longs == sorted(longs), "the second weekend ride goes backwards"
    assert longs[-1] > longs[0]


def test_transform_helpers_do_not_share_mutable_state():
    changed = add_lit_day(DEFAULT_PLAN)
    assert changed[0].hard_variants is not DEFAULT_PLAN[0].hard_variants
    assert changed[0].recovery_weeks is not DEFAULT_PLAN[0].recovery_weeks


def test_add_lit_day_honours_its_minutes_argument():
    """The parameter existed in the signature and was never read."""
    assert all(b.lit_min == 90 for b in add_lit_day(DEFAULT_PLAN, minutes=90))


# ── reporting ─────────────────────────────────────────────────────────────────

def test_intensity_split_uses_the_same_bands_as_the_analysis_layer():
    sessions = expand_plan(DEFAULT_PLAN, START, "t")
    split = intensity_split(sessions)
    assert set(split["pct"]) == {"easy", "moderate", "hard"}

    expected = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    for s in sessions:
        if s["archetype"] == "strength":
            continue
        i = s["intensity_factor"]
        key = "easy" if i < EASY_IF_CEILING else "moderate" if i < HARD_IF_FLOOR else "hard"
        expected[key] += s["minutes"]
    total = sum(expected.values())
    for band, minutes in expected.items():
        assert split["pct"][band] == pytest.approx(round(minutes / total * 100, 1))


def test_intensity_split_survives_an_empty_plan():
    split = intensity_split([])
    assert split["pct"]["easy"] is None


# ── plan weeks ────────────────────────────────────────────────────────────────

def test_plan_weeks_cover_the_race_and_share_the_session_plan_name():
    rows = plan_weeks_rows(DEFAULT_PLAN, START)
    assert rows[0]["plan_name"] == PLAN_NAME
    race = [r for r in rows if r["week_start_date"] <= "2027-06-18" <= r["week_end_date"]]
    assert race, "the plan does not reach the 2027-06-18 A race"
    assert race[0]["phase"] == "taper"


def test_plan_weeks_are_contiguous_and_numbered_without_gaps():
    rows = plan_weeks_rows(DEFAULT_PLAN, START)
    assert [r["plan_week_number"] for r in rows] == list(range(1, len(rows) + 1))
    for earlier, later in zip(rows, rows[1:]):
        assert (date.fromisoformat(later["week_start_date"])
                - date.fromisoformat(earlier["week_start_date"])) == timedelta(days=7)


def test_recovery_weeks_are_marked_rest():
    rows = plan_weeks_rows(DEFAULT_PLAN, START)
    for block_number, block in enumerate(DEFAULT_PLAN, start=1):
        in_block = [r for r in rows if r["phase_number"] == block_number]
        for row in in_block:
            expected_rest = row["phase_week_number"] in block.recovery_weeks
            assert (row["week_type"] == "rest") == expected_rest, row

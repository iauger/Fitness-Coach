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
    MAX_HARD_PER_WEEK, ROTATION, ROTATION_LOOP_FROM, Block, expand_week,
    intensity_split, pattern_for, plan_week_row, rotation_after, weekly_rollup,
)
from src.planning.constants import plan_name as configured_plan_name
from src.planning.progression import CAP_TIER, TIERS

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

def test_no_block_declares_more_hard_sessions_than_the_cap():
    for block in ROTATION:
        hard = sum(1 for _, role in block.pattern if role == "hard")
        assert hard <= MAX_HARD_PER_WEEK, block.name


def test_block_rejects_too_many_hard_sessions():
    with pytest.raises(ValueError, match="hard sessions"):
        Block(name="bad", block_type="hiit", weeks=1,
              pattern=[(0, "hard"), (2, "hard"), (4, "hard")])


def test_block_rejects_two_rides_on_one_weekday():
    """
    Two rides on a day is a generator error every time, so it stays forbidden.

    One session per weekday was the rule until ride days became configurable. At six or seven
    riding days there is no empty day left for a lift, and a ride plus a lift on one day is
    ordinary practice — so that is now allowed, and only the doubled *ride* is rejected.
    """
    with pytest.raises(ValueError, match="two rides on weekday"):
        Block(name="bad", block_type="base", weeks=1,
              pattern=[(1, "lit"), (1, "long")])


def test_a_lift_may_share_a_day_with_a_ride():
    """At seven ride days there is nowhere else for it to go."""
    block = Block(name="busy", block_type="base", weeks=1,
                  pattern=[(1, "lit"), (1, "strength"), (5, "long")])
    assert (1, "strength") in block.pattern


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_a_tier_never_adds_a_hard_session_the_block_did_not_declare(block, tier):
    """Reduction only. If a tier could add one, the cap would need re-validating per tier."""
    declared = sum(1 for _, r in block.pattern if r == "hard")
    got = sum(1 for _, r in pattern_for(block, tier) if r == "hard")
    assert got <= declared
    assert got <= TIERS[tier].hard


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_the_long_ride_is_never_dropped(block, tier):
    """It is the session the whole plan exists to protect."""
    assert any(r == "long" for _, r in pattern_for(block, tier)), f"{block.name} T{tier}"


@pytest.mark.parametrize("week_type", ["normal", "reentry"])
@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_strength_is_never_trimmed(block, tier, week_type):
    """
    Not at the floor, not in a recovery week, not on a re-entry week. Strength survived 8 of the
    21 zero-ride weeks in the reference history; it is the anchor the floor is built on.
    """
    for week in range(1, block.weeks + 1):
        sessions = expand_week(block, week, tier, START, week_type=week_type)
        assert sum(1 for s in sessions if s["archetype"] == "strength") == 2


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_legs_are_never_loaded_next_to_a_long_ride(block, tier):
    """
    The guard has been wrong twice. First it pinned hardcoded weekdays 0 and 4 while every block
    scheduled strength on 2 and 6, so it never fired and 6 of 19 weeks put lower body on the
    Sunday after the Saturday long ride. Then it read `block.pattern` — the nominal full week —
    which after tier reduction is not the week being generated.

    Parametrised over tiers for exactly that reason: the reduction is where the risk now lives.
    """
    for week in range(1, block.weeks + 1):
        for week_type in ("normal", "reentry"):
            sessions = expand_week(block, week, tier, START, week_type=week_type)
            long_days = {s["weekday"] for s in sessions if s["role"] == "long"}
            for s in sessions:
                if s["archetype"] == "strength" and s["variant"] == "lower":
                    adjacent = long_days & {(s["weekday"] - 1) % 7, (s["weekday"] + 1) % 7}
                    assert not adjacent, (
                        f"{block.name} T{tier} wk{week} {week_type}: lower body on weekday "
                        f"{s['weekday']}, adjacent to a long ride")


@pytest.mark.parametrize("tier", sorted(TIERS))
@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_a_week_never_repeats_the_same_lift_pattern(block, tier):
    for week in range(1, block.weeks + 1):
        variants = [s["variant"] for s in expand_week(block, week, tier, START)
                    if s["archetype"] == "strength"]
        # two upper-body days are allowed when both abut a long ride; otherwise distinct
        if variants.count("upper") < 2:
            assert len(set(variants)) == len(variants), f"{block.name} T{tier} wk{week}"


# ── dose comes from the tier, never from the calendar ─────────────────────────

@pytest.mark.parametrize("block", ROTATION, ids=[b.name for b in ROTATION])
def test_the_long_ride_does_not_grow_across_a_block(block):
    """
    The single most important property of the rebuild. `_long_minutes` used to be
    `long_start_min + long_step_min * (week_in_block - 1)`, so a block's seventh week asked for
    more than its sixth whether or not the first six had happened.
    """
    for tier in TIERS:
        longs = [
            next(s["minutes"] for s in expand_week(block, week, tier, START)
                 if s["role"] == "long")
            for week in range(1, block.weeks + 1)
        ]
        assert len(set(longs)) <= 2, f"{block.name} T{tier}: long ride varies {longs}"
        # the only permitted variation is the recovery-week step-down
        assert max(longs) == TIERS[tier].long_min


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_weekly_volume_lands_near_the_tier_target(tier):
    """Within one short session: the hard slot is 52-82min against a nominal 55-60."""
    for block in ROTATION:
        week = weekly_rollup(expand_week(block, 1, tier, START))[0]
        target = TIERS[tier].target_min
        assert abs(week["ride_minutes"] - target) <= 30, (
            f"{block.name} T{tier}: {week['ride_minutes']}min vs target {target}min")
        assert week["rides"] == TIERS[tier].rides


def test_a_recovery_week_drops_the_hard_session_and_shortens_the_long_ride():
    block = ROTATION[2]                                    # Threshold, recovery in 3 and 6
    normal = expand_week(block, 1, 3, START)
    recovery = expand_week(block, 3, 3, START)
    assert any(s["role"] == "hard" for s in normal)
    assert not any(s["role"] == "hard" for s in recovery)
    long_normal = next(s["minutes"] for s in normal if s["role"] == "long")
    long_recovery = next(s["minutes"] for s in recovery if s["role"] == "long")
    assert long_recovery < long_normal


def test_a_reentry_week_carries_no_hard_session_at_any_tier():
    for block in ROTATION:
        for tier in TIERS:
            sessions = expand_week(block, 1, tier, START, week_type="reentry")
            assert not any(s["role"] == "hard" for s in sessions), f"{block.name} T{tier}"


def test_the_entry_block_has_no_hard_session_at_any_tier():
    """
    "When starting from a lower baseline, the first thing to build is consistency." The entry
    block is also where a long absence restarts, so it must stay easy even at the ceiling.
    """
    consistency = ROTATION[0]
    for tier in TIERS:
        for week in range(1, consistency.weeks + 1):
            sessions = expand_week(consistency, week, tier, START)
            assert not any(s["role"] == "hard" for s in sessions)


def test_hard_variants_rotate_so_no_two_weeks_of_a_block_repeat():
    for block in ROTATION:
        if not block.hard_variants:
            continue
        variants = [
            next((s["variant"] for s in expand_week(block, week, CAP_TIER, START)
                  if s["role"] == "hard"), None)
            for week in range(1, block.weeks + 1)
        ]
        ridden = [v for v in variants if v]
        assert len(set(ridden)) == len(ridden), f"{block.name} repeats: {variants}"


# ── rotation ──────────────────────────────────────────────────────────────────

def test_the_rotation_loops_over_the_emphasis_blocks_and_never_ends():
    seen = []
    index = ROTATION_LOOP_FROM
    for _ in range(len(ROTATION) * 3):
        seen.append(index)
        index = rotation_after(index)
    assert 0 not in seen, "the entry block is only re-entered by a long absence"
    assert set(seen) == set(range(ROTATION_LOOP_FROM, len(ROTATION)))


def test_nothing_in_the_rotation_tapers():
    assert not any("taper" in b.phase for b in ROTATION)
    assert not any("taper" in b.name.lower() for b in ROTATION)


# ── reporting ─────────────────────────────────────────────────────────────────

def _every_session_at(tier: int) -> list[dict]:
    return [s for block in ROTATION for week in range(1, block.weeks + 1)
            for s in expand_week(block, week, tier, START)]


def test_intensity_split_uses_the_same_bands_as_the_analysis_layer():
    sessions = _every_session_at(CAP_TIER)
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
    assert intensity_split([])["pct"]["easy"] is None


def test_the_rotation_is_mostly_easy_riding():
    pct = intensity_split(_every_session_at(CAP_TIER))["pct"]
    assert pct["easy"] > 60, pct
    assert pct["hard"] < 5, pct


# ── plan weeks ────────────────────────────────────────────────────────────────

def test_plan_week_row_shares_the_plan_name_with_the_sessions():
    block = ROTATION[0]
    row = plan_week_row(START, block, 1, 1, 1, tier=1)
    session = expand_week(block, 1, 1, START)[0]
    assert row["plan_name"] == session["plan_name"] == configured_plan_name()


def test_plan_week_row_spans_monday_to_sunday_and_normalises_its_date():
    row = plan_week_row(date(2026, 11, 12), ROTATION[0], 1, 1, 1, tier=1)   # a Thursday
    assert row["week_start_date"] == "2026-11-09"
    assert row["week_end_date"] == "2026-11-15"


def test_a_recovery_week_is_marked_rest_and_a_reentry_week_is_marked_reentry():
    block = ROTATION[2]                                    # recovery in 3 and 6
    assert plan_week_row(START, block, 1, 1, 1, tier=1)["week_type"] == block.phase
    assert plan_week_row(START, block, 1, 3, 3, tier=1)["week_type"] == "rest"
    assert plan_week_row(START, block, 1, 1, 1, tier=1,
                         week_type="reentry")["week_type"] == "reentry"


def test_the_plan_has_no_end_date_only_a_goal():
    """
    plan_end_date carries the CX goal (2027-10-01) rather than a plan end, because the rotation
    does not end. The old DEFAULT_PLAN ran out in mid-March 2027 and left week 33 undefined.
    """
    row = plan_week_row(START, ROTATION[0], 1, 1, 1, tier=1)
    assert row["plan_end_date"] == "2027-10-01"
    assert row["plan_end_date"] > row["week_end_date"]

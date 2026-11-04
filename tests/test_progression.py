"""
Tests for the adherence gate.

Three tests, deliberately. The gate is about forty lines of logic — a five-row ladder, six lines
of comparison and twenty of branching — and an exhaustive property suite over that would be
test-theatre. What earns its place:

1. `test_replay_against_a_real_year` — the only test that checks the *design* rather than the
   code. Nothing else can catch "promotes too easily" or "re-entry never fires".
2. one table-driven transition test, covering every branch of `next_state`
3. one classification test, covering the outcome boundaries

The replay's expectations were written down before the implementation existed, so they are
falsifiable claims about the design rather than a snapshot of whatever it happens to produce. If
one fails, either the design or the expectation is wrong — and that is the finding, not a reason
to loosen the assertion.

Freeze properties ("a decided week never re-decides") are NOT here: they are enforced by the
storage layer's scoped delete, not by a pure function, so they belong with `store_horizon`.
"""

import json
import pathlib

import pytest

from src.planning.progression import (
    CAP_TIER, FLOOR_TIER, MET_FRACTION, PARTIAL_FRACTION, TIERS,
    classify_week, next_state, replay, trailing_gap,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "history_2025-09_2026-11.json"


def wk(ride_min: float, ride_days: int, strength: int = 0, start: str = "2026-01-05") -> dict:
    """A week_volume-shaped dict. Only the fields the gate reads need to be real."""
    return {
        "week_start": start, "ride_min": ride_min, "ride_days": ride_days,
        "longest_ride_min": ride_min, "strength_sessions": strength,
        "training_days": ride_days + strength, "any_activity": bool(ride_days or strength),
    }


def graded(outcome: str, tier: int = 1, habit: bool = False) -> dict:
    """A classify_week-shaped row, for driving next_state directly."""
    return {"week_start": "2026-01-05", "tier": tier, "outcome": outcome,
            "ride_min": 0.0, "ride_days": 0, "habit_held": habit}


# ── the ladder itself ─────────────────────────────────────────────────────────

def test_the_ladder_is_internally_consistent():
    """
    `rides` must equal the number of ride slots the tier actually describes, and `target_min`
    the sum of their durations. An earlier draft stated round hour values (2.75h, 3.25h, 3.75h)
    independently of the components, so three of the five tiers asked for a weekly total that
    could not be built from their own sessions — and `met` at 90% of an unreachable target is
    not a meaningful bar.
    """
    for index, tier in TIERS.items():
        assert tier.index == index
        # one long ride + one easy ride, plus the hard session when the tier has one
        assert tier.rides == 2 + tier.hard, f"T{index} rides vs slots"
        assert tier.target_min == tier.long_min + tier.lit_min + tier.hard_min * tier.hard


def test_strength_is_not_a_per_tier_dose():
    """
    There is no `Tier.strength`, on purpose.

    It used to exist, read like a knob, and was consulted by nothing: the slots come from the
    block's pattern and `pattern_for` passes them through untouched. Setting it to 1 changed
    no prescription, which a mutation test caught. The rule is enforced in `blocks.pattern_for`
    and asserted in tests/test_invariants.py against generated weeks, which is where a claim
    about what gets prescribed belongs.
    """
    assert not hasattr(TIERS[0], "strength")


def test_the_ladder_climbs_and_stays_inside_the_agreed_band():
    targets = [TIERS[i].target_min for i in sorted(TIERS)]
    assert targets == sorted(targets), "tiers must be monotonically bigger"
    assert TIERS[FLOOR_TIER].target_hours == pytest.approx(2.0, abs=0.1)
    # The ceiling is the volume actually held for 12 consecutive weeks (3.84h/wk), not an
    # aspiration, and it sits below the 5h band that preceded the one load-induced collapse.
    assert 3.8 <= TIERS[CAP_TIER].target_hours <= 4.3
    assert all(TIERS[i].long_min <= 120 for i in TIERS), "long ride holds at 2h; extend via override"
    assert all(TIERS[i].hard <= 1 for i in TIERS), "one hard session a week, every tier"


# ── classification ────────────────────────────────────────────────────────────

def test_outcome_boundaries():
    target = TIERS[1].target_min                       # 175
    rides = TIERS[1].rides                             # 3

    # exactly at the met line, with enough ride days
    assert classify_week(wk(MET_FRACTION * target, rides), 1)["outcome"] == "met"
    # a shade under
    assert classify_week(wk(MET_FRACTION * target - 1, rides), 1)["outcome"] == "partial"
    # enough volume but too few sessions — three rides means three rides
    assert classify_week(wk(target, rides - 1), 1)["outcome"] == "partial"
    # exactly at the partial line
    assert classify_week(wk(PARTIAL_FRACTION * target, 2), 1)["outcome"] == "partial"
    assert classify_week(wk(PARTIAL_FRACTION * target - 1, 2), 1)["outcome"] == "missed"
    # a week with nothing in it is always missed, never partial
    assert classify_week(wk(0, 0), 1)["outcome"] == "missed"
    assert classify_week(wk(0, 0), CAP_TIER)["outcome"] == "missed"


def test_a_week_is_graded_against_its_own_tier_not_the_current_one():
    """Same ride volume, graded at two tiers. Without this the gate stops being reproducible."""
    week = wk(TIERS[1].target_min, 3)
    assert classify_week(week, 1)["outcome"] == "met"
    assert classify_week(week, CAP_TIER)["outcome"] == "partial"


def test_strength_is_recorded_on_every_week_regardless_of_outcome():
    assert classify_week(wk(0, 0, strength=2), 1)["habit_held"] is True
    assert classify_week(wk(0, 0, strength=0), 1)["habit_held"] is False


# ── transitions ───────────────────────────────────────────────────────────────

MET, PART, MISS = "met", "partial", "missed"

TRANSITIONS = [
    # (label, outcomes oldest-first, tier in, expected tier out, expected verdict)
    # One met week climbs one rung: `met` is already a volume gate, so the climb cannot outrun
    # the riding, and a bad fortnight takes the rung straight back.
    ("a single met week advances",               [MET],                   1, 2, "advance"),
    ("a met week after a bad one still advances", [MET, MISS, MET, MET],  1, 2, "advance"),
    ("most recent partial -> hold",              [MET, MET, MET, PART],   1, 1, "hold"),
    ("most recent missed is a gap, not a hold",  [MET, MET, MET, MISS],   1, 1, "hold"),
    ("at the ceiling, a good streak holds",      [MET, MET, MET, MET],    CAP_TIER, CAP_TIER, "hold"),
    # Progressive de-ramp: one rung per evaluation once the gap passes a single week, clamped
    # at the floor. `tier_in` is the tier already in force, which during a gap is whatever the
    # earlier weeks of that same gap reduced it to.
    ("one missed week -> tier holds",            [MET, MET, MET, MISS],   2, 2, "hold"),
    ("two missed -> down one",                   [MET, MET, MISS, MISS],  3, 2, "step_back"),
    ("still missing -> down one more",           [MET, MISS, MISS, MISS], 2, 1, "step_back"),
    ("two missed at T1 -> the floor",            [MET, MET, MISS, MISS],  1, 0, "step_back"),
    # The bug this pins down: expressed as a `max(1, tier-1)` floor, missing two more weeks at
    # the floor moved the athlete UP to T1 — a promotion for not riding. Clamping instead of
    # flooring makes it unrepresentable.
    ("two missed at the floor stays there",      [MET, MET, MISS, MISS],  0, 0, "hold"),
    ("a long gap at the floor stays there",      [MISS] * 6,              0, 0, "hold"),
    ("a partial week closes a gap",              [MISS, MISS, PART, PART], 3, 3, "hold"),
]


@pytest.mark.parametrize("label,outcomes,tier_in,tier_out,verdict",
                         TRANSITIONS, ids=[t[0] for t in TRANSITIONS])
def test_transitions(label, outcomes, tier_in, tier_out, verdict):
    history = [graded(o, tier_in, habit=True) for o in outcomes]
    decision = next_state(history, tier_in)
    assert decision.tier == tier_out, f"{label}: {decision.reason}"
    assert decision.verdict == verdict, f"{label}: {decision.reason}"


def test_the_deramp_steps_one_rung_per_week_and_never_rises():
    """
    The shape of a long absence, week by week — the test that catches a cumulative formula.
    `tier - (gap - 1)` applied against an already-reduced tier compounds, and from the ceiling
    produced 4 -> 3 -> 1 -> 0, skipping a rung. It also pins the "one week costs nothing" rule:
    the first missed week holds.
    """
    tier = CAP_TIER
    path = [tier]
    history: list[dict] = []
    for _ in range(6):
        history.append(graded(MISS, tier))
        tier = next_state(history, tier).tier
        path.append(tier)

    assert path == [4, 4, 3, 2, 1, 0, 0], path
    assert path == sorted(path, reverse=True), "a gap must never raise the tier"


def test_a_gap_is_gentler_than_fitness_decay():
    """
    Deliberate, and worth pinning so nobody 'corrects' it later. Four weeks off costs roughly
    49% of CTL but only three rungs. Tracking decay exactly would land a returning athlete well
    below what they can comfortably ride, and an ignored prescription is the failure mode this
    whole design exists to prevent.
    """
    tier, history = CAP_TIER, []
    for _ in range(4):
        history.append(graded(MISS, tier))
        tier = next_state(history, tier).tier
    volume_kept = TIERS[tier].target_min / TIERS[CAP_TIER].target_min
    assert volume_kept > 0.51, "a four-week gap should cost less volume than it costs CTL"


def test_entering_the_band_requires_the_strength_habit():
    """T2 -> T3 is the one gated transition. Four met weeks without lifting must not promote."""
    met4 = [graded(MET, 2, habit=False) for _ in range(4)]
    held = next_state(met4, 2)
    assert held.tier == 2 and held.verdict == "hold"
    assert "strength" in held.reason

    with_strength = [graded(MET, 2, habit=True) for _ in range(4)]
    assert next_state(with_strength, 2).tier == 3

    # ...and it gates nowhere else: T1 -> T2 promotes without it.
    assert next_state([graded(MET, 1, habit=False) for _ in range(4)], 1).tier == 2


def test_a_gap_softens_the_week_and_restarts_the_block_appropriately():
    assert next_state([graded(MISS)], 2).week_type == "reentry"
    assert next_state([graded(MISS)], 2).rotation == "continue"
    assert next_state([graded(MISS), graded(MISS)], 2).rotation == "restart_block"
    assert next_state([graded(MISS)] * 4, 2).rotation == "restart_rotation"
    assert next_state([graded(MET)] * 4, 1).week_type == "normal"


def test_trailing_gap_counts_only_the_run_at_the_end():
    assert trailing_gap([]) == 0
    assert trailing_gap([graded(MISS), graded(MET)]) == 0
    assert trailing_gap([graded(MET), graded(MISS), graded(MISS)]) == 2
    assert trailing_gap([graded(MISS), graded(PART), graded(MISS)]) == 1


# ── the replay: does the design behave sanely against a real year? ────────────

@pytest.fixture(scope="module")
def history():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(payload["weeks"]) == 62
    return payload["weeks"]


# Gaps of two or more zero-ride weeks in the reference history. Facts about the data, not about
# the implementation — these are the weeks a re-entry must be offered.
KNOWN_GAP_STARTS = ["2026-01-26", "2026-04-13", "2026-06-01", "2026-07-06",
                    "2026-08-03", "2026-09-14"]


def test_replay_against_a_real_year(history):
    """
    A counterfactual, not a reconstruction: no gate was running over this history, so there is
    no stored tier to check against. It answers "where would this have put the athlete", seeded
    at T1 per the agreed starting tier.

    Each assertion is a claim about the design, registered before the code existed.
    """
    out = replay(history, start_tier=1)
    assert len(out) == len(history)
    tiers = [row["next_tier"] for row in out]
    by_week = {row["week_start"]: row for row in out}

    # 1 — structural: the ladder is never left
    assert all(FLOOR_TIER <= t <= CAP_TIER for t in tiers)

    # 2 — never moves more than one rung per week. This held no exception once the flat drop to
    # the floor became a progressive de-ramp; it previously needed an escape clause for it.
    for before, after in zip([1] + tiers, tiers):
        assert abs(after - before) <= 1, (before, after)

    # 3 — a real streak is rewarded. Sep-Nov 2025 ran 12 consecutive weeks at 3.84h/wk.
    streak = [by_week[w]["next_tier"] for w in by_week if w < "2025-11-24"]
    assert max(streak) >= 2, f"a 12-week streak at 3.84h/wk should promote; reached T{max(streak)}"

    # 4 — the long gaps reach the floor
    assert FLOOR_TIER in tiers, "no gap in this history ever reached the floor tier"

    # 5 — every gap of 2+ weeks is met with a re-entry week, not a continuation
    for start in KNOWN_GAP_STARTS:
        assert by_week[start]["week_type"] == "reentry", f"{start} did not trigger re-entry"

    # 6 — never promoted across a gap
    for row in out:
        if row["verdict"] == "advance":
            assert row["outcome"] == "met", f"{row['week_start']} advanced on a {row['outcome']} week"

    # 7 — ends low, because the last nine weeks average ~2.4h with three gaps in them
    assert tiers[-1] <= 2, f"ended at T{tiers[-1]} after a year averaging under 3h/wk"

    # 8 — the gate must not park at the floor while the athlete is riding. Under a trailing
    # 3-of-4 advance rule this history stalled at T0 for 19 consecutive weeks through a stretch
    # of 3.0-3.5h weeks, and across three weeks of 6.6h/5.7h/5.1h it asked for 2.0h. A
    # prescription at a third of what someone is already doing is ignored exactly like one at
    # triple, so this is the same abandonment risk as an over-ambitious plan, inverted.
    longest_floor, run = 0, 0
    for t in tiers:
        run = run + 1 if t == FLOOR_TIER else 0
        longest_floor = max(longest_floor, run)
    assert longest_floor <= 8, f"parked at the floor for {longest_floor} consecutive weeks"

    # 9 — and it must respond quickly to a strong return. The week after 6.6h was ridden off a
    # 2.0h prescription, the ask has to move.
    assert by_week["2026-01-05"]["tier"] > FLOOR_TIER, (
        "still asking for the floor the week after a 6.6h week")


def test_replay_is_deterministic(history):
    assert replay(history, start_tier=1) == replay(history, start_tier=1)


def test_replay_never_promotes_on_an_empty_history():
    assert next_state([], 1).verdict == "hold"
    assert replay([], start_tier=1) == []

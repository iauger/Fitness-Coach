"""
The weekly template, which used to be two literal lists.

`_EASY_WEEK` and `_EMPHASIS_WEEK` were written out with three ride slots each, and because
`pattern_for` only ever reduces, three was a hard ceiling for every athlete this project would
ever have. They are generated now, and the first test here is the one that made that safe to
do: at three ride days the generator must reproduce both lists exactly.

Same discipline as the config refactor's byte-identical check. This template decides what day
every session of every week lands on, for everyone.
"""

import pytest

from src.planning.blocks import (
    HARD_DAY, LONG_DAY, ROTATION, build_pattern, rotation_for, rotation_rides,
)

ORIGINAL_EASY = [(1, "lit"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")]
ORIGINAL_EMPHASIS = [(1, "hard"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")]
WIDTHS = [2, 3, 4, 5, 6, 7]


def test_three_ride_days_reproduces_the_hand_written_template():
    """
    Byte-identical, or the generalisation changed this athlete's plan as a side effect.

    Wednesday and Sunday for the lifts is not hardcoded — it is what the scoring picks out of
    the four free days a three-ride week leaves, by preferring rest days, then spacing, then
    the day after a ride. That it agrees with the hand-written answer is the result.
    """
    assert build_pattern(3, strength_days=2, hard=False) == ORIGINAL_EASY
    assert build_pattern(3, strength_days=2, hard=True) == ORIGINAL_EMPHASIS


def test_the_default_rotation_is_the_three_day_one():
    assert [b.pattern for b in rotation_for(3)] == [b.pattern for b in ROTATION]


# ── placement rules that hold at every width ─────────────────────────────────

@pytest.mark.parametrize("ride_days", WIDTHS)
def test_a_lift_never_shares_a_day_with_the_long_ride_or_the_hard_session(ride_days):
    """
    The one placement rule here that is about safety rather than tidiness.

    At six and seven ride days there are no rest days left and a lift has to double up onto a
    riding day. Which day it picks matters: stacking it on the long ride or the hard session
    turns the week's two biggest sessions into three.
    """
    pattern = build_pattern(ride_days, strength_days=2)
    strength_days = {d for d, r in pattern if r == "strength"}
    assert LONG_DAY not in strength_days
    assert HARD_DAY not in strength_days or ride_days == 1


@pytest.mark.parametrize("ride_days", WIDTHS)
def test_lifts_go_on_rest_days_while_there_are_any(ride_days):
    """
    A lift on an otherwise empty day is the cheapest place to put one. Scoring that below
    spacing put both lifts onto riding days at five ride days while Monday and Friday sat
    empty — plausible-looking, and more fatigue for nothing.
    """
    pattern = build_pattern(ride_days, strength_days=2)
    ride_days_used = {d for d, r in pattern if r != "strength"}
    strength = {d for d, r in pattern if r == "strength"}
    free = set(range(7)) - ride_days_used
    expected_on_free = min(len(free), 2)
    assert len(strength & free) == expected_on_free, (
        f"{ride_days} ride days: lifts on {sorted(strength)}, free days {sorted(free)}")


@pytest.mark.parametrize("ride_days", WIDTHS)
def test_the_long_ride_anchors_saturday_and_is_the_only_one(ride_days):
    pattern = build_pattern(ride_days, strength_days=2)
    longs = [d for d, r in pattern if r == "long"]
    assert longs == [LONG_DAY]


@pytest.mark.parametrize("ride_days", WIDTHS)
def test_the_week_declares_exactly_the_rides_asked_for(ride_days):
    pattern = build_pattern(ride_days, strength_days=2)
    rides = [d for d, r in pattern if r != "strength"]
    assert len(rides) == ride_days
    assert len(set(rides)) == ride_days, "two rides on one day"


@pytest.mark.parametrize("ride_days", WIDTHS)
def test_both_lifts_are_kept_however_wide_the_week(ride_days):
    """Strength is never trimmed — the anchor habit, at every width and every tier."""
    pattern = build_pattern(ride_days, strength_days=2)
    assert sum(1 for _, r in pattern if r == "strength") == 2


@pytest.mark.parametrize("ride_days", WIDTHS)
def test_at_most_one_hard_session_is_declared(ride_days):
    pattern = build_pattern(ride_days, strength_days=2)
    assert sum(1 for _, r in pattern if r == "hard") <= 1


def test_lifts_are_spread_rather_than_adjacent():
    """
    Two lifts on consecutive days wastes the second. Spacing is scored below rest-day
    preference but above everything else, and at three ride days it is what separates
    Wednesday-and-Sunday from Wednesday-and-Friday.
    """
    for ride_days in (2, 3, 4, 5):
        days = sorted(d for d, r in build_pattern(ride_days, strength_days=2)
                      if r == "strength")
        gap = min(days[1] - days[0], days[0] + 7 - days[1])
        assert gap >= 2, f"{ride_days} ride days: lifts on {days}"


# ── what the rotation does with it ───────────────────────────────────────────

@pytest.mark.parametrize("ride_days", WIDTHS)
def test_every_block_in_a_generated_rotation_declares_the_full_width(ride_days):
    """
    `pattern_for` only reduces, so a block declaring fewer ride slots than the top tier asks
    for hands back a short week at that tier rather than failing.
    """
    assert rotation_rides(rotation_for(ride_days)) == ride_days


def test_an_impossible_width_is_refused_rather_than_clamped():
    for bad in (0, 8, -1):
        with pytest.raises(ValueError, match="ride_days"):
            build_pattern(bad)


def test_more_lifts_than_there_are_days_for_is_refused():
    with pytest.raises(ValueError, match="without using the long ride"):
        build_pattern(3, strength_days=6)

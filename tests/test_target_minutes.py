"""
A rung's target has to be the minutes the generator actually schedules.

`Tier.target_min` counted exactly one easy ride whatever the ride count. That is right at three
rides, which is the week it was written for, so every test and the whole live plan agreed with
it — and wrong everywhere else. At five rides the gate expected 195 minutes against 315 on the
calendar; at two rides with a hard session it expected a third session that was never
scheduled. The hours shown by `status.py`, the outline and the coach all read it, so they said
one thing while the calendar said another.

Nothing caught it because every other test asserts the formula against itself. These compare it
to the generator's own output instead, which is the only independent source of what a week
contains.
"""

from datetime import date

import pytest

from src.planning.blocks import expand_week, rotation_for
from src.planning.bootstrap import DerivedTier
from src.planning.progression import Tier

SIT_MINUTES = 45      # the sprint block's real session length, so the hard part is exact


def scheduled_ride_minutes(rides: int, hard: int, long_min: int = 90, lit_min: int = 60,
                           hard_min: int = SIT_MINUTES) -> int:
    """Minutes of riding in one ordinary week, from the generator, strength excluded."""
    ladder = {
        0: Tier(0, rides=2, long_min=60, hard=0, hard_min=0, lit_min=45),
        1: Tier(1, rides=rides, long_min=long_min, hard=hard,
                hard_min=hard_min if hard else 0, lit_min=lit_min),
    }
    block = next(b for b in rotation_for(rides, 2) if b.block_type == "sit")
    week = expand_week(block, 1, 1, date(2026, 11, 2), "p", 1, tiers=ladder)
    return round(sum(s["minutes"] for s in week if s["role"] != "strength"))


@pytest.mark.parametrize("rides", range(2, 8))
@pytest.mark.parametrize("hard", [0, 1])
def test_the_target_is_what_the_generator_schedules(rides, hard):
    """
    Mutant this catches: the formula going back to `long + lit + hard_min * hard`, which agrees
    at three rides with a hard session and nowhere else — the case every other test used.
    """
    if hard and rides < 2:
        pytest.skip("a hard session needs a second ride")
    spec = Tier(1, rides=rides, long_min=90, hard=hard,
                hard_min=SIT_MINUTES if hard else 0, lit_min=60)
    assert spec.target_min == scheduled_ride_minutes(rides, hard), (
        f"{rides} rides, hard={hard}: the gate expects {spec.target_min}min and the generator "
        f"schedules {scheduled_ride_minutes(rides, hard)}min")


@pytest.mark.parametrize("rides", range(2, 8))
@pytest.mark.parametrize("hard", [0, 1])
def test_the_two_tier_definitions_agree(rides, hard):
    """
    `bootstrap.DerivedTier` mirrors `progression.Tier` because bootstrap imports nothing from
    the project. A formula duplicated across two files is a formula that gets fixed in one.
    """
    args = dict(index=1, rides=rides, long_min=90, hard=hard,
                hard_min=55 if hard else 0, lit_min=60)
    assert DerivedTier(**args).target_min == Tier(**args).target_min


def test_a_two_ride_week_with_a_hard_session_has_no_easy_ride():
    """The over-count, stated plainly: long ride plus hard session is the whole week."""
    spec = Tier(1, rides=2, long_min=90, hard=1, hard_min=55, lit_min=60)
    assert spec.target_min == 90 + 55


def test_the_hand_written_ladder_is_unchanged():
    """
    Every rung of it is three rides with a hard session, or two without — the two cases the old
    formula got right — so fixing it must not move a number the live plan already uses.
    """
    from src.planning.progression import TIERS

    assert [t.target_min for t in TIERS.values()] == [120, 190, 205, 225, 245]

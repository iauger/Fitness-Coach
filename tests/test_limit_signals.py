"""
When the plan runs into the athlete's own ride limit.

The ladder is built *inside* the limits, so a ladder at its top because of a limit is a
different situation from one at its top because the athlete has not asked for more. The first is
worth raising with them — with the options and what each costs — and the second is context.

This is the notification half of per-ride limits. The derivation half is in test_ride_limits.py.
"""

from collections import namedtuple
from datetime import date

import pytest

from src.planning.bootstrap import RideLimits
from src.planning.outgrown import (
    MIN_LADDER_AGE_WEEKS, assess, describe_options, limit_bound, unlock_options,
)
from src.planning.progression import Tier

LADDER = {
    0: Tier(0, rides=2, long_min=75, hard=0, hard_min=0, lit_min=50),
    1: Tier(1, rides=3, long_min=75, hard=1, hard_min=55, lit_min=65),
    2: Tier(2, rides=3, long_min=90, hard=1, hard_min=55, lit_min=65),
    3: Tier(3, rides=3, long_min=105, hard=1, hard_min=60, lit_min=65),
    4: Tier(4, rides=3, long_min=120, hard=1, hard_min=60, lit_min=65),
}
CAP = LADDER[4]
OLD_ENOUGH = MIN_LADDER_AGE_WEEKS + 4


def wk(ride_min: float, ride_days: int) -> dict:
    return {"week_start": "2026-11-02", "ride_min": ride_min, "ride_days": ride_days,
            "longest_ride_min": 90.0, "strength_sessions": 2,
            "training_days": max(ride_days, 2), "any_activity": ride_min > 0}


def met_at_cap(n: int) -> list[dict]:
    return [{"outcome": "met", "tier_applied": 4} for _ in range(n)]


HELD_LONG = RideLimits(long_cap=120, long_setting="plan.max_ride_min")
FLAT = [wk(CAP.target_min, 3)] * 8        # exactly what the top rung asks for: no volume signal


def check(limits, decided=(), weeks=FLAT, max_days=7, age=OLD_ENOUGH, ladder=None, **kw):
    return assess(list(weeks), ladder or LADDER, list(decided), max_days, age,
                  limits=limits, **kw)


def test_a_ladder_held_at_its_limit_is_raised_with_the_options_and_their_cost():
    """
    The notification this was built for: a month of doing everything the top rung asks, and the
    plan cannot go further because the athlete's own limit is the ceiling. Raised as a question
    with the numbers, not as an instruction. Mutant this catches: the held-and-bound branch
    removed, which falls back to "Nothing is wrong" and never mentions the limit.
    """
    found = check(HELD_LONG, met_at_cap(5))
    assert found.outgrown and found.blocked_by_config
    assert "limit_bound" in found.signals and "held_cap" in found.signals
    assert "plan.max_ride_min from 120 to 135min" in found.summary
    assert "about +15min a week" in found.summary
    assert "4th day" in found.summary, "adding a day is the other way out and should be offered"
    assert "bootstrap_plan.py --write --replace" in found.summary
    assert "their call" in found.summary


def test_reaching_the_limit_is_noted_before_it_is_a_recommendation():
    """
    Arriving at the top is not the same as being ready to leave it. The coach is told now, so
    "why has my plan stopped getting harder?" already has an answer, but nothing is pushed.
    Mutant this catches: the at-the-top branch removed.
    """
    found = check(HELD_LONG, met_at_cap(1))
    assert not found.outgrown and not found.blocked_by_config
    assert found.signals == ("at_limit",)
    assert "Nothing to do yet" in found.summary and "plan.max_ride_min" in found.summary


def test_it_says_nothing_before_they_have_reached_the_top():
    """Below T4 the limit is not what is holding anything back."""
    found = check(HELD_LONG, [{"outcome": "met", "tier_applied": 2}] * 5)
    assert not found and found.summary == ""


def test_a_limit_above_the_ladder_changes_nothing():
    """
    Mutant this catches: reporting a limit that is not binding. A 150-minute limit against a
    120-minute top rung leaves the plan free to climb, so the held-cap message is the ordinary
    one and the detail says so.
    """
    found = check(RideLimits(long_cap=150), met_at_cap(5))
    assert not found.outgrown and "Nothing is wrong" in found.summary
    assert found.detail["limit_bound"] is False and "unlock" not in found.detail


def test_without_limits_the_assessment_is_exactly_what_it_was():
    plain = assess(FLAT, LADDER, met_at_cap(5), 7, OLD_ENOUGH)
    assert plain == check(None, met_at_cap(5))
    assert plain == check(RideLimits(), met_at_cap(5))
    assert plain.detail["limit_bound"] is False


def test_every_limited_axis_has_to_be_at_its_limit():
    """
    A long ride at its limit while the easy rides still have room is not bound: the ladder can
    still climb inside the limits. Mutant this catches: `any` for `all` in `limit_bound`, which
    would tell someone to raise a limit that is not the thing in the way.
    """
    assert not limit_bound(CAP, RideLimits(long_cap=120, easy_cap=90))
    assert limit_bound(CAP, RideLimits(long_cap=120, easy_cap=65))


def test_an_easy_ride_that_stopped_short_of_its_limit_stopped_for_the_band_not_the_limit():
    """The athlete's own band is intent; raising a limit would not change where it stops."""
    assert not limit_bound(CAP, RideLimits(easy_cap=120))


def test_the_options_name_the_setting_for_each_axis():
    options = unlock_options(CAP, RideLimits(long_cap=120, easy_cap=65,
                                             long_setting="plan.day_max_ride_min.sat",
                                             easy_setting="plan.max_ride_min"), 7)
    by_kind = {o["kind"]: o for o in options}
    assert by_kind["long_ride"]["setting"] == "plan.day_max_ride_min.sat"
    assert by_kind["easy_rides"]["setting"] == "plan.max_ride_min"
    assert by_kind["long_ride"]["to"] == 135 and by_kind["easy_rides"]["to"] == 80


def test_the_gain_from_longer_easy_rides_counts_every_easy_ride():
    """
    Five rides is a long ride, a hard session and three easy rides, so raising the easy limit
    by 15 minutes buys 45. Mutant this catches: reporting a single ride's worth.
    """
    wide = Tier(4, rides=5, long_min=120, hard=1, hard_min=60, lit_min=65)
    easy = [o for o in unlock_options(wide, RideLimits(long_cap=120, easy_cap=65), 7)
            if o["kind"] == "easy_rides"][0]
    assert easy["gain_min"] == 45


def test_a_two_ride_week_has_no_easy_rides_to_lengthen():
    two = Tier(4, rides=2, long_min=120, hard=1, hard_min=60, lit_min=65)
    kinds = {o["kind"] for o in unlock_options(two, RideLimits(long_cap=120, easy_cap=65), 7)}
    assert "easy_rides" not in kinds


def test_adding_a_day_names_the_ceiling_only_when_it_is_the_thing_in_the_way():
    """
    With three rides and a ceiling of three, a fourth day needs the ceiling raised. With a
    ceiling of five it needs nothing but to be ridden. Mutant this catches: always naming the
    setting, which tells someone to raise a number that is already high enough.
    """
    at_ceiling = [o for o in unlock_options(CAP, HELD_LONG, 3, "plan.available_days")
                  if o["kind"] == "ride_day"][0]
    assert at_ceiling["setting"] == "plan.available_days"
    under = [o for o in unlock_options(CAP, HELD_LONG, 5) if o["kind"] == "ride_day"][0]
    assert under["setting"] is None
    assert "ride a 4th day, which the ceiling already allows" in describe_options([under])


def test_an_extra_day_is_worth_one_easy_ride():
    day = [o for o in unlock_options(CAP, HELD_LONG, 3) if o["kind"] == "ride_day"][0]
    assert day["gain_min"] == CAP.lit_min


@pytest.mark.parametrize("n, text", [(2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"),
                                     (12, "12th"), (21, "21st")])
def test_ordinals_are_right(n, text):
    from src.planning.outgrown import _ordinal

    assert _ordinal(n) == text


def test_riding_more_than_the_top_rung_while_a_limit_holds_it_down_says_so():
    """
    Re-deriving would rebuild the same capped ladder, so "re-derive" on its own would send them
    round a loop that changes nothing — the same reasoning as a day ceiling. Mutant this
    catches: dropping `limit_blocked`, which gives the generic advice.
    """
    heavy = [wk(CAP.target_min * 1.5, 3)] * 8
    found = check(HELD_LONG, weeks=heavy)
    assert found.outgrown and found.blocked_by_config
    assert "ride limits are what is holding the ladder down" in found.summary
    assert "plan.max_ride_min" in found.summary


def test_a_fresh_ladder_is_left_alone_even_when_bound():
    assert not check(HELD_LONG, met_at_cap(5), age=2)


def test_the_report_passes_the_configured_limits_through(monkeypatch, tmp_path):
    """
    `assess` is pure and tested above; this is the join from configuration to the coach's text.
    Mutant this catches: dropping `limits=` from the call in `report._outgrown`, after which
    every assessment behaves as if no limit were set and the coach never hears about one.
    """
    import src.analysis.report as R
    import src.config as C
    import src.planning.progression_store as PS
    import src.planning.tiers_store as TS

    path = tmp_path / "cfg.yaml"
    path.write_text("athlete:\n  name: X\nplan:\n  name: P\n  start_monday: 2026-11-02\n"
                    "  day_max_ride_min: {sat: 120}\n", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", path)
    C.reset()
    monkeypatch.setattr(TS, "weeks_for", lambda *a, **k: FLAT)
    monkeypatch.setattr(TS, "age_weeks", lambda *a, **k: OLD_ENOUGH)
    monkeypatch.setattr(PS, "rows", lambda *a, **k: met_at_cap(5))

    try:
        plan = namedtuple("Plan", "id name")(1, "P")
        found = R._outgrown(None, plan, LADDER, date(2026, 11, 6))
    finally:
        C.reset()

    assert found is not None and found["outgrown"] and found["blocked_by_config"]
    assert "limit_bound" in found["signals"]
    assert "plan.day_max_ride_min.sat from 120 to 135min" in found["summary"]


def test_easy_rides_held_down_by_the_long_ride_are_not_offered_a_longer_easy_limit():
    """
    The easy rides cannot be longer than the long ride, so when they stopped there, raising the
    *easy* limit changes nothing; the long ride's limit is the one in the way. Offering it
    would send someone to edit a line that does nothing. Mutant this catches: judging the easy
    axis against `min(easy_cap, long ride)` in `unlock_options`, which offers exactly that.
    """
    tier = Tier(4, rides=3, long_min=90, hard=1, hard_min=60, lit_min=90)
    limits = RideLimits(long_cap=90, easy_cap=120)
    assert limit_bound(tier, limits), "both axes are at the edge of what the long ride allows"
    kinds = [o["kind"] for o in unlock_options(tier, limits, 7)]
    assert "long_ride" in kinds and "easy_rides" not in kinds


def test_the_long_ride_is_only_offered_a_longer_limit_when_it_is_at_its_limit():
    """Mutant this catches: offering it unconditionally, so a long ride with 30 minutes still to
    climb is told to raise a limit it has not reached."""
    kinds = [o["kind"] for o in unlock_options(CAP, RideLimits(long_cap=150, easy_cap=65), 7)]
    assert "long_ride" not in kinds and "easy_rides" in kinds


def test_one_setting_governing_both_axes_is_one_option_with_the_combined_gain():
    """
    A plan-wide `max_ride_min` limits the long ride and the easy rides with the same line, so
    raising it lengthens both. Listing it twice read as a stutter and, worse, understated the
    gain: 30 minutes a week here, not 15. Mutant this catches: no merge, which produces the
    duplicate sentence the first version of this printed.
    """
    shared = RideLimits(long_cap=90, easy_cap=90, long_setting="plan.max_ride_min",
                        easy_setting="plan.max_ride_min")
    tier = Tier(4, rides=3, long_min=90, hard=1, hard_min=60, lit_min=90)
    options = unlock_options(tier, shared, 3)
    shared_option = [o for o in options if o["kind"] == "all_rides"]
    assert len(shared_option) == 1 and not any(o["kind"] in ("long_ride", "easy_rides")
                                               for o in options)
    assert shared_option[0]["gain_min"] == 30 and shared_option[0]["to"] == 105
    text = describe_options(options)
    assert text.count("plan.max_ride_min from 90 to 105min") == 1
    assert "about +30min a week" in text


def test_different_settings_for_the_two_axes_stay_separate_options():
    split = RideLimits(long_cap=90, easy_cap=90, long_setting="plan.day_max_ride_min.sat",
                       easy_setting="plan.max_ride_min")
    tier = Tier(4, rides=3, long_min=90, hard=1, hard_min=60, lit_min=90)
    kinds = [o["kind"] for o in unlock_options(tier, split, 3)]
    assert "long_ride" in kinds and "all_rides" not in kinds

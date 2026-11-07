"""
The gate: how a week's prescription changes based on what was actually ridden.

This replaces calendar-driven progression. The old generator computed a dose as
`start_min + step_min * (week_in_block - 1)` — week 7 of a block asked for more than week 6
whether or not weeks 1-6 happened. Against a 34% missed-week rate that produces a plan
describing a past the athlete didn't live, which is how the September 2026 plan was abandoned.

Pure functions, no database and no I/O, so the whole gate can be replayed against a year of real
history in a test. That replay is the only check that validates the *design* rather than the
code, and it has to pass before anything generates prescriptions from this.

Two deliberate exclusions:

- **Adherence is not an input.** `analysis/compliance.py` fails for reasons unrelated to
  training — strength is excluded by design, HR-only and non-power rides score wrong, an outdoor
  ride that doesn't resemble its prescription scores partial. The gate reads measured ride
  minutes from `analysis/activities.week_volume` instead, which has one failure mode: a ride
  that didn't sync.
- **Readiness is not an input.** Of eleven dropouts in the reference history, exactly one had a
  fatigue signature. 2026-04-13 began with HRV 52 and the best sleep of the spring, then four
  weeks off; 2026-09-14 began with HRV above its period average. A readiness gate has the wrong
  sensitivity and the wrong specificity for this failure mode.
"""

import math
from dataclasses import dataclass, replace

# Fractions of the week's target that define each outcome. The 0.90 band is one short session's
# worth of slack — it also absorbs the 5-8 minute wobble between hard-session variants, since the
# target below uses a nominal hard duration rather than the one the emphasis block happens to pick.
MET_FRACTION = 0.90
PARTIAL_FRACTION = 0.50

# Consecutive met weeks needed to climb one rung.
#
# One, because `met` is *already* a volume gate — it requires riding at least MET_FRACTION of
# what was asked and hitting the tier's ride-day count — and each rung above T1 is only about
# 10% more volume. Requiring several on top mostly adds latency, and the de-ramp above takes the
# rung straight back if the next fortnight goes badly, so the climb is self-limiting: you cannot
# climb faster than you actually ride.
#
# This was a trailing 3-of-4 until it was measured against the reference year, where it parked
# the gate at the floor for 19 consecutive weeks while the athlete was riding 3.0-3.5h, and
# through one stretch asked for 2.0h across three weeks of 6.6h, 5.7h and 5.1h. A prescription
# at a third of what someone is already doing gets ignored exactly like one at triple. At one
# week the same history stalls for 6 weeks instead of 19.
ADVANCE_MET_WEEKS = 1

# Entering the 3.5-4h band costs the anchor habit: strength in this many of the last few weeks.
# Required at exactly one transition. Strength has not been logged since 2026-09-02, so requiring
# it on every promotion would freeze the gate at the floor on day one. The window here is
# independent of ADVANCE_MET_WEEKS — the habit question is about the recent past, not about the
# single week that triggered the climb.
STRENGTH_GATED_TIER = 3
STRENGTH_GATED_WEEKS = 3
STRENGTH_WINDOW = 4


def gate_thresholds(spec) -> dict:
    """
    What a week has to reach to count, in minutes and rides, for one rung.

    The one place these numbers are worked out: the plan screen shows them to the athlete and
    the coach is told them, and each used to be free to word the rule its own way. The coach,
    given only "the tier moves when a completed week meets what was asked", told the athlete
    they would need to hit it three times in a row; the gate needs one week.
    """
    return {"met_min": math.ceil(MET_FRACTION * spec.target_min),
            "partial_min": math.ceil(PARTIAL_FRACTION * spec.target_min),
            "rides": spec.rides}


@dataclass(frozen=True)
class Tier:
    """
    One rung of the ladder: the shape of a week, not a dose that ramps.

    `hard_min` is a nominal planning duration, because the actual hard session is 52-82min
    depending on which variant the emphasis block rotates to. `target_min` is therefore
    approximate by a few minutes, which is exactly what MET_FRACTION's slack is for.

    There is deliberately **no strength field**. Strength is not a per-tier dose: the slots
    come from the block's own pattern and `pattern_for` passes them through untouched at
    every tier, in recovery weeks and on re-entry. A `strength: int` here read as a knob and
    was not one — a mutation test set it to 1 and no prescription changed, because nothing
    consulted it. A field stating an intent the code does not implement is worse than no
    field, so the rule lives in `blocks.pattern_for`, where it is enforced, and is asserted
    against generated weeks in tests/test_invariants.py.
    """
    index: int
    rides: int
    long_min: int
    hard: int
    hard_min: int
    lit_min: int

    @property
    def target_min(self) -> int:
        """
        The minutes this rung asks for: the long ride, the hard session if there is one, and
        an easy ride for every other ride the rung declares.

        It counted exactly one easy ride whatever the ride count, which is right for three
        rides — the week this was written for — and wrong for every other. At five rides the
        gate expected 195 minutes against 315 actually scheduled, so the hours shown on the
        status line, in the outline and to the coach did not match the calendar; at two rides
        with a hard session it expected a third session that was never scheduled. Measured
        against the generator's own output in tests/test_target_minutes.py.
        """
        easy_rides = max(0, self.rides - 1 - self.hard)
        return self.long_min + (self.hard_min * self.hard) + (self.lit_min * easy_rides)

    @property
    def target_hours(self) -> float:
        return round(self.target_min / 60, 2)


# Anchored on measured practice, not on an aspiration. The reference history's last eight
# completed weeks were 2.26h, 0, 0, 2.71h, 2.01h, 0, 2.91h, 2.07h, every ride 60-65min indoors
# and the longest ride in six months 75min. So T1 is roughly where the athlete already is, and
# the top of the ladder is the 3.84h/wk that was actually held for 12 consecutive weeks.
#
# The long ride tops out at 2h. Going beyond that is an override on a specific date, which is
# what "hold the long ride, extend it on demand" means mechanically — not a ramp.
# The easy ride is 60min from T1 up, because that is what the athlete is already adapted to —
# every ride in the last eight weeks was 60-65min. An earlier draft had it at 45-55min, which
# cut duration at the same time as the plan was cutting intensity (habitual rides sat at ~71%
# moderate, so the easy day drops from roughly 200W to 160W). Two reductions at once leaves a
# session below the athlete's own baseline on both axes, and the source is explicit that a
# session shorter than what you are adapted to gives little novel stimulus.
#
# T0 keeps a 45min easy ride: it is the minimum viable week, and its job is to be impossible to
# miss rather than to drive adaptation.
TIERS: dict[int, Tier] = {
    0: Tier(0, rides=2, long_min=75,  hard=0, hard_min=0,  lit_min=45),
    1: Tier(1, rides=3, long_min=75,  hard=1, hard_min=55, lit_min=60),
    2: Tier(2, rides=3, long_min=90,  hard=1, hard_min=55, lit_min=60),
    3: Tier(3, rides=3, long_min=105, hard=1, hard_min=60, lit_min=60),
    4: Tier(4, rides=3, long_min=120, hard=1, hard_min=60, lit_min=65),
}

FLOOR_TIER = min(TIERS)
CAP_TIER = max(TIERS)


@dataclass(frozen=True)
class Decision:
    """What the next week should look like, and why."""
    tier: int
    verdict: str            # advance | hold | step_back | reentry
    week_type: str          # normal | reentry
    rotation: str           # continue | restart_block | restart_rotation
    reason: str


def classify_week(week: dict, tier: int, tiers: dict[int, Tier] | None = None) -> dict:
    """
    Grade one elapsed week against the tier that was prescribed *for that week*.

    `week` is an `analysis.activities.week_volume` dict. The tier argument is the one stored on
    the week's own row, never the current tier — otherwise a later promotion would retroactively
    re-grade history and the gate would stop being reproducible.

    `partial` exists as a distinct outcome because it does real work: it does not advance you,
    but it also does not count toward a gap. A week where something was ridden is not a week off,
    and treating it as one would trigger a re-entry ramp the athlete doesn't need.

    `tiers` is the plan's own ladder. It defaults to the hand-written one so tests and the
    pure-replay path read as they always did; the generator never relies on that default —
    `tiers_store.require` raises before a prescription is built without a stored ladder.
    """
    spec = (tiers or TIERS)[tier]
    target = spec.target_min
    rides_wanted = spec.rides
    ride_min = week["ride_min"]

    if ride_min >= MET_FRACTION * target and week["ride_days"] >= rides_wanted:
        outcome = "met"
    elif ride_min >= PARTIAL_FRACTION * target:
        outcome = "partial"
    else:
        outcome = "missed"

    return {
        "week_start": week["week_start"],
        "tier": tier,
        "outcome": outcome,
        "ride_min": ride_min,
        "ride_days": week["ride_days"],
        # Recorded on every week, acted on at exactly one transition. See STRENGTH_GATED_TIER.
        "habit_held": week["strength_sessions"] >= 1,
    }


def trailing_gap(history: list[dict]) -> int:
    """Consecutive `missed` weeks at the end of the history. A `partial` week closes a gap."""
    gap = 0
    for row in reversed(history):
        if row["outcome"] != "missed":
            break
        gap += 1
    return gap


def _may_enter_tier(tier: int, window: list[dict]) -> tuple[bool, str]:
    """The one tier-specific precondition."""
    if tier != STRENGTH_GATED_TIER:
        return True, ""
    held = sum(1 for row in window if row["habit_held"])
    if held >= STRENGTH_GATED_WEEKS:
        return True, ""
    return False, (f"T{STRENGTH_GATED_TIER} needs strength in {STRENGTH_GATED_WEEKS} of the last "
                   f"{STRENGTH_WINDOW} weeks; {held} of {len(window)} held")


def next_state(history: list[dict], tier: int, advisories: list[dict] | None = None) -> Decision:
    """
    The gate's decision, then, if any advisory was *applied*, the one adjustment those may make.

    `advisories` come from `planning.advisories.plan_advisories` and default to none, in which case
    this is `_decide` and nothing else: with nothing applied every decision is what it was before
    the seam existed (a test pins that over the reference year). The only adjustment today is that
    an applied `hold` turns an advance into a hold. Nothing marks an advisory applied yet, and an
    action that raises load is never honoured, whatever it says.
    """
    decision = _decide(history, tier)
    holds = [a for a in advisories or () if a.get("applied") and a.get("action") == "hold"]
    if holds and decision.verdict == "advance":
        return replace(decision, tier=tier, verdict="hold",
                       reason=f"{decision.reason}; held by what you told the coach ({holds[0]['reason']})")
    return decision


def _decide(history: list[dict], tier: int) -> Decision:
    """
    Decide the next week from the decided weeks behind it.

    `history` is oldest-first, one entry per fully elapsed week, as returned by `classify_week`.
    The current week is never included — it hasn't happened yet, and grading it would read an
    unfinished week as underperformance.

    Streak and gap are both derived from the stored outcomes rather than kept as counters. A
    counter is a third thing to keep consistent with the other two; a sequence of frozen outcomes
    yields streak, gap and 3-of-4 for free and is self-auditing.
    """
    gap = trailing_gap(history)

    if gap:
        # Proportional de-ramp: one rung per missed week after the first. A single week costs
        # nothing (CTL decays only ~15% over seven days, and six of eleven dropout runs in the
        # reference history were exactly one week), and each further week steps down one more.
        #
        # This replaces what were three separate branches — hold / tier-1 / flat-drop-to-the-floor
        # — and in collapsing them removes the bug they contained. Expressed as a floor,
        # `max(1, tier - 1)` moved an athlete already at T0 *up* to T1: a promotion for not
        # riding. Clamping at the floor rather than flooring at T1 makes that unrepresentable.
        #
        # The gradation also matters more than the old cliff did. A flat drop to T0 meant a
        # month off and a fortnight off cost the same thing, and climbing back took four met
        # weeks per rung — which against a gap every 4-8 weeks parked the replay at T0 for 19
        # consecutive weeks while the athlete was actually riding 3.0-3.5h. A prescription at a
        # third of what someone is already doing gets ignored exactly like one at triple.
        #
        # Deliberately gentler than fitness decay: four weeks off costs ~49% of CTL but only
        # three rungs (T4 3.92h -> T1 2.92h, about -26%). Tracking CTL exactly would land a
        # returning athlete well below what they can comfortably ride.
        # One rung per *evaluation*, not `tier - (gap - 1)`. The gate runs every week, so `tier`
        # already carries the reductions the earlier weeks of this same gap applied; subtracting
        # the cumulative gap from it compounds. From T4 that went 4 -> 3 -> 1 -> 0, skipping T2
        # entirely, which is the opposite of a progressive de-ramp.
        stepped = tier if gap == 1 else max(FLOOR_TIER, tier - 1)
        return Decision(
            stepped,
            "hold" if stepped == tier else "step_back",
            "reentry",
            "continue" if gap == 1 else "restart_block" if gap < 4 else "restart_rotation",
            ("one week missed — tier holds, week comes back easy" if gap == 1
             else f"{gap} weeks without a ride — T{tier} -> T{stepped}"),
        )

    if not history:
        return Decision(tier, "hold", "normal", "continue", "no decided weeks yet")

    recent = history[-ADVANCE_MET_WEEKS:]
    if len(recent) < ADVANCE_MET_WEEKS or any(row["outcome"] != "met" for row in recent):
        return Decision(tier, "hold", "normal", "continue",
                        f"last week was {history[-1]['outcome']}, not met")

    if tier >= CAP_TIER:
        return Decision(CAP_TIER, "hold", "normal", "continue",
                        f"already at the T{CAP_TIER} ceiling — going higher is an explicit "
                        f"decision, not a consequence of a good streak")

    allowed, why = _may_enter_tier(tier + 1, history[-STRENGTH_WINDOW:])
    if not allowed:
        return Decision(tier, "hold", "normal", "continue", why)

    return Decision(tier + 1, "advance", "normal", "continue",
                    f"met at T{tier} — T{tier} -> T{tier + 1}")


def replay(weeks: list[dict], start_tier: int = 1,
           tiers: dict[int, Tier] | None = None) -> list[dict]:
    """
    Run the gate over a sequence of measured weeks, oldest first.

    A counterfactual, not a reconstruction: no gate was running over this history, so there is no
    stored tier to validate against. It answers "where would this have put the athlete", which is
    the only way to check the design against a real year rather than against invented fixtures.
    """
    tier = start_tier
    history: list[dict] = []
    out: list[dict] = []
    for week in weeks:
        graded = classify_week(week, tier, tiers)
        history.append(graded)
        decision = next_state(history, tier)
        out.append({**graded, "verdict": decision.verdict, "next_tier": decision.tier,
                    "week_type": decision.week_type, "rotation": decision.rotation,
                    "reason": decision.reason})
        tier = decision.tier
    return out

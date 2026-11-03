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

from dataclasses import dataclass

# Fractions of the week's target that define each outcome. The 0.90 band is one short session's
# worth of slack — it also absorbs the 5-8 minute wobble between hard-session variants, since the
# target below uses a nominal hard duration rather than the one the emphasis block happens to pick.
MET_FRACTION = 0.90
PARTIAL_FRACTION = 0.50

# Consecutive met weeks needed to advance, out of a trailing window of this size.
ADVANCE_MET = 3
ADVANCE_WINDOW = 4

# Entering the 3.5-4h band costs the anchor habit: strength in this many of the window's weeks.
# Required at exactly one transition. Strength has not been logged since 2026-09-02, so requiring
# it on every promotion would freeze the gate at the floor on day one.
STRENGTH_GATED_TIER = 3
STRENGTH_GATED_WEEKS = 3


@dataclass(frozen=True)
class Tier:
    """
    One rung of the ladder: the shape of a week, not a dose that ramps.

    `hard_min` is a nominal planning duration, because the actual hard session is 52-82min
    depending on which variant the emphasis block rotates to. `target_min` is therefore
    approximate by a few minutes, which is exactly what MET_FRACTION's slack is for.
    """
    index: int
    rides: int
    long_min: int
    hard: int
    hard_min: int
    lit_min: int
    strength: int

    @property
    def target_min(self) -> int:
        return self.long_min + self.lit_min + (self.hard_min * self.hard)

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
TIERS: dict[int, Tier] = {
    0: Tier(0, rides=2, long_min=75,  hard=0, hard_min=0,  lit_min=45, strength=2),
    1: Tier(1, rides=3, long_min=75,  hard=1, hard_min=55, lit_min=45, strength=2),
    2: Tier(2, rides=3, long_min=90,  hard=1, hard_min=55, lit_min=50, strength=2),
    3: Tier(3, rides=3, long_min=105, hard=1, hard_min=60, lit_min=50, strength=2),
    4: Tier(4, rides=3, long_min=120, hard=1, hard_min=60, lit_min=55, strength=2),
}

FLOOR_TIER = min(TIERS)
CAP_TIER = max(TIERS)
# A short gap steps down but not below here. T0 is reserved for long gaps.
SHORT_GAP_FLOOR = 1


@dataclass(frozen=True)
class Decision:
    """What the next week should look like, and why."""
    tier: int
    verdict: str            # advance | hold | step_back | reentry
    week_type: str          # normal | reentry
    rotation: str           # continue | restart_block | restart_rotation
    reason: str


def classify_week(week: dict, tier: int) -> dict:
    """
    Grade one elapsed week against the tier that was prescribed *for that week*.

    `week` is an `analysis.activities.week_volume` dict. The tier argument is the one stored on
    the week's own row, never the current tier — otherwise a later promotion would retroactively
    re-grade history and the gate would stop being reproducible.

    `partial` exists as a distinct outcome because it does real work: it does not advance you,
    but it also does not count toward a gap. A week where something was ridden is not a week off,
    and treating it as one would trigger a re-entry ramp the athlete doesn't need.
    """
    target = TIERS[tier].target_min
    rides_wanted = TIERS[tier].rides
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
    return False, (f"T{STRENGTH_GATED_TIER} needs strength in {STRENGTH_GATED_WEEKS} of "
                   f"{len(window)} weeks; {held} held")


def next_state(history: list[dict], tier: int) -> Decision:
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

    if gap >= 4:
        # The observed maximum was four weeks, and 2026 as a whole ran at 1.9h/wk. Resuming a
        # 3.5h week after a month off is what produced the abandonment this design exists to stop.
        return Decision(FLOOR_TIER, "reentry", "reentry", "restart_rotation",
                        f"{gap} weeks without a ride — restarting at the floor")

    if gap >= 2:
        # Step down, but never *up*. Written as a bare floor this read `max(1, tier - 1)`, which
        # moved someone already at T0 up to T1 — a promotion for missing two more weeks. T0 is
        # reachable here: it is where a 4+ week gap lands you, and missing two of those weeks
        # produces a fresh 2-week gap from T0.
        stepped = min(tier, max(SHORT_GAP_FLOOR, tier - 1))
        return Decision(stepped, "step_back", "reentry", "restart_block",
                        f"{gap} weeks without a ride — T{tier} -> T{stepped}, block restarts")

    if gap == 1:
        # The modal case: six of eleven dropout runs in the reference history were a single week.
        # It costs nothing — CTL over seven days decays by about 15% — so the tier holds and only
        # the week itself is softened.
        return Decision(tier, "hold", "reentry", "continue",
                        "one week missed — tier holds, week comes back easy")

    window = history[-ADVANCE_WINDOW:]
    met = sum(1 for row in window if row["outcome"] == "met")

    if len(window) < ADVANCE_WINDOW:
        return Decision(tier, "hold", "normal", "continue",
                        f"only {len(window)} of {ADVANCE_WINDOW} weeks decided so far")

    if met < ADVANCE_MET:
        return Decision(tier, "hold", "normal", "continue",
                        f"{met} of {ADVANCE_WINDOW} weeks met, need {ADVANCE_MET}")

    if history[-1]["outcome"] != "met":
        # 3-of-4 rather than 3-consecutive, because a strict run promotes essentially never
        # against real data — but the most recent week still has to be a good one, or the gate
        # would promote on the way down.
        return Decision(tier, "hold", "normal", "continue",
                        "3 of 4 met but the most recent week was not")

    if tier >= CAP_TIER:
        return Decision(CAP_TIER, "hold", "normal", "continue",
                        f"already at the T{CAP_TIER} ceiling — going higher is an explicit "
                        f"decision, not a consequence of a good streak")

    allowed, why = _may_enter_tier(tier + 1, window)
    if not allowed:
        return Decision(tier, "hold", "normal", "continue", why)

    return Decision(tier + 1, "advance", "normal", "continue",
                    f"{met} of {ADVANCE_WINDOW} weeks met — T{tier} -> T{tier + 1}")


def replay(weeks: list[dict], start_tier: int = 1) -> list[dict]:
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
        graded = classify_week(week, tier)
        history.append(graded)
        decision = next_state(history, tier)
        out.append({**graded, "verdict": decision.verdict, "next_tier": decision.tier,
                    "week_type": decision.week_type, "rotation": decision.rotation,
                    "reason": decision.reason})
        tier = decision.tier
    return out

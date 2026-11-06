"""
Deriving the tier ladder from an athlete's own synced history.

`progression.TIERS` is a hand-written ladder, anchored on one athlete's measured practice.
Everything about it is defensible for that athlete and arbitrary for anyone else: T1 at a
75-minute long ride is roughly where he was, and would be insulting to a rider doing ten hours
a week and crushing to someone starting from nothing. The roadmap's own argument applies in
both directions — *"a prescription at a third of what the athlete is already doing is ignored
exactly like one at triple"*.

So the ladder moves onto the athlete's scale. Pure functions here, no database and no I/O, for
the same reason `progression.py` is: the derivation can then be replayed against a frozen year
of real history in a test, which is the only check that validates the *design* rather than the
code.

**What is derived and what is not.**

Derived from history: the long ride, the easy ride, the ride-day count, and where the bottom
of the ladder sits. Those are the numbers that describe a particular body.

Not derived, and deliberately: the number of rungs, one hard session per week at every tier
above the floor, no hard session at the floor, and strength never trimmed. Those are design
constraints from the rebuild. They do not vary by athlete, and a config that could move them
would be a config that could break the plan's reason for existing.

From config: `sustainable_band_hours` and `long_ride_ceiling_min` — **intent, not numbers.**
They say where the athlete wants the ladder to top out, not what each rung contains. The
derivation then checks whether the shape it found can actually reach that intent and says so
when it cannot, because that disagreement is information rather than an error.

**Why medians and not means.** A single 5-hour week after a month off would drag a mean
upward and put the whole ladder above anything sustainable, which is precisely the failure
being designed against. Every statistic here is a median or a windowed median.
"""

import statistics
from dataclasses import dataclass

# How far back "where they are now" looks. Long enough to span a gap and the climb out of it,
# short enough that a year-old block does not set today's entry rung.
RECENT_WEEKS = 12

# The window over which volume has to be *held* to count as sustained. Eight weeks because the
# reference history's two real streaks were 12 and 9 weeks, so a shorter window would promote a
# good fortnight into a ceiling, and a longer one would find neither streak.
SUSTAINED_WEEKS = 8

# Rungs, floor to cap. Not configurable: the gate's step-back rule is "one rung per week", so
# the rung count is what sets how fast a de-ramp reaches the floor. Five rungs means a month
# off lands at the floor, which is the behaviour the re-entry design was validated against.
RUNGS = 5

# Durations round to these, because a prescription reading "68min" implies a precision that a
# median of eleven weeks does not have. The long ride's step is the *preferred* granularity and
# drops to the next one down when the range between floor and cap cannot hold five distinct
# rungs at it — see `_long_sequence`, and the dead rung that made it necessary.
LONG_STEPS = (15, 10, 5)
EASY_STEP = 5

# Floors below which a session stops being worth prescribing.
MIN_LONG = 45
MIN_EASY = 30

# The nominal hard session, in minutes. Not derived: the archetype library's sessions run
# 52-82min depending on which variant the rotation picks, so this is a planning placeholder
# that `MET_FRACTION`'s slack absorbs. Deriving it would imply a control the library does not
# offer.
HARD_MIN_LOW = 55
HARD_MIN_HIGH = 60

# The floor tier's easy ride, as a fraction of the others'. The floor's job is to be impossible
# to miss rather than to drive adaptation.
FLOOR_EASY_FRACTION = 0.75

# How far the derived top may land from the configured band before it is worth saying so.
BAND_TOLERANCE = 0.25


class BootstrapError(Exception):
    """Not enough history to derive a ladder, with a message saying how much is missing."""


# The least history that produces a ladder rather than a guess. Four ridden weeks inside the
# recent window: fewer than that and the median is one or two weeks wearing a statistic's hat.
MIN_RIDDEN_WEEKS = 4


@dataclass(frozen=True)
class Observed:
    """
    What the history says, before any ladder is built from it.

    Kept as its own value because it is what `derived_from` records and what makes a ladder
    explainable later — "T3 asks for a 105-minute long ride because your sustained best was
    104" is an answer; "T3 is 105" is not.
    """
    weeks_seen: int
    ridden_weeks: int
    window_start: str
    window_end: str
    recent_volume_min: float          # median weekly ride minutes, recent ridden weeks
    recent_long_min: float            # median longest ride, recent ridden weeks
    recent_session_min: float         # median length of an individual ride, recent
    recent_ride_days: float           # median ride days per ridden week, recent
    sustained_volume_min: float       # best held weekly volume over SUSTAINED_WEEKS
    sustained_long_min: float         # median longest ride inside that same window
    sustained_window_start: str

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class DerivedTier:
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


@dataclass(frozen=True)
class Ladder:
    tiers: tuple[DerivedTier, ...]
    observed: Observed
    notes: tuple[str, ...]

    @property
    def floor(self) -> DerivedTier:
        return self.tiers[0]

    @property
    def cap(self) -> DerivedTier:
        return self.tiers[-1]


# ── reading the history ───────────────────────────────────────────────────────

def _median(values, default: float = 0.0) -> float:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else default


def _best_sustained_window(weeks: list[dict]) -> tuple[int, float]:
    """
    The window of `SUSTAINED_WEEKS` whose *median* weekly volume is highest.

    Median rather than total, and **zero weeks are included**, which is the point. A window
    holding 4h, 4h, 0, 4h, 0, 4h, 0, 4h has a median of 4h by total and 4h by mean-of-ridden,
    but its median including zeros is 4h too — while a window of 5h, 0, 0, 0, 0, 0, 0, 0 has a
    median of 0. Sustained means held, not reached.
    """
    if len(weeks) < SUSTAINED_WEEKS:
        return 0, _median([w["ride_min"] for w in weeks])

    best_i, best = 0, -1.0
    for i in range(len(weeks) - SUSTAINED_WEEKS + 1):
        window = weeks[i:i + SUSTAINED_WEEKS]
        score = _median([w["ride_min"] for w in window])
        if score > best:
            best_i, best = i, score
    return best_i, best


def observe(weeks: list[dict]) -> Observed:
    """
    Read a list of `analysis.activities.week_volume` dicts into the statistics a ladder needs.

    `weeks` must be in chronological order and must include zero-ride weeks — `week_volume`
    zero-fills for exactly this reason. A caller that filtered them out would be handing this
    function a history in which the athlete never missed a week, which is the one thing the
    reference history is most emphatic is untrue.
    """
    if not weeks:
        raise BootstrapError(
            "no synced weeks at all. Run scripts/fetch_history.py first — the ladder is "
            "derived from your own riding, so there is nothing to derive it from yet.")

    recent = weeks[-RECENT_WEEKS:]
    recent_ridden = [w for w in recent if w["ride_min"] > 0]
    if len(recent_ridden) < MIN_RIDDEN_WEEKS:
        raise BootstrapError(
            f"only {len(recent_ridden)} of the last {len(recent)} weeks have any ride in them, "
            f"and {MIN_RIDDEN_WEEKS} are needed before a median means anything. Sync more "
            f"history, or ride for a few weeks and bootstrap then — a ladder derived from two "
            f"weeks is a guess wearing a statistic's hat.")

    best_i, sustained_volume = _best_sustained_window(weeks)
    window = weeks[best_i:best_i + SUSTAINED_WEEKS] or weeks
    window_ridden = [w for w in window if w["ride_min"] > 0] or window

    # Median length of one ride, not of one week. ride_min / ride_days per week, then the
    # median of those — so a week of 3x60 and a week of 1x180 do not look alike.
    session_lengths = [w["ride_min"] / w["ride_days"] for w in recent_ridden if w["ride_days"]]

    return Observed(
        weeks_seen=len(weeks),
        ridden_weeks=sum(1 for w in weeks if w["ride_min"] > 0),
        window_start=weeks[0]["week_start"],
        window_end=weeks[-1]["week_start"],
        recent_volume_min=round(_median([w["ride_min"] for w in recent_ridden]), 1),
        recent_long_min=round(_median([w["longest_ride_min"] for w in recent_ridden]), 1),
        recent_session_min=round(_median(session_lengths), 1),
        recent_ride_days=round(_median([w["ride_days"] for w in recent_ridden]), 1),
        sustained_volume_min=round(sustained_volume, 1),
        sustained_long_min=round(_median([w["longest_ride_min"] for w in window_ridden]), 1),
        sustained_window_start=window[0]["week_start"],
    )


# ── building the ladder ───────────────────────────────────────────────────────

def _round_to(value: float, step: int, minimum: int) -> int:
    """
    Round half *up*, not to even.

    `round()` is banker's rounding, and it was not a nitpick here: at a 15-minute step the
    second and third rungs landed on 86.25 and 97.5, which round to 90 and — because 6.5 goes
    to 6 — also 90. Two identical rungs, so the gate could advance a tier and prescribe exactly
    the same week. It raised no error and the ladder looked plausible.
    """
    return max(minimum, int((value / step) + 0.5) * step)


def _long_sequence(floor: int, cap: int) -> tuple[list[int], int]:
    """
    Long-ride durations for every rung, from `floor` to `cap`.

    **The floor shares the first rung's long ride.** T0 differs from T1 by carrying no hard
    session, which is already a whole session of difference — forcing the long ride to climb
    there as well made the first step 68% of a week's volume, and compressed everything above
    it into a narrower range than the athlete's own history supports. So the climb happens
    across the `RUNGS - 1` tiers above the floor, and T0 takes T1's long ride.

    This was found by deriving against the reference history and comparing to the ladder a
    human chose from the same data: the hand-written ladder has T0 and T1 both at 75min, and
    the reason is the hard session. Imposing a strict increase everywhere was a rule invented
    here rather than read off the design.

    The step drops from the preferred 15 minutes when the range cannot hold that many distinct
    values — three gaps across 45 minutes fits exactly; a narrower range does not. Returned,
    because a ladder built at 5-minute granularity says something about how little room the
    athlete has, and that belongs in the notes rather than buried in arithmetic.
    """
    span = cap - floor
    climbing = RUNGS - 1                      # tiers above the floor
    gaps = climbing - 1
    step = LONG_STEPS[-1]
    for candidate in LONG_STEPS:
        if gaps and span >= gaps * candidate:
            step = candidate
            break

    values = [_round_to(floor + (span * i / gaps if gaps else 0), step, MIN_LONG)
              for i in range(climbing)]
    values[0], values[-1] = floor, cap
    for i in range(1, climbing - 1):          # repair ties; rounding can still produce them
        values[i] = max(values[i], values[i - 1] + step)
    return [values[0]] + values, step         # T0 shares T1's long ride


@dataclass(frozen=True)
class RideLimits:
    """
    The longest ride each kind of day can hold, as the derivation needs to see it. `None` is no
    limit, and a `RideLimits()` with every field `None` is how everyone who sets none is derived
    — exactly as before.

    The setting names ride along because a refusal that says "raise your limit" without saying
    which setting sends the athlete hunting; the long ride and the easy rides can be limited by
    different lines of the same file.
    """
    long_cap: int | None = None
    easy_cap: int | None = None          # the tightest day any easy ride can land on
    hard_cap: int | None = None
    long_setting: str = "plan.max_ride_min"
    easy_setting: str = "plan.max_ride_min"

    @property
    def declared(self) -> bool:
        return any(v is not None for v in (self.long_cap, self.easy_cap, self.hard_cap))


# Gaps between the rungs that climb: T1 to T4. T0 differs from T1 by the hard session, not by
# a bigger session, so it is not one of them.
GAPS = RUNGS - 2

# The smallest step a rung may take, on either axis. Below this two rungs are the same week to
# anyone but a stopwatch.
MIN_STEP = 5


def _round_down(value: float, step: int, minimum: int) -> int:
    return max(minimum, int(value // step) * step)


def _spread(start: int, end: int, gaps: int) -> list[int]:
    """
    `gaps + 1` values from `start` to `end`, evenly spaced and rounded to the step.

    Rounding can land two neighbours on the same value or push one past the end, so the values
    are then forced strictly increasing from the front and strictly inside the range from the
    back. The caller guarantees the room — `end - start >= gaps * MIN_STEP` — so this never has
    to choose between the two.
    """
    if gaps == 0:
        return [start]
    values = [_round_to(start + (end - start) * i / gaps, MIN_STEP, 0) for i in range(gaps + 1)]
    values[0], values[-1] = start, end
    for i in range(1, gaps):
        values[i] = max(values[i], values[i - 1] + MIN_STEP)
    for i in range(gaps - 1, 0, -1):
        values[i] = min(values[i], values[i + 1] - MIN_STEP)
    return values


def _climb(long_floor: int, long_cap: int, easy: int,
           easy_max: int | None) -> tuple[list[int], list[int], int] | None:
    """
    Long-ride and easy-ride length for every rung, T0 to T4, or None when there is no room.

    **One axis per rung.** The ladder's rule is that a rung changes one thing, because two
    changes at once make a rung impossible to attribute. So the long ride climbs for some
    number of rungs and then the easy ride does, never both in one step.

    With room on the long ride alone — three 15-minute steps — this is the ladder it has always
    been, and it is also the only path for anyone who has declared no limit: the easy ride grows
    only when a limit has capped the long ride and left the easy rides room, which is the case
    this exists for. A 90-minute limit on a three-day week is 4.5 hours of capacity; climbing
    the long ride alone reaches 3.6 and reports "no further" with most of the week unused.

    The step is the long ride's own `_long_sequence` when it can carry the whole climb, because
    that function has the dead-rung history and should not be duplicated.
    """
    head_long = long_cap - long_floor
    head_easy = max(0, (easy_max or easy) - easy)

    if head_long >= GAPS * LONG_STEPS[0] or head_easy == 0:
        if head_long < GAPS * MIN_STEP:
            return None
        longs, step = _long_sequence(long_floor, long_cap)
        return longs, [easy] * RUNGS, step

    best = None
    for g_long in range(GAPS + 1):
        g_easy = GAPS - g_long
        if g_long and head_long < MIN_STEP * g_long:
            continue
        if g_easy and head_easy < MIN_STEP * g_easy:
            continue
        ideal = GAPS * head_long / (head_long + head_easy)
        score = (abs(g_long - ideal), -g_long)
        if best is None or score < best[0]:
            best = (score, g_long)
    if best is None:
        return None

    g_long = best[1]
    g_easy = GAPS - g_long
    long_path = _spread(long_floor, long_cap, g_long)
    easy_path = _spread(easy, easy_max, g_easy) if g_easy else [easy]
    longs = [long_path[min(k, g_long)] for k in range(GAPS + 1)]
    easies = [easy_path[max(0, k - g_long)] for k in range(GAPS + 1)]
    return [longs[0]] + longs, [easy] + easies, 0


def derive(weeks: list[dict], band_hours: tuple[float, float],
           long_ceiling_min: int, max_rides: int,
           ceiling_setting: str = "plan.max_ride_days",
           limits_for=None) -> Ladder:
    """
    A ladder on this athlete's scale, from their history and their stated intent.

    The shape: the floor is roughly where they are now with the hard session removed, the cap
    reaches for the configured band without exceeding what the long-ride ceiling allows, and
    the rungs between interpolate the long ride. Volume climbs because the long ride climbs,
    which is what "hold the long ride, extend it on demand" means in reverse — the easy ride
    and the ride-day count stay put, because changing three things per rung makes a rung
    impossible to attribute.

    `max_rides` is the athlete's configured availability (`plan.max_ride_days`), passed in
    rather than read here so this module keeps no imports but stdlib. It caps; it does not set.
    History says how many days you ride, config says how many you *could* — so someone who sets
    7 and rides 3 gets a three-ride ladder, and the ceiling only bites when their riding grows
    into it.

    `limits_for(rides)` returns the `RideLimits` for a week of that many rides. A callable and
    not a value because which days the long ride and the easy rides land on depends on how many
    rides there are, and that is only known here, from the history. Limits are derived
    *inside*: the ladder climbs to what they allow and stops, so every rung is a week the
    calendar can deliver. When they leave no room to climb at all this refuses, naming the
    setting and the amount, rather than building a ladder whose rungs are the same week.

    This used to be the rotation's own ride-slot count, which was three for everybody because
    the weekly template was two hardcoded lists. `blocks.rotation_for` now builds the template
    around the ladder instead, so the ceiling is the athlete's week rather than the code's.
    """
    obs = observe(weeks)
    notes: list[str] = []

    wanted_rides = max(2, int(round(obs.recent_ride_days)))
    rides_cap = min(wanted_rides, max_rides)
    rides_floor = max(2, rides_cap - 1)
    if wanted_rides > max_rides:
        notes.append(
            f"you ride {wanted_rides} days in a typical week but {ceiling_setting} allows "
            f"{max_rides}, so the ladder tops out there. Raise it if those days are really "
            f"available; otherwise the extra day stays yours and the plan will not claim it.")

    limits = limits_for(rides_cap) if limits_for else RideLimits()
    if limits.long_cap is not None and limits.long_cap < MIN_LONG:
        raise BootstrapError(
            f"{limits.long_setting} limits the long ride's day to {limits.long_cap}min, which is "
            f"shorter than the {MIN_LONG}min the shortest long ride is. Raise it to at least "
            f"{MIN_LONG}, or move the long ride to a day with more room.")

    # The easy ride is the athlete's habitual session length. Cutting it at the same time as
    # the plan cuts intensity leaves a session below their own baseline on both axes, and a
    # session shorter than what someone is adapted to gives little novel stimulus.
    easy = _round_to(obs.recent_session_min, EASY_STEP, MIN_EASY)
    easy_max = None
    if limits.easy_cap is not None:
        easy_limit = _round_down(limits.easy_cap, MIN_STEP, MIN_EASY)
        if easy > easy_limit:
            easy = easy_limit
            notes.append(
                f"you usually ride {obs.recent_session_min:.0f}min but {limits.easy_setting} "
                f"limits the easy rides to {limits.easy_cap}min, so the ladder starts there.")
        easy_max = easy_limit

    # The long ride starts from what they are already doing and climbs toward the configured
    # ceiling. `sustained_long` is the floor of the cap rather than the cap itself: having once
    # held a 90min long ride is a reason not to *stop* there, not a reason to be capped there.
    long_floor = _round_to(obs.recent_long_min, LONG_STEPS[0], MIN_LONG)
    long_cap = _round_to(
        min(float(long_ceiling_min), max(obs.sustained_long_min, obs.recent_long_min) * 1.6),
        LONG_STEPS[0], MIN_LONG)
    binding = "plan.long_ride_ceiling_min"
    if limits.long_cap is not None:
        long_limit = _round_down(limits.long_cap, MIN_STEP, MIN_LONG)
        if long_cap > long_limit:
            long_cap, binding = long_limit, limits.long_setting
        if long_floor > long_limit:
            long_floor = long_limit
            notes.append(
                f"you have been riding {obs.recent_long_min:.0f}min long rides, longer than "
                f"{limits.long_setting} allows ({limits.long_cap}min), so the ladder starts at "
                f"the limit.")
    if long_cap < long_floor:
        long_cap = long_floor
        if binding == "plan.long_ride_ceiling_min":
            notes.append(
                f"the long ride does not climb: the configured ceiling ({long_ceiling_min}min) "
                f"is at or below the {obs.recent_long_min:.0f}min you already ride.")
    easy_rides = max(0, rides_cap - 2)           # the week less its long ride and hard session
    band_limited = False
    if easy_max is not None and easy_rides == 0:
        easy_max = None                           # two-ride weeks have no easy ride above T0
    if easy_max is not None:
        easy_max = min(easy_max, long_cap)
        # The easy rides stop lengthening when the top rung reaches the band the athlete asked
        # for. Without this a generous limit lets them grow past it: the band is intent, and a
        # ladder that overshoots it by a third of an hour has not respected what was asked.
        hard_top = HARD_MIN_HIGH if limits.hard_cap is None else min(HARD_MIN_HIGH, limits.hard_cap)
        band_top = _round_down((band_hours[1] * 60 - long_cap - hard_top) / easy_rides,
                               EASY_STEP, MIN_EASY)
        if band_top < easy_max:
            easy_max, band_limited = max(easy, band_top), True

    climbed = _climb(long_floor, long_cap, easy, easy_max)
    if climbed is None:
        head = (long_cap - long_floor) + (max(0, easy_max - easy) if easy_max else 0)
        start = (f"the ladder would start at a {long_floor}min long ride and {easy}min easy "
                 f"rides (what you ride now, "
                 f"{obs.recent_long_min:.0f}min and {obs.recent_session_min:.0f}min, held to "
                 f"your limits)")
        if band_limited:
            raise BootstrapError(
                f"there is no room to climb inside your band: {start}, and your configured "
                f"band tops out at {band_hours[1]}h, which that already reaches. Raise "
                f"plan.sustainable_band_hours if you want the ladder to go higher.")
        raise BootstrapError(
            f"there is no room to climb: {start}, with a ceiling of {long_cap}min on the long "
            f"ride{f' and {easy_max}min on easy rides' if easy_max else ''} — {head} minutes of "
            f"headroom, where {GAPS} distinct steps need at least {GAPS * MIN_STEP}. "
            f"Raise {binding} to at least {long_cap + GAPS * MIN_STEP - head}min and the "
            f"ladder can start climbing, or ride more days "
            f"(plan.max_ride_days / plan.available_days).")
    longs, easies, step = climbed
    if step and step != LONG_STEPS[0] and long_cap > long_floor:
        notes.append(
            f"rungs step the long ride by {step}min rather than {LONG_STEPS[0]}: there are only "
            f"{long_cap - long_floor}min between the {long_floor}min you ride now and the "
            f"{long_cap}min ceiling, which does not divide into {RUNGS} rungs any coarser.")
    if limits.declared and any(e != easy for e in easies[1:]):
        notes.append(
            f"the long ride reaches its {long_cap}min limit, so the easy rides lengthen from "
            f"{easy}min to {easies[-1]}min from there. One thing changes per rung, never both.")

    tiers = []
    for i in range(RUNGS):
        at_floor = i == 0
        hard_nominal = HARD_MIN_LOW if i < RUNGS - 2 else HARD_MIN_HIGH
        if limits.hard_cap is not None:
            hard_nominal = min(hard_nominal, limits.hard_cap)
        tiers.append(DerivedTier(
            index=i,
            rides=rides_floor if at_floor else rides_cap,
            long_min=longs[i],
            hard=0 if at_floor else 1,
            hard_min=0 if at_floor else hard_nominal,
            lit_min=(_round_to(easy * FLOOR_EASY_FRACTION, EASY_STEP, MIN_EASY)
                     if at_floor else easies[i]),
        ))

    ladder = Ladder(tuple(tiers), obs,
                    tuple(notes + _check_band(tiers, band_hours, obs, binding)))
    _validate(ladder)
    return ladder


def _check_band(tiers: list[DerivedTier], band_hours: tuple[float, float],
                obs: Observed, binding: str = "plan.long_ride_ceiling_min") -> list[str]:
    """
    Say so when the derived shape cannot reach the configured intent.

    Not an error. The athlete said where they want to get to and the history said what shape
    their riding has; those disagreeing is a real fact about them, and the useful response is
    to name it rather than to quietly bend one to fit the other. Bending the ladder up to meet
    an aspiration is exactly how the old plan reached 8.4h/week.
    """
    low, high = band_hours
    cap = tiers[-1].target_hours
    out = []
    if cap < low - BAND_TOLERANCE:
        out.append(
            f"the top rung reaches {cap}h against a configured band of {low}-{high}h. Your "
            f"recent riding ({obs.recent_volume_min / 60:.1f}h/wk, longest ride "
            f"{obs.recent_long_min:.0f}min) does not support that band yet; raise "
            f"{binding} to climb further, or leave it and re-bootstrap once "
            f"the gate has moved you up.")
    elif cap > high + BAND_TOLERANCE:
        out.append(
            f"the top rung reaches {cap}h, above the configured band of {low}-{high}h. That is "
            f"the long-ride ceiling driving it; lower {binding} to bring the "
            f"cap down.")
    return out


def _validate(ladder: Ladder) -> None:
    """
    The properties a usable ladder has, checked against itself.

    Every one of these is a way a derivation can produce plausible-looking nonsense: a rung
    that asks for less than the one below it, a floor that already contains a hard session, a
    cap the athlete could not reach in a year. Raising here means a bad ladder never reaches
    the `tiers` table, where it would silently become a hundred wrong prescriptions.
    """
    tiers = ladder.tiers
    if len(tiers) != RUNGS:
        raise BootstrapError(f"expected {RUNGS} rungs, built {len(tiers)}")
    if tiers[0].hard != 0:
        raise BootstrapError("the floor tier must carry no hard session")
    if any(t.hard != 1 for t in tiers[1:]):
        raise BootstrapError("every tier above the floor carries exactly one hard session")

    for lower, upper in zip(tiers, tiers[1:]):
        # Strictly greater, not merely not-less. Two rungs asking for the same week is a rung
        # the gate can advance into with nothing changing — it reads as progress, prescribes
        # the identical sessions, and costs a week before the next real step. A 15-minute
        # rounding step produced exactly that on the first run of this derivation.
        #
        # The one exception is the step that *introduces* the hard session. For someone who
        # rides two days a week the hard session replaces the easy ride rather than joining it,
        # so that step lowers the minutes a little — and it is still a real step, because the
        # week now contains a session the one below did not. Counting minutes alone is what
        # let the old formula hide this: it counted a third ride that two-day weeks never had.
        introduces_hard = lower.hard == 0 and upper.hard > 0
        if upper.target_min <= lower.target_min and not introduces_hard:
            raise BootstrapError(
                f"T{upper.index} ({upper.target_min}min) does not ask for more than "
                f"T{lower.index} ({lower.target_min}min); every rung must be a real step")
        # Non-decreasing, not strictly increasing: T0 and T1 legitimately share a long ride,
        # because what separates them is the hard session.
        if upper.long_min < lower.long_min:
            raise BootstrapError(
                f"T{upper.index}'s long ride ({upper.long_min}min) is shorter than "
                f"T{lower.index}'s ({lower.long_min}min)")

    if tiers[-1].target_min <= tiers[0].target_min:
        raise BootstrapError(
            "the cap asks for no more than the floor, so the gate would have nowhere to climb")


def summarise(ladder: Ladder) -> str:
    """One readable block: what was observed, what was built, and anything worth saying."""
    o = ladder.observed
    lines = [
        f"  from {o.window_start} to {o.window_end} — {o.ridden_weeks} of {o.weeks_seen} "
        f"weeks with a ride",
        f"  recent      {o.recent_volume_min / 60:.1f}h/wk over {o.recent_ride_days:.0f} days, "
        f"longest {o.recent_long_min:.0f}min, typical session {o.recent_session_min:.0f}min",
        f"  sustained   {o.sustained_volume_min / 60:.1f}h/wk held from "
        f"{o.sustained_window_start}, longest {o.sustained_long_min:.0f}min",
        "",
        f"  {'tier':<5}{'rides':>6}{'long':>7}{'hard':>7}{'easy':>7}{'week':>9}",
    ]
    for t in ladder.tiers:
        hard = f"{t.hard}x{t.hard_min}" if t.hard else "-"
        lines.append(f"  {'T' + str(t.index):<5}{t.rides:>6}{str(t.long_min) + 'min':>7}"
                     f"{hard:>7}{str(t.lit_min) + 'min':>7}{str(t.target_hours) + 'h':>9}")
    for note in ladder.notes:
        lines += ["", f"  note: {note}"]
    return "\n".join(lines)

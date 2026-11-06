"""
Noticing when an athlete has ridden past their own ladder.

The ladder is derived once, from the history that existed at the time, and then it is fixed.
That is correct — a prescription that re-derived itself every week would jitter, and the gate's
recorded decisions were graded against the rungs in force when each week closed. But it leaves
a loop open, and the loop is self-reinforcing: the plan asks for three days, so three days get
ridden, so a re-derivation finds three days. `plan.max_ride_days` can be seven and never once
be reached by the system's own operation.

The escape hatch already works — `week_volume` counts every ride whether or not it matched a
prescription, so someone who starts riding a fourth day on their own *is* visible in the
history. What was missing is that nothing looked, and nothing said anything. Someone could sit
at the cap for a year riding five days a week and keep being handed three.

**This only ever suggests.** It does not re-derive, and it must not: re-bootstrapping changes
what the tiers already recorded in `plan_progression` meant, which is a decision for the
athlete. The output is a sentence for the coach to raise at a check-in.

Pure functions, no database, for the same reason `progression.py` and `bootstrap.py` are: the
thresholds here are judgements, and a judgement that can only be exercised against a live
database cannot be argued with.
"""

from dataclasses import dataclass

from src.planning.bootstrap import LONG_STEPS, MIN_STEP, RideLimits

# How much recent history the signals read. Six weeks spans a block and a recovery week, so a
# single strong fortnight cannot trigger this on its own.
WINDOW_WEEKS = 6

# Consecutive met weeks at the cap before holding the ceiling is worth remarking on. Four is a
# month of completing everything the plan asked for at the top rung.
HELD_CAP_WEEKS = 4

# Volume has to exceed the cap by this much to count. The cap's target is already approximate
# by a few minutes — `hard_min` is a nominal planning duration — so a 10% margin is the
# smallest difference that means anything.
VOLUME_MARGIN = 1.10

# A freshly derived ladder is left alone regardless. Re-bootstrapping every few weeks would
# make `tier_applied` mean a different thing in every row.
MIN_LADDER_AGE_WEEKS = 8


@dataclass(frozen=True)
class Assessment:
    """What the recent history says about the ladder it was ridden under."""
    outgrown: bool                  # the ladder is too small for this athlete now
    blocked_by_config: bool         # ...and re-deriving will not help until config changes
    signals: tuple[str, ...]        # machine-readable names of what fired
    summary: str                    # one line, written for the coach to raise
    detail: dict                    # the numbers behind it

    def __bool__(self) -> bool:
        return self.outgrown


NOTHING = Assessment(False, False, (), "", {})


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return (ordered[mid] if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2)


# How far one suggestion raises a limit. The long ride's preferred rung step, because a limit
# raised by less than a step would not change the ladder the derivation builds.
LIMIT_STEP = LONG_STEPS[0]


def _at(value: int, limit: int | None) -> bool:
    """Whether `value` is as high as `limit` lets it go. Limits are rounded down to 5 minutes."""
    return limit is not None and value >= limit - (MIN_STEP - 1)


def limit_bound(cap, limits: RideLimits | None) -> bool:
    """
    Whether the top rung stopped climbing because of a ride limit, as opposed to the band or the
    long-ride ceiling.

    Every axis that *has* a limit must be at it. A long ride at its limit with the easy rides
    still lengthening is not bound, because the ladder can still climb inside the limits; and
    an easy ride that stopped short of its limit stopped because the band was reached, which is
    the athlete's own intent and not something a raised limit would change.
    """
    if limits is None or not limits.declared:
        return False
    easy_rides = max(0, cap.rides - 1 - cap.hard)
    axes = []
    if limits.long_cap is not None:
        axes.append(_at(cap.long_min, limits.long_cap))
    if limits.easy_cap is not None and easy_rides:
        axes.append(_at(cap.lit_min, min(limits.easy_cap, cap.long_min)))
    return bool(axes) and all(axes)


def unlock_options(cap, limits: RideLimits | None, max_ride_days: int,
                   ceiling_setting: str = "plan.max_ride_days") -> list[dict]:
    """
    The smallest changes that would let the ladder climb again, each with what it buys.

    Offered as options with their cost rather than as an instruction: raising a limit is the
    athlete's call, and "you need 105 minutes" is a different sentence from "raising Saturday
    from 90 to 105 adds a quarter of an hour a week, or a fourth day adds about an hour and a
    half". They also have different prices — one is a longer Saturday and the other is a
    different week — so the choice is theirs to make with both numbers in front of them.
    """
    options: list[dict] = []
    easy_rides = max(0, cap.rides - 1 - cap.hard)
    if limits is not None and limits.long_cap is not None and _at(cap.long_min, limits.long_cap):
        options.append({"kind": "long_ride", "setting": limits.long_setting,
                        "from": limits.long_cap, "to": limits.long_cap + LIMIT_STEP,
                        "gain_min": LIMIT_STEP})
    if (limits is not None and limits.easy_cap is not None and easy_rides
            and _at(cap.lit_min, limits.easy_cap)):
        options.append({"kind": "easy_rides", "setting": limits.easy_setting,
                        "from": limits.easy_cap, "to": limits.easy_cap + LIMIT_STEP,
                        "gain_min": LIMIT_STEP * easy_rides})
    # One setting raising both axes is one option, not two. With a plan-wide `max_ride_min` the
    # long ride and the easy rides share a line of the config, and listing it twice both read as
    # a stutter and understated what raising it buys: the long ride's 15 minutes *and* the easy
    # rides', which is what the athlete would actually get.
    long_opt = next((o for o in options if o["kind"] == "long_ride"), None)
    easy_opt = next((o for o in options if o["kind"] == "easy_rides"), None)
    if (long_opt and easy_opt and long_opt["setting"] == easy_opt["setting"]
            and long_opt["from"] == easy_opt["from"] and long_opt["to"] == easy_opt["to"]):
        options = [o for o in options if o not in (long_opt, easy_opt)]
        options.insert(0, {"kind": "all_rides", "setting": long_opt["setting"],
                           "from": long_opt["from"], "to": long_opt["to"],
                           "gain_min": long_opt["gain_min"] + easy_opt["gain_min"]})

    options.append({
        "kind": "ride_day",
        # None when the ceiling already allows another day: nothing to change but to ride it,
        # and the plan follows once the history shows it.
        "setting": ceiling_setting if cap.rides >= max_ride_days else None,
        "from": cap.rides, "to": cap.rides + 1,
        "gain_min": cap.lit_min,
    })
    return options


def _ordinal(n: int) -> str:
    suffix = "th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_options(options: list[dict]) -> str:
    """The options as prose for the coach to put to the athlete."""
    parts = []
    for o in options:
        # Minutes for small gains. `0.25h` formats as "0.2h", and "a quarter of an hour" is
        # what the number means.
        gain = (f"about +{o['gain_min']}min a week" if o["gain_min"] < 60
                else f"about +{o['gain_min'] / 60:.1f}h a week")
        if o["kind"] in ("long_ride", "all_rides"):
            parts.append(f"raise {o['setting']} from {o['from']} to {o['to']}min ({gain})")
        elif o["kind"] == "easy_rides":
            parts.append(f"raise {o['setting']} from {o['from']} to {o['to']}min ({gain})")
        elif o["setting"]:
            parts.append(f"allow a {_ordinal(o['to'])} ride day by raising "
                         f"{o['setting']} ({gain})")
        else:
            parts.append(f"ride a {_ordinal(o['to'])} day, which the ceiling already "
                         f"allows and the plan will follow once it is in the history ({gain})")
    return "; or ".join(parts)


def assess(weeks: list[dict], ladder: dict, decided: list[dict],
           max_ride_days: int, ladder_age_weeks: float | None = None,
           ceiling_setting: str = "plan.max_ride_days",
           limits: RideLimits | None = None) -> Assessment:
    """
    Whether this athlete's riding has moved past the ladder they are being prescribed from.

    `weeks` is recent `week_volume` dicts, oldest first and including zero weeks. `ladder` is
    the plan's stored tiers. `decided` is recent `plan_progression` rows, oldest first.

    Three signals, and they do different jobs:

    - **more ride days than the cap asks for** — the headline. Availability has changed, and
      it is the one thing a re-derivation cannot discover on its own, because the plan never
      asks for the extra day.
    - **more volume than the cap asks for** — the ladder is too short, whatever the day count.
    - **holding the cap** — context, never a recommendation on its own. Sitting at the top rung
      is a perfectly good steady state; the design says going higher is a decision, not a
      consequence of a good streak. It is reported so the coach can ask, not so the plan can
      push.
    """
    if not weeks or not ladder:
        return NOTHING
    if ladder_age_weeks is not None and ladder_age_weeks < MIN_LADDER_AGE_WEEKS:
        return NOTHING

    cap = ladder[max(ladder)]
    recent = weeks[-WINDOW_WEEKS:]
    ridden = [w for w in recent if w["ride_min"] > 0]
    if len(ridden) < 3:
        # Too thin to mean anything. Someone returning from a gap is the opposite of someone
        # outgrowing their ladder, and the same scarcity of data describes both.
        return NOTHING

    ride_days = _median([w["ride_days"] for w in ridden])
    volume = _median([w["ride_min"] for w in ridden])

    signals: list[str] = []
    if ride_days > cap.rides:
        signals.append("ride_days")
    if volume > cap.target_min * VOLUME_MARGIN:
        signals.append("volume")

    held = 0
    for row in reversed(decided):
        if row.get("outcome") == "met" and row.get("tier_applied") == max(ladder):
            held += 1
        else:
            break
    if held >= HELD_CAP_WEEKS:
        signals.append("held_cap")

    detail = {
        "recent_ride_days": round(ride_days, 1),
        "ladder_ride_days": cap.rides,
        "recent_volume_min": round(volume, 1),
        "ladder_cap_min": cap.target_min,
        "met_weeks_at_cap": held,
        "max_ride_days": max_ride_days,
        "weeks_considered": len(ridden),
    }

    # Whether the ladder stopped climbing because of a ride limit, and what would let it climb
    # again. Computed whatever else fires, so the numbers are always in the detail.
    bound = limit_bound(cap, limits)
    options = unlock_options(cap, limits, max_ride_days, ceiling_setting) if bound else []
    detail["limit_bound"] = bound
    if options:
        detail["unlock"] = options

    at_top = bool(decided) and decided[-1].get("tier_applied") == max(ladder)
    actionable = [s for s in signals if s != "held_cap"]

    if not actionable:
        if "held_cap" in signals and bound:
            # The case this exists for. They have done everything the top rung asks for, for a
            # month, and the plan has nowhere to go because *their own limit* is the ceiling —
            # so unlike an ordinary held cap this is worth raising, and with the options and
            # what each costs rather than as a bare "do you want to go higher".
            return Assessment(
                True, True, tuple(signals) + ("limit_bound",),
                f"They have held T{max(ladder)} for {held} straight weeks, and it is as far as "
                f"their ride limits let the plan climb, so it cannot progress them further as "
                f"it stands. Worth asking whether it still feels hard. If it does not, the ways "
                f"to keep progressing are: {describe_options(options)}. Either way the ladder "
                f"then needs re-deriving: `python scripts/bootstrap_plan.py --write --replace`. "
                f"It is their call, and staying here is a perfectly good answer.",
                detail)
        if "held_cap" in signals:
            return Assessment(
                False, False, tuple(signals),
                f"Held T{max(ladder)} — the top of the ladder — for {held} straight weeks, "
                f"riding what it asks for. Nothing is wrong; going higher is a decision rather "
                f"than something the gate does on its own, and it is worth asking whether they "
                f"want to.",
                detail)
        if bound and at_top:
            # Not yet a recommendation: they have only just arrived. Said now so the coach is
            # not surprised later, and so "why has my plan stopped getting harder?" has an
            # answer already written down.
            return Assessment(
                False, False, ("at_limit",),
                f"They are at T{max(ladder)}, which is as far as their ride limits let the plan "
                f"climb. Nothing to do yet — when it stops being hard, the ways to keep "
                f"progressing are: {describe_options(options)}.",
                detail)
        return NOTHING

    # Re-deriving reads history and then clamps to configured availability, so when the day
    # count is already at the ceiling the ladder is not the binding constraint — the config is,
    # and saying "re-bootstrap" would send them round a loop that changes nothing.
    blocked = "ride_days" in actionable and cap.rides >= max_ride_days
    # The same reasoning for a limit: riding more than the top rung while that rung is held down
    # by a ride limit, so re-deriving would rebuild the same capped ladder.
    limit_blocked = bound and "volume" in actionable
    blocked = blocked or limit_blocked

    parts = []
    if "ride_days" in actionable:
        parts.append(
            f"riding {ride_days:.0f} days a week against a ladder built for {cap.rides}")
    if "volume" in actionable:
        parts.append(
            f"averaging {volume / 60:.1f}h against a top rung of {cap.target_min / 60:.1f}h")
    observation = " and ".join(parts)

    if limit_blocked:
        advice = (f"Their ride limits are what is holding the ladder down, so re-deriving would "
                  f"rebuild the same one. To let it climb: {describe_options(options)}. Then "
                  f"re-derive: `python scripts/bootstrap_plan.py --write --replace`.")
    elif blocked and "volume" in actionable:
        # Both fired: re-deriving lengthens the rungs but cannot widen the week, so saying only
        # "re-bootstrap" would quietly leave half the problem, and saying only "raise the
        # ceiling" would leave the other half.
        advice = (f"Re-deriving will lengthen the rungs, but it cannot add the extra day — "
                  f"{ceiling_setting} allows {max_ride_days}, which is already what the ladder "
                  f"uses. If those days are genuinely available, raise that first, then "
                  f"re-derive: `python scripts/bootstrap_plan.py --write --replace`.")
    elif blocked:
        advice = (f"Re-deriving will not change that on its own — {ceiling_setting} allows "
                  f"{max_ride_days}, which is already what the ladder uses. If those extra "
                  f"days are genuinely available, that setting is what needs raising first.")
    else:
        advice = ("Their ladder was derived from an earlier version of this history and is "
                  "now behind them. Worth suggesting they re-derive it: "
                  "`python scripts/bootstrap_plan.py --write --replace`.")

    return Assessment(
        True, blocked, tuple(signals),
        f"They have outgrown their tier ladder: {observation}"
        f"{f', and have held the top rung for {held} weeks' if 'held_cap' in signals else ''}. "
        f"{advice}",
        detail)

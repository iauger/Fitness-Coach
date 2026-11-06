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


def assess(weeks: list[dict], ladder: dict, decided: list[dict],
           max_ride_days: int, ladder_age_weeks: float | None = None) -> Assessment:
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

    actionable = [s for s in signals if s != "held_cap"]
    if not actionable:
        if "held_cap" in signals:
            return Assessment(
                False, False, tuple(signals),
                f"Held T{max(ladder)} — the top of the ladder — for {held} straight weeks, "
                f"riding what it asks for. Nothing is wrong; going higher is a decision rather "
                f"than something the gate does on its own, and it is worth asking whether they "
                f"want to.",
                detail)
        return NOTHING

    # Re-deriving reads history and then clamps to configured availability, so when the day
    # count is already at the ceiling the ladder is not the binding constraint — the config is,
    # and saying "re-bootstrap" would send them round a loop that changes nothing.
    blocked = "ride_days" in actionable and cap.rides >= max_ride_days

    parts = []
    if "ride_days" in actionable:
        parts.append(
            f"riding {ride_days:.0f} days a week against a ladder built for {cap.rides}")
    if "volume" in actionable:
        parts.append(
            f"averaging {volume / 60:.1f}h against a top rung of {cap.target_min / 60:.1f}h")
    observation = " and ".join(parts)

    if blocked and "volume" in actionable:
        # Both fired: re-deriving lengthens the rungs but cannot widen the week, so saying only
        # "re-bootstrap" would quietly leave half the problem, and saying only "raise the
        # ceiling" would leave the other half.
        advice = (f"Re-deriving will lengthen the rungs, but it cannot add the extra day — "
                  f"plan.max_ride_days is {max_ride_days}, which is already what the ladder "
                  f"uses. If those days are genuinely available, raise that first, then "
                  f"re-derive: `python scripts/bootstrap_plan.py --write --replace`.")
    elif blocked:
        advice = (f"Re-deriving will not change that on its own — plan.max_ride_days is "
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

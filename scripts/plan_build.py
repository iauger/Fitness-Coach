"""
Show what the rolling plan asks for, and optionally write it to Zwift.

    python scripts/plan_build.py                      # this week, at the tier the gate derives
    python scripts/plan_build.py --weeks 3            # the committed horizon
    python scripts/plan_build.py --watts              # resolve percentages against tracked FTP
    python scripts/plan_build.py --steps              # every interval of every session
    python scripts/plan_build.py --zwo 2026-11-03     # write that day's session to Zwift

The tier is *derived*, not chosen: measured weekly volume goes through the gate in
`planning/progression.py`, which decides what next week should look like. Pass `--tier` to
override it for a what-if.

This writes nothing and pushes nothing — `scripts/sync_now.py` is what stores a horizon and
puts it on your calendar. What this does is answer "what am I riding tomorrow", and let you
try a tier or a date without recording anything.
"""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.analysis.dates import monday_of
from src.athlete.profile import get_metric
from src.db.schema import read_connection, write_connection
from src.planning.blocks import (
    ROTATION, check_fit, expand_week, intensity_split, plan_rotation, rotation_after,
    weekly_rollup,
)
from src.planning import plans, tiers_store
from src.planning.constants import plan_name as configured_plan_name
from src.planning.progression import TIERS
from src.planning.progression_store import current_state
from src.clock import today_local

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _strength_slots(rotation: list, block_index: int) -> int:
    """How many strength days the block actually schedules.

    Counted from the pattern rather than read off the tier: strength is not a per-tier dose,
    and a tier field claiming otherwise was removed for saying something the code did not do.
    The rotation is passed in because it is now built around the plan's own ride-day count.
    """
    return sum(1 for _, role in rotation[block_index].pattern if role == "strength")


def derive_state(today: date, start_tier: int = 1, persist: bool = True) -> dict:
    """
    Where the athlete actually is: tier, rotation position and week type.

    Read from `plan_progression` via `planning/progression_store`, which decides each week once
    when it closes and never re-decides it. This used to replay 26 weeks on every invocation and
    guess the rotation position, so the answer could move under a session already on the
    calendar — a Thursday ride syncing late would re-grade the week behind it.

    `persist=False` for an `--as-of` what-if: computing where a future date would put you must
    not file that date's decisions as though they had happened.

    Returns the plan's stored ladder and rotation alongside the state, because everything
    printed below has to describe the plan that is actually generated. Reading the module
    constants here would print 3.42h beside a calendar showing 3.5h — the same two-sources
    split that had to be fixed in the API and the coaching report, and missed here because
    this script was not checked when those were.
    """
    opener = write_connection if persist else read_connection
    with opener() as conn:
        plan = plans.by_name(conn, configured_plan_name())
        ladder = tiers_store.load(conn, plan.id) if plan else None
        state = current_state(conn, today, start_tier=start_tier, persist=persist,
                              tiers=ladder)
    ladder = ladder or dict(TIERS)
    rotation = plan_rotation(max(t.rides for t in ladder.values()))
    check_fit(rotation, ladder)
    return {**state, "_ladder": ladder, "_rotation": rotation}


def _watts(session: dict) -> str:
    """Resolve a prescription's percentages to watts using the FTP tracked for that date."""
    ftp = get_metric("ftp", as_of=session["date"])
    p = session["params"]
    if not ftp or session["archetype"] == "strength" or "pct" not in p:
        return ""
    return f"   -> {int(p['pct'] * ftp)}W"


def print_week(sessions: list[dict], watts: bool = False, steps: bool = False) -> None:
    roll = weekly_rollup(sessions)[0]
    tag = "  (recovery week)" if roll["recovery"] else ""
    wt = sessions[0].get("week_type", "normal")
    tag += "  (re-entry week)" if wt == "reentry" else ""
    print(f"\n  week of {roll['week_start']} — {roll['block']}, T{roll['tier']}{tag}")
    for s in sessions:
        print(f"    {DAYS[s['weekday']]}  {s['role']:<9} {s['prescription']}"
              f"{_watts(s) if watts else ''}")
        if steps and s["archetype"] != "strength":
            for st in _session_steps(s):
                print(f"           {st}")
    print(f"    {'':<4} {'':<9} {roll['rides']} rides, {roll['ride_minutes']/60:.2f}h, "
          f"{roll['tss']:.0f} TSS")


def _session_steps(session: dict) -> list[str]:
    """Rebuild the step list for display. The params carry everything needed."""
    from src.planning import archetypes as A
    builder = {"lit": lambda: A.lit(int(session["minutes"]), session["variant"]),
               "sit": lambda: A.sit(session["variant"]),
               "hiit": lambda: A.hiit(session["variant"]),
               "threshold": lambda: A.threshold(session["variant"])}[session["archetype"]]
    out = []
    for st in builder().steps:
        mins = st.seconds / 60
        dur = f"{st.seconds}s" if st.seconds < 60 else f"{mins:.1f}min"
        cad = f" @ {st.cadence}rpm" if st.cadence else ""
        out.append(f"{dur:>8}  {int(st.pct * 100):>4}%{cad}   {st.label}")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=date.fromisoformat, default=None,
                   help="pretend today is this date")
    p.add_argument("--weeks", type=int, default=1, help="how many weeks to show")
    p.add_argument("--tier", type=int, default=None, help="override the derived tier")
    p.add_argument("--block", type=int, default=None, help="override the rotation block index")
    p.add_argument("--watts", action="store_true")
    p.add_argument("--steps", action="store_true", help="list every interval")
    p.add_argument("--plan-name", default=None,
                   help="defaults to plan.name from the configuration file")
    args = p.parse_args()

    plan_name = args.plan_name or configured_plan_name()
    today = args.as_of or today_local()
    # A pretend date must not file its decisions as real ones — see derive_state.
    persisted = args.as_of is None
    state = derive_state(today, persist=persisted)
    ladder, rotation = state["_ladder"], state["_rotation"]
    tier = args.tier if args.tier is not None else state["tier"]
    block_index = args.block if args.block is not None else state["block_index"]

    print(f"\n  {plan_name} — as of {today} ({DAYS[today.weekday()]})")
    if state["recent"]:
        if not persisted:
            label = "simulated weeks — nothing was recorded"
        elif state["seeded_from"] == "stored":
            label = "recent decided weeks"
        else:
            label = "weeks before the plan started (counterfactual — no gate was running)"
        print(f"\n  {label}:")
        for row in state["recent"]:
            print(f"    {row['week_start']}  {row['ride_min']/60:>5.2f}h  "
                  f"{row['ride_days']} rides  vs T{row['tier']}  {row['outcome']}"
                  f"{'  (strength held)' if row['habit_held'] else ''}")
    print(f"\n  gate: T{state['tier']} — {state['reason']}")
    through = state["decided_through"]
    n = state["decided_weeks"]
    if not persisted:
        print(f"        {n} week{'' if n == 1 else 's'} simulated through {through} "
              f"— a what-if, not written")
    elif through:
        print(f"        decided through {through} ({n} week{'' if n == 1 else 's'} on record)")
    else:
        print(f"        nothing on record yet — the plan's first week closes "
              f"{(monday_of(today) + timedelta(days=6)).isoformat()}")
    if args.tier is not None:
        print(f"  (overridden to T{tier})")
    spec = ladder[tier]
    print(f"  T{tier}: {spec.rides} rides, {spec.target_hours}h, long {spec.long_min}min, "
          f"{spec.hard} hard, strength {_strength_slots(rotation, block_index)}")

    week_type = state["week_type"]
    all_sessions = []
    bi, wib = block_index, state["week_in_block"]
    for offset in range(args.weeks):
        monday = monday_of(today) + timedelta(weeks=offset)
        block = rotation[bi]
        sessions = expand_week(block, wib, tier, monday, plan_name, bi,
                               week_type if offset == 0 else "normal", tiers=ladder)
        all_sessions += sessions
        print_week(sessions, watts=args.watts, steps=args.steps)
        wib += 1
        if wib > block.weeks:
            bi, wib = rotation_after(bi), 1

    split = intensity_split(all_sessions)
    pct, n_hard = split["pct"], split["hard_sessions"]
    weeks_shown = max(1, args.weeks)
    print(f"\n  {n_hard} hard session{'' if n_hard == 1 else 's'} across {weeks_shown} week"
          f"{'' if weeks_shown == 1 else 's'}  (the source caps this at 2 per week)")
    print(f"  ride time by band: {pct['easy']}% easy / {pct['moderate']}% moderate / "
          f"{pct['hard']}% hard")

    print("\n  preview only — python scripts/sync_now.py stores and pushes a plan.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

This does not yet write to the database or push to intervals.icu — that is the roller (step 4)
and the export path (steps 5-7). What it does do is answer "what am I riding tomorrow".
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
    PLAN_NAME, ROTATION, expand_week, intensity_split, rotation_after, weekly_rollup,
)
from src.planning.progression import TIERS
from src.planning.progression_store import current_state

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Verified live: the OneDrive copy of this folder is stale from 2021, so do not write there.
ZWIFT_DIR = r"C:\Users\iauge\Documents\Zwift\Workouts\393440"


def derive_state(today: date, start_tier: int = 1, persist: bool = True) -> dict:
    """
    Where the athlete actually is: tier, rotation position and week type.

    Read from `plan_progression` via `planning/progression_store`, which decides each week once
    when it closes and never re-decides it. This used to replay 26 weeks on every invocation and
    guess the rotation position, so the answer could move under a session already on the
    calendar — a Thursday ride syncing late would re-grade the week behind it.

    `persist=False` for an `--as-of` what-if: computing where a future date would put you must
    not file that date's decisions as though they had happened.
    """
    opener = write_connection if persist else read_connection
    with opener() as conn:
        return current_state(conn, today, start_tier=start_tier, persist=persist)


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
    p.add_argument("--plan-name", default=PLAN_NAME)
    p.add_argument("--zwo", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="write that day's ride to the Zwift workouts folder")
    p.add_argument("--zwo-dir", default=ZWIFT_DIR,
                   help="where .zwo files go (default: the live Zwift folder)")
    args = p.parse_args()

    today = args.as_of or date.today()
    # A pretend date must not file its decisions as real ones — see derive_state.
    persisted = args.as_of is None
    state = derive_state(today, persist=persisted)
    tier = args.tier if args.tier is not None else state["tier"]
    block_index = args.block if args.block is not None else state["block_index"]

    print(f"\n  {args.plan_name} — as of {today} ({DAYS[today.weekday()]})")
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
    spec = TIERS[tier]
    print(f"  T{tier}: {spec.rides} rides, {spec.target_hours}h, long {spec.long_min}min, "
          f"{spec.hard} hard, strength {spec.strength}")

    week_type = state["week_type"]
    all_sessions = []
    bi, wib = block_index, state["week_in_block"]
    for offset in range(args.weeks):
        monday = monday_of(today) + timedelta(weeks=offset)
        block = ROTATION[bi]
        sessions = expand_week(block, wib, tier, monday, args.plan_name, bi,
                               week_type if offset == 0 else "normal")
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

    if args.zwo:
        rc = write_zwo(all_sessions, args.zwo, Path(args.zwo_dir))
        if rc:
            return rc
    else:
        print("\n  preview only — the roller (step 4) is what stores and pushes this.\n")
    return 0


def write_zwo(sessions: list[dict], on: date, out_dir: Path) -> int:
    """Write one day's ride to the Zwift workouts folder."""
    from src.planning import archetypes as A
    from src.planning.zwo import safe_filename, session_name, to_zwo

    want = [s for s in sessions if s["date"] == on.isoformat()
            and s["archetype"] != "strength"]
    if not want:
        print(f"\n  nothing to ride on {on} ({DAYS[on.weekday()]}) in the weeks shown.")
        return 1
    if not out_dir.is_dir():
        print(f"\n  {out_dir} does not exist — pass --zwo-dir.")
        return 1

    for s in want:
        session = {"lit": lambda: A.lit(int(s["minutes"]), s["variant"]),
                   "sit": lambda: A.sit(s["variant"]),
                   "hiit": lambda: A.hiit(s["variant"]),
                   "threshold": lambda: A.threshold(s["variant"])}[s["archetype"]]()
        name = session_name(session)
        desc = (f"{s['prescription']}  |  {s['block_name']} wk{s['week_in_block']}, "
                f"T{s['tier']}  |  {s['plan_name']}")
        path = out_dir / safe_filename(f"{on.isoformat()}_{name}")
        path.write_text(to_zwo(session, name, desc), encoding="utf-8")
        print(f"\n  wrote {path}")
        print(f"    {name} — {s['prescription']}")
        print(f"    {len(session.steps)} steps. Restart Zwift if it is already running; it "
              f"reads this folder at launch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

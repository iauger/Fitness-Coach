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

from src.analysis.activities import week_volume
from src.analysis.dates import monday_of, mondays_between
from src.athlete.profile import get_metric
from src.db.schema import get_connection
from src.planning.blocks import (
    PLAN_NAME, ROTATION, ROTATION_LOOP_FROM, expand_week, intensity_split, rotation_after,
    weekly_rollup,
)
from src.planning.progression import TIERS, classify_week, next_state

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Verified live: the OneDrive copy of this folder is stale from 2021, so do not write there.
ZWIFT_DIR = r"C:\Users\iauge\Documents\Zwift\Workouts\393440"

# How far back to look when deriving the current tier. Long enough to cover the longest gap in
# the reference history plus the climb out of it.
LOOKBACK_WEEKS = 26


def derive_state(today: date, start_tier: int = 1) -> dict:
    """
    Replay the gate over recent measured weeks to find where the athlete actually is.

    Only *fully elapsed* weeks are graded — the current week has not happened yet, and grading it
    would read an unfinished week as underperformance.
    """
    conn = get_connection()
    try:
        this_monday = monday_of(today)
        first = this_monday - timedelta(weeks=LOOKBACK_WEEKS)
        weeks = [week_volume(conn, m) for m in mondays_between(first, this_monday)
                 if m < this_monday]
    finally:
        conn.close()

    tier = start_tier
    history: list[dict] = []
    decision = None
    for week in weeks:
        history.append(classify_week(week, tier))
        decision = next_state(history, tier)
        tier = decision.tier

    # Where in the rotation to start. The Consistency block is the entry point for a cold start
    # and where a long absence restarts; it carries no hard session at any tier. An athlete who
    # has been riding — six of the last eight weeks here — is returning from something rather
    # than from nothing, and four weeks of pure endurance is its own abandonment risk.
    # Block position is derived here only because nothing persists it yet; the roller (step 4)
    # takes this over.
    block_index = 0 if (decision and decision.rotation == "restart_rotation") \
        else ROTATION_LOOP_FROM

    return {"tier": tier, "decision": decision, "block_index": block_index, "weeks": weeks,
            "recent": history[-6:] if history else []}


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
    state = derive_state(today)
    tier = args.tier if args.tier is not None else state["tier"]
    block_index = args.block if args.block is not None else state["block_index"]

    print(f"\n  {args.plan_name} — as of {today} ({DAYS[today.weekday()]})")
    if state["recent"]:
        print("\n  recent completed weeks:")
        for row in state["recent"]:
            print(f"    {row['week_start']}  {row['ride_min']/60:>5.2f}h  "
                  f"{row['ride_days']} rides  {row['outcome']}"
                  f"{'  (strength held)' if row['habit_held'] else ''}")
    if state["decision"]:
        print(f"\n  gate: T{state['tier']} — {state['decision'].reason}")
    if args.tier is not None:
        print(f"  (overridden to T{tier})")
    spec = TIERS[tier]
    print(f"  T{tier}: {spec.rides} rides, {spec.target_hours}h, long {spec.long_min}min, "
          f"{spec.hard} hard, strength {spec.strength}")

    week_type = "reentry" if (state["decision"] and
                              state["decision"].week_type == "reentry") else "normal"
    all_sessions = []
    bi, wib = block_index, 1
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

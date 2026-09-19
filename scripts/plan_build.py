"""
Build a training plan from the archetype library and preview or store it.

    python scripts/plan_build.py                        # weekly summary of the proposed plan
    python scripts/plan_build.py --detail               # every session, dated
    python scripts/plan_build.py --week 2026-09-21      # one week's prescriptions
    python scripts/plan_build.py --big-weekend          # Sunday endurance behind the Sat long
    python scripts/plan_build.py --four-rides           # add a midweek fourth ride
    python scripts/plan_build.py --start 2026-09-21 --write   # store to prescribed_sessions

Prescriptions are doses, not workouts: "3x15min @ 93%, 73min, 77 TSS". You match each to a
TrainerRoad library workout, which keeps TR as the execution layer while this owns the
structure. Nothing here writes to `planned_workouts` — that stays the TrainerRoad calendar's,
so compliance and the cycle reviews keep working off an independent source.

Watts are deliberately absent. Everything is a percentage of FTP, resolved at ride time, because
the tracked number is currently uncertain (TR prescribes against ~231, athlete_profile says 238,
intervals' eFTP says 260). Ramp test first; the plan does not need regenerating when it changes.
"""

import sys
import json
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.athlete.profile import get_metric
from src.db.schema import get_connection, migrate_db
from src.planning.blocks import (
    DEFAULT_PLAN, add_lit_day, add_second_weekend_ride, expand_plan, intensity_split,
    weekly_rollup,
)

PLAN_NAME = "Sustainable 2026-27"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def store(sessions: list[dict], plan_name: str) -> int:
    migrate_db()
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM prescribed_sessions WHERE plan_name = ?", (plan_name,))
        conn.executemany("""
            INSERT INTO prescribed_sessions (
                plan_name, date, weekday, block_index, block_name, week_in_block,
                is_recovery_week, role, archetype, variant, prescription, minutes, tss,
                intensity_factor, params_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [(plan_name, s["date"], s["weekday"], s["block_index"], s["block_name"],
               s["week_in_block"], int(s["is_recovery_week"]), s["role"], s["archetype"],
               s["variant"], s["prescription"], s["minutes"], s["tss"],
               s["intensity_factor"], json.dumps(s["params"]), now) for s in sessions])
    conn.close()
    return len(sessions)


def print_weekly(sessions: list[dict]) -> None:
    print(f"  {'week':<12} {'block':<32} {'rides':>5} {'hard':>4} {'longest':>7} "
          f"{'ride h':>7} {'TSS':>5}")
    for w in weekly_rollup(sessions):
        tag = "  recovery" if w["recovery"] else ""
        print(f"  {w['week_start']:<12} {w['block'][:30]:<32} {w['rides']:>5} {w['hard']:>4} "
              f"{w['longest']:>6.0f}m {w['ride_minutes']/60:>7.1f} {w['tss']:>5.0f}{tag}")


def _watts(session: dict) -> str:
    """
    Resolve a prescription's percentages to watts using the FTP tracked for that date.

    Done at render time, never stored. The plan is expressed in percentages precisely so that a
    ramp test updates every future session at once instead of invalidating the plan.
    """
    ftp = get_metric("ftp", as_of=session["date"])
    p = session["params"]
    if not ftp or session["archetype"] == "strength":
        return ""
    if "pct" in p:
        return f"   -> {int(p['pct'] * ftp)}W"
    return ""


def print_detail(sessions: list[dict], only_week: date | None = None,
                 watts: bool = False) -> None:
    current = None
    for s in sessions:
        d = date.fromisoformat(s["date"])
        monday = d - timedelta(days=d.weekday())
        if only_week and monday != only_week:
            continue
        if monday.isoformat() != current:
            current = monday.isoformat()
            wk = next(w for w in weekly_rollup(sessions) if w["week_start"] == current)
            tag = "  (recovery week)" if wk["recovery"] else ""
            print(f"\n  week of {current} — {wk['block']}{tag}")
        print(f"    {DAYS[s['weekday']]}  {s['role']:<9} {s['prescription']}"
              f"{_watts(s) if watts else ''}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help="first Monday of the plan (default: next Monday)")
    p.add_argument("--plan-name", default=PLAN_NAME)
    p.add_argument("--four-rides", action="store_true", help="add a fourth ride each week")
    p.add_argument("--big-weekend", action="store_true",
                   help="add a Sunday endurance ride behind the Saturday long ride")
    p.add_argument("--detail", action="store_true", help="list every session")
    p.add_argument("--week", type=date.fromisoformat, help="show one week only")
    p.add_argument("--write", action="store_true", help="store to prescribed_sessions")
    p.add_argument("--watts", action="store_true",
                   help="resolve percentages to watts using the tracked FTP for each date")
    args = p.parse_args()

    start = args.start
    if start is None:
        today = date.today()
        start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    if start.weekday() != 0:
        print(f"  --start {start} is a {DAYS[start.weekday()]}; plans begin on a Monday.")
        return 1

    blocks = DEFAULT_PLAN
    if args.big_weekend:
        blocks = add_second_weekend_ride(blocks)
    if args.four_rides:
        blocks = add_lit_day(blocks)
    sessions = expand_plan(blocks, start, args.plan_name)
    split = intensity_split(sessions)

    print(f"\n  {args.plan_name} — {len(sessions)} sessions from {start}")
    for i, b in enumerate(blocks, start=1):
        print(f"    {i}. {b.name}  ({b.weeks} weeks)")
        if b.note:
            print(f"       {b.note}")
    print()

    if args.week or args.detail:
        print_detail(sessions, args.week, watts=args.watts)
    else:
        print_weekly(sessions)

    print(f"\n  intensity: {split['easy_pct']}% easy / {split['hard_pct']}% hard "
          f"({split['easy_hours']}h / {split['hard_hours']}h)")
    print("  measured over the last 28 days for comparison: 22% easy / 78% hard")

    ftp = get_metric("ftp")
    if args.watts:
        print(f"  watts resolved against tracked FTP {ftp:.0f}W — re-run after a ramp test "
              f"and every future session updates.")

    if args.write:
        n = store(sessions, args.plan_name)
        print(f"\n  stored {n} prescriptions to prescribed_sessions "
              f"(planned_workouts untouched).")
    else:
        print("\n  preview only — pass --write to store.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

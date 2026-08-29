"""
Seed training_plan_weeks from a TrainerRoad plan's phase view.

Run once whenever a new TR plan is loaded. Edit PLAN below, then:

    python scripts/seed_training_plan.py --dry-run
    python scripts/seed_training_plan.py

Why this is hand-seeded rather than synced: intervals.icu carries its own unrelated plan
(a `PLAN` event applied 2026-05-26, phases tagged "Peak", ending 2026-09-21) and knows nothing
about the TR plan. The TR calendar only reaches ~14 days forward, so it can't supply a 45-week
structure either.

Safe to re-run: an existing week row is updated in place, never duplicated, and re-running with
an unchanged PLAN is a genuine no-op. Modelled on the *corrected* seed_profile.py — a bare run
here writes exactly what PLAN says and nothing else.
"""

import sys
import argparse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import get_connection, migrate_db

PLAN_NAME = "TrainerRoad 2026-27"

# (phase, first Monday, number of weeks). Taken from the TR plan's phase view.
# A trailing standalone recovery block is phase "rest".
PHASES: list[tuple[str, date, int]] = [
    ("base",      date(2026, 8, 17), 12),
    ("build",     date(2026, 11, 9),  8),
    ("specialty", date(2027, 1, 4),   8),
    ("build",     date(2027, 3, 1),   8),
    ("specialty", date(2027, 4, 26),  8),
    ("rest",      date(2027, 6, 21),  1),
]

# Which weeks *inside* each phase are recovery weeks, keyed by 1-based phase number (phases
# repeat, so the phase name isn't a unique key). Values are 1-based week numbers within
# that phase.
#
# This is the one thing the phase view doesn't show, and it's what cycle boundaries key off —
# see cycle_boundaries() in src/analysis/training_plan.py. A phase with no entry here produces
# no cycle boundary and the script warns about it.
#
# Confirmed 2026-08-28: TrainerRoad's classic 3-on/1-off — every 4th week of every phase.
REST_WEEKS: dict[int, set[int]] = {
    1: {4, 8, 12},  # base 12wk      (2026-08-17 .. 2026-11-08)
    2: {4, 8},      # build 8wk      (2026-11-09 .. 2027-01-03)
    3: {4, 8},      # specialty 8wk  (2027-01-04 .. 2027-02-28)
    4: {4, 8},      # build 8wk      (2027-03-01 .. 2027-04-25)
    5: {4, 8},      # specialty 8wk  (2027-04-26 .. 2027-06-20)
    6: {1},         # standalone recovery week (2027-06-21 .. 2027-06-27)
}

VALID_WEEK_TYPES = {"base", "build", "specialty", "rest"}


def build_weeks() -> list[dict]:
    """Expand PHASES into one row per week, validating contiguity as it goes."""
    rows: list[dict] = []
    plan_start = PHASES[0][1]
    plan_end = PHASES[-1][1] + timedelta(days=PHASES[-1][2] * 7 - 1)

    expected_start = plan_start
    plan_week = 0
    for phase_number, (phase, start, n_weeks) in enumerate(PHASES, start=1):
        if start.weekday() != 0:
            raise ValueError(
                f"phase {phase_number} ({phase}) starts {start} which is a "
                f"{start.strftime('%A')}, not a Monday"
            )
        if start != expected_start:
            raise ValueError(
                f"phase {phase_number} ({phase}) starts {start} but the previous phase ends "
                f"{expected_start - timedelta(days=1)} — phases must be contiguous"
            )
        rest = REST_WEEKS.get(phase_number, set())
        if bad := rest - set(range(1, n_weeks + 1)):
            raise ValueError(f"phase {phase_number} ({phase}) has {n_weeks} weeks; REST_WEEKS "
                             f"names week(s) {sorted(bad)} which don't exist")

        for phase_week in range(1, n_weeks + 1):
            plan_week += 1
            week_start = start + timedelta(days=(phase_week - 1) * 7)
            week_type = "rest" if phase_week in rest or phase == "rest" else phase
            if week_type not in VALID_WEEK_TYPES:
                raise ValueError(f"invalid week_type {week_type!r}")
            rows.append({
                "plan_name": PLAN_NAME,
                "plan_start_date": plan_start.isoformat(),
                "plan_end_date": plan_end.isoformat(),
                "week_start_date": week_start.isoformat(),
                "week_end_date": (week_start + timedelta(days=6)).isoformat(),
                "week_type": week_type,
                # A rest week keeps its parent block, so a cycle review can still say which
                # phase it closed out. Standalone recovery blocks are their own phase.
                "phase": phase,
                "phase_number": phase_number,
                "phase_week_number": phase_week,
                "plan_week_number": plan_week,
            })
        expected_start = start + timedelta(days=n_weeks * 7)

    return rows


def seed(rows: list[dict], dry_run: bool) -> None:
    migrate_db()
    conn = get_connection()

    existing = {
        r["week_start_date"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM training_plan_weeks WHERE plan_name = ?", (PLAN_NAME,)
        ).fetchall()
    }

    new = [r for r in rows if r["week_start_date"] not in existing]
    changed = [
        r for r in rows
        if r["week_start_date"] in existing
        and any(existing[r["week_start_date"]][k] != v for k, v in r.items())
    ]
    stale = [d for d in existing if d not in {r["week_start_date"] for r in rows}]

    print(f"Plan: {PLAN_NAME}")
    print(f"  {rows[0]['plan_start_date']} -> {rows[0]['plan_end_date']}  ({len(rows)} weeks)")
    print(f"  new: {len(new)}   changed: {len(changed)}   unchanged: "
          f"{len(rows) - len(new) - len(changed)}   no longer in plan: {len(stale)}")

    rest = [r for r in rows if r["week_type"] == "rest"]
    print(f"\n  rest weeks ({len(rest)}):")
    for r in rest:
        print(f"    week {r['plan_week_number']:>2}  {r['week_start_date']}  "
              f"(closes {r['phase']} phase {r['phase_number']}, week {r['phase_week_number']})")
    missing = [n for n, (p, _, _) in enumerate(PHASES, start=1)
               if p != "rest" and not REST_WEEKS.get(n)]
    if missing:
        print(f"\n  WARNING: phases {missing} have no rest weeks defined. Cycle boundaries are")
        print( "  derived from week_type='rest', so those phases will yield no cycles.")

    if dry_run:
        print("\nDry run — nothing written.")
        return

    with conn:
        for r in rows:
            conn.execute("""
                INSERT INTO training_plan_weeks (
                    plan_name, plan_start_date, plan_end_date, week_start_date, week_end_date,
                    week_type, phase, phase_number, phase_week_number, plan_week_number
                ) VALUES (
                    :plan_name, :plan_start_date, :plan_end_date, :week_start_date,
                    :week_end_date, :week_type, :phase, :phase_number, :phase_week_number,
                    :plan_week_number
                )
                ON CONFLICT(plan_name, week_start_date) DO UPDATE SET
                    plan_start_date=excluded.plan_start_date,
                    plan_end_date=excluded.plan_end_date,
                    week_end_date=excluded.week_end_date,
                    week_type=excluded.week_type,
                    phase=excluded.phase,
                    phase_number=excluded.phase_number,
                    phase_week_number=excluded.phase_week_number,
                    plan_week_number=excluded.plan_week_number
            """, r)
        # A shortened or re-cut plan leaves orphan weeks behind; drop them rather than let
        # them keep answering "what week am I in?" long after the plan changed.
        for d in stale:
            conn.execute(
                "DELETE FROM training_plan_weeks WHERE plan_name = ? AND week_start_date = ?",
                (PLAN_NAME, d),
            )
    conn.close()
    print(f"\nSeeded {len(rows)} weeks" + (f", removed {len(stale)} stale" if stale else "") + ".")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    seed(build_weeks(), args.dry_run)

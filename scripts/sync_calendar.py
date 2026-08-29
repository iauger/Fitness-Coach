"""
Sync planned TrainerRoad workouts from the calendar's public iCal feed into planned_workouts,
then match them against actual activities to compute plan adherence.

Requires GCAL_ICAL_URL in .env — see src/integrations/tr_calendar.py. No auth step.

Usage:
    python scripts/sync_calendar.py
    python scripts/sync_calendar.py --days-back 60 --days-forward 365
"""

import sys
import argparse
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import init_db, migrate_db
from src.db.store import upsert_planned_workouts
from src.analysis.compliance import match_planned_workouts
from src.integrations.tr_calendar import sync_planned_workouts


def run(days_back: int, days_forward: int) -> None:
    init_db()
    migrate_db()

    today = date.today()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_forward)

    print(f"Fetching planned workouts  {start}  to  {end} ...")
    try:
        workouts = sync_planned_workouts(start, end)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    n = upsert_planned_workouts(workouts)
    print(f"  {n} planned workouts synced.")

    # Never match today — the day isn't over yet, so a still-pending workout would
    # get wrongly scored "skipped" before there's been a chance to do it.
    match_end = min(end, today - timedelta(days=1))
    if match_end < start:
        print("Nothing in the past to match yet.")
        return
    print(f"Matching against actual activities  {start}  to  {match_end} ...")
    stats = match_planned_workouts(start.isoformat(), match_end.isoformat())
    print(f"  completed: {stats.get('completed', 0)}  "
          f"partial: {stats.get('partial', 0)}  "
          f"skipped: {stats.get('skipped', 0)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days-back", type=int, default=60,
                        help="How many days back to sync + match (default 60)")
    parser.add_argument("--days-forward", type=int, default=365,
                        help="How many days ahead to pull upcoming planned workouts (default 365)")
    args = parser.parse_args()
    run(args.days_back, args.days_forward)

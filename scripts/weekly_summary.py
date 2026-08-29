"""
Generate weekly summaries — the one-time compression that keeps future check-ins cheap.

Normally runs by itself as a byproduct of `scripts/checkin.py`. Use this to backfill, to
regenerate a week after adding notes late, or to inspect a week without spending a call.

Usage:
    python scripts/weekly_summary.py                    # the last completed week
    python scripts/weekly_summary.py --backfill 8       # last 8 completed weeks, skipping done
    python scripts/weekly_summary.py --week 2026-08-17  # one specific week (any day in it)
    python scripts/weekly_summary.py --force            # regenerate even if already stored
    python scripts/weekly_summary.py --metrics-only     # no API call, just show the rollup
"""

import sys
import json
import argparse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.db.schema import migrate_db
from src.analysis.weekly import (
    week_metrics, save_summary, monday_of, last_completed_week,
    summarized_weeks, has_qualitative_content,
)


def run_week(week_start: date, force: bool, metrics_only: bool, done: set[str]) -> bool:
    m = week_metrics(week_start)
    key = m["week_start"]
    label = f"{key} to {m['week_end']}"

    if metrics_only:
        print(f"\n{label}")
        print(json.dumps({k: v for k, v in m.items()
                          if k not in ("sessions", "subjective")}, indent=2))
        print(f"  qualitative content: {has_qualitative_content(m)}")
        return False

    if key in done and not force:
        print(f"  {label}  already summarised (use --force to regenerate)")
        return False

    if not has_qualitative_content(m):
        save_summary(m, None, None)
        print(f"  {label}  stats only, no notes to compress (no API call)")
        return True

    from src.coach.summarize import weekly_narrative
    narrative, model = weekly_narrative(m)
    save_summary(m, narrative, model)
    print(f"  {label}  summarised")
    if narrative:
        print(f"      {narrative}")
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--week", type=date.fromisoformat, help="Any date inside the target week")
    p.add_argument("--backfill", type=int, metavar="N", help="Last N completed weeks")
    p.add_argument("--force", action="store_true")
    p.add_argument("--metrics-only", action="store_true")
    args = p.parse_args()

    migrate_db()
    done = summarized_weeks()

    if args.week:
        weeks = [monday_of(args.week)]
    elif args.backfill:
        newest = last_completed_week()
        weeks = [newest - timedelta(weeks=i) for i in range(args.backfill)][::-1]
    else:
        weeks = [last_completed_week()]

    print(f"Weekly summaries — {len(weeks)} week(s)")
    n = sum(run_week(w, args.force, args.metrics_only, done) for w in weeks)
    if not args.metrics_only:
        print(f"\n{n} written.")

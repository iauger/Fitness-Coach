"""
4-week training-cycle review — the wider look that replaces the weekly check-in when a
mesocycle closes.

Usage:
    python scripts/cycle_review.py
    python scripts/cycle_review.py --verbose
    python scripts/cycle_review.py --as-of 2026-09-14    (review as if run on that date)
    python scripts/cycle_review.py --metrics-only        (print the rollup, no API call)

Reviews are stored in the cycle_reviews table, keyed by cycle, so re-running replaces rather
than duplicating and the next cycle can compare against this one from stored figures.
"""

import sys
import argparse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.db.schema import migrate_db
from src.analysis.cycle import (
    cycle_for_review, cycle_metrics, cycle_context_text, previous_review,
)


def run(as_of: date | None, metrics_only: bool, verbose: bool) -> int:
    migrate_db()

    if as_of and as_of > date.today():
        print(f"--as-of {as_of} is in the future; clamping to today ({date.today()}). "
              "A cycle that hasn't finished yet has no data to review.")
        as_of = None

    cyc = cycle_for_review(as_of)
    if cyc is None:
        nxt = _next_cycle_end(as_of)
        print("No training cycle has closed yet — nothing to review.")
        if nxt:
            print(f"The current cycle closes {nxt}; run this the day after.")
        return 1

    metrics = cycle_metrics(cyc)
    if metrics_only:
        print(cycle_context_text(metrics, previous_review(cyc["start_date"])))
        return 0

    from src.coach.session import cycle_review
    print(cycle_review(on=as_of, verbose=verbose))
    return 0


def _next_cycle_end(as_of: date | None) -> str | None:
    from src.analysis.training_plan import cycles
    d = (as_of or date.today()).isoformat()
    return next((c["end_date"] for c in cycles() if c["end_date"] >= d), None)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", type=date.fromisoformat, default=None,
                   help="Review as if run on this past date (YYYY-MM-DD). Clamped to today — "
                        "a cycle that hasn't finished has no data to review.")
    p.add_argument("--metrics-only", action="store_true",
                   help="Print the deterministic rollup without calling the API")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    sys.exit(run(args.as_of, args.metrics_only, args.verbose))

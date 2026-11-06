"""
Fetch full history from intervals.icu and store in local SQLite.
Pulls in yearly chunks to avoid API timeouts.

Usage:
    python scripts/fetch_history.py
    python scripts/fetch_history.py --start 2020-01-01
    python scripts/fetch_history.py --start 2017-01-01 --end 2024-12-31

The fetch itself is `src/history_fetch.py`, shared with the setup screen's button.
"""

import sys
import argparse
from pathlib import Path
from datetime import date

# allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.clock import today_local
from src.history_fetch import HistoryError, fetch_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch intervals.icu history into local DB")
    parser.add_argument("--start", default="2017-01-01", help="Start date (YYYY-MM-DD)")
    # Read after the parse, not as a default: `--help` should not need the clock, and the
    # clock reads the config.
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD), default today")
    args = parser.parse_args(argv)

    try:
        totals = fetch_all(date.fromisoformat(args.start),
                           date.fromisoformat(args.end) if args.end else today_local(),
                           progress=print)
    except HistoryError as exc:
        print(f"\n{exc}\n")
        return 1
    print(f"\nDone. Activities: {totals['activities']}  |  Wellness: {totals['wellness']}"
          f"  |  Events: {totals['events']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

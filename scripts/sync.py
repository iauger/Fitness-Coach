"""
Manual incremental sync — same logic the agent runs silently at startup.

Usage:
    python scripts/sync.py
    python scripts/sync.py --dry-run   (show window only, no API calls)
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync import incremental_sync, sync_window

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the sync window without making API calls")
    args = parser.parse_args()

    if args.dry_run:
        start, end = sync_window()
        print(f"Sync window: {start}  to  {end}")
    else:
        result = incremental_sync(silent=False)
        print(f"\nDone. Activities: {result['activities']}  |  "
              f"Wellness: {result['wellness']}  |  Events: {result['events']}")
        if result["errors"]:
            print("Errors:", result["errors"])

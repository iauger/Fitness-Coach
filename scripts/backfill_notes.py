"""
Backfill athlete_note from intervals.icu activity chat threads.

Incremental sync only scans the last NOTE_LOOKBACK_DAYS. Run this once after adding the
column, or any time notes were written further back than that window reaches:

    python scripts/backfill_notes.py                # last 365 days
    python scripts/backfill_notes.py --days 3000    # everything
    python scripts/backfill_notes.py --dry-run
"""

import sys
import argparse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.intervals.client import IntervalsClient
from src.db.store import update_activity_notes
from src.sync import sync_activity_notes


def run(days: int, dry_run: bool) -> None:
    start = date.today() - timedelta(days=days)
    end = date.today()
    print(f"Scanning activities {start} to {end} for chat threads...")

    client = IntervalsClient()
    activities = client.get_activities(start, end)
    threaded = [a for a in activities if a.get("icu_chat_id")]
    print(f"  {len(activities)} activities, {len(threaded)} with a chat thread")

    notes = sync_activity_notes(client, threaded)
    for act_id, text in notes.items():
        act = next((a for a in threaded if str(a.get("id")) == act_id), {})
        label = f"{act.get('start_date_local', '')[:10]}  {act.get('name', '?')}"
        if text is None:
            print(f"  {label}\n      (thread exists but is empty — clearing)")
        else:
            print(f"  {label}\n      {text[:160]}{'...' if len(text) > 160 else ''}")

    if dry_run:
        print(f"\nDry run — nothing written. Would have set {len(notes)} activities.")
        return

    updated = update_activity_notes(notes)
    print(f"\nWrote athlete_note on {updated} activities.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365, help="how far back to scan (default 365)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(args.days, args.dry_run)

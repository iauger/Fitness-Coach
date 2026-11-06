"""
Fetch full history from intervals.icu and store in local SQLite.
Pulls in yearly chunks to avoid API timeouts.

Usage:
    python scripts/fetch_history.py
    python scripts/fetch_history.py --start 2020-01-01
    python scripts/fetch_history.py --start 2017-01-01 --end 2024-12-31
"""

import sys
import argparse
from pathlib import Path
from datetime import date, timedelta

# allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.intervals.client import IntervalsClient, MissingCredentials
from src.db.schema import migrate_db
from src.db.store import upsert_activities, upsert_wellness, upsert_events, log_sync
from src.db.schema import get_connection
from src.clock import today_local

CHUNK_DAYS = 90  # pull in 90-day windows to stay well within API limits


def date_chunks(start: date, end: date, chunk: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_all(start: date, end: date, verbose: bool = True) -> None:
    client = IntervalsClient()
    migrate_db()
    conn = get_connection()

    def log(msg):
        if verbose:
            print(msg)

    log(f"Checking connection to intervals.icu...")
    # `ping` raises MissingCredentials rather than returning False when the key is rejected,
    # so the message it carries is the one worth showing — and showing it as a message rather
    # than a traceback, since this is the first command that talks to the network and the
    # commonest first-run failure is a key that was pasted wrong.
    try:
        connected = client.ping()
    except MissingCredentials as exc:
        print(f"\n{exc}\n")
        sys.exit(1)
    if not connected:
        print("ERROR: Could not reach intervals.icu. Check your network, then "
              "python scripts/status.py")
        sys.exit(1)
    log("  Connected.\n")

    total_activities = 0
    total_wellness = 0
    total_events = 0

    for chunk_start, chunk_end in date_chunks(start, end):
        label = f"{chunk_start}  to  {chunk_end}"

        # Activities
        try:
            activities = client.get_activities(chunk_start, chunk_end)
            n = upsert_activities(activities)
            with conn:
                log_sync(conn, "activities", chunk_start, chunk_end, n)
            total_activities += n
            log(f"[activities] {label}  ({n} records)")
        except Exception as e:
            log(f"[activities] {label}  ERROR: {e}")
            with conn:
                log_sync(conn, "activities", chunk_start, chunk_end, 0, status=f"error: {e}")

        # Wellness
        try:
            wellness = client.get_wellness(chunk_start, chunk_end)
            n = upsert_wellness(wellness)
            with conn:
                log_sync(conn, "wellness", chunk_start, chunk_end, n)
            total_wellness += n
            log(f"[wellness  ] {label}  ({n} records)")
        except Exception as e:
            log(f"[wellness  ] {label}  ERROR: {e}")
            with conn:
                log_sync(conn, "wellness", chunk_start, chunk_end, 0, status=f"error: {e}")

    # Events — single pull (usually small)
    try:
        events = client.get_events(start, end)
        n = upsert_events(events)
        with conn:
            log_sync(conn, "events", start, end, n)
        total_events = n
        log(f"[events    ] {start}  to  {end}  ({n} records)")
    except Exception as e:
        log(f"[events    ] ERROR: {e}")

    conn.close()
    print(f"\nDone. Activities: {total_activities}  |  Wellness: {total_wellness}  |  Events: {total_events}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch intervals.icu history into local DB")
    parser.add_argument("--start", default="2017-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=today_local().isoformat(), help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    fetch_all(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
    )

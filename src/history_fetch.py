"""
Pull the full history from intervals.icu into the local database, in 90-day windows.

The one implementation behind `scripts/fetch_history.py` and the setup screen's button. It
lived in the script, and it reported by printing and failed by `sys.exit`, neither of which a
background job can use: an exit inside a thread ends the thread and says nothing. So it now
reports through a callback and fails with `HistoryError`, which carries the message the
script used to print.

A window that errors is logged and skipped, not fatal, exactly as before: one bad 90-day
window should not throw away the other thirty.
"""

from datetime import date, timedelta
from typing import Callable

from src.db.schema import get_connection, migrate_db
from src.db.store import log_sync, upsert_activities, upsert_events, upsert_wellness
from src import applog
from src.intervals.client import IntervalsClient, MissingCredentials

CHUNK_DAYS = 90  # pull in 90-day windows to stay well within API limits


class HistoryError(Exception):
    """The fetch could not start: no credentials, or intervals.icu cannot be reached."""


def date_chunks(start: date, end: date, chunk: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_all(start: date, end: date,
              progress: Callable[[str], None] = lambda message: None) -> dict:
    """Fetch and store. Returns the totals; raises HistoryError if it cannot begin."""
    client = IntervalsClient()
    migrate_db()
    conn = get_connection()
    try:
        progress("Checking connection to intervals.icu...")
        # `ping` raises MissingCredentials rather than returning False when the key is rejected,
        # so the message it carries is the one worth showing: the commonest first-run failure
        # is a key that was pasted wrong.
        try:
            connected = client.ping()
        except MissingCredentials as exc:
            raise HistoryError(str(exc)) from None
        if not connected:
            raise HistoryError("Could not reach intervals.icu. Check your network, then "
                               "python scripts/status.py")
        progress("  Connected.")

        total = {"activities": 0, "wellness": 0, "events": 0, "errors": 0}
        for chunk_start, chunk_end in date_chunks(start, end):
            label = f"{chunk_start}  to  {chunk_end}"

            try:
                n = upsert_activities(client.get_activities(chunk_start, chunk_end))
                with conn:
                    log_sync(conn, "activities", chunk_start, chunk_end, n)
                total["activities"] += n
                progress(f"[activities] {label}  ({n} records)")
            except Exception as e:
                applog.warning("history.chunk_failed", e, kind="activities")
                total["errors"] += 1
                progress(f"[activities] {label}  ERROR: {e}")
                with conn:
                    log_sync(conn, "activities", chunk_start, chunk_end, 0,
                             status=f"error: {e}")

            try:
                n = upsert_wellness(client.get_wellness(chunk_start, chunk_end))
                with conn:
                    log_sync(conn, "wellness", chunk_start, chunk_end, n)
                total["wellness"] += n
                progress(f"[wellness  ] {label}  ({n} records)")
            except Exception as e:
                applog.warning("history.chunk_failed", e, kind="wellness")
                total["errors"] += 1
                progress(f"[wellness  ] {label}  ERROR: {e}")
                with conn:
                    log_sync(conn, "wellness", chunk_start, chunk_end, 0, status=f"error: {e}")

        # Events: a single pull, usually small.
        try:
            n = upsert_events(client.get_events(start, end))
            with conn:
                log_sync(conn, "events", start, end, n)
            total["events"] = n
            progress(f"[events    ] {start}  to  {end}  ({n} records)")
        except Exception as e:
            applog.warning("history.chunk_failed", e, kind="events")
            total["errors"] += 1
            progress(f"[events    ] ERROR: {e}")
    finally:
        conn.close()
    return total

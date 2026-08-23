"""
Incremental sync from intervals.icu into local SQLite.

Window logic:
  - Anchor on MAX(wellness.date) — wellness covers every calendar day including rest days
  - Always re-pull the last 2 days (Garmin lag + partial mid-day data)
  - Cap at 30 days; warn if gap is larger (use fetch_history.py for backfill)
  - End date is always today
"""

from datetime import date, timedelta
from src.intervals.client import IntervalsClient
from src.db.schema import get_connection
from src.db.store import upsert_activities, upsert_wellness, upsert_events, upsert_planned_workouts, log_sync
from src.analysis.compliance import match_planned_workouts
from src.integrations.google_calendar import sync_planned_workouts, TOKEN_PATH

OVERLAP_DAYS = 2
MAX_INCREMENTAL_DAYS = 30
CALENDAR_LOOKAHEAD_DAYS = 14


def _max_wellness_date() -> date | None:
    conn = get_connection()
    row = conn.execute("""
        SELECT MAX(date) as max_date FROM wellness
        WHERE date <= ?
    """, (date.today().isoformat(),)).fetchone()
    conn.close()
    val = row["max_date"] if row else None
    return date.fromisoformat(val) if val else None


def sync_window() -> tuple[date, date]:
    """Return (start, end) dates for the incremental sync."""
    today = date.today()
    max_date = _max_wellness_date()

    if max_date is None:
        return today - timedelta(days=MAX_INCREMENTAL_DAYS), today

    gap_days = (today - max_date).days
    if gap_days > MAX_INCREMENTAL_DAYS:
        return today - timedelta(days=MAX_INCREMENTAL_DAYS), today

    return max(max_date - timedelta(days=OVERLAP_DAYS), date(2017, 1, 1)), today


def incremental_sync(silent: bool = False) -> dict:
    """
    Pull recent data from intervals.icu and upsert into local DB.
    Returns a summary dict with counts and the window used.

    silent=True suppresses all print output (for agent startup calls).
    """
    def log(msg: str) -> None:
        if not silent:
            print(msg)

    start, end = sync_window()
    today = date.today()
    max_date = _max_wellness_date()
    gap_days = (today - max_date).days if max_date else MAX_INCREMENTAL_DAYS

    if gap_days > MAX_INCREMENTAL_DAYS and not silent:
        print(
            f"[sync] Warning: data gap is {gap_days} days. "
            f"Only pulling last {MAX_INCREMENTAL_DAYS} days. "
            f"Run scripts/fetch_history.py to backfill older data."
        )

    log(f"[sync] {start}  to  {end}  (gap was {gap_days}d, overlap {OVERLAP_DAYS}d)")

    client = IntervalsClient()
    conn = get_connection()
    results = {"start": start.isoformat(), "end": end.isoformat(),
               "activities": 0, "wellness": 0, "events": 0, "errors": []}

    try:
        activities = client.get_activities(start, end)
        n = upsert_activities(activities)
        with conn:
            log_sync(conn, "activities", start, end, n)
        results["activities"] = n
        log(f"[sync] activities  {n} records")
    except Exception as e:
        results["errors"].append(f"activities: {e}")
        log(f"[sync] activities  ERROR: {e}")

    try:
        wellness = client.get_wellness(start, end)
        n = upsert_wellness(wellness)
        with conn:
            log_sync(conn, "wellness", start, end, n)
        results["wellness"] = n
        log(f"[sync] wellness    {n} records")
    except Exception as e:
        results["errors"].append(f"wellness: {e}")
        log(f"[sync] wellness    ERROR: {e}")

    try:
        events = client.get_events(start, end)
        n = upsert_events(events)
        with conn:
            log_sync(conn, "events", start, end, n)
        results["events"] = n
        log(f"[sync] events      {n} records")
    except Exception as e:
        results["errors"].append(f"events: {e}")
        log(f"[sync] events      ERROR: {e}")

    conn.close()

    # Calendar sync is best-effort: silently skipped until the user runs
    # scripts/auth_google_calendar.py once. Not a hard dependency of the core sync.
    if TOKEN_PATH.exists():
        try:
            cal_end = end + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
            planned = sync_planned_workouts(start, cal_end)
            n_planned = upsert_planned_workouts(planned)
            # Never match today — the day isn't over yet, so a still-pending workout
            # would get wrongly scored "skipped" before there's been a chance to do it.
            match_end = min(end, today - timedelta(days=1))
            compliance = match_planned_workouts(start.isoformat(), match_end.isoformat()) if match_end >= start else {}
            results["planned_workouts"] = n_planned
            results["compliance"] = compliance
            log(f"[sync] calendar    {n_planned} planned workouts  ({compliance})")
        except Exception as e:
            results["errors"].append(f"calendar: {e}")
            log(f"[sync] calendar    ERROR: {e}")

    return results

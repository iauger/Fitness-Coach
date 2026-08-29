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
from src.db.store import (
    upsert_activities, upsert_wellness, upsert_events, upsert_planned_workouts,
    update_activity_notes, log_sync,
)
from src.analysis.compliance import match_planned_workouts
from src.integrations.tr_calendar import sync_planned_workouts

OVERLAP_DAYS = 2
MAX_INCREMENTAL_DAYS = 30
# The iCal feed returns the whole plan in one unauthenticated request, so these windows are
# about how much we keep matched and stored, not about API cost. Lookback covers a full
# mesocycle plus margin so a 4-week cycle review always has complete compliance data — the
# old ~2-day window (inherited from sync_window()) was the Session 14 blocker for item 8.
CALENDAR_LOOKBACK_DAYS = 60
CALENDAR_LOOKAHEAD_DAYS = 120
# Notes are written whenever the athlete gets round to it, not when the ride uploads, so the
# 2-day activity window is too narrow to catch them. Scanning a wider span costs one extra
# list request (chat threads are only fetched for the handful of activities that have one).
NOTE_LOOKBACK_DAYS = 28


def flatten_messages(messages: list[dict], athlete_id: str = "") -> str | None:
    """
    Collapse an activity's chat thread into a single note string.

    Keeps non-deleted TEXT messages in the order the API returns them. Messages from anyone
    other than the athlete are attributed inline, so a comment left by someone else can't be
    mistaken for the athlete's own read of the session. Returns None for an empty thread —
    an emptied thread should clear athlete_note, not leave a stale note behind.
    """
    parts = []
    for m in messages:
        if m.get("type") != "TEXT" or m.get("deleted"):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if athlete_id and m.get("athlete_id") and m["athlete_id"] != athlete_id:
            content = f"[{m.get('name') or 'other'}] {content}"
        parts.append(content)
    return "\n\n".join(parts) if parts else None


def sync_activity_notes(client, activities: list[dict]) -> dict[str, str | None]:
    """
    Fetch athlete notes for the activities that have a chat thread.

    `icu_chat_id` is set exactly when a thread exists — verified against 23 activities since
    2026-07-01, where it was non-null on precisely the 2 carrying notes. Filtering on it keeps
    this to a couple of requests per sync instead of one per activity.
    """
    notes: dict[str, str | None] = {}
    for a in activities:
        if not a.get("icu_chat_id"):
            continue
        act_id = str(a.get("id", ""))
        notes[act_id] = flatten_messages(
            client.get_activity_messages(act_id), client.athlete_id
        )
    return notes


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
               "activities": 0, "wellness": 0, "events": 0, "notes": 0, "errors": []}

    try:
        activities = client.get_activities(start, end)
        n = upsert_activities(activities)
        with conn:
            log_sync(conn, "activities", start, end, n)
        results["activities"] = n
        log(f"[sync] activities  {n} records")

        # Notes are a separate request per activity, so a failure here must not cost us the
        # activity rows already written above.
        try:
            note_start = min(start, today - timedelta(days=NOTE_LOOKBACK_DAYS))
            note_scope = (activities if note_start >= start
                          else client.get_activities(note_start, end))
            notes = sync_activity_notes(client, note_scope)
            n_notes = update_activity_notes(notes)
            results["notes"] = n_notes
            if notes:
                log(f"[sync] notes       {n_notes} activities with athlete notes")
        except Exception as e:
            results["errors"].append(f"notes: {e}")
            log(f"[sync] notes       ERROR: {e}")
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

    # Calendar sync is best-effort — a feed failure must not fail the whole sync.
    # Unlike the activity window, this deliberately spans a wide range on every run: the
    # feed is a single request that returns the entire plan regardless, so narrowing it
    # would cost nothing and buy nothing. The wide window is also what lets a workout whose
    # activity synced late get re-matched, which the old ~2-day window never did.
    try:
        cal_start = today - timedelta(days=CALENDAR_LOOKBACK_DAYS)
        cal_end = today + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
        planned = sync_planned_workouts(cal_start, cal_end)
        n_planned = upsert_planned_workouts(planned)
        # Never match today — the day isn't over yet, so a still-pending workout
        # would get wrongly scored "skipped" before there's been a chance to do it.
        match_end = today - timedelta(days=1)
        compliance = (match_planned_workouts(cal_start.isoformat(), match_end.isoformat())
                      if match_end >= cal_start else {})
        results["planned_workouts"] = n_planned
        results["compliance"] = compliance
        log(f"[sync] calendar    {n_planned} planned workouts  ({compliance})")
    except Exception as e:
        results["errors"].append(f"calendar: {e}")
        log(f"[sync] calendar    ERROR: {e}")

    return results

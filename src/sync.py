"""
Incremental sync from intervals.icu into local SQLite.

Window logic:
  - Anchor on MAX(wellness.date) — wellness covers every calendar day including rest days
  - Always re-pull the last 2 days (Garmin lag + partial mid-day data)
  - Cap at 30 days; warn if gap is larger (use fetch_history.py for backfill)
  - End date is always today
"""

from datetime import date, timedelta
import time
from src import applog
from src.intervals.client import IntervalsClient
from src.db.schema import get_connection, read_connection, write_connection
from src.db.store import (
    upsert_activities, upsert_wellness, upsert_events,
    update_activity_notes, log_sync,
)
from src.analysis.compliance import match_planned_workouts
from src.planning.roller import store_horizon
from src.clock import today_local

OVERLAP_DAYS = 2
MAX_INCREMENTAL_DAYS = 30
# How far back adherence is re-scored on every sync. A full mesocycle plus margin, so a 4-week
# cycle review always has complete compliance data, and so a ride that synced late still gets
# matched to the session it was meant for — the old ~2-day window never did.
ADHERENCE_LOOKBACK_DAYS = 60
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
    with read_connection() as conn:
        row = conn.execute("""
            SELECT MAX(date) as max_date FROM wellness
            WHERE date <= ?
        """, (today_local().isoformat(),)).fetchone()
    val = row["max_date"] if row else None
    return date.fromisoformat(val) if val else None


def _last_ok_end(data_type: str) -> date | None:
    """The end date of the last sync of this kind that succeeded, or None if none is on record."""
    with read_connection() as conn:
        row = conn.execute(
            "SELECT MAX(end_date) AS d FROM sync_log WHERE data_type = ? AND status = 'ok' "
            "AND end_date <= ?", (data_type, today_local().isoformat())).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def _anchor(data_type: str) -> date | None:
    """
    Where this kind of data was last known to be complete.

    **Each kind resumes from its own last success.** The window used to be anchored on the newest
    wellness date for all three, so a run in which activities failed and wellness succeeded moved
    the anchor to today and the next run asked for activities from two days back: the days in
    between were never fetched again. Wellness is still the fallback for a database that has no
    sync record of this kind (one that predates the log).
    """
    return _last_ok_end(data_type) or _max_wellness_date()


def window_for(data_type: str) -> tuple[date, date]:
    """Return (start, end) dates for the incremental sync of one kind of data."""
    today = today_local()
    anchor = _anchor(data_type)

    if anchor is None:
        return today - timedelta(days=MAX_INCREMENTAL_DAYS), today

    gap_days = (today - anchor).days
    if gap_days > MAX_INCREMENTAL_DAYS:
        return today - timedelta(days=MAX_INCREMENTAL_DAYS), today

    return max(anchor - timedelta(days=OVERLAP_DAYS), date(2017, 1, 1)), today


def sync_window() -> tuple[date, date]:
    """The activities window, which is the one that matters to a decision about a week."""
    return window_for("activities")


def incremental_sync(silent: bool = False) -> dict:
    """
    Pull recent data from intervals.icu and upsert into local DB.
    Returns a summary dict with counts and the window used.

    silent=True suppresses all print output (for agent startup calls).
    """
    def log(msg: str) -> None:
        if not silent:
            print(msg)

    started = time.monotonic()
    start, end = window_for("activities")
    well_start, _ = window_for("wellness")
    event_start, _ = window_for("events")
    today = today_local()
    anchor = _anchor("activities")
    gap_days = (today - anchor).days if anchor else MAX_INCREMENTAL_DAYS

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
    activities_ok = False

    try:
        activities = client.get_activities(start, end)
        skipped: list[str] = []
        n = upsert_activities(activities, skipped=skipped)
        activities_ok = True
        if skipped:
            applog.warning("sync.records_skipped", count=len(skipped))
            # Said, not swallowed. The rest of the batch is stored; these are the records that
            # had no id or no date to file them under.
            results["errors"].append(
                f"activities: {len(skipped)} record(s) skipped, not storable: {', '.join(skipped[:3])}")
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
            applog.warning("sync.notes_failed", e)
            results["errors"].append(f"notes: {e}")
            log(f"[sync] notes       ERROR: {e}")
    except Exception as e:
        applog.error("sync.activities_failed", e, window_days=(end - start).days)
        results["errors"].append(f"activities: {e}")
        log(f"[sync] activities  ERROR: {e}")

    try:
        wellness = client.get_wellness(well_start, end)
        n = upsert_wellness(wellness)
        with conn:
            log_sync(conn, "wellness", well_start, end, n)
        results["wellness"] = n
        log(f"[sync] wellness    {n} records")
    except Exception as e:
        applog.error("sync.wellness_failed", e, window_days=(end - well_start).days)
        results["errors"].append(f"wellness: {e}")
        log(f"[sync] wellness    ERROR: {e}")

    try:
        events = client.get_events(event_start, end)
        n = upsert_events(events)
        with conn:
            log_sync(conn, "events", event_start, end, n)
        results["events"] = n
        log(f"[sync] events      {n} records")
    except Exception as e:
        applog.error("sync.events_failed", e)
        results["errors"].append(f"events: {e}")
        log(f"[sync] events      ERROR: {e}")
    finally:
        # Defensive rather than a fix for an observed leak: each of the three sections above
        # catches Exception itself, so the close was already reached on every normal path.
        # This covers the case where a future section stops catching, or raises before its own
        # try. The genuinely leaky idiom was `conn = get_connection(); with conn: ...;
        # conn.close()` with no except at all, which is what write_connection() now handles.
        conn.close()

    # Score adherence over the window. This used to sit under a TrainerRoad iCal fetch that
    # re-upserted the whole feed on every run; the feed is retired and the rolling plan writes
    # planned_workouts itself, so what gets scored is now the plan we actually prescribed.
    try:
        scored_from = today - timedelta(days=ADHERENCE_LOOKBACK_DAYS)
        # Never match today — the day isn't over yet, so a still-pending workout
        # would get wrongly scored "skipped" before there's been a chance to do it.
        match_end = today - timedelta(days=1)
        compliance = (match_planned_workouts(scored_from.isoformat(), match_end.isoformat())
                      if match_end >= scored_from else {})
        results["compliance"] = compliance
        log(f"[sync] adherence   {compliance}")
    except Exception as e:
        applog.error("sync.adherence_failed", e)
        results["errors"].append(f"adherence: {e}")
        log(f"[sync] adherence   ERROR: {e}")

    # Roll the plan forward. This runs *after* activities are in, because the gate grades
    # measured weekly volume — rolling first would decide a closed week from data this sync was
    # about to fetch, and that decision is then permanent.
    #
    # Best-effort for the same reason the calendar block is: a generator failure must not cost
    # us the activity and wellness rows already written above. Unlike the calendar it writes
    # through its own connection, so a failure here cannot leave a half-written horizon —
    # write_connection rolls back.
    #
    # **And only from a sync that actually succeeded.** A closed week is graded once and for good,
    # so grading one from a database whose activities fetch just failed would make a permanent
    # decision from data this run knew to be incomplete. When the fetch failed the horizon is still
    # built (from the decision as it stands in memory) and nothing is written to the record; the
    # next sync that gets the rides in makes it.
    try:
        with write_connection() as plan_conn:
            rolled = store_horizon(plan_conn, today, persist_state=activities_ok)
        results["plan"] = {k: v for k, v in rolled.items() if k != "sessions"}
        log(f"[sync] plan        T{rolled['tier']} {rolled['week_type']}, "
            f"{rolled['written']} sessions from {rolled['cutoff']}, "
            f"{rolled['weeks']} week rows  (backfilled {rolled['backfilled']})")
    except Exception as e:
        applog.error("sync.plan_failed", e)
        results["errors"].append(f"plan: {e}")
        log(f"[sync] plan        ERROR: {e}")

    done = {"activities": results["activities"], "wellness": results["wellness"],
            "events": results["events"], "notes_synced": results["notes"],
            "recorded_decisions": activities_ok, "problems": len(results["errors"]),
            "ms": int((time.monotonic() - started) * 1000)}
    (applog.warning if results["errors"] else applog.event)("sync.done", **done)
    return results

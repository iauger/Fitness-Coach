"""
Manually-maintained athlete profile — height, weight, FTP — tracked as a dated history
rather than a single overwritable value. This is the source of truth for these stats,
used in place of intervals.icu's synced FTP (icu_ftp), which has no user-facing edit
path and visibly lags real fitness changes (e.g. during a post-gap FTP drop).

Dated history matters because a *past* activity should be judged against the FTP that
was actually true then, not whatever it is today — get_metric(metric, as_of=activity_date)
looks up the value effective on that date, not the latest one.
"""

from datetime import date
from src.db.schema import read_connection, write_connection
from src.clock import utc_now_iso

METRICS = ("height_in", "weight_lbs", "ftp")


def set_metric(metric: str, value: float, effective_date: str = None, note: str = None) -> None:
    if metric not in METRICS:
        raise ValueError(f"unknown metric: {metric!r}. Must be one of {METRICS}")
    with write_connection() as conn:
        conn.execute("""
            INSERT INTO athlete_profile (metric, value, effective_date, note, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (metric, value, effective_date or date.today().isoformat(), note,
              utc_now_iso()))
    # analysis/load.py caches FTP history for the life of the process, so a long-lived one
    # (scripts/serve.py) would keep scoring rides against the old value after this write.
    # reset_ftp_cache existed for exactly this and had no caller.
    from src.analysis.load import reset_ftp_cache
    reset_ftp_cache()


def get_metric(metric: str, as_of: str = None) -> float | None:
    """
    Most recent value of `metric` effective on or before `as_of` (default: today).
    Falls back to the earliest known value if `as_of` predates our first record for this
    metric — e.g. asking about a ride from before the profile was ever seeded. That's a
    closer guess than returning nothing, since these stats don't reset to zero at the
    point we happened to start tracking them.
    """
    as_of = as_of or date.today().isoformat()
    with read_connection() as conn:
        row = conn.execute("""
            SELECT value FROM athlete_profile
            WHERE metric = ? AND effective_date <= ?
            ORDER BY effective_date DESC, id DESC LIMIT 1
        """, (metric, as_of)).fetchone()
        if row is None:
            row = conn.execute("""
                SELECT value FROM athlete_profile
                WHERE metric = ?
                ORDER BY effective_date ASC, id ASC LIMIT 1
            """, (metric,)).fetchone()
    return row["value"] if row else None


def current_profile() -> dict:
    """Latest value of every tracked metric, as of today."""
    return {m: get_metric(m) for m in METRICS}


def metric_history(metric: str) -> list[dict]:
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT value, effective_date, note FROM athlete_profile
            WHERE metric = ? ORDER BY effective_date
        """, (metric,)).fetchall()
    return [dict(r) for r in rows]

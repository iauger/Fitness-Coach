"""
Activity breakdown — sport distribution, recent training, load patterns.
"""

import json
from datetime import date, timedelta
from collections import defaultdict
from src.db.schema import read_connection
from src.analysis.dates import monday_of
from src.analysis.load import effective_tss
from src.clock import today_local
from src.sports import SPORT_GROUPS, sport_group  # noqa: F401  (re-export)


_sport_group = sport_group


def recent_activities(days: int = 28) -> list[dict]:
    """Last N days of activities with key fields extracted."""
    since = (today_local() - timedelta(days=days)).isoformat()
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT id, date, name, type, moving_time, distance, avg_hr, tss, calories, feel,
                   athlete_note, raw_json
            FROM activities
            WHERE date >= ? AND moving_time IS NOT NULL
            ORDER BY date DESC
        """, (since,)).fetchall()

    results = []
    for r in rows:
        raw = json.loads(r["raw_json"])
        # Corrected where intervals.icu analysed the ride against the wrong FTP (see load.py),
        # so the per-session figure the coach reads agrees with the weekly totals.
        load, load_corrected = effective_tss(r, raw)
        results.append({
            "date": r["date"],
            "name": r["name"],
            "type": r["type"],
            "sport_group": _sport_group(r["type"] or ""),
            "duration_min": round((r["moving_time"] or 0) / 60, 0),
            "distance_km": round((r["distance"] or 0) / 1000, 1) if r["distance"] else None,
            "avg_hr": r["avg_hr"],
            # Athlete's own session RPE, 1-10 (1 = easiest) on TrainerRoad's scale. Sparsely
            # logged — None on most sessions. See prompt.py's "Reading RPE" block.
            "rpe": r["feel"],
            # Athlete's own words about the session, synced from the intervals.icu chat thread.
            "note": r["athlete_note"],
            "load": round(load) if load else None,
            "load_corrected": load_corrected,
            "trimp": round(raw.get("trimp", 0), 1),
            "intensity": round(raw.get("icu_intensity", 0), 1),
        })
    return results


def weekly_load_by_sport(weeks: int | None = 12) -> list[dict]:
    """Weekly training load broken out by sport group. weeks=None pulls full history."""
    with read_connection() as conn:
        if weeks is None:
            rows = conn.execute("""
                SELECT date, type, raw_json FROM activities ORDER BY date ASC
            """).fetchall()
        else:
            since = (today_local() - timedelta(days=weeks * 7)).isoformat()
            rows = conn.execute("""
                SELECT date, type, raw_json FROM activities WHERE date >= ? ORDER BY date ASC
            """, (since,)).fetchall()

    weekly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        week_start = monday_of(date.fromisoformat(r["date"])).isoformat()
        raw = json.loads(r["raw_json"])
        load = raw.get("icu_training_load") or raw.get("hr_load") or 0
        group = _sport_group(r["type"] or "")
        weekly[week_start][group] += load
        weekly[week_start]["total"] += load

    return [
        {"week": week, **{k: round(v, 1) for k, v in loads.items()}}
        for week, loads in sorted(weekly.items())
    ]


def week_volume(conn, monday: date) -> dict:
    """
    What was actually trained in one Monday-to-Sunday week.

    The repo had no measured weekly-hours function before this — `derive.weekly_load` compares
    planned to actual *TSS*, and `yearly_volume` estimates hours as `tss / 60`. The rolling plan
    generator grades a week on ride minutes, so it needs the measured figure.

    Deliberately NOT built on `analysis/compliance.py`. A gate reading plan adherence inherits
    every way the matcher fails for reasons unrelated to training: strength is excluded by design
    (`planned_tss IS NULL`), HR-only and non-power rides score wrong, and an outdoor ride that
    doesn't resemble its prescription scores `partial`. Reading `moving_time` straight off
    `activities` inherits exactly one failure mode — a ride that didn't sync.

    Takes `conn` rather than opening its own, because the roller evaluates many weeks per run.
    Returns a zero-filled dict for a week with no rows: an empty week is a real answer here, not
    missing data, and it is the case the re-entry rule turns on.
    """
    start = monday_of(monday)
    end = start + timedelta(days=7)
    rows = conn.execute("""
        SELECT date, type, moving_time FROM activities
        WHERE date >= ? AND date < ? AND moving_time IS NOT NULL
    """, (start.isoformat(), end.isoformat())).fetchall()

    ride_sec = 0.0
    longest_sec = 0.0
    ride_days: set[str] = set()
    training_days: set[str] = set()
    strength = 0
    for r in rows:
        # The same table compliance.CYCLING_TYPES is derived from, so "is this a ride" has
        # one answer across the gate, the matcher and this rollup.
        group = sport_group(r["type"])
        secs = r["moving_time"] or 0
        day = r["date"][:10]
        training_days.add(day)
        if group == "cycling":
            ride_sec += secs
            longest_sec = max(longest_sec, secs)
            ride_days.add(day)
        elif group == "strength":
            strength += 1

    return {
        "week_start": start.isoformat(),
        "ride_min": round(ride_sec / 60, 1),
        "ride_days": len(ride_days),
        "longest_ride_min": round(longest_sec / 60, 1),
        "strength_sessions": strength,
        "training_days": len(training_days),
        "any_activity": bool(rows),
    }


def sport_distribution(days: int = 90) -> dict:
    """Sport group breakdown by activity count and time for recent period."""
    since = (today_local() - timedelta(days=days)).isoformat()
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT type, moving_time FROM activities WHERE date >= ?
        """, (since,)).fetchall()

    counts: dict[str, int] = defaultdict(int)
    minutes: dict[str, float] = defaultdict(float)
    for r in rows:
        group = _sport_group(r["type"] or "")
        counts[group] += 1
        minutes[group] += (r["moving_time"] or 0) / 60

    total_acts = sum(counts.values())
    total_min = sum(minutes.values())
    return {
        group: {
            "activities": counts[group],
            "pct_activities": round(counts[group] / total_acts * 100, 1) if total_acts else 0,
            "hours": round(minutes[group] / 60, 1),
            "pct_time": round(minutes[group] / total_min * 100, 1) if total_min else 0,
        }
        for group in sorted(counts, key=lambda g: counts[g], reverse=True)
    }


def yearly_volume() -> list[dict]:
    """
    Annual training load by year — uses wellness ctlLoad (TSS) which is computed
    server-side from all activities including Strava. Moving time from the activities
    table is incomplete because Strava-sourced activities are not exposed via the API.
    """
    import json as _json
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT date, raw_json FROM wellness ORDER BY date"
        ).fetchall()

    by_year: dict[str, float] = {}
    for r in rows:
        raw = _json.loads(r["raw_json"])
        yr = r["date"][:4]
        by_year[yr] = by_year.get(yr, 0) + (raw.get("ctlLoad") or 0)

    return [
        {"year": yr, "tss": round(tss, 0), "hours_est": round(tss / 60, 0)}
        for yr, tss in sorted(by_year.items())
    ]

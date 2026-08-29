"""
Activity breakdown — sport distribution, recent training, load patterns.
"""

import json
from datetime import date, timedelta
from collections import defaultdict
from src.db.schema import get_connection
from src.analysis.load import effective_tss

SPORT_GROUPS = {
    "Ride": "cycling", "VirtualRide": "cycling", "GravelRide": "cycling",
    "MountainBikeRide": "cycling", "EBikeRide": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
    "Swim": "swimming", "OpenWaterSwim": "swimming",
    "WeightTraining": "strength", "Workout": "strength",
    "Yoga": "yoga", "Pilates": "yoga",
    "Walk": "walk", "Hike": "hike",
}


def _sport_group(activity_type: str) -> str:
    return SPORT_GROUPS.get(activity_type, "other")


def recent_activities(days: int = 28) -> list[dict]:
    """Last N days of activities with key fields extracted."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, date, name, type, moving_time, distance, avg_hr, tss, calories, feel,
               athlete_note, raw_json
        FROM activities
        WHERE date >= ? AND moving_time IS NOT NULL
        ORDER BY date DESC
    """, (since,)).fetchall()
    conn.close()

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
    conn = get_connection()
    if weeks is None:
        rows = conn.execute("""
            SELECT date, type, raw_json FROM activities ORDER BY date ASC
        """).fetchall()
    else:
        since = (date.today() - timedelta(days=weeks * 7)).isoformat()
        rows = conn.execute("""
            SELECT date, type, raw_json FROM activities WHERE date >= ? ORDER BY date ASC
        """, (since,)).fetchall()
    conn.close()

    weekly: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        d = date.fromisoformat(r["date"])
        week_start = (d - timedelta(days=d.weekday())).isoformat()
        raw = json.loads(r["raw_json"])
        load = raw.get("icu_training_load") or raw.get("hr_load") or 0
        group = _sport_group(r["type"] or "")
        weekly[week_start][group] += load
        weekly[week_start]["total"] += load

    return [
        {"week": week, **{k: round(v, 1) for k, v in loads.items()}}
        for week, loads in sorted(weekly.items())
    ]


def sport_distribution(days: int = 90) -> dict:
    """Sport group breakdown by activity count and time for recent period."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT type, moving_time FROM activities WHERE date >= ?
    """, (since,)).fetchall()
    conn.close()

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
    conn = get_connection()
    rows = conn.execute(
        "SELECT date, raw_json FROM wellness ORDER BY date"
    ).fetchall()
    conn.close()

    by_year: dict[str, float] = {}
    for r in rows:
        raw = _json.loads(r["raw_json"])
        yr = r["date"][:4]
        by_year[yr] = by_year.get(yr, 0) + (raw.get("ctlLoad") or 0)

    return [
        {"year": yr, "tss": round(tss, 0), "hours_est": round(tss / 60, 0)}
        for yr, tss in sorted(by_year.items())
    ]

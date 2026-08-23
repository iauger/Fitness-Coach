"""
Plan adherence — matches Google Calendar planned workouts (synced from TrainerRoad)
against actual activities and reports compliance.
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from src.athlete.profile import get_metric
from src.db.schema import get_connection

CYCLING_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide")

COMPLETED_PCT = 85
PARTIAL_PCT = 40


def _manual_tss(np: float | None, moving_time_sec: float | None, activity_date: str) -> float | None:
    """
    Recompute TSS against our own tracked FTP history (src/athlete/profile.py) instead of
    trusting intervals.icu's icu_training_load, which is itself derived from icu_ftp — a
    value that has no user-facing edit path and visibly lags real FTP changes (e.g. during
    a post-gap rebuild, icu_ftp can sit well above actual current FTP for a long stretch).
    Standard formula: TSS = (duration_sec * IF^2 / 3600) * 100, where IF = NP / FTP.
    Looks up FTP as of the activity's own date, not today's, so old rides are judged fairly.
    """
    if not np or not moving_time_sec:
        return None
    ftp = get_metric("ftp", as_of=activity_date)
    if not ftp:
        return None
    intensity_factor = np / ftp
    return round((moving_time_sec * intensity_factor ** 2 / 3600) * 100, 1)


def _activity_load(raw_json: str) -> float | None:
    """Fallback for activities with no power meter (HR-based load estimate from intervals.icu)."""
    raw = json.loads(raw_json or "{}")
    return raw.get("icu_training_load") or raw.get("power_load") or raw.get("hr_load")


def _cycling_activities_on(conn, day: str) -> list[dict]:
    rows = conn.execute(f"""
        SELECT id, moving_time, np, raw_json FROM activities
        WHERE date = ? AND type IN ({",".join("?" * len(CYCLING_TYPES))})
        ORDER BY moving_time DESC
    """, (day, *CYCLING_TYPES)).fetchall()
    return [dict(r) for r in rows]


def match_planned_workouts(start: str, end: str) -> dict:
    """
    Match each planned_workouts row in [start, end] to an activity on the same date,
    set matched_activity_id / compliance_status / compliance_pct.
    Returns counts by status. Only meant to be called for dates that have already happened.

    Only workouts with a planned_tss are matched — that's the signal a session is a bike
    workout with power targets. Non-power sessions (mobility, strength, etc.) are still
    stored in planned_workouts for later analysis, just not scored for adherence here since
    there's no reliable way to match them against the activities table's cycling-only search.
    """
    conn = get_connection()
    planned = [dict(r) for r in conn.execute("""
        SELECT id, date, planned_tss, planned_duration_min
        FROM planned_workouts
        WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
        ORDER BY date
    """, (start, end)).fetchall()]

    used_activity_ids: set[str] = set()
    counts: dict[str, int] = defaultdict(int)

    with conn:
        for pw in planned:
            candidates = [a for a in _cycling_activities_on(conn, pw["date"])
                         if a["id"] not in used_activity_ids]

            if not candidates:
                conn.execute("""
                    UPDATE planned_workouts
                    SET matched_activity_id = NULL, compliance_status = 'skipped', compliance_pct = 0
                    WHERE id = ?
                """, (pw["id"],))
                counts["skipped"] += 1
                continue

            planned_dur = pw["planned_duration_min"] or 0
            best = (min(candidates, key=lambda a: abs((a["moving_time"] or 0) / 60 - planned_dur))
                   if planned_dur else candidates[0])
            used_activity_ids.add(best["id"])

            actual_load = (_manual_tss(best.get("np"), best.get("moving_time"), pw["date"])
                          or _activity_load(best["raw_json"]))
            if pw["planned_tss"] and actual_load:
                pct = round(actual_load / pw["planned_tss"] * 100, 1)
            elif planned_dur:
                pct = round((best["moving_time"] or 0) / 60 / planned_dur * 100, 1)
            else:
                pct = None

            if pct is None or pct >= COMPLETED_PCT:
                status = "completed"
            elif pct >= PARTIAL_PCT:
                status = "partial"
            else:
                status = "partial"  # matched but well short — still logged as partial, not skipped

            conn.execute("""
                UPDATE planned_workouts
                SET matched_activity_id = ?, compliance_status = ?, compliance_pct = ?
                WHERE id = ?
            """, (best["id"], status, pct, pw["id"]))
            counts[status] += 1

    conn.close()
    return dict(counts)


def compliance_summary(weeks: int = 8) -> dict:
    """Adherence rate over the trailing N weeks (only dates already matched)."""
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT compliance_status, COUNT(*) as n FROM planned_workouts
        WHERE date >= ? AND date <= ? AND compliance_status IS NOT NULL
        GROUP BY compliance_status
    """, (since, date.today().isoformat())).fetchall()
    conn.close()

    counts = {r["compliance_status"]: r["n"] for r in rows}
    total = sum(counts.values())
    completed = counts.get("completed", 0)
    return {
        "weeks": weeks,
        "total_planned": total,
        "completed": completed,
        "partial": counts.get("partial", 0),
        "skipped": counts.get("skipped", 0),
        "adherence_pct": round(completed / total * 100, 1) if total else None,
    }


def recent_planned_workouts(days: int = 14) -> list[dict]:
    """Recent planned workouts with their compliance outcome, most recent first."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, name, planned_tss, planned_duration_min, compliance_status, compliance_pct
        FROM planned_workouts WHERE date >= ? ORDER BY date DESC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

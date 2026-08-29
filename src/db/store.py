import json
import sqlite3
from datetime import date
from pathlib import Path
from .schema import get_connection, DB_PATH


_SPORT_GROUPS = {
    "Ride": "cycling", "VirtualRide": "cycling", "GravelRide": "cycling",
    "MountainBikeRide": "cycling", "EBikeRide": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
    "Swim": "swimming", "OpenWaterSwim": "swimming",
    "WeightTraining": "strength", "Workout": "strength",
    "Yoga": "yoga", "Pilates": "yoga",
    "Walk": "walk", "Hike": "hike",
}


def _has_near_duplicate(conn, date: str, moving_time: int, activity_type: str,
                        exclude_id: str, tolerance: int = 60) -> bool:
    """True if a same-sport activity with similar duration already exists on this date."""
    group = _SPORT_GROUPS.get(activity_type or "", "other")
    # Fetch same-date activities with duration within tolerance
    rows = conn.execute("""
        SELECT type FROM activities
        WHERE date = ? AND id != ?
          AND moving_time IS NOT NULL
          AND ABS(moving_time - ?) <= ?
    """, (date, exclude_id, moving_time, tolerance)).fetchall()
    return any(_SPORT_GROUPS.get(r["type"] or "", "other") == group for r in rows)


def upsert_activities(activities: list[dict], db_path: Path = DB_PATH) -> int:
    conn = get_connection(db_path)
    inserted = 0
    with conn:
        for a in activities:
            act_id = str(a.get("id", ""))
            act_date = a.get("start_date_local", a.get("date", ""))[:10]
            act_time = a.get("moving_time") or a.get("movingTime")
            act_type = a.get("type", "")
            # Skip if a same-sport activity with similar duration already exists
            if act_time and _has_near_duplicate(conn, act_date, act_time, act_type, act_id):
                continue
            # ON CONFLICT DO UPDATE, not INSERT OR REPLACE: the latter is implemented as
            # DELETE + INSERT, so any column not named here would silently reset to NULL on
            # every sync. Since incremental_sync() re-pulls the last ~2 days at the start of
            # every coach session, athlete-authored columns would be wiped within days.
            # Only synced columns belong in the SET list below — never add an athlete-authored
            # one (notes, manual RPE), or it will be overwritten by the next sync.
            conn.execute("""
                INSERT INTO activities (
                    id, date, name, type, sport, moving_time, distance,
                    elevation, avg_hr, max_hr, avg_power, max_power, np,
                    tss, ctl, atl, tsb, feel, calories, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    name=excluded.name,
                    type=excluded.type,
                    sport=excluded.sport,
                    moving_time=excluded.moving_time,
                    distance=excluded.distance,
                    elevation=excluded.elevation,
                    avg_hr=excluded.avg_hr,
                    max_hr=excluded.max_hr,
                    avg_power=excluded.avg_power,
                    max_power=excluded.max_power,
                    np=excluded.np,
                    tss=excluded.tss,
                    ctl=excluded.ctl,
                    atl=excluded.atl,
                    tsb=excluded.tsb,
                    feel=excluded.feel,
                    calories=excluded.calories,
                    raw_json=excluded.raw_json
            """, (
                str(a.get("id", "")),
                a.get("start_date_local", a.get("date", ""))[:10],
                a.get("name"),
                a.get("type"),
                a.get("sport"),
                a.get("moving_time") or a.get("movingTime"),
                a.get("distance"),
                a.get("total_elevation_gain") or a.get("elevation"),
                a.get("average_heartrate") or a.get("avgHr"),
                a.get("max_heartrate") or a.get("maxHr"),
                a.get("icu_average_watts") or a.get("average_watts") or a.get("avgPower"),
                a.get("icu_max_watts") or a.get("max_watts") or a.get("maxPower"),
                a.get("icu_weighted_avg_watts") or a.get("weighted_average_watts") or a.get("normalizedPower"),
                a.get("icu_training_load") or a.get("training_stress_score") or a.get("tss"),
                a.get("icu_ctl") or a.get("ctl"),
                a.get("icu_atl") or a.get("atl"),
                a.get("icu_tsb") or a.get("tsb"),
                # icu_rpe is the field intervals.icu actually populates (154 activities as of
                # 2026-08-23); perceived_exertion is null on every record we've ever synced.
                # Mapping only the latter silently dropped 130 real RPE values — same bug class
                # as the Session 7 avg_power/np/tss mismatch. 0 means "unset", so `or` chaining
                # correctly falls through to the next candidate.
                a.get("icu_rpe") or a.get("perceived_exertion") or a.get("feel"),
                a.get("kilojoules") or a.get("calories"),
                json.dumps(a),
            ))
            inserted += 1
    conn.close()
    return inserted


def update_activity_notes(notes: dict[str, str | None], db_path: Path = DB_PATH) -> int:
    """
    Write athlete_note for the given activity ids. Keyed {activity_id: note_text}.

    Deliberately a targeted UPDATE rather than a column on upsert_activities: notes are
    fetched only for activities carrying an icu_chat_id, so folding athlete_note into the
    main upsert would null it out on every other activity in the same sync window.
    """
    if not notes:
        return 0
    conn = get_connection(db_path)
    updated = 0
    with conn:
        for act_id, text in notes.items():
            cur = conn.execute(
                "UPDATE activities SET athlete_note = ? WHERE id = ?", (text, act_id)
            )
            updated += cur.rowcount
    conn.close()
    return updated


def upsert_wellness(wellness: list[dict], db_path: Path = DB_PATH) -> int:
    conn = get_connection(db_path)
    inserted = 0
    with conn:
        for w in wellness:
            conn.execute("""
                INSERT OR REPLACE INTO wellness (
                    date, ctl, atl, tsb, rhr, hrv, hrv_score,
                    sleep_hrs, sleep_score, weight_kg, steps, kcal, feel, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                w.get("id", "")[:10],  # wellness uses date as id
                w.get("ctl"),
                w.get("atl"),
                w.get("tsb"),
                w.get("restingHR"),
                w.get("hrv"),
                w.get("hrvScore"),
                w.get("sleepSecs") / 3600 if w.get("sleepSecs") else None,
                w.get("sleepScore"),
                w.get("weight"),
                w.get("steps"),
                w.get("kcal"),
                w.get("feel"),
                json.dumps(w),
            ))
            inserted += 1
    conn.close()
    return inserted


def upsert_events(events: list[dict], db_path: Path = DB_PATH) -> int:
    conn = get_connection(db_path)
    inserted = 0
    with conn:
        for e in events:
            conn.execute("""
                INSERT OR REPLACE INTO events (id, date, name, category, type, distance_km, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                str(e.get("id", "")),
                e.get("start_date_local", e.get("date", ""))[:10],
                e.get("name"),
                e.get("category"),
                e.get("type"),
                e.get("distance_km"),
                json.dumps(e),
            ))
            inserted += 1
    conn.close()
    return inserted


def upsert_planned_workouts(workouts: list[dict], db_path: Path = DB_PATH) -> int:
    conn = get_connection(db_path)
    inserted = 0
    with conn:
        for w in workouts:
            conn.execute("""
                INSERT INTO planned_workouts (
                    id, date, name, planned_tss, planned_duration_min, planned_if,
                    planned_kj, description, workout_type, workout_url, gcal_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,
                    name=excluded.name,
                    planned_tss=excluded.planned_tss,
                    planned_duration_min=excluded.planned_duration_min,
                    planned_if=excluded.planned_if,
                    planned_kj=excluded.planned_kj,
                    description=excluded.description,
                    workout_type=excluded.workout_type,
                    workout_url=excluded.workout_url,
                    gcal_updated=excluded.gcal_updated
            """, (
                w["id"], w["date"], w["name"], w.get("planned_tss"),
                w.get("planned_duration_min"), w.get("planned_if"),
                w.get("planned_kj"), w.get("description"), w.get("workout_type"),
                w.get("workout_url"), w.get("gcal_updated"),
            ))
            inserted += 1
    conn.close()
    return inserted


def log_sync(conn: sqlite3.Connection, data_type: str, start: date, end: date,
             records: int, status: str = "ok") -> None:
    from datetime import datetime
    conn.execute("""
        INSERT INTO sync_log (synced_at, data_type, start_date, end_date, records, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (datetime.utcnow().isoformat(), data_type, start.isoformat(), end.isoformat(), records, status))

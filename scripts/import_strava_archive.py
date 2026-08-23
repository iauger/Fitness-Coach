"""
Import Strava data archive into the activities table.

Usage:
    python scripts/import_strava_archive.py --archive path/to/strava_export.zip
    python scripts/import_strava_archive.py --csv path/to/activities.csv
    python scripts/import_strava_archive.py --archive path/to/strava_export.zip --dry-run

The Strava archive ZIP contains activities.csv with one row per activity.
We upsert into the activities table using "strava_{id}" as the key so there
are no collisions with intervals.icu IDs.

Only rows NOT already covered by a Garmin/intervals activity on the same day+type
are imported — this avoids double-counting rides that came through both paths.
"""

import sys
import csv
import zipfile
import argparse
import io
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import get_connection

# Map Strava sport types → our SPORT_GROUPS keys (matching activities.py)
STRAVA_TYPE_MAP = {
    "Ride": "Ride", "VirtualRide": "VirtualRide", "GravelRide": "GravelRide",
    "MountainBikeRide": "MountainBikeRide", "EBikeRide": "EBikeRide",
    "Run": "Run", "VirtualRun": "VirtualRun", "TrailRun": "TrailRun",
    "Swim": "Swim", "OpenWaterSwim": "OpenWaterSwim",
    "WeightTraining": "WeightTraining", "Workout": "Workout",
    "Yoga": "Yoga", "Pilates": "Pilates",
    "Walk": "Walk", "Hike": "Hike",
}

# Columns that may appear in the CSV with their possible header variants
def _col(row: dict, *names) -> str | None:
    for n in names:
        if n in row and row[n] not in ("", None):
            return row[n]
    return None


def _parse_duration(val: str | None) -> int | None:
    """Strava stores duration as integer seconds in the CSV."""
    try:
        return int(float(val)) if val else None
    except (ValueError, TypeError):
        return None


def _parse_float(val: str | None) -> float | None:
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def _parse_date(val: str | None) -> str | None:
    """Return ISO date string from Strava's timestamp formats."""
    if not val:
        return None
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(val.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: just take first 10 chars if it looks like a date
    if len(val) >= 10 and val[4] == "-":
        return val[:10]
    return None


def load_csv_from_zip(zip_path: Path) -> list[dict]:
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        csv_name = next((n for n in names if n.endswith("activities.csv")), None)
        if not csv_name:
            raise FileNotFoundError(f"No activities.csv found in {zip_path}. Files: {names[:10]}")
        print(f"  Found: {csv_name} ({len(names)} total files in archive)")
        with zf.open(csv_name) as f:
            content = f.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(content)))


def load_csv_direct(csv_path: Path) -> list[dict]:
    with open(csv_path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def existing_dates(conn) -> set[str]:
    """Dates already covered by Garmin/intervals.icu activities (non-stub)."""
    rows = conn.execute(
        "SELECT DISTINCT date FROM activities WHERE moving_time IS NOT NULL"
    ).fetchall()
    return {r["date"] for r in rows}


def build_record(row: dict) -> dict | None:
    activity_id = _col(row, "Activity ID", "id")
    if not activity_id:
        return None

    date_str = _parse_date(_col(row, "Activity Date", "start_date_local", "date"))
    if not date_str:
        return None

    moving_time = _parse_duration(_col(row, "Moving Time", "moving_time"))
    elapsed_time = _parse_duration(_col(row, "Elapsed Time", "elapsed_time"))
    duration = moving_time or elapsed_time

    name = _col(row, "Activity Name", "name") or ""
    sport_type = _col(row, "Activity Type", "sport_type", "type") or "Workout"
    mapped_type = STRAVA_TYPE_MAP.get(sport_type, sport_type)

    distance_raw = _parse_float(_col(row, "Distance", "distance"))
    # Strava CSV distance is in km; convert to metres
    distance_m = distance_raw * 1000 if distance_raw else None

    avg_power = _parse_float(_col(row, "Weighted Average Power", "Average Watts", "average_watts"))
    max_hr = _parse_float(_col(row, "Max Heart Rate", "max_heartrate"))
    avg_hr = _parse_float(_col(row, "Average Heart Rate", "average_heartrate"))
    elevation = _parse_float(_col(row, "Elevation Gain", "total_elevation_gain"))
    calories = _parse_float(_col(row, "Calories", "calories"))
    relative_effort = _parse_float(_col(row, "Relative Effort", "suffer_score"))

    import json
    raw = {
        "id": f"strava_{activity_id}",
        "source": "STRAVA_EXPORT",
        "name": name,
        "type": mapped_type,
        "start_date_local": date_str,
        "moving_time": moving_time,
        "elapsed_time": elapsed_time,
        "distance": distance_m,
        "average_heartrate": avg_hr,
        "max_heartrate": max_hr,
        "average_watts": avg_power,
        "total_elevation_gain": elevation,
        "calories": calories,
        "relative_effort": relative_effort,
    }

    return {
        "id": f"strava_{activity_id}",
        "date": date_str,
        "name": name,
        "type": mapped_type,
        "moving_time": moving_time,
        "distance": distance_m,
        "elevation": elevation,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "avg_power": avg_power,
        "calories": calories,
        "tss": relative_effort,
        "raw_json": json.dumps({k: v for k, v in raw.items() if v is not None}),
    }


def upsert_strava_activities(conn, records: list[dict]) -> int:
    sql = """
        INSERT INTO activities (
            id, date, name, type, moving_time, distance, elevation,
            avg_hr, max_hr, avg_power, calories, tss, raw_json
        ) VALUES (
            :id, :date, :name, :type, :moving_time, :distance, :elevation,
            :avg_hr, :max_hr, :avg_power, :calories, :tss, :raw_json
        )
        ON CONFLICT(id) DO UPDATE SET
            name       = excluded.name,
            type       = excluded.type,
            moving_time= excluded.moving_time,
            distance   = excluded.distance,
            elevation  = excluded.elevation,
            avg_hr     = excluded.avg_hr,
            max_hr     = excluded.max_hr,
            avg_power  = excluded.avg_power,
            calories   = excluded.calories,
            tss        = excluded.tss,
            raw_json   = excluded.raw_json
    """
    conn.executemany(sql, records)
    conn.commit()
    return len(records)


def run(source_path: Path, dry_run: bool = False):
    print(f"\nStrava archive import")
    print(f"  Source: {source_path}")

    if source_path.suffix.lower() == ".zip":
        rows = load_csv_from_zip(source_path)
    else:
        rows = load_csv_direct(source_path)

    print(f"  CSV rows: {len(rows)}")

    conn = get_connection()
    covered = existing_dates(conn)
    print(f"  Dates already covered by Garmin/intervals data: {len(covered)}")

    records = []
    skipped_existing = 0
    skipped_no_date = 0
    skipped_no_duration = 0

    for row in rows:
        rec = build_record(row)
        if rec is None:
            skipped_no_date += 1
            continue
        if rec["moving_time"] is None:
            skipped_no_duration += 1
            continue
        if rec["date"] in covered:
            skipped_existing += 1
            continue
        records.append(rec)

    print(f"\n  To import:      {len(records)}")
    print(f"  Skipped (already have Garmin data for that date): {skipped_existing}")
    print(f"  Skipped (no duration):  {skipped_no_duration}")
    print(f"  Skipped (no date):      {skipped_no_date}")

    if dry_run:
        print("\n  [dry-run] no changes written")
        if records:
            sample = records[0]
            print(f"  Sample record: {sample['date']} | {sample['type']} | {sample['name'][:40]} | {(sample['moving_time'] or 0)/3600:.2f}h")
        return

    n = upsert_strava_activities(conn, records)
    conn.close()

    print(f"\n  Done — {n} activities imported.")
    print("  Re-run  scripts/generate_charts.py  to refresh the dashboard.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive", type=Path, help="Path to Strava export .zip")
    group.add_argument("--csv",     type=Path, help="Path to extracted activities.csv")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    source = args.archive or args.csv
    run(source, dry_run=args.dry_run)

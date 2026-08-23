"""
Detect and remove duplicate activities.

Duplicates are activities on the same date whose moving_time is within 5 minutes
of each other — same workout recorded by both Garmin and the Strava/TR archive.

Keeps the record with the most non-null fields (typically Garmin, which has
power, HR, TSS etc). Discards the thinner duplicate.

Usage:
    python scripts/dedupe_activities.py            # preview only
    python scripts/dedupe_activities.py --delete   # delete duplicates
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import get_connection

DURATION_TOLERANCE_SECS = 60  # 1 minute — covers Garmin/Strava clock drift


def _richness(row: dict) -> int:
    """Count non-null fields in raw_json as a proxy for data completeness."""
    try:
        raw = json.loads(row["raw_json"] or "{}")
        return sum(1 for v in raw.values() if v is not None)
    except Exception:
        return 0


SPORT_GROUPS = {
    "Ride": "cycling", "VirtualRide": "cycling", "GravelRide": "cycling",
    "MountainBikeRide": "cycling", "EBikeRide": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
    "Swim": "swimming", "OpenWaterSwim": "swimming",
    "WeightTraining": "strength", "Workout": "strength",
    "Yoga": "yoga", "Pilates": "yoga",
    "Walk": "walk", "Hike": "hike",
}


def _sport_group(activity_type: str | None) -> str:
    return SPORT_GROUPS.get(activity_type or "", "other")


def find_duplicates(conn) -> list[tuple]:
    """Return list of (keep_id, discard_id, date, type_a, type_b, duration_a, duration_b)."""
    rows = conn.execute("""
        SELECT id, date, type, moving_time, raw_json
        FROM activities
        WHERE moving_time IS NOT NULL
        ORDER BY date, moving_time
    """).fetchall()

    rows = [dict(r) for r in rows]

    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    duplicates = []
    seen_ids: set[str] = set()

    for date, day_rows in by_date.items():
        for i in range(len(day_rows)):
            for j in range(i + 1, len(day_rows)):
                a, b = day_rows[i], day_rows[j]
                if a["id"] in seen_ids or b["id"] in seen_ids:
                    continue
                # Only flag same sport group (won't confuse a ride with a run)
                if _sport_group(a["type"]) != _sport_group(b["type"]):
                    continue
                diff = abs((a["moving_time"] or 0) - (b["moving_time"] or 0))
                if diff <= DURATION_TOLERANCE_SECS:
                    if _richness(a) >= _richness(b):
                        keep, discard = a, b
                    else:
                        keep, discard = b, a
                    seen_ids.add(discard["id"])
                    duplicates.append((
                        keep["id"], discard["id"],
                        date,
                        keep["type"], discard["type"],
                        keep["moving_time"], discard["moving_time"],
                    ))

    return duplicates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true",
                        help="Delete duplicates (default is preview only)")
    args = parser.parse_args()

    conn = get_connection()
    dupes = find_duplicates(conn)

    if not dupes:
        print("No duplicates found.")
        conn.close()
        return

    total_before = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    print(f"Activities before: {total_before}")
    print(f"Duplicates found:  {len(dupes)}\n")

    print(f"{'Date':<12} {'Keep type':<14} {'Discard type':<14} {'Keep dur':>9} {'Disc dur':>9}  Keep ID / Discard ID")
    print("-" * 100)
    for keep_id, discard_id, date, kt, dt, kd, dd in dupes:
        print(f"{date:<12} {(kt or '?'):<14} {(dt or '?'):<14} {kd:>9}s {dd:>9}s  {keep_id} / {discard_id}")

    if not args.delete:
        print(f"\n[preview] Run with --delete to remove the {len(dupes)} duplicate(s).")
        conn.close()
        return

    discard_ids = [d[1] for d in dupes]
    placeholders = ",".join("?" * len(discard_ids))
    conn.execute(f"DELETE FROM activities WHERE id IN ({placeholders})", discard_ids)
    conn.commit()

    total_after = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    print(f"\nDeleted {len(dupes)} duplicates.")
    print(f"Activities after:  {total_after}")
    conn.close()


if __name__ == "__main__":
    main()

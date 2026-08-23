"""
Seed the goals table with known athlete targets.
Safe to re-run — skips goals that already exist (matched by description).

Usage:
    python scripts/seed_goals.py
    python scripts/seed_goals.py --reset   (clears and re-seeds)
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import get_connection

GOALS = [
    {
        "description": "Return to consistent training rhythm after 6-month gap",
        "target_date": None,
        "priority": "primary",
    },
    {
        "description": "Cyclocross racing fall 2027",
        "target_date": "2027-10-01",
        "priority": "primary",
    },
    {
        "description": "Gravel racing spring/summer 2027",
        "target_date": "2027-05-01",
        "priority": "secondary",
    },
    {
        "description": "Baseline fitness: 120-mile ride not an overreach",
        "target_date": "2027-06-01",
        "priority": "secondary",
    },
    {
        "description": "Introduce multi-sport variety: yoga, strength, running, swimming",
        "target_date": None,
        "priority": "secondary",
    },
]


def seed(reset: bool = False) -> None:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()

    with conn:
        if reset:
            conn.execute("DELETE FROM goals")
            print("Goals table cleared.")

        existing = {
            row["description"]
            for row in conn.execute("SELECT description FROM goals").fetchall()
        }

        added = 0
        for g in GOALS:
            if g["description"] in existing:
                print(f"  skip (exists): {g['description'][:60]}")
                continue
            conn.execute("""
                INSERT INTO goals (description, target_date, priority, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
            """, (g["description"], g["target_date"], g["priority"], now, now))
            print(f"  added: {g['description'][:60]}")
            added += 1

    conn.close()
    print(f"\n{added} goal(s) added.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Clear all goals before seeding")
    args = parser.parse_args()
    seed(reset=args.reset)

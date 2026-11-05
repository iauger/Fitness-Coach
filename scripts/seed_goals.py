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

from src.config import config
from src.db.schema import get_connection

def goals_from_config() -> list[dict]:
    """
    The configured goals, in the shape the `goals` table wants.

    This was a hardcoded list of five of the athlete's own targets. The table is still the
    source of truth once seeded — it is what an in-conversation edit writes to and what
    `_active_goals` reads — so this only ever fills an empty one.
    """
    return [
        {"description": g.description,
         "target_date": g.date.isoformat() if g.date else None,
         # The table predates `opt_in` and stores a two-valued priority. An opt-in goal is one
         # the rotation does not taper for, which is a planning distinction rather than an
         # importance one, so it lands as secondary here.
         "priority": "secondary" if g.priority == "opt_in" else g.priority}
        for g in config().goals
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
        for g in goals_from_config():
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

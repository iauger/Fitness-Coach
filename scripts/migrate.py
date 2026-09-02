"""
Inspect and apply schema migrations.

    python scripts/migrate.py              # show status, apply nothing
    python scripts/migrate.py --apply      # apply pending migrations
    python scripts/migrate.py --db path    # target another database file

Migrations also run automatically wherever init_db()/migrate_db() are already called, including
scripts/serve.py at boot. This script exists so the state can be inspected without starting
anything, and so applying is an explicit act when you want it to be.
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import DB_PATH
from src.db.migrations import migrate, status


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="apply pending migrations")
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args()

    if args.apply:
        applied = migrate(args.db, verbose=not args.as_json)
        if not args.as_json:
            print(f"  {len(applied)} migration(s) applied." if applied
                  else "  Already up to date.")

    st = status(args.db)
    if args.as_json:
        print(json.dumps(st, indent=2))
        return 0

    print(f"\n  database  {st['db_path']}")
    print(f"  version   {st['current_version']} of {st['latest_version']}")
    if st["history"]:
        print("\n  applied:")
        for h in st["history"]:
            print(f"    {h['version']:03d}  {h['name']}  ({h['applied_at'][:19]})")
    if st["pending"]:
        print("\n  pending:")
        for pnd in st["pending"]:
            print(f"    {pnd['version']:03d}  {pnd['name']}")
        print("\n  run with --apply to bring the database up to date.")
    else:
        print("\n  up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

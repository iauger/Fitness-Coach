"""
Run one maintenance cycle: pull from intervals.icu, roll the plan, push the horizon.

    python scripts/sync_now.py              # the full cycle
    python scripts/sync_now.py --no-push    # pull and roll only, leave the calendar alone
    python scripts/sync_now.py --verbose    # show each step

This is the entry point the Windows Scheduled Task runs, and it is the actual fix for the
problem that started the rebuild: six weeks of unsynced data, because syncing was something a
person had to remember to do.

Exit codes matter here, because a scheduled task reports them: 0 for a clean run or a skipped
one, 1 if anything in the cycle failed. A failure is logged and the next run tries again —
nothing retries in-process, since the schedule is the retry.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.clock import utc_now_iso
from src.db.schema import migrate_db
from src.maintenance import run_cycle, summarise

# Written next to the database rather than into the repo: it is operational output, it grows
# without bound, and data/ is already gitignored.
LOG_PATH = Path(__file__).parent.parent / "data" / "maintenance.log"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--no-push", action="store_true",
                   help="pull and roll, but do not touch the intervals.icu calendar")
    p.add_argument("--verbose", action="store_true", help="show each sync step")
    p.add_argument("--no-log", action="store_true", help="do not append to the log file")
    args = p.parse_args()

    # A scheduled task may be the first thing to run after a code update, so the schema cannot
    # be assumed current.
    migrate_db()

    result = run_cycle(push_enabled=not args.no_push, silent=not args.verbose)
    line = summarise(result)
    print(line)

    if not args.no_log:
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(f"{utc_now_iso()}  {line}\n")
        except OSError as exc:
            # Losing the log must not fail the cycle that already succeeded.
            print(f"  (could not write {LOG_PATH}: {exc})")

    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())

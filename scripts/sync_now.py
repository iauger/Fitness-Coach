"""
Run one maintenance cycle: pull from intervals.icu, roll the plan, push the horizon.

    python scripts/sync_now.py              # the full cycle
    python scripts/sync_now.py --no-push    # pull and roll only, leave the calendar alone
    python scripts/sync_now.py --verbose    # show each step

This is the entry point the Windows Scheduled Task runs, and it is the actual fix for the
problem that started the rebuild: six weeks of unsynced data, because syncing was something a
person had to remember to do.

**Everything here assumes nobody is watching.** The task runs under `pythonw.exe`, which has
no console, so on Windows `sys.stdout` can be `None` — touching `sys.stdout.encoding` at
import would then raise `AttributeError` before the first line of work, every hour, with
nothing printed anywhere to say so. That is the same silent-stoppage failure the scheduler
exists to prevent, so the log file is the real output and the console is a convenience.

Exit codes matter, because a scheduled task records them: 0 for a clean or skipped run, 1 if
anything failed. Nothing retries in-process — the schedule is the retry.
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Written next to the database rather than into the repo: it is operational output, it grows
# without bound, and data/ is already gitignored.
LOG_PATH = Path(__file__).parent.parent / "data" / "maintenance.log"


def _init_console() -> None:
    """
    Give the process a usable stdout and stderr, then make them UTF-8. Never fatal.

    Under `pythonw.exe` with no console — which is how the Scheduled Task runs this — Windows
    gives Python `None` for *both* streams. Guarding only the calls in this file is not enough:
    anything further down that writes a warning or a traceback hits `None.write` and the run
    dies with nothing recorded anywhere. Measured, not assumed: with `stdout=None` alone the
    cycle completed and logged; adding `stderr=None` made it exit 1 in silence.

    Pointing both at the null device means every writer downstream has somewhere to go, and
    the log file stays the record that matters.
    """
    for name in ("stdout", "stderr"):
        try:
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass
    try:
        if sys.stdout is not None and sys.stdout.encoding != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _say(line: str, log: bool = True) -> None:
    """
    Report a line to whoever is listening, which may be nobody.

    Both halves are best-effort and independent: a console that cannot take the text must not
    cost us the log entry, and a log that cannot be written must not crash a cycle that has
    already done its work.
    """
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass
    if not log:
        return
    try:
        from src.clock import utc_now_iso

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{utc_now_iso()}  {line}\n")
    except Exception:
        pass


def main() -> int:
    _init_console()
    p = argparse.ArgumentParser()
    p.add_argument("--no-push", action="store_true",
                   help="pull and roll, but do not touch the intervals.icu calendar")
    p.add_argument("--verbose", action="store_true", help="show each sync step")
    p.add_argument("--no-log", action="store_true", help="do not append to the log file")
    args = p.parse_args()

    try:
        from src.db.schema import migrate_db
        from src.maintenance import run_cycle, summarise

        # A scheduled run may be the first thing to execute after a code update, so the schema
        # cannot be assumed current.
        migrate_db()
        result = run_cycle(push_enabled=not args.no_push, silent=not args.verbose)
        _say(summarise(result), log=not args.no_log)
        return 1 if result.get("errors") else 0
    except Exception:
        # Anything that escapes run_cycle — an import error after a bad edit, a corrupt
        # database, a failed migration. Unlogged, this is a task that fails invisibly.
        _say(f"[maintenance] FAILED: {traceback.format_exc().strip()}", log=not args.no_log)
        return 1


if __name__ == "__main__":
    sys.exit(main())

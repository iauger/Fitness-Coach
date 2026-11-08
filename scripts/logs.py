"""
Read the application log.

    python scripts/logs.py                          # the last 40 events
    python scripts/logs.py --level warning --since 24h
    python scripts/logs.py --event sync --since 7d
    python scripts/logs.py --counts --since 7d      # how often each warning or error happened
    python scripts/logs.py --json                   # raw lines, for a tool to read

Events are written by `src/applog.py` to data/logs/app.jsonl. They are counts, durations and
exception types, never anyone's words. Read-only: it creates nothing.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import applog

SKIP = {"ts", "level", "event", "exc_type", "exc", "at"}


def line(row: dict) -> str:
    when = row.get("ts", "")[:19].replace("T", " ")
    text = f"{when}  {row.get('level', '?').upper():7}  {row['event']}"
    detail = "  ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in row.items() if k not in SKIP)
    if detail:
        text += f"  {detail}"
    if row.get("exc_type"):
        text += f"\n{'':30}{row['exc_type']}: {row.get('exc', '')}"
        if row.get("at"):
            text += f"\n{'':30}at {row['at']}"
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--since", help="90m, 24h, 7d, or a date like 2026-11-08")
    ap.add_argument("--level", choices=applog.LEVELS, help="this level and above")
    ap.add_argument("--event", help="only events whose name contains this")
    ap.add_argument("--tail", type=int, default=40, help="how many of the newest to show (0: all)")
    ap.add_argument("--counts", action="store_true", help="a count per event instead of the events")
    ap.add_argument("--json", action="store_true", help="the raw lines")
    args = ap.parse_args(argv)

    try:
        since = applog.parse_since(args.since)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    rows = applog.read(since=since, level=args.level, event_like=args.event,
                       limit=None if args.counts or not args.tail else args.tail)

    if not applog.files():
        print(f"No log yet. It is written to {applog.log_dir() / applog.FILE_NAME} the first time the "
              f"app does anything worth recording.")
        return 0
    if args.counts:
        from collections import Counter
        counts = Counter((r["level"], r["event"]) for r in rows)
        for (level, name), n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"{n:6}  {level.upper():7}  {name}")
        return 0
    if not rows:
        print("Nothing matches.")
        return 0
    for row in rows:
        print(json.dumps(row, ensure_ascii=False) if args.json else line(row))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

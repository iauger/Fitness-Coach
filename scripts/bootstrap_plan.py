"""
Derive this athlete's tier ladder from their own synced riding, and store it.

Run once, after `fetch_history.py`, before the plan will generate anything. The roller refuses
to build a horizon for a plan with no ladder, so this is the step that turns a fresh clone into
a working plan.

Usage:
    python scripts/bootstrap_plan.py                 # show what it would derive, write nothing
    python scripts/bootstrap_plan.py --write         # store it
    python scripts/bootstrap_plan.py --write --replace   # re-derive over an existing ladder
    python scripts/bootstrap_plan.py --weeks 52      # how much history to read

Dry by default, because a ladder is read every week for months and decides every session in
them. Look at it before you keep it.

Re-deriving with `--replace` writes a **new version** rather than overwriting: the old rungs
stay, and every week already decided keeps pointing at the ladder it was graded under. So
re-deriving is safe for the record, and is still a real change to what gets prescribed from
the next roll onward, which is why it needs the flag.

The derivation itself is `src/planning/ladder_ops.py`, shared with the UI's ladder screen.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.schema import migrate_db, read_connection
from src.planning import ladder_ops
from src.planning.bootstrap import BootstrapError, summarise
from src.planning.constants import plan_name as configured_plan_name
from src.planning.progression import TIERS


def compare_to_fallback(ladder) -> str:
    """
    The derived ladder beside the hand-written one.

    Worth printing every time. The hand-written ladder is one real athlete's, chosen from the
    same data this derivation reads, so the comparison is the closest thing to a known answer
    this step has — and a derived ladder wildly above measured practice means the derivation is
    wrong, not that the athlete should ride more.
    """
    lines = [f"  {'tier':<6}{'derived':>18}{'hand-written':>18}{'delta':>9}"]
    for d, h in zip(ladder.tiers, TIERS.values()):
        delta = round(d.target_hours - h.target_hours, 2)
        lines.append(
            f"  {'T' + str(d.index):<6}"
            f"{f'{d.long_min}/{d.lit_min} {d.target_hours}h':>18}"
            f"{f'{h.long_min}/{h.lit_min} {h.target_hours}h':>18}"
            f"{delta:>+9}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--write", action="store_true", help="store the ladder (default: dry run)")
    p.add_argument("--replace", action="store_true",
                   help="re-derive over an existing ladder, as a new version; past decisions "
                        "keep the rungs they were graded against")
    p.add_argument("--weeks", type=int, default=78,
                   help="how many weeks of history to read (default 78)")
    p.add_argument("--compare", action="store_true",
                   help="print the hand-written ladder beside the derived one")
    args = p.parse_args()

    # Cheap and idempotent, and it has to happen before the first read: a fresh clone has no
    # database at all, and `no such table: activities` is not a useful first impression.
    migrate_db()

    try:
        with read_connection() as conn:
            ladder = ladder_ops.derive_now(conn, args.weeks)
    except BootstrapError as exc:
        print(f"\n  cannot derive a ladder yet:\n    {exc}\n")
        return 1

    print(f"\n  {configured_plan_name()}\n")
    print(summarise(ladder))
    if args.compare:
        print()
        print(compare_to_fallback(ladder))

    if not args.write:
        print("\n  dry run — nothing written. Pass --write to store this.\n")
        return 0

    try:
        done = ladder_ops.store(replace=args.replace, weeks=args.weeks)
    except ladder_ops.LadderError as exc:
        print(f"\n  {exc}\n")
        return 1

    print(f"\n  stored {done['written']} rungs for {configured_plan_name()} "
          f"as ladder version {done['version']}.")
    if done["version"] > 1:
        print("  Weeks already decided keep the version they were graded against.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

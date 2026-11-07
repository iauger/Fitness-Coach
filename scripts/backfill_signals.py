"""
File the athlete's earlier writing as signals.

    python scripts/backfill_signals.py            # preview: calls the model, writes nothing
    python scripts/backfill_signals.py --apply    # file what it found

Reads the notes on feel logs, the notes on rides, and finished conversations, and sends each
through the extractor (one cheap call apiece). Anything already filed is skipped, so it is safe
to run again. Conversations keep the note they already have; only their signals are added.
Check the preview first: this is the first real run of the extractor on your own words.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main(argv: list[str] | None = None) -> int:
    # Parsed first, so `--help` and a mistyped flag never touch the database or the model.
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true", help="file the signals (default: preview only)")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv

    from src.coach import backfill, memory
    from src.db.schema import migrate_db

    load_dotenv()
    migrate_db()
    items = backfill.collect()
    if not items:
        print("nothing to read: everything is already filed")
        return 0
    print(f"{len(items)} piece{'s' if len(items) != 1 else ''} of writing to read"
          f"{'' if args.apply else ' (preview: nothing will be written)'}\n")

    def show(item, found):
        print(f"{item.day}  {item.source}  {item.ref}")
        print(f"    \"{' '.join(item.text.split())[:110]}\"")
        for s in found.signals:
            print(f"    -> {s['kind']} [{s['severity']}] {s['topic'] or '-'}  ({s['state']})  \"{s['quote'][:70]}\"")
        for r in found.rejected:
            print(f"    x  dropped {r.get('kind')}: {r['why']}")
        if not found.signals:
            print("    -> nothing to file")

    done = backfill.run(memory._client(), apply=args.apply, items=items, on_item=show)
    filed = sum(len(f.signals) for _, f in done)
    print(f"\n{filed} signal{'s' if filed != 1 else ''} {'filed' if args.apply else 'found'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

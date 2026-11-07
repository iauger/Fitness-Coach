"""
Bring the command line's old conversation transcripts into the conversation tables.

    python scripts/import_transcripts.py

Before the command line and the app shared one store, `ask.py` wrote a JSON file under
data/transcripts and a row in `transcripts` for each conversation. This turns each of those into
an ordinary conversation, keeping the note it already had, so they can be opened and managed
beside new ones. Safe to run twice: a conversation already imported is skipped. The old files and
rows are left where they are, and the notes already in the coach's memory are not duplicated.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main(argv: list[str] | None = None) -> int:
    # Parsed first, so `--help` and a mistyped flag never touch the database.
    argparse.ArgumentParser(description=__doc__.split("\n")[1]).parse_args(argv)

    from src.coach import memory
    from src.db.schema import migrate_db

    migrate_db()
    n = memory.import_legacy()
    print(f"imported {n} conversation{'s' if n != 1 else ''}" if n else "nothing to import")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

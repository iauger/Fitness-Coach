"""
Pack what a person helping would need into one zip.

    python scripts/support_bundle.py

Writes data/bundles/support-<time>.zip: status, the application log, installed versions, the shape of
the database and the non-text settings. No activities, notes, conversations, goals or coach
replies, no keys, and your name removed wherever it appeared. Nothing is sent: open it, read it,
and send it yourself if you want to.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import support


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__.strip().splitlines()[0]).parse_args(argv)     # so --help prints help and builds nothing
    path = support.build()
    print(f"\nWrote {path}\n")
    print("Inside:")
    for name, text in support.contents(path).items():
        print(f"  {name:28} {len(text):>8} characters")
    print("\nNothing has been sent. Open the zip and look before you share it.\n")
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

"""
Where your setup has got to, and the one command to run next.

The first thing to reach for when something is not working, and the thing a fresh clone is
pointed at before anything else. Read-only: it creates no files, migrates no schema and calls
no API, because a status command that changed things would be the opposite of what someone
reaches for when they are lost.

Usage:
    python scripts/status.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.firstrun import ready, report


def main() -> int:
    print()
    print(report())
    print()
    return 0 if ready() else 1


if __name__ == "__main__":
    raise SystemExit(main())

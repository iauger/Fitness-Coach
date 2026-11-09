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

# The report contains em-dashes, and a Windows console defaults to cp1252, where they print
# as a replacement character. Every other script that prints prose does this; status.py did
# not, and it is the one script guaranteed to be run on a machine nobody has configured yet.
if sys.stdout is not None and sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.firstrun import ready, report


def main(argv: list[str] | None = None) -> int:
    if {"-h", "--help"} & set(sys.argv[1:] if argv is None else argv):
        # Asking for help is not asking whether setup is ready; it must not exit 1 on a fresh clone.
        print(__doc__.strip())
        return 0
    print()
    print(report())
    print()
    return 0 if ready() else 1


if __name__ == "__main__":
    raise SystemExit(main())

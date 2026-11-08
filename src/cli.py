"""
What a command-line script does when the thing it needs is missing.

A missing API key is not a crash, it is a next step. `friendly_errors()` replaces the traceback with
the message the exception carries and leaves every other exception exactly as it was, so a real
bug still shows its stack.
"""

import sys


def utf8_output() -> None:
    """
    Write UTF-8 whatever stdout is attached to.

    On Windows a terminal is fine but a pipe or a redirect takes the ANSI code page (cp1252), which
    cannot encode "→" or an emoji, and the coach uses both. `checkin.py > week.txt` then crashed on
    the first one, after the API call had already been paid for. `errors="replace"` is the net under
    that: a character that still cannot be written becomes "?" rather than ending the run.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def friendly_errors() -> None:
    """Install both: UTF-8 output, and a readable message for a missing key."""
    utf8_output()
    from src.coach.settings import MissingKey
    from src.intervals.client import MissingCredentials

    previous = sys.excepthook

    def hook(kind, value, tb):
        if isinstance(value, (MissingKey, MissingCredentials)):
            print(f"\n{value}", file=sys.stderr)
            return
        previous(kind, value, tb)

    sys.excepthook = hook

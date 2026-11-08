"""
The application log: what happened, never what anyone said.

One JSON object per line in `data/logs/app.jsonl`, rotated by size (five files of 1 MB), written with
the standard `logging` handler so threads (the API, the background loop) can share it. Nothing here
is a metrics system or a search interface: `scripts/logs.py` reads it, `status.py` reports problems
from it, and `scripts/support_bundle.py` packs it for someone to send.

    applog.event("sync.done", activities=3, ms=840)
    applog.warning("sync.records_skipped", count=2)
    applog.error("sync.activities_failed", exc, window_days=30)

**It records events, not content.** The file is meant to be sent to whoever is helping, so a name, a
note, a question or a coach reply never goes in it: callers pass codes, counts, durations and
exception types. That is enforced here rather than trusted. A field whose name suggests words
(`DENY`) is dropped, values are cut to 200 characters, and every string and exception message goes
through `scrub`, which removes keys, athlete ids, emails and anything in the environment that looks
like a secret.

**A logger must never take the app down.** Every write is inside a net that swallows its own
failures: a full disk or a locked file on Windows loses a line, not a sync.

Reading never creates anything. The directory is made on the first write, and
`FITNESS_COACH_LOG_DIR` redirects the whole thing (the tests set it, so no test writes to yours).
"""

import json
import logging
import logging.handlers
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DIR = Path(__file__).parent.parent / "data" / "logs"
ENV_DIR = "FITNESS_COACH_LOG_DIR"
FILE_NAME = "app.jsonl"
MAX_BYTES = 1_000_000
BACKUPS = 5

LEVELS = ("debug", "info", "warning", "error")
VALUE_LIMIT = 200
MESSAGE_LIMIT = 300
LIST_LIMIT = 20

# Field names that carry someone's words or identity. Dropped whatever the caller passes, and
# said so in `_dropped`, so a call that tried to log a note is visible in review and in the file.
DENY = frozenset({
    "name", "note", "notes", "text", "content", "question", "answer", "reply", "description",
    "quote", "gist", "body", "prompt", "email", "title", "goal", "reason", "message", "comment",
})

# Environment variables whose *values* must never appear in a log line, wherever they came from.
SECRET_ENV = ("INTERVALS_API_KEY", "INTERVALS_ATHLETE_ID", "ANTHROPIC_API_KEY", "GCAL_ICAL_URL")

_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "<key>"),
    (re.compile(r"(?i)([?&](?:api_?key|token|key|secret)=)[^&\s]+"), r"\1<key>"),
    (re.compile(r"\bi\d{4,}\b"), "<athlete-id>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
)


def log_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def scrub(text: str, extra: tuple[str, ...] = ()) -> str:
    """`text` with secrets, ids and emails replaced. `extra` is more literal strings to remove."""
    text = str(text)
    literals = [os.environ.get(k, "") for k in SECRET_ENV] + list(extra)
    for value in sorted({v for v in literals if v and len(v) >= 4}, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return scrub(value)[:VALUE_LIMIT]
    return f"<{type(value).__name__}>"


def _clean(value):
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scalar(v) for v in list(value)[:LIST_LIMIT]]
    return _scalar(value)


def _where(exc: BaseException, frames: int = 3) -> str:
    """The last few frames as `file.py:12 function`: places, never the source lines or values."""
    stack = traceback.extract_tb(exc.__traceback__)[-frames:]
    return " < ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(stack))


# -- writing -------------------------------------------------------------------------------------

_logger = logging.getLogger("fitness_coach.app")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False
_attached: dict[str, logging.Handler] = {}


class _Quiet(logging.handlers.RotatingFileHandler):
    def handleError(self, record) -> None:      # a rotation that fails on a locked Windows file
        pass


def _handler() -> logging.Handler:
    path = log_dir() / FILE_NAME
    key = str(path)
    if key not in _attached:
        for old in _attached.values():
            _logger.removeHandler(old)
            old.close()
        _attached.clear()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _Quiet(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)
        _attached[key] = handler
    return _attached[key]


def release() -> None:
    """Close the open log file, so a directory it was in can be deleted (Windows will not otherwise)."""
    for old in _attached.values():
        _logger.removeHandler(old)
        old.close()
    _attached.clear()


def event(name: str, level: str = "info", exc: BaseException | None = None, /, **fields) -> None:
    """Write one event. Never raises: the three leading parameters are positional-only so that a
    field called `name` or `level` is a field (and dropped, being on the deny list), not a TypeError."""
    try:
        if level not in LEVELS:
            level = "info"
        record: dict = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "level": level, "event": scrub(name)[:80]}
        dropped = sorted(k for k in fields if k in DENY)
        for key, value in fields.items():
            if key not in DENY and key not in record:
                record[key] = _clean(value)
        if dropped:
            record["_dropped"] = dropped
        if exc is not None:
            record["exc_type"] = type(exc).__name__
            record["exc"] = " ".join(scrub(str(exc)).split())[:MESSAGE_LIMIT]
            record["at"] = _where(exc)
        _handler()
        _logger.log(getattr(logging, level.upper()), json.dumps(record, ensure_ascii=False))
    except Exception:       # not logged: this is the logger; there is nowhere left to report to
        pass


def warning(name: str, exc: BaseException | None = None, /, **fields) -> None:
    event(name, "warning", exc, **fields)


def error(name: str, exc: BaseException | None = None, /, **fields) -> None:
    event(name, "error", exc, **fields)


# -- reading -------------------------------------------------------------------------------------

def files(directory: Path | None = None) -> list[Path]:
    """The log files that exist, oldest first."""
    base = (directory or log_dir()) / FILE_NAME
    rotated = [base.with_name(f"{FILE_NAME}.{n}") for n in range(BACKUPS, 0, -1)]
    return [p for p in rotated + [base] if p.exists()]


def parse_since(text: str | None, now: datetime | None = None) -> datetime | None:
    """`90m`, `24h`, `7d`, or an ISO date or timestamp, as a UTC datetime."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return now - timedelta(**{unit: int(m.group(1))})
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"{text!r} is not a time: use 90m, 24h, 7d or a date like 2026-11-08") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def read(since: datetime | None = None, level: str | None = None, event_like: str | None = None,
         limit: int | None = None, directory: Path | None = None) -> list[dict]:
    """
    Events oldest first, filtered. A line that is not valid JSON (a write cut short by a crash) is
    skipped rather than fatal: the point of a log is to be readable when something has gone wrong.
    """
    floor = LEVELS.index(level) if level in LEVELS else 0
    out = []
    for path in files(directory):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "event" not in row:
                    continue
                if row.get("level") in LEVELS and LEVELS.index(row["level"]) < floor:
                    continue
                if event_like and event_like not in row["event"]:
                    continue
                if since is not None:
                    try:
                        if datetime.fromisoformat(row["ts"]) < since:
                            continue
                    except (KeyError, ValueError):
                        continue
                out.append(row)
    return out[-limit:] if limit else out


def problems(hours: int = 24, directory: Path | None = None) -> dict:
    """Warnings and errors in the last `hours`: `{"error": n, "warning": n, "events": Counter}`."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    found = read(since=since, level="warning", directory=directory)
    return {"error": sum(1 for r in found if r.get("level") == "error"),
            "warning": sum(1 for r in found if r.get("level") == "warning"),
            "events": Counter(r["event"] for r in found)}

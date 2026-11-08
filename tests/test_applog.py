"""
The application log: what it writes, what it refuses to write, and that it can never hurt the app.

Mutants: a field that carries someone's words written anyway; a secret, athlete id or email left in a
message; a field called `name` raising into the caller; a long value or list written whole; a
logger that raises when the disk is not writable; reading that creates the directory; a corrupt line
ending the read; a filter that ignores its argument; rotation that never rotates.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from src import applog


def lines():
    path = applog.log_dir() / applog.FILE_NAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_an_event_is_one_json_line_with_a_time_a_level_and_its_fields():
    applog.event("sync.done", activities=3, ms=840, ok=True, tags=["a", "b"])
    (row,) = lines()
    assert row["event"] == "sync.done" and row["level"] == "info"
    assert (row["activities"], row["ms"], row["ok"], row["tags"]) == (3, 840, True, ["a", "b"])
    assert datetime.fromisoformat(row["ts"]).tzinfo is not None


@pytest.mark.parametrize("field", sorted(applog.DENY))
def test_a_field_that_carries_words_is_never_written(field):
    """Mutant: the deny list ignored. The log is meant to be sent to someone, so it holds no words."""
    applog.event("x", **{field: "what the athlete said"}, count=1)
    (row,) = lines()
    assert field not in row and "what the athlete said" not in json.dumps(row)
    assert row["_dropped"] == [field] and row["count"] == 1


def test_a_field_named_like_the_functions_own_parameters_is_a_field_not_a_crash():
    """Mutant: `name`/`level`/`exc` as ordinary parameters. A TypeError here would escape the net."""
    applog.event("x", name="Ian", level="loud", exc="boom", count=2)
    (row,) = lines()
    assert row["level"] == "info" and row["count"] == 2 and "name" not in row


def test_values_are_bounded_and_made_plain():
    applog.event("x", long="y" * 5000, many=list(range(100)), thing=object(), nan=float("nan"),
                 inf=float("inf"), nested={"a": 1})
    (row,) = lines()
    assert len(row["long"]) == applog.VALUE_LIMIT
    assert len(row["many"]) == applog.LIST_LIMIT
    assert row["thing"] == "<object>" and row["nested"] == "<dict>"
    assert row["nan"] is None and row["inf"] is None


# -- scrubbing ---------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, gone", [
    ("key sk-abcdefgh12345678 rejected", "sk-abcdefgh12345678"),
    ("GET /athlete/i123456/activities failed", "i123456"),
    ("mail me at someone@example.com", "someone@example.com"),
    ("https://x.test/feed?api_key=SECRET123&b=1", "SECRET123"),
])
def test_secrets_ids_and_emails_are_removed_from_text(raw, gone):
    assert gone not in applog.scrub(raw)


def test_a_secret_from_the_environment_is_removed_wherever_it_appears(monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "plainlookingvalue99")
    assert "plainlookingvalue99" not in applog.scrub("the server said plainlookingvalue99 is wrong")


def test_extra_literals_are_removed_too():
    assert applog.scrub("Ian Auger rode", extra=("Ian Auger",)) == "<redacted> rode"


def test_an_error_records_the_type_a_scrubbed_message_and_where_but_no_source():
    def deep():
        raise ValueError("bad token sk-abcdefgh12345678 for i999999")

    try:
        deep()
    except ValueError as exc:
        applog.error("sync.activities_failed", exc, days=30)
    (row,) = lines()
    assert row["level"] == "error" and row["exc_type"] == "ValueError"
    assert "sk-abcdefgh12345678" not in row["exc"] and "i999999" not in row["exc"]
    assert "deep" in row["at"] and "test_applog.py:" in row["at"]
    assert "raise ValueError" not in json.dumps(row), "frames name a place, never print its source"


def test_a_long_message_is_cut():
    applog.error("x", RuntimeError("m" * 5000))
    assert len(lines()[0]["exc"]) == applog.MESSAGE_LIMIT


# -- it can never hurt the app -------------------------------------------------------------------

def test_a_log_directory_that_cannot_be_written_does_not_raise(tmp_path, monkeypatch):
    """Mutant: the net removed. The directory is a file, so creating it fails."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(applog.ENV_DIR, str(blocker / "logs"))
    applog.event("x", count=1)
    applog.error("y", RuntimeError("z"))


def test_reading_never_creates_the_directory(tmp_path, monkeypatch):
    target = tmp_path / "absent" / "logs"
    monkeypatch.setenv(applog.ENV_DIR, str(target))
    assert applog.files() == [] and applog.read() == []
    assert applog.problems()["error"] == 0
    assert not target.exists() and not target.parent.exists()


# -- reading -------------------------------------------------------------------------------------

def put(path, *rows, raw=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        for text in raw:
            handle.write(text + "\n")


def at(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_read_filters_by_time_level_and_event_and_keeps_order(tmp_path):
    base = applog.log_dir() / applog.FILE_NAME
    put(base.with_name(applog.FILE_NAME + ".1"),
        {"ts": at(300), "level": "error", "event": "sync.old"})
    put(base,
        {"ts": at(120), "level": "info", "event": "sync.done"},
        {"ts": at(60), "level": "warning", "event": "chat.sync_failed"},
        {"ts": at(10), "level": "error", "event": "sync.plan_failed"})
    names = lambda **kw: [r["event"] for r in applog.read(**kw)]
    assert names() == ["sync.old", "sync.done", "chat.sync_failed", "sync.plan_failed"]
    assert names(since=applog.parse_since("90m")) == ["chat.sync_failed", "sync.plan_failed"]
    assert names(level="warning") == ["sync.old", "chat.sync_failed", "sync.plan_failed"]
    assert names(level="error") == ["sync.old", "sync.plan_failed"]
    assert names(event_like="sync_failed") == ["chat.sync_failed"]
    assert names(limit=2) == ["chat.sync_failed", "sync.plan_failed"]


def test_a_corrupt_line_is_skipped_not_fatal():
    base = applog.log_dir() / applog.FILE_NAME
    put(base, {"ts": at(5), "level": "info", "event": "ok.one"},
        raw=['{"ts": "2026-11-08T01:', "not json at all", '["a list"]', '{"no": "event"}'])
    assert [r["event"] for r in applog.read()] == ["ok.one"]


@pytest.mark.parametrize("text, minutes", [("90m", 90), ("24h", 24 * 60), ("7d", 7 * 24 * 60)])
def test_parse_since_understands_the_short_forms(text, minutes):
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    assert applog.parse_since(text, now) == now - timedelta(minutes=minutes)


def test_parse_since_takes_a_date_and_refuses_nonsense():
    assert applog.parse_since("2026-11-01") == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert applog.parse_since(None) is None
    with pytest.raises(ValueError, match="not a time"):
        applog.parse_since("yesterday")


def test_problems_counts_recent_warnings_and_errors_by_event():
    base = applog.log_dir() / applog.FILE_NAME
    put(base,
        {"ts": at(30 * 60), "level": "error", "event": "too.old"},
        {"ts": at(60), "level": "error", "event": "sync.activities_failed"},
        {"ts": at(50), "level": "error", "event": "sync.activities_failed"},
        {"ts": at(40), "level": "warning", "event": "sync.records_skipped"},
        {"ts": at(30), "level": "info", "event": "sync.done"})
    found = applog.problems(24)
    assert (found["error"], found["warning"]) == (2, 1)
    assert found["events"] == {"sync.activities_failed": 2, "sync.records_skipped": 1}


# -- the file itself -----------------------------------------------------------------------------

def test_the_file_rotates_and_keeps_a_bounded_number(monkeypatch):
    """Mutant: a plain file handler. A year of events in one file nobody can open."""
    monkeypatch.setattr(applog, "MAX_BYTES", 400)
    monkeypatch.setattr(applog, "BACKUPS", 2)
    applog._attached.clear()
    for n in range(60):
        applog.event("rotate.me", n=n, pad="p" * 40)
    present = sorted(p.name for p in applog.log_dir().iterdir())
    assert present == ["app.jsonl", "app.jsonl.1", "app.jsonl.2"]
    assert [r["n"] for r in applog.read()][-1] == 59, "the newest event is always readable"


def test_the_test_suite_never_writes_to_the_real_log():
    """The autouse fixture in conftest. If this fails, a test run would fill the athlete's own log."""
    assert applog.log_dir() != applog.DEFAULT_DIR
    assert os.environ[applog.ENV_DIR] == str(applog.log_dir())


def test_rotated_files_are_read_oldest_first():
    """
    Mutant: the numbered files read in ascending order. `.2` is older than `.1`, and a log that reads
    its history backwards puts a tail command's "newest" events in the wrong place.
    """
    base = applog.log_dir() / applog.FILE_NAME
    put(base.with_name(applog.FILE_NAME + ".2"), {"ts": at(300), "level": "info", "event": "oldest"})
    put(base.with_name(applog.FILE_NAME + ".1"), {"ts": at(200), "level": "info", "event": "older"})
    put(base, {"ts": at(100), "level": "info", "event": "newest"})
    assert [r["event"] for r in applog.read()] == ["oldest", "older", "newest"]
    assert [p.name for p in applog.files()] == ["app.jsonl.2", "app.jsonl.1", "app.jsonl"]


def test_an_exception_message_is_one_line():
    """Mutant: the message kept as raised. The intervals.icu 401 text is three lines and printed ragged."""
    applog.error("x", RuntimeError("first line.\n  second line.\n\tthird   line."))
    assert lines()[0]["exc"] == "first line. second line. third line."

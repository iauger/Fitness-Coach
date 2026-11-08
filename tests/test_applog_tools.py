"""
The three ways the log is used: read it (`scripts/logs.py`), be told about it (`status.py`), send it
(`scripts/support_bundle.py`).

The bundle is the one with consequences. It is built to be sent to another person, so the tests plant
a recognisable string in every place the athlete's own material lives (an activity name, a goal, a
chat message, the settings text, the name inside an exception) and assert that none of them are in
the zip, while the things that are meant to be there (counts, versions, the log) are.

Mutants: the bundle including content; the athlete's name surviving in an exception message; free-text
settings shown rather than measured; a reader that creates the directory or crashes on a bad time;
a problems line that blocks the setup, or appears when there is nothing wrong.
"""

import importlib.util
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import applog, firstrun, support
from src.db.migrations import migrate

ROOT = Path(__file__).parent.parent


def script(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(rows):
    path = applog.log_dir() / applog.FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# -- scripts/logs.py ----------------------------------------------------------------------------------

def test_with_no_log_the_reader_says_so_and_creates_nothing(capsys):
    assert script("logs").main([]) == 0
    assert "No log yet" in capsys.readouterr().out
    assert list(applog.log_dir().iterdir()) == [], "reading wrote nothing into the directory"


def test_the_reader_shows_events_and_the_exception_beneath_an_error(capsys):
    write([{"ts": ago(5), "level": "error", "event": "sync.activities_failed", "window_days": 30,
            "exc_type": "RuntimeError", "exc": "down", "at": "sync.py:131 incremental_sync"}])
    script("logs").main([])
    out = capsys.readouterr().out
    assert "ERROR" in out and "sync.activities_failed" in out and "window_days=30" in out
    assert "RuntimeError: down" in out and "at sync.py:131" in out


def test_the_reader_filters_and_counts(capsys):
    write([{"ts": ago(30 * 60), "level": "error", "event": "old.thing"},
           {"ts": ago(30), "level": "warning", "event": "a.warn"},
           {"ts": ago(20), "level": "warning", "event": "a.warn"},
           {"ts": ago(10), "level": "info", "event": "b.info"}])
    logs = script("logs")
    logs.main(["--level", "warning", "--since", "24h", "--counts"])
    out = capsys.readouterr().out
    assert "2  WARNING  a.warn" in out and "old.thing" not in out and "b.info" not in out
    logs.main(["--event", "b.", "--json"])
    assert json.loads(capsys.readouterr().out)["event"] == "b.info"


def test_a_bad_time_is_a_message_and_a_nonzero_exit_not_a_traceback(capsys):
    write([{"ts": ago(1), "level": "info", "event": "x"}])
    assert script("logs").main(["--since", "yesterday"]) == 2
    assert "not a time" in capsys.readouterr().err


# -- the status line ----------------------------------------------------------------------------------

def test_status_says_nothing_when_nothing_went_wrong():
    write([{"ts": ago(5), "level": "info", "event": "sync.done"}])
    assert firstrun._problems() is None


def test_status_reports_recent_problems_without_blocking_anything():
    write([{"ts": ago(30 * 60), "level": "error", "event": "too.old"},
           {"ts": ago(20), "level": "error", "event": "sync.activities_failed"},
           {"ts": ago(10), "level": "error", "event": "sync.activities_failed"},
           {"ts": ago(5), "level": "warning", "event": "sync.records_skipped"}])
    found = firstrun._problems()
    assert not found.ok and not found.blocking
    assert "2 errors and 1 warnings in the last 24 hours" in found.detail
    assert "sync.activities_failed x2" in found.detail and "too.old" not in found.detail
    assert "scripts/logs.py --level warning --since 24h" in found.fix


# -- the support bundle -------------------------------------------------------------------------------

SECRETS = ["SECRETRIDE", "SECRETGOAL", "SECRETCHAT", "SECRETNOTES", "Ian Auger", "plainsecretkey99"]


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO activities (id, date, name, type, moving_time, athlete_note) "
                 "VALUES ('a1', '2026-11-01', 'SECRETRIDE', 'Ride', 3600, 'SECRETNOTES')")
    conn.execute("INSERT INTO goals (description, status, created_at, updated_at) "
                 "VALUES ('SECRETGOAL', 'active', 'x', 'x')")
    conn.execute("INSERT INTO sync_log (synced_at, data_type, start_date, end_date, records, status) "
                 "VALUES ('x', 'activities', '2026-11-01', '2026-11-02', 0, 'error: Ian Auger was refused')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("INTERVALS_API_KEY", "plainsecretkey99")
    monkeypatch.setattr(support, "_own_words", lambda: ("Ian Auger", "Auger"))
    applog.error("sync.activities_failed", RuntimeError("Ian Auger with plainsecretkey99 failed"), days=3)
    return support.contents(support.build(out_dir=tmp_path / "out", db_path=db)), tmp_path


def test_nothing_the_athlete_wrote_is_in_the_bundle(bundle):
    """Mutant: any file carrying content. This is the file that gets sent to another person."""
    files, _ = bundle
    everything = "\n".join(files.values())
    for secret in SECRETS:
        assert secret not in everything, f"{secret!r} leaked into the bundle"


def test_the_bundle_holds_the_shape_of_the_data_and_the_log(bundle):
    files, _ = bundle
    assert set(files) == {"README.txt", "status.txt", "environment.txt", "settings.txt", "database.txt",
                          "logs/app.jsonl"}
    assert "activities: 1 rows" in files["database.txt"] and "goals: 1 rows" in files["database.txt"]
    assert "activities span 2026-11-01 to 2026-11-01" in files["database.txt"]
    assert "sync.activities_failed" in files["logs/app.jsonl"] and "<redacted>" in files["logs/app.jsonl"]
    assert "python " in files["environment.txt"] and "fastapi " in files["environment.txt"]
    assert "Nothing was sent anywhere" in files["README.txt"]


def test_free_text_settings_appear_only_as_a_length(bundle):
    files, _ = bundle
    assert "athlete.name: <" in files["settings.txt"] and "characters, not included>" in files["settings.txt"]
    assert "plan.max_ride_days:" in files["settings.txt"]


def test_the_bundle_goes_under_data_bundles_by_default_and_is_a_valid_zip(tmp_path, monkeypatch):
    db = tmp_path / "data" / "t.db"
    db.parent.mkdir()
    migrate(db)
    path = support.build(db_path=db)
    assert path.parent == db.parent / "bundles" and zipfile.is_zipfile(path)


def test_a_part_of_the_bundle_that_cannot_be_collected_says_so_and_costs_nothing_else(tmp_path, monkeypatch):
    """
    Mutant: no per-section net. A broken setup is exactly where status, the database or the config
    cannot be read, and a support tool that dies there is useless when it is wanted.
    """
    db = tmp_path / "t.db"
    migrate(db)

    def boom(*a, **k):
        raise RuntimeError("the database is on fire")

    monkeypatch.setattr(support, "_database", boom)
    monkeypatch.setattr(support, "_versions", boom)
    files = support.contents(support.build(out_dir=tmp_path / "out", db_path=db))
    assert "could not be collected: RuntimeError: the database is on fire" in files["database.txt"]
    assert "could not be collected" in files["environment.txt"]
    assert "Nothing was sent anywhere" in files["README.txt"] and files["status.txt"].strip()
    assert [r["section"] for r in applog.read(event_like="support.section_failed")] == ["environment", "database"]

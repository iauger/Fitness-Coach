"""
Tests for the unattended maintenance cycle.

An automated job gets the benefit of nobody watching it, and pays for it: a bug that would be
obvious once is invisible when it happens hourly. So these cover the three ways this one could
quietly do damage:

1. **Two cycles at once.** SQLite in WAL mode allows many readers and exactly one writer. The
   in-server scheduler and the Windows Scheduled Task can both fire, so the lock has to hold
   across processes, not just threads.
2. **A stale lock disabling everything.** A process killed mid-cycle leaves the file behind.
   Without an expiry that silently stops all future runs — which is precisely the six-week
   silence this whole module exists to prevent.
3. **Pushing when nothing changed.** Fifteen deletes and fifteen creates, every hour, against
   a real calendar.

Nothing here touches the network.
"""

import os
import sqlite3
import sys
import time
from datetime import date, timedelta

import pytest

from src.db.migrations import migrate
from src import maintenance
from src.maintenance import AlreadyRunning, _exclusive, needs_push, run_cycle, summarise
from src.planning.roller import store_horizon

PLAN = "test rolling"
TODAY = date(2026, 11, 3)


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    p = tmp_path / "maintenance.lock"
    monkeypatch.setattr(maintenance, "LOCK_PATH", p)
    return p


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store_horizon(conn, TODAY, PLAN)
    conn.commit()
    yield conn
    conn.close()


# ── the lock ──────────────────────────────────────────────────────────────────

def test_the_lock_is_taken_and_released(lock_path):
    assert not lock_path.exists()
    with _exclusive():
        assert lock_path.exists()
    assert not lock_path.exists()


def test_a_second_cycle_cannot_start_while_one_holds_the_lock(lock_path):
    with _exclusive():
        with pytest.raises(AlreadyRunning):
            with _exclusive():
                pass


def test_the_lock_is_released_even_when_the_cycle_raises(lock_path):
    """Otherwise one crash disables every future run, which is the failure being prevented."""
    with pytest.raises(ValueError):
        with _exclusive():
            raise ValueError("boom")
    assert not lock_path.exists()


def test_a_stale_lock_is_broken(lock_path, monkeypatch):
    """
    A process killed mid-cycle leaves the file behind.

    Without an expiry that is indistinguishable from a cycle that runs forever, and the
    maintenance loop stops silently — the exact shape of the six-week gap.
    """
    lock_path.write_text("99999 old")
    old = time.time() - (maintenance.LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old, old))

    with _exclusive():
        assert lock_path.exists()
        assert str(os.getpid()) in lock_path.read_text()


def test_a_fresh_lock_is_not_broken(lock_path):
    lock_path.write_text("99999 recent")
    with pytest.raises(AlreadyRunning):
        with _exclusive():
            pass
    assert lock_path.exists()


def test_a_cycle_that_loses_the_lock_reports_a_skip_not_an_error(lock_path, monkeypatch):
    """
    The other runner is doing the work. Treating that as a failure would make a scheduled task
    report errors every time the server happened to be running.
    """
    lock_path.write_text("99999 running")
    result = run_cycle(TODAY)
    assert result["skipped"]
    assert result["errors"] == []
    assert "skipped" in summarise(result)


# ── pushing only when something changed ───────────────────────────────────────

def test_a_never_pushed_horizon_needs_pushing(db):
    assert needs_push(db, TODAY, PLAN) is True


def test_a_fully_pushed_horizon_does_not(db):
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()
    assert needs_push(db, TODAY, PLAN) is False


def test_a_roll_that_changes_nothing_leaves_the_horizon_pushed(db):
    """
    The property that keeps an hourly job from rewriting the calendar hourly.

    Clearing `pushed_at` on every upsert would be the easy mistake: correct-looking, and it
    would delete and recreate fifteen events every hour forever.
    """
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()

    store_horizon(db, TODAY, PLAN)
    db.commit()

    assert needs_push(db, TODAY, PLAN) is False


def test_a_changed_prescription_marks_the_horizon_for_pushing(db, monkeypatch):
    """
    Same sessions, different dose.

    T3 and T4 produce the identical set of (date, role) slots and different durations, which
    is what makes this test about `pushed_at` rather than about row churn. An earlier version
    forced a different *block*, so the slot set changed too and `needs_push` fired on freshly
    inserted rows with no remote id — it passed with the pushed_at logic entirely disabled.
    """
    from src.planning import roller
    from tests.test_roller import force_state

    monkeypatch.setattr(roller, "current_state", force_state(tier=3, block_index=1))
    store_horizon(db, TODAY, PLAN)
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()
    assert needs_push(db, TODAY, PLAN) is False
    before = {(r["date"], r["role"]) for r in db.execute(
        "SELECT date, role FROM prescribed_sessions WHERE plan_name = ?", (PLAN,))}

    monkeypatch.setattr(roller, "current_state", force_state(tier=4, block_index=1))
    store_horizon(db, TODAY, PLAN)
    db.commit()

    after = {(r["date"], r["role"]) for r in db.execute(
        "SELECT date, role FROM prescribed_sessions WHERE plan_name = ?", (PLAN,))}
    assert after == before, "the slot set moved, so this is not testing pushed_at"
    assert needs_push(db, TODAY, PLAN) is True
    still_linked = db.execute(
        "SELECT COUNT(*) n FROM prescribed_sessions WHERE plan_name = ? AND date >= ? "
        "AND remote_event_id IS NOT NULL", (PLAN, TODAY.isoformat())).fetchone()["n"]
    assert still_linked > 0, "the push can no longer clean up the events it replaces"


def test_past_sessions_never_trigger_a_push(db):
    """They are the record of what was asked; the remote calendar has no business with them."""
    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ? AND date >= ?",
               (PLAN, TODAY.isoformat()))
    db.execute("INSERT INTO prescribed_sessions (plan_name, date, weekday, block_index, "
               "block_name, week_in_block, is_recovery_week, role, archetype, variant, "
               "prescription, params_json, created_at) VALUES "
               "(?, '2026-09-22', 1, 0, 'Old', 1, 0, 'hard', 'threshold', 'continuous', "
               "'old ask', '{}', '2026-09-20')", (PLAN,))
    db.commit()
    assert needs_push(db, TODAY, PLAN) is False


# ── the summary line ──────────────────────────────────────────────────────────

def test_the_summary_reports_errors_rather_than_hiding_them():
    result = {"sync": {"activities": 2, "wellness": 1, "plan": {"tier": 3}},
              "push": {"pushed": False}, "errors": ["activities: timeout"]}
    line = summarise(result)
    assert "ERRORS" in line and "timeout" in line


def test_the_summary_survives_a_cycle_that_failed_before_syncing():
    assert "maintenance" in summarise({"errors": ["cycle: boom"]})


# ── the cycle only calls the API when it has to ───────────────────────────────

class RecordingClient:
    def __init__(self):
        self.gets = self.creates = self.deletes = 0

    def get_events(self, start, end):
        self.gets += 1
        return []

    def create_events(self, events):
        self.creates += 1
        return [{"id": i, "external_id": e["external_id"]}
                for i, e in enumerate(events, start=9000)]

    def delete_events_by_id(self, ids):
        self.deletes += 1
        return len(ids)


def test_the_cycle_does_not_push_when_the_calendar_already_matches(db, monkeypatch, tmp_path):
    """
    The difference between an hourly job and an hourly calendar rewrite.

    Reconcile still runs — it is a read, and it is what repairs a lost link — but no event is
    created or deleted.
    """
    import src.planning.export as export

    db.execute("UPDATE prescribed_sessions SET remote_event_id = 'e' || id, "
               "pushed_at = '2026-11-03T12:00:00' WHERE plan_name = ?", (PLAN,))
    db.commit()

    client = RecordingClient()
    monkeypatch.setattr(maintenance, "IntervalsClient", lambda: client, raising=False)
    monkeypatch.setattr("src.intervals.client.IntervalsClient", lambda: client)
    monkeypatch.setattr(maintenance, "write_connection", _const_conn(db))
    monkeypatch.setattr(maintenance, "read_connection", _const_conn(db))

    out = maintenance._reconcile_and_push(TODAY, PLAN)

    assert out["pushed"] is False
    assert client.creates == 0 and client.deletes == 0
    assert client.gets == 1, "reconcile should still have looked"


def test_the_cycle_pushes_when_something_changed(db, monkeypatch):
    import src.planning.export as export

    client = RecordingClient()
    monkeypatch.setattr("src.intervals.client.IntervalsClient", lambda: client)
    monkeypatch.setattr(maintenance, "write_connection", _const_conn(db))
    monkeypatch.setattr(maintenance, "read_connection", _const_conn(db))

    out = maintenance._reconcile_and_push(TODAY, PLAN)

    assert out["pushed"] is True
    assert client.creates == 1


def _const_conn(conn):
    from contextlib import contextmanager

    @contextmanager
    def opener(*a, **k):
        yield conn
    return opener


# ── running with no console ───────────────────────────────────────────────────

def _sync_now_module():
    """Load scripts/sync_now.py as a module, without running main()."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "scripts" / "sync_now.py"
    spec = importlib.util.spec_from_file_location("sync_now_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_entry_point_survives_having_no_stdout_or_stderr(monkeypatch):
    """
    Under pythonw.exe with no console — which is how the Scheduled Task runs it — Windows
    gives Python None for both streams.

    Measured, not assumed: with stdout=None alone the cycle completed and logged; adding
    stderr=None made it exit 1 in total silence, because something further down wrote a
    warning to a stream that was not there. Guarding only this file's own prints would not
    have caught it. An hourly job that dies before its first line of work, with nothing
    written anywhere, is the exact failure the scheduler exists to prevent.
    """
    mod = _sync_now_module()
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)

    mod._init_console()

    assert sys.stdout is not None and sys.stderr is not None
    sys.stdout.write("this must not raise")
    sys.stderr.write("nor this")


def test_reporting_never_raises_even_with_nowhere_to_write(monkeypatch, tmp_path):
    """A log that cannot be written must not crash a cycle that has already done its work."""
    mod = _sync_now_module()
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "nope" / "deep" / "x.log")

    class Hostile:
        def write(self, *a):
            raise OSError("no")

        def flush(self):
            raise OSError("no")

    monkeypatch.setattr("sys.stdout", Hostile())
    mod._say("a line that has nowhere to go")       # must not raise

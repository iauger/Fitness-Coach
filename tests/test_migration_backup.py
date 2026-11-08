"""
A database that has anything in it is copied aside before a migration touches it.

Migration 15 dropped columns for good, the server migrates at boot, and the file holds history no
sync can fetch back. Mutants: no copy; a copy of an empty first-run database (clutter and noise);
a copy taken when nothing is pending (one per boot); a copy that is not a usable database; no limit
on how many are kept; the copy taken after the migration instead of before.
"""

import sqlite3
import time

import pytest

from src.db import migrations
from src.db.migrations import MIGRATIONS, backup_before_migrating, migrate

LAST = MIGRATIONS[-1][0]


def backups(path):
    folder = path.parent / "backups"
    return sorted(folder.glob("*.db")) if folder.exists() else []


def make_old(path, version=LAST - 1):
    """A fully migrated database, wound back so the last migration is pending, with a row in it."""
    migrate(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO activities (id, date, type, moving_time) VALUES ('mine', '2026-11-01', 'Ride', 3600)")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("DELETE FROM schema_migrations WHERE version > ?", (version,))
    conn.commit()
    conn.close()


def test_a_first_run_database_is_not_copied(tmp_path):
    migrate(tmp_path / "t.db")
    assert backups(tmp_path / "t.db") == []


def test_a_database_with_history_is_copied_before_a_pending_migration(tmp_path):
    path = tmp_path / "t.db"
    make_old(path)
    migrate(path)
    (copy,) = backups(path)
    assert f"-v{LAST - 1}-" in copy.name, "named for the version it was taken at"
    got = sqlite3.connect(copy)
    try:
        assert got.execute("SELECT id FROM activities").fetchall() == [("mine",)]
        assert got.execute("PRAGMA user_version").fetchone()[0] == LAST - 1, "taken before, not after"
    finally:
        got.close()


def test_nothing_is_copied_when_nothing_is_pending(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    sqlite3.connect(path).execute("INSERT INTO activities (id, date) VALUES ('x', '2026-11-01')").connection.commit()
    migrate(path)
    migrate(path)
    assert backups(path) == []


def test_only_the_newest_few_are_kept(tmp_path):
    path = tmp_path / "t.db"
    for _ in range(migrations.BACKUPS_KEPT + 3):
        make_old(path) if not path.exists() else _rewind(path)
        migrate(path)
        time.sleep(0.01)
    assert len(backups(path)) == migrations.BACKUPS_KEPT


def _rewind(path):
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LAST - 1}")
    conn.execute("DELETE FROM schema_migrations WHERE version > ?", (LAST - 1,))
    conn.commit()
    conn.close()


def test_the_copy_is_consistent_with_a_write_ahead_log_that_has_not_been_checkpointed(tmp_path):
    """Mutant: a plain file copy. In WAL mode the newest rows can still be in the -wal file."""
    path = tmp_path / "t.db"
    make_old(path)
    holder = sqlite3.connect(path)                    # keeps the WAL from being checkpointed away
    holder.execute("INSERT INTO activities (id, date, type, moving_time) VALUES ('late', '2026-11-02', 'Ride', 60)")
    holder.commit()
    migrate(path)
    (copy,) = backups(path)
    got = sqlite3.connect(copy)
    try:
        assert {r[0] for r in got.execute("SELECT id FROM activities")} == {"mine", "late"}
    finally:
        got.close()
        holder.close()

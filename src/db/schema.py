import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "fitness.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate_db(db_path: Path = DB_PATH) -> None:
    """
    Apply pending schema migrations. Retained as the name every script already calls; the
    actual definitions live in db/migrations.py as of phase 12B.

    Previously this held a list of ALTERs run under `except Exception: pass`, which could not
    tell an already-applied change from a broken one, and which had drifted apart from
    init_db() — three tables existed only here.
    """
    from .migrations import migrate
    migrate(db_path)


def init_db(db_path: Path = DB_PATH) -> None:
    """
    Create the schema. Now identical in effect to migrate_db(): a single ordered set of
    migrations builds an empty database and brings an existing one forward, so the two can no
    longer disagree. Both names are kept because scripts call one, the other, or both.
    """
    from .migrations import migrate
    migrate(db_path)


def schema_version(db_path: Path = DB_PATH) -> int:
    from .migrations import current_version
    conn = get_connection(db_path)
    try:
        return current_version(conn)
    finally:
        conn.close()

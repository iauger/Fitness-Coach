import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "fitness.db"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def write_connection(db_path: Path = DB_PATH):
    """
    A connection that commits on success, rolls back on error, and always closes.

    `with conn:` alone is sqlite3's *transaction* manager — it commits or rolls back, and does
    not close. The idiom this replaces was:

        conn = get_connection()
        with conn:
            conn.execute(...)
        conn.close()

    where any exception inside the block skips the close. In a one-shot script that is
    harmless, but scripts/serve.py is long-lived, and in WAL mode a connection that is only
    collected later holds back checkpointing in the meantime.
    """
    conn = get_connection(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def read_connection(db_path: Path = DB_PATH):
    """A connection that always closes. No transaction is opened."""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def migrate_db(db_path: Path = DB_PATH) -> None:
    """
    Apply pending schema migrations. Retained as the name every script already calls; the
    actual definitions live in db/migrations.py as of phase 12B.

    Previously this held a list of ALTERs run under `except Exception: pass`, which could not
    tell an already-applied change from a broken one, and which had drifted apart from
    the old init_db() — three tables existed only here.
    """
    from .migrations import migrate
    migrate(db_path)



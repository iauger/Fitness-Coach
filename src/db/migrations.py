"""
Versioned schema migrations.

Replaces the pair of functions this supersedes: `init_db()` held CREATE TABLE statements while
`migrate_db()` held an ever-growing list of ALTERs wrapped in a bare `except Exception: pass`.
That arrangement had three problems, all of which were real rather than theoretical:

  1. **The two could drift.** `training_plan_weeks`, `cycle_reviews` and `weekly_summaries` were
     only ever in `migrate_db()`, so `scripts/fetch_history.py` — which calls `init_db()` alone —
     would leave a fresh database missing three tables.
  2. **`except: pass` hid real failures.** It was there to swallow "duplicate column name" on a
     re-run, but it swallowed everything else too, so a genuinely broken migration was
     indistinguishable from an already-applied one.
  3. **There was no notion of what version a database was at.** With `scripts/serve.py` running
     migrations at boot (phase 12A), a server can start against a database in any state; it needs
     to know rather than guess.

Design, chosen to add no dependency: SQLite's own `PRAGMA user_version` is the authoritative
pointer and `schema_migrations` records the history. Each step is a function taking a connection,
applied in order, with the version bumped only after it succeeds.

**Every migration must be idempotent.** Version 1 is a baseline describing the whole schema as it
stood when this module was introduced, so it runs harmlessly against the existing ~19MB database
(everything is already there) and builds the full schema on an empty one. That avoids a fragile
"does this database predate migrations" heuristic — the baseline converges both starting points
on the same result.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from .schema import DB_PATH, get_connection


# ── helpers that make a step safe to re-run ───────────────────────────────────

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """
    ALTER TABLE ... ADD COLUMN, skipped when the column already exists.

    Checked rather than caught. The old code wrapped every ALTER in `except Exception: pass`,
    which could not distinguish "already applied" from a syntax error or a missing table.
    """
    if not table_exists(conn, table):
        raise RuntimeError(f"cannot add {table}.{column}: table {table} does not exist")
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ── migrations ────────────────────────────────────────────────────────────────

BASELINE_SQL = """
CREATE TABLE IF NOT EXISTS activities (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    name        TEXT,
    type        TEXT,
    sport       TEXT,
    moving_time INTEGER,
    distance    REAL,
    elevation   REAL,
    avg_hr      REAL,
    max_hr      REAL,
    avg_power   REAL,
    max_power   REAL,
    np          REAL,
    tss         REAL,
    ctl         REAL,
    atl         REAL,
    tsb         REAL,
    feel        INTEGER,
    calories    REAL,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(type);

CREATE TABLE IF NOT EXISTS wellness (
    date        TEXT PRIMARY KEY,
    ctl         REAL,
    atl         REAL,
    tsb         REAL,
    rhr         REAL,
    hrv         REAL,
    hrv_score   REAL,
    sleep_hrs   REAL,
    sleep_score REAL,
    weight_kg   REAL,
    steps       INTEGER,
    kcal        REAL,
    feel        INTEGER,
    raw_json    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    name        TEXT,
    category    TEXT,
    type        TEXT,
    distance_km REAL,
    raw_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at   TEXT NOT NULL,
    data_type   TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    records     INTEGER,
    status      TEXT
);

CREATE TABLE IF NOT EXISTS life_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date  TEXT NOT NULL,
    end_date    TEXT,
    type        TEXT NOT NULL,
    severity    TEXT,
    note        TEXT,
    logged_at   TEXT NOT NULL,
    logged_via  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_life_events_start ON life_events(start_date);

CREATE TABLE IF NOT EXISTS subjective_feel (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    feel_score  INTEGER,
    energy      INTEGER,
    motivation  INTEGER,
    legs        INTEGER,
    notes       TEXT,
    logged_at   TEXT NOT NULL,
    logged_via  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subjective_feel_date ON subjective_feel(date);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    target_date TEXT,
    priority    TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coaching_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    date        TEXT NOT NULL,
    type        TEXT NOT NULL,
    model       TEXT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coaching_log_date ON coaching_log(date);

CREATE TABLE IF NOT EXISTS transcripts (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    model       TEXT,
    turns       INTEGER,
    summary     TEXT,
    file_path   TEXT
);

CREATE TABLE IF NOT EXISTS planned_workouts (
    id                   TEXT PRIMARY KEY,
    date                 TEXT NOT NULL,
    name                 TEXT NOT NULL,
    planned_tss          REAL,
    planned_duration_min REAL,
    planned_if           REAL,
    workout_url          TEXT,
    gcal_updated         TEXT,
    matched_activity_id  TEXT,
    compliance_status    TEXT,
    compliance_pct       REAL
);
CREATE INDEX IF NOT EXISTS idx_planned_workouts_date ON planned_workouts(date);

CREATE TABLE IF NOT EXISTS athlete_profile (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    metric          TEXT NOT NULL,
    value           REAL NOT NULL,
    effective_date  TEXT NOT NULL,
    note            TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_athlete_profile_metric_date
    ON athlete_profile(metric, effective_date);

CREATE TABLE IF NOT EXISTS training_plan_weeks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name         TEXT NOT NULL,
    plan_start_date   TEXT NOT NULL,
    plan_end_date     TEXT NOT NULL,
    week_start_date   TEXT NOT NULL,
    week_end_date     TEXT NOT NULL,
    week_type         TEXT NOT NULL,
    phase             TEXT NOT NULL,
    phase_number      INTEGER NOT NULL,
    phase_week_number INTEGER NOT NULL,
    plan_week_number  INTEGER NOT NULL,
    UNIQUE(plan_name, week_start_date)
);
CREATE INDEX IF NOT EXISTS idx_training_plan_weeks_start
    ON training_plan_weeks(week_start_date);

CREATE TABLE IF NOT EXISTS cycle_reviews (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name         TEXT NOT NULL,
    cycle_start       TEXT NOT NULL,
    cycle_end         TEXT NOT NULL,
    phase             TEXT,
    plan_week_start   INTEGER,
    plan_week_end     INTEGER,
    weeks             INTEGER,
    ctl_start         REAL,
    ctl_end           REAL,
    ctl_ramp_per_week REAL,
    tsb_end           REAL,
    planned_tss       REAL,
    actual_tss        REAL,
    adherence_pct     REAL,
    hrv_build         REAL,
    hrv_rest          REAL,
    metrics_json      TEXT NOT NULL,
    content           TEXT,
    model             TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE(plan_name, cycle_start)
);
CREATE INDEX IF NOT EXISTS idx_cycle_reviews_start ON cycle_reviews(cycle_start);

CREATE TABLE IF NOT EXISTS weekly_summaries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start       TEXT NOT NULL UNIQUE,
    week_end         TEXT NOT NULL,
    plan_week_number INTEGER,
    week_type        TEXT,
    phase            TEXT,
    planned_tss      REAL,
    actual_tss       REAL,
    sessions         INTEGER,
    adherence_pct    REAL,
    ctl_end          REAL,
    tsb_end          REAL,
    hrv              REAL,
    rhr              REAL,
    sleep_hrs        REAL,
    mean_rpe         REAL,
    metrics_json     TEXT NOT NULL,
    narrative        TEXT,
    model            TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_summaries_start ON weekly_summaries(week_start);
"""


def _m001_baseline(conn: sqlite3.Connection) -> None:
    """The complete schema as of phase 12B. Idempotent by construction."""
    conn.executescript(BASELINE_SQL)

    # Columns added by ALTER over the project's life, in the order they were introduced. Each is
    # skipped when already present, so this converges whether the database is new or existing.
    add_column(conn, "subjective_feel", "tr_compliance", "TEXT")
    add_column(conn, "planned_workouts", "planned_kj", "REAL")
    add_column(conn, "planned_workouts", "description", "TEXT")
    add_column(conn, "planned_workouts", "workout_type", "TEXT")
    # Athlete's own words, from the intervals.icu activity chat thread. Written by a targeted
    # UPDATE in store.update_activity_notes, never by upsert_activities.
    add_column(conn, "activities", "athlete_note", "TEXT")



CHAT_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    ended_at    TEXT,
    model       TEXT,
    mode        TEXT NOT NULL DEFAULT 'conversation',
    title       TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,        -- user | assistant
    content     TEXT NOT NULL,        -- JSON: a string, or a list of content blocks
    created_at  TEXT NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, seq);
"""


def _m002_chat_sessions(conn: sqlite3.Connection) -> None:
    """
    Durable chat history, so a conversation survives a page refresh.

    CoachSession kept `history` in process memory, which is fine for a CLI that exits when the
    conversation ends and impossible for a web UI where the client can reload at any moment.
    `transcripts` does not serve here: it stores one summary row per finished session, written
    at the end, whereas a resumable conversation needs every turn as it happens.

    `content` is JSON rather than text because a turn is not always a string — an assistant turn
    that used a tool is a list of content blocks, and those have to round-trip exactly or the
    next request to the API is malformed.
    """
    conn.executescript(CHAT_SESSIONS_SQL)

# (version, name, step). Append only — never renumber or edit an applied migration.
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline schema as of phase 12B", _m001_baseline),
    (2, "chat sessions and messages for resumable conversations", _m002_chat_sessions),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


# ── runner ────────────────────────────────────────────────────────────────────

def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(db_path: Path = DB_PATH, verbose: bool = False) -> list[tuple[int, str]]:
    """Apply every pending migration in order. Returns the list that was applied."""
    conn = get_connection(db_path)
    applied: list[tuple[int, str]] = []
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            )
        """)
        conn.commit()

        version = current_version(conn)
        for number, name, step in MIGRATIONS:
            if number <= version:
                continue
            if verbose:
                print(f"  applying {number:03d} — {name}")
            try:
                step(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (number, name, datetime.utcnow().isoformat()),
                )
                # PRAGMA cannot be parameterised; `number` comes from this module, not input.
                conn.execute(f"PRAGMA user_version = {number}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append((number, name))
    finally:
        conn.close()
    return applied


def status(db_path: Path = DB_PATH) -> dict:
    conn = get_connection(db_path)
    try:
        version = current_version(conn)
        history = []
        if table_exists(conn, "schema_migrations"):
            history = [
                dict(r) for r in conn.execute(
                    "SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            ]
        return {
            "db_path": str(db_path),
            "current_version": version,
            "latest_version": LATEST_VERSION,
            "pending": [{"version": v, "name": n} for v, n, _ in MIGRATIONS if v > version],
            "history": history,
        }
    finally:
        conn.close()

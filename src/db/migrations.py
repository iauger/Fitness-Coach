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
from pathlib import Path

from .schema import DB_PATH, get_connection
from src.clock import utc_now_iso


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


PRESCRIBED_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS prescribed_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name        TEXT NOT NULL,
    date             TEXT NOT NULL,
    weekday          INTEGER NOT NULL,
    block_index      INTEGER NOT NULL,
    block_name       TEXT NOT NULL,
    week_in_block    INTEGER NOT NULL,
    is_recovery_week INTEGER NOT NULL DEFAULT 0,
    role             TEXT NOT NULL,        -- hard | long | lit | strength
    archetype        TEXT NOT NULL,        -- lit | sit | hiit | threshold | strength
    variant          TEXT NOT NULL,
    prescription     TEXT NOT NULL,        -- the one-line dose, matched to a library workout
    minutes          REAL,
    tss              REAL,
    intensity_factor REAL,
    params_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(plan_name, date, role)
);
CREATE INDEX IF NOT EXISTS idx_prescribed_sessions_date ON prescribed_sessions(date);
"""


def _m003_prescribed_sessions(conn: sqlite3.Connection) -> None:
    """
    What our own plan builder prescribes, kept separate from what is actually on the calendar.

    Deliberately NOT written into `planned_workouts`. That table is owned by
    integrations/tr_calendar.py, which upserts it wholesale from the TrainerRoad iCal feed, and
    it is what compliance matching, the calendar view, weekly summaries and cycle reviews all
    read. Writing prescriptions there would both be clobbered on the next calendar sync and
    destroy the independence of the adherence figure.

    Keeping them apart also buys a third comparison the project could not make before:
    prescribed -> planned -> actual. That answers "did the library workout I picked actually
    match the dose we prescribed", which is the failure mode of using TrainerRoad as a workout
    source while owning the plan structure ourselves.
    """
    conn.executescript(PRESCRIBED_SESSIONS_SQL)

# ── migration 4 ───────────────────────────────────────────────────────────────

# Every literal below is frozen on purpose. A migration that reads a code constant produces a
# different database once that constant is edited, and one that reads the clock produces a
# different database every day it is replayed. Both make "replay from empty" stop converging on
# the state a live database is already in, which is the one property this runner depends on.
OLD_PLAN_NAME = "Sustainable 2026-27"
NEW_PLAN_NAME = "Sustainable rolling"
TR_PLAN_NAME = "TrainerRoad 2026-27"

# The Monday the rolling plan takes over. 2026-11-02: the week whose sessions the rebuilt
# generator has actually produced, which is what makes it the boundary rather than the Monday
# after. An earlier draft of the rebuild plan said 2026-11-09, written before this week's
# prescription existed.
CUTOVER_MONDAY = "2026-11-02"

PLAN_PROGRESSION_SQL = """
CREATE TABLE IF NOT EXISTS plan_progression (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name       TEXT NOT NULL,
    week_start      TEXT NOT NULL,      -- the Monday of the week being graded
    ride_minutes    REAL NOT NULL,
    ride_days       INTEGER NOT NULL,
    strength_logged INTEGER NOT NULL,
    outcome         TEXT NOT NULL,      -- met | partial | missed
    tier_applied    INTEGER NOT NULL,   -- the tier this week was graded against
    verdict         TEXT NOT NULL,      -- advance | hold | step_back
    tier_next       INTEGER NOT NULL,   -- the tier the NEXT week is built at
    week_type_next  TEXT NOT NULL,      -- normal | reentry
    rotation        TEXT NOT NULL,      -- continue | restart_block | restart_rotation
    block_index     INTEGER NOT NULL,   -- rotation position of the NEXT week
    week_in_block   INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(plan_name, week_start)
);
CREATE INDEX IF NOT EXISTS idx_plan_progression_week
    ON plan_progression(plan_name, week_start);
"""


def _m004_plan_progression(conn: sqlite3.Connection) -> None:
    """
    The gate's memory, the export columns, and one plan namespace instead of three.

    **`plan_progression`** holds one row per *closed* week. The gate in planning/progression.py
    is pure and could be replayed from `activities` on every read, but a prescription that
    re-derives itself is a prescription that can change under the athlete mid-week — and the
    decision is the thing worth auditing ("why am I at T3"), not the arithmetic that produced it.
    So the inputs are derived and the decision is persisted, written once when a week closes.

    Two departures from the rebuild plan's draft schema, both forced by the gate's actual
    signature:

      - `outcome TEXT` rather than `met INTEGER`. The gate has *three* outcomes, and the
        distinction it leans on hardest is partial-vs-missed: a partial week does not advance
        you but also does not count toward a gap, so it never triggers a re-entry ramp. A
        boolean cannot represent that, and `trailing_gap()` rebuilt from a boolean would read
        every non-met week as a week off.
      - `tier_applied` *and* `tier_next`. `classify_week` grades a week against the tier that
        was prescribed for it, so replaying history needs that tier, not the current one. It is
        recoverable from the previous row's `tier_next` for every row but the first; storing it
        makes each row self-contained and the sequence self-auditing.

    **Export columns** on `prescribed_sessions` for the intervals.icu push (step 5).
    `external_id` is ours and stable across re-pushes, which is what lets an upsert update an
    event instead of duplicating it; `remote_event_id` is intervals.icu's.

    **One namespace.** The builder wrote `Sustainable 2026-27` (114 rows) while the seeder wrote
    `TrainerRoad 2026-27`, and the join between prescribed and planned keys on the name.

    **A superseded plan keeps its past and loses its future** — applied to both plan tables:

      - `training_plan_weeks`: 34 TrainerRoad rows from CUTOVER_MONDAY on go. The 11 elapsed ones
        stay, because `cycle_reviews` holds a row at `cycle_start = 2026-08-17` and `cycles()`
        derives that review's boundaries from these weeks; deleting them would leave a stored
        review whose own range could not be reproduced.
      - `prescribed_sessions`: the 78 rows from CUTOVER_MONDAY on were generated by the calendar
        ramp this rebuild replaced. They are not merely stale, they contradict what the rebuilt
        generator asks for the very same week — the old rows want a 150min long ride and two hard
        sessions where the gate puts the athlete at T2 with one. Renaming them into the plan of
        record would make that namespace self-contradictory on day one. The 36 elapsed rows stay
        as the record of what was asked.

    The rename runs before both deletes so there is one name for them to match on.
    """
    conn.executescript(PLAN_PROGRESSION_SQL)

    # Ours, stable across re-pushes, so intervals.icu can upsert rather than duplicate.
    add_column(conn, "prescribed_sessions", "external_id", "TEXT")
    add_column(conn, "prescribed_sessions", "remote_event_id", "TEXT")
    add_column(conn, "prescribed_sessions", "pushed_at", "TEXT")

    # Idempotent by construction: after the first run no row matches.
    conn.execute("UPDATE prescribed_sessions SET plan_name = ? WHERE plan_name = ?",
                 (NEW_PLAN_NAME, OLD_PLAN_NAME))

    conn.execute(
        "DELETE FROM training_plan_weeks WHERE plan_name = ? AND week_start_date >= ?",
        (TR_PLAN_NAME, CUTOVER_MONDAY),
    )
    conn.execute(
        "DELETE FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (NEW_PLAN_NAME, CUTOVER_MONDAY),
    )


# ── migration 5 ───────────────────────────────────────────────────────────────

def _m005_tier_on_the_stored_plan(conn: sqlite3.Connection) -> None:
    """
    Record the tier and week type a prescription was built at.

    Both generators already produce these — `blocks.expand_week` returns `tier` and `week_type`,
    `blocks.plan_week_row` returns `volume_tier` — and neither table could hold them, so
    inserting a generated row raised on the week table and silently dropped the tier on the
    session table. Found by trying to store a horizon rather than by reading the schema.

    They matter beyond completeness: the whole point of keeping past prescriptions is to be able
    to say "you were asked for 3 rides and 205 minutes at T2; you rode 2 and 124". Without the
    tier on the row, the ask is only recoverable by replaying the gate, and a replay after a
    later promotion would reconstruct the wrong ask.
    """
    add_column(conn, "prescribed_sessions", "volume_tier", "INTEGER")
    add_column(conn, "prescribed_sessions", "week_type", "TEXT")
    add_column(conn, "training_plan_weeks", "volume_tier", "INTEGER")


# ── migration 6 ───────────────────────────────────────────────────────────────

# Frozen, like every other literal in this module. The day the TrainerRoad iCal feed was
# retired and the rolling plan became the only source of planned workouts.
TR_RETIRED_ON = "2026-11-03"


def _m006_retire_the_trainerroad_feed(conn: sqlite3.Connection) -> None:
    """
    Drop the future half of the TrainerRoad calendar now that nothing refetches it.

    `planned_workouts` was owned by integrations/tr_calendar.py, which re-upserted the whole
    iCal feed on every sync. That is why the rebuild could not simply write there: any row we
    added sat beside TrainerRoad's and double-counted `planned_tss` in `derive.weekly_load`,
    `cycle._load_for_range` and `forecast.project_fitness`, and any row we deleted came back on
    the next run. With the feed gone the table becomes ours, and keeping it as the interface
    means sixteen readers — compliance, the charts, the API, the coach's tools — keep working
    untouched instead of being repointed.

    Same rule as the other two plan tables: **a superseded plan keeps its past and loses its
    future.** The 84 elapsed rows stay, because `compliance_status` on them is the adherence
    record the cycle reviews were written against. The 235 future ones go — they describe a
    plan that was abandoned in September and would otherwise sit there forever, scoring the
    athlete against workouts nobody intends them to do.
    """
    conn.execute("DELETE FROM planned_workouts WHERE date >= ?", (TR_RETIRED_ON,))


# ── migration 7 ───────────────────────────────────────────────────────────────

def _m007_close_the_cutover_overlap(conn: sqlite3.Connection) -> None:
    """
    Remove the one TrainerRoad day that fell inside the rolling plan's own first week.

    Migration 6 cut at TR_RETIRED_ON (2026-11-03, the day it was written). The plan actually
    starts on PLAN_START_MONDAY (2026-11-02), so Monday the 2nd kept a TrainerRoad row — and
    it only became visible when the calendar stopped filtering out sessions with no training
    load, at which point the first week of the new plan rendered with somebody else's mobility
    session in it.

    Scoped by id so it can only remove rows this project did not write: ours all begin with
    the plan slug. Both dates stay frozen rather than being derived from
    planning/constants.py, for the same reason as everywhere else in this module — a migration
    that reads a code constant stops replaying to the same database once that constant moves.
    """
    conn.execute(
        "DELETE FROM planned_workouts WHERE date >= ? AND id NOT LIKE ?",
        ("2026-11-02", "sustainable-rolling-%"),
    )


# (version, name, step). Append only — never renumber or edit an applied migration.
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline schema as of phase 12B", _m001_baseline),
    (2, "chat sessions and messages for resumable conversations", _m002_chat_sessions),
    (3, "prescribed_sessions for the plan builder", _m003_prescribed_sessions),
    (4, "plan_progression, export columns, one plan namespace", _m004_plan_progression),
    (5, "tier and week type on the stored plan", _m005_tier_on_the_stored_plan),
    (6, "retire the TrainerRoad feed", _m006_retire_the_trainerroad_feed),
    (7, "close the cutover overlap", _m007_close_the_cutover_overlap),
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
                    (number, name, utc_now_iso()),
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

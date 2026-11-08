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
from src import applog


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


# ── migration 8 ───────────────────────────────────────────────────────────────

PLANS_SQL = """
CREATE TABLE IF NOT EXISTS plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,   -- stable identity; generated once, never edited
    name         TEXT NOT NULL,          -- the label, free to change
    started_on   TEXT NOT NULL,
    goal_date    TEXT,
    config_hash  TEXT,                   -- which configuration produced this plan
    created_at   TEXT NOT NULL,
    retired_at   TEXT                    -- a plan ends by being retired, never deleted
);
CREATE INDEX IF NOT EXISTS idx_plans_slug ON plans(slug);
"""


def _slug_for(name: str) -> str:
    """
    The stable identity derived from a name, once.

    Deliberately identical to `planning.roller.external_id_prefix`'s derivation, because the
    external ids already pushed to the intervals.icu calendar are built from it. A different
    rule here would orphan fifteen live calendar events on the next push.
    """
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


TIERS_SQL = """
CREATE TABLE IF NOT EXISTS tiers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL REFERENCES plans(id),
    tier_index    INTEGER NOT NULL,
    rides         INTEGER NOT NULL,
    long_min      INTEGER NOT NULL,
    hard          INTEGER NOT NULL,
    hard_min      INTEGER NOT NULL,
    lit_min       INTEGER NOT NULL,
    derived_from  TEXT NOT NULL,
    notes         TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE(plan_id, tier_index)
);
"""


def _m008_plans_table(conn: sqlite3.Connection) -> None:
    """
    Give a plan a row, and an identity that survives being renamed.

    `plan_name` is a string repeated across `prescribed_sessions`, `training_plan_weeks` and
    `plan_progression`, and five readers join on it. That was tolerable while the name lived in
    a Python constant that nobody edited. It stops being tolerable the moment the name comes
    from a config file, because renaming a plan there would silently orphan every stored row —
    no error, just a plan that appears to have no history.

    So identity moves to a generated `slug`, and `name` becomes a label. The slug is derived
    from the name exactly once, here, using the same rule that produced the `external_id`s
    already sitting on the intervals.icu calendar.

    **`plan_name` stays** on all three tables. It is used in 116 places across 14 modules, and
    rewriting those in the same change that introduces the table would make neither reviewable.
    It is now a cached label rather than the key: `planning/plans.py` owns keeping it in step
    with `plans.name`, and a later migration drops it once the readers have moved.

    `config_hash` exists for the same reason `tier_applied` exists on `plan_progression`: store
    the inputs a decision was made under. Once configuration is editable, "which settings
    produced this prescription" is otherwise unanswerable, because re-deriving it later gives
    today's answer to yesterday's question.
    """
    conn.executescript(PLANS_SQL)

    tables = (("prescribed_sessions", "date"),
              ("training_plan_weeks", "week_start_date"),
              ("plan_progression", "week_start"))
    for table, _ in tables:
        add_column(conn, table, "plan_id", "INTEGER REFERENCES plans(id)")

    # Every distinct plan that already has rows, oldest first so ids read chronologically.
    seen: dict[str, str] = {}
    for table, date_col in tables:
        for row in conn.execute(
                f"SELECT plan_name, MIN({date_col}) started FROM {table} "
                f"WHERE plan_name IS NOT NULL GROUP BY plan_name"):
            name, started = row[0], row[1]
            if name not in seen or (started and started < seen[name]):
                seen[name] = started

    now = utc_now_iso()
    for name, started in sorted(seen.items(), key=lambda kv: (kv[1] or "", kv[0])):
        conn.execute(
            "INSERT OR IGNORE INTO plans (slug, name, started_on, created_at) "
            "VALUES (?, ?, ?, ?)", (_slug_for(name), name, started or now[:10], now))

    for table, _ in tables:
        conn.execute(
            f"UPDATE {table} SET plan_id = (SELECT id FROM plans WHERE plans.name = "
            f"{table}.plan_name) WHERE plan_id IS NULL")


def _m009_tiers_table(conn: sqlite3.Connection) -> None:
    """
    The tier ladder becomes data belonging to a plan, instead of a constant in source.

    `progression.TIERS` is one athlete's ladder, anchored on his measured practice. It stays as
    the fallback and as the shape every test reasons about, but a second athlete needs rungs on
    their own scale: a prescription at a third of what someone already rides is ignored exactly
    like one at triple.

    **`derived_from` is the column that earns this table.** It holds the JSON of what the
    bootstrap observed — the window walked, median recent volume, the sustained window and
    where it started. Without it a ladder cannot be explained ("why is T3 105 minutes?") or
    regenerated, and the first question anyone asks of a number a program chose is where it
    came from. Same reasoning as `tier_applied` on `plan_progression` and `config_hash` on
    `plans`: store the inputs the decision was made under, because re-deriving them later
    answers today's question rather than the one that was asked.

    No backfill. An empty table means "not bootstrapped", which is a state the roller has to
    be able to see — it refuses rather than quietly generating from the fallback and never
    revisiting it. Writing this athlete's hand ladder in here as a migration would erase
    exactly that distinction.
    """
    conn.executescript(TIERS_SQL)


def _m010_ladder_versions(conn: sqlite3.Connection) -> None:
    """
    A ladder gets a version, and every decided week records which one graded it.

    `plan_progression.tier_applied` exists so a decision can be read back against the inputs it
    was made under — the same reasoning as `config_hash` on `plans`. That stops being true the
    moment a ladder is re-derived: `tier_applied = 2` means one week of training before the
    re-derivation and a different one after, with nothing in the row to say which.

    It became urgent rather than theoretical when the outgrown check started *recommending*
    re-derivation. Telling an athlete to do something with a known unresolved consequence is
    worse than not noticing in the first place.

    So `tiers` stops being replaced and starts accumulating: `UNIQUE(plan_id, version,
    tier_index)`, with the old rungs kept so a past decision can still be resolved. SQLite
    cannot alter a UNIQUE constraint, so the table is rebuilt — the standard twelve-step
    procedure, minus the foreign-key dance, since nothing references `tiers.id`.

    **Backfill.** Existing rungs become version 1. `plan_progression.ladder_version` is left
    NULL for every row already decided, and NULL has a meaning rather than being an absence:
    *graded before this plan had a derived ladder, against the built-in rungs.* Writing 1 into
    those rows would claim they were graded by a ladder that did not exist when they closed,
    which is the exact dishonesty this column is here to prevent.
    """
    conn.executescript("""
        CREATE TABLE tiers_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id       INTEGER NOT NULL REFERENCES plans(id),
            version       INTEGER NOT NULL DEFAULT 1,
            tier_index    INTEGER NOT NULL,
            rides         INTEGER NOT NULL,
            long_min      INTEGER NOT NULL,
            hard          INTEGER NOT NULL,
            hard_min      INTEGER NOT NULL,
            lit_min       INTEGER NOT NULL,
            derived_from  TEXT NOT NULL,
            notes         TEXT,
            created_at    TEXT NOT NULL,
            UNIQUE(plan_id, version, tier_index)
        );
        INSERT INTO tiers_new (id, plan_id, version, tier_index, rides, long_min, hard,
                               hard_min, lit_min, derived_from, notes, created_at)
            SELECT id, plan_id, 1, tier_index, rides, long_min, hard, hard_min, lit_min,
                   derived_from, notes, created_at FROM tiers;
        DROP TABLE tiers;
        ALTER TABLE tiers_new RENAME TO tiers;
        CREATE INDEX IF NOT EXISTS idx_tiers_plan_version ON tiers(plan_id, version);
    """)
    add_column(conn, "plan_progression", "ladder_version", "INTEGER")


def _m011_optional_goal_date(conn: sqlite3.Connection) -> None:
    """
    A plan may have no goal date, so `training_plan_weeks.plan_end_date` stops being NOT NULL.

    `plan.goal_date` is optional in configuration — correctly, since plenty of people train
    without a dated event, and the rotation deliberately does not taper for one anyway. But
    the column it lands in was declared NOT NULL back when the date was a module constant that
    always existed, so omitting it made `store_horizon` die on an IntegrityError several
    frames from anything that mentioned goals.

    Found by testing something else: a minimal config written for the archetype-override tests
    had no `goal_date`, and the generator could not store a single week. Nobody would have hit
    it before step 7, and whoever did would have been someone running this for the first time.

    SQLite cannot drop a NOT NULL constraint, so the table is rebuilt. `plan_end_date` is read
    by `report.py` and the calendar; both already treat it as a goal rather than a commitment,
    and both handle its absence.
    """
    conn.executescript("""
        CREATE TABLE training_plan_weeks_new (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name        TEXT NOT NULL,
            plan_start_date  TEXT NOT NULL,
            plan_end_date    TEXT,
            week_start_date  TEXT NOT NULL,
            week_end_date    TEXT NOT NULL,
            week_type        TEXT,
            phase            TEXT,
            phase_number     INTEGER,
            phase_week_number INTEGER,
            plan_week_number INTEGER,
            volume_tier      INTEGER,
            plan_id          INTEGER REFERENCES plans(id),
            UNIQUE(plan_name, week_start_date)
        );
        INSERT INTO training_plan_weeks_new
            SELECT id, plan_name, plan_start_date, plan_end_date, week_start_date,
                   week_end_date, week_type, phase, phase_number, phase_week_number,
                   plan_week_number, volume_tier, plan_id FROM training_plan_weeks;
        DROP TABLE training_plan_weeks;
        ALTER TABLE training_plan_weeks_new RENAME TO training_plan_weeks;
    """)


def _m012_session_memory(conn: sqlite3.Connection) -> None:
    """
    A conversation carries its own summary, so it can be remembered once it is over.

    Summaries lived in two places that did not know about each other: the command line wrote one
    to `transcripts` and `coaching_log` when it quit, and the app's conversations, stored in
    `chat_sessions`, were never summarised at all, so anything said to the coach in the app was
    forgotten by the next conversation.

    `summary` is the note (NULL when nothing was worth carrying forward) and `summarised_at`
    says when it was written, so a conversation that continues after being summarised is
    recognisably stale (`summarised_at < updated_at`) and gets summarised again.
    """
    # Guarded, like every other step here: the suite replays migrations over a database that
    # already has the columns, and ALTER TABLE ADD COLUMN has no IF NOT EXISTS.
    have = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
    for column in ("summary", "summarised_at"):
        if column not in have:
            conn.execute(f"ALTER TABLE chat_sessions ADD COLUMN {column} TEXT")


def _m013_signals(conn: sqlite3.Connection) -> None:
    """
    The signal ledger: everything the athlete says about how life and training are going, as
    dated, graded, append-only mentions.

    `life_events` is folded in (a life event is a signal with a date range) and left in place,
    unwritten, because an applied step is never edited and nothing here deletes data. The
    mappings are frozen in this step rather than imported, so a replay years from now builds the
    same ledger even if the live vocabulary has moved on.

    The only mutable columns are `dismissed_at` / `dismissed_reason`: a dismissal is a soft delete
    that keeps the row, since a signal the athlete called wrong is the best test case there is.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at      TEXT NOT NULL,
            on_date          TEXT NOT NULL,
            kind             TEXT NOT NULL,
            topic            TEXT NOT NULL DEFAULT '',
            severity         INTEGER NOT NULL CHECK (severity BETWEEN 0 AND 3),
            state            TEXT NOT NULL DEFAULT 'new',
            quote            TEXT NOT NULL DEFAULT '',
            start_date       TEXT,
            end_date         TEXT,
            source           TEXT NOT NULL,
            source_ref       TEXT,
            extractor_version TEXT,
            dismissed_at     TEXT,
            dismissed_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_signals_on_date ON signals(on_date);
        CREATE INDEX IF NOT EXISTS idx_signals_kind ON signals(kind, topic);
    """)
    kind = {"vacation": "travel", "illness": "illness", "injury": "injury_pain",
            "life_stress": "stress", "gear_change": "gear", "other": "other"}
    severity = {"mild": 1, "moderate": 2, "severe": 3}
    rows = conn.execute("SELECT id, start_date, end_date, type, severity, note, logged_at "
                        "FROM life_events ORDER BY id").fetchall()
    for r in rows:
        ref = str(r[0])
        if conn.execute("SELECT 1 FROM signals WHERE source = 'life_event' AND source_ref = ?",
                        (ref,)).fetchone():
            continue                                  # replay: already carried over
        conn.execute(
            "INSERT INTO signals (observed_at, on_date, kind, topic, severity, state, quote, "
            "start_date, end_date, source, source_ref) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, 'life_event', ?)",
            (r[6], r[1], kind.get(r[3], "other"), severity.get(r[4], 1),
             "resolved" if r[2] else "ongoing", (r[5] or "")[:280], r[1], r[2], ref))


def _m014_followups(conn: sqlite3.Connection) -> None:
    """What the athlete committed to, or asked the coach to raise, kept on the conversation it came from (JSON list)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)")}
    if "followups" not in have:
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN followups TEXT")


def _m015_retire_trainerroad_columns(conn: sqlite3.Connection) -> None:
    """
    Drop the two columns that only the TrainerRoad era used.

    `subjective_feel.tr_compliance` held how the week went against a TrainerRoad plan (five
    answers to a menu in the weekly script; five rows carried one, all `completed_all`).
    `planned_workouts.gcal_updated` was the Google Calendar event's timestamp, and nothing has
    read or written it since the calendar feed went. Plan adherence is measured now, from rides
    against the sessions the rolling plan prescribed, so neither has a reader to serve.

    Guarded, because the baseline step adds the first one back on a fresh database and the suite
    replays migrations over databases that already lack them.
    """
    for table, column in (("subjective_feel", "tr_compliance"), ("planned_workouts", "gcal_updated")):
        if column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


# (version, name, step). Append only — never renumber or edit an applied migration.
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline schema as of phase 12B", _m001_baseline),
    (2, "chat sessions and messages for resumable conversations", _m002_chat_sessions),
    (3, "prescribed_sessions for the plan builder", _m003_prescribed_sessions),
    (4, "plan_progression, export columns, one plan namespace", _m004_plan_progression),
    (5, "tier and week type on the stored plan", _m005_tier_on_the_stored_plan),
    (6, "retire the TrainerRoad feed", _m006_retire_the_trainerroad_feed),
    (7, "close the cutover overlap", _m007_close_the_cutover_overlap),
    (8, "plans table and plan_id foreign keys", _m008_plans_table),
    (9, "tiers table: the ladder as data, with its provenance", _m009_tiers_table),
    (10, "ladder versions, and which one graded each week", _m010_ladder_versions),
    (11, "a plan may have no goal date", _m011_optional_goal_date),
    (12, "conversations carry their own summary", _m012_session_memory),
    (13, "the signal ledger, with life events folded in", _m013_signals),
    (14, "follow-ups on the conversation they came from", _m014_followups),
    (15, "drop the TrainerRoad-era columns", _m015_retire_trainerroad_columns),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


# ── runner ────────────────────────────────────────────────────────────────────

def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


BACKUPS_KEPT = 5


def backup_before_migrating(conn: sqlite3.Connection, db_path: Path, version: int) -> Path | None:
    """
    Copy the database aside before a migration touches it, and return where, or None.

    A migration can drop a column for good (15 did), and a database holds years of the athlete's
    own history and conversations that no sync can fetch back. So a file that has anything in it
    is copied to `backups/` beside it first, using SQLite's own backup so the copy is consistent
    with the write-ahead log, named for the version it was taken at. The newest few are kept.

    A database with nothing in it (a first run) has nothing to protect and gets no copy. Not a
    migration step: it runs once before any of them, and reads the clock, which a step may not.
    """
    from datetime import datetime, timezone

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not tables - {"schema_migrations", "sqlite_sequence"}:
        return None
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = folder / f"{db_path.stem}-v{version}-{stamp}.db"
    copy = sqlite3.connect(dest)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    older = sorted(folder.glob(f"{db_path.stem}-v*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in older[BACKUPS_KEPT:]:
        stale.unlink(missing_ok=True)
    return dest


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
        saved = None
        if any(number > version for number, _, _ in MIGRATIONS):
            saved = backup_before_migrating(conn, db_path, version)
            if saved and verbose:
                print(f"  backed up to {saved}")
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
            except Exception as exc:
                conn.rollback()
                applog.error("db.migration_failed", exc, version=number)
                raise
            applied.append((number, name))
    finally:
        conn.close()
    if applied:
        applog.event("db.migrated", from_version=applied[0][0] - 1, to_version=applied[-1][0],
                     steps=len(applied), backed_up=bool(saved))
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

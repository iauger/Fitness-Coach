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
    """Apply incremental schema changes to an existing DB."""
    conn = get_connection(db_path)
    migrations = [
        "ALTER TABLE subjective_feel ADD COLUMN tr_compliance TEXT",
        """CREATE TABLE IF NOT EXISTS planned_workouts (
            id              TEXT PRIMARY KEY,   -- Google Calendar event ID
            date            TEXT NOT NULL,
            name            TEXT NOT NULL,      -- workout name from TR
            planned_tss     REAL,
            planned_duration_min REAL,
            planned_if      REAL,               -- intensity factor
            planned_kj      REAL,               -- planned kilojoules / calories
            description     TEXT,               -- raw workout description text from TR
            workout_type    TEXT,               -- keyword-classified: Sweet Spot, Threshold, VO2 Max, etc.
            workout_url     TEXT,
            gcal_updated    TEXT,               -- last modified in Google Cal
            matched_activity_id TEXT,           -- FK to activities.id after matching
            compliance_status TEXT,             -- completed / partial / skipped
            compliance_pct  REAL                -- actual_load / planned_tss * 100
        )""",
        "CREATE INDEX IF NOT EXISTS idx_planned_workouts_date ON planned_workouts(date)",
        "ALTER TABLE planned_workouts ADD COLUMN planned_kj REAL",
        "ALTER TABLE planned_workouts ADD COLUMN description TEXT",
        "ALTER TABLE planned_workouts ADD COLUMN workout_type TEXT",
        """CREATE TABLE IF NOT EXISTS athlete_profile (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            metric          TEXT NOT NULL,   -- height_in, weight_lbs, ftp
            value           REAL NOT NULL,
            effective_date  TEXT NOT NULL,   -- value is treated as true on/after this date
            note            TEXT,
            created_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_athlete_profile_metric_date ON athlete_profile(metric, effective_date)",
        # Athlete's own words about the session, pulled from the intervals.icu activity chat
        # thread (see IntervalsClient.get_activity_messages). Written by a targeted UPDATE in
        # store.update_activity_notes, never by upsert_activities.
        "ALTER TABLE activities ADD COLUMN athlete_note TEXT",
        # One row per week of a TrainerRoad plan, seeded by hand from the plan's phase view
        # (scripts/seed_training_plan.py). Not inferable from synced data: intervals.icu carries
        # its own unrelated plan, and the TR calendar only reaches ~14 days forward.
        # Plan dates are denormalized onto every row — a full plan is only tens of rows.
        """CREATE TABLE IF NOT EXISTS training_plan_weeks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name        TEXT NOT NULL,
            plan_start_date  TEXT NOT NULL,
            plan_end_date    TEXT NOT NULL,
            week_start_date  TEXT NOT NULL,   -- always a Monday
            week_end_date    TEXT NOT NULL,   -- the Sunday, inclusive
            week_type        TEXT NOT NULL,   -- base | build | specialty | rest
            phase            TEXT NOT NULL,   -- parent block: base | build | specialty
            phase_number     INTEGER NOT NULL,-- 1-based index of the phase within the plan
            phase_week_number INTEGER NOT NULL, -- 1-based week within that phase
            plan_week_number INTEGER NOT NULL,  -- 1-based week within the whole plan
            UNIQUE(plan_name, week_start_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_training_plan_weeks_start ON training_plan_weeks(week_start_date)",
        # One row per completed mesocycle. Deliberately NOT in coaching_log: that table is
        # rendered with a 300-char truncation and read via a type-blind `LIMIT 3`, so a review
        # would be cut mid-sentence and then evicted by three weekly check-ins — vanishing right
        # when the next cycle needs it for comparison.
        # The named metric columns are the ones compared across cycles, so they have to be
        # queryable; `metrics_json` holds the full rollup so adding a metric doesn't need a
        # migration. Both are written from the same deterministic rollup.
        """CREATE TABLE IF NOT EXISTS cycle_reviews (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_name       TEXT NOT NULL,
            cycle_start     TEXT NOT NULL,
            cycle_end       TEXT NOT NULL,
            phase           TEXT,
            plan_week_start INTEGER,
            plan_week_end   INTEGER,
            weeks           INTEGER,
            ctl_start       REAL,
            ctl_end         REAL,
            ctl_ramp_per_week REAL,
            tsb_end         REAL,
            planned_tss     REAL,
            actual_tss      REAL,
            adherence_pct   REAL,
            hrv_build       REAL,   -- mean HRV across the build weeks
            hrv_rest        REAL,   -- mean HRV across the rest week
            metrics_json    TEXT NOT NULL,   -- full deterministic rollup
            content         TEXT,            -- the coach's written review
            model           TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE(plan_name, cycle_start)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_cycle_reviews_start ON cycle_reviews(cycle_start)",
        # One row per completed week. Same shape as cycle_reviews and for the same reason: the
        # numbers have to stay pinned to the narrative written alongside them, and week-over-week
        # comparison should be a query rather than something the model recalls.
        # `narrative` is the ONLY part that costs an LLM call, and it is null when the week had
        # no notes or RPE to compress — a quiet week is free.
        """CREATE TABLE IF NOT EXISTS weekly_summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start      TEXT NOT NULL UNIQUE,
            week_end        TEXT NOT NULL,
            plan_week_number INTEGER,
            week_type       TEXT,
            phase           TEXT,
            planned_tss     REAL,
            actual_tss      REAL,
            sessions        INTEGER,
            adherence_pct   REAL,
            ctl_end         REAL,
            tsb_end         REAL,
            hrv             REAL,
            rhr             REAL,
            sleep_hrs       REAL,
            mean_rpe        REAL,
            metrics_json    TEXT NOT NULL,
            narrative       TEXT,
            model           TEXT,
            created_at      TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_weekly_summaries_start ON weekly_summaries(week_start)",
    ]
    with conn:
        for sql in migrations:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
    conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activities (
                id          TEXT PRIMARY KEY,
                date        TEXT NOT NULL,
                name        TEXT,
                type        TEXT,
                sport       TEXT,
                moving_time INTEGER,   -- seconds
                distance    REAL,      -- meters
                elevation   REAL,      -- meters
                avg_hr      REAL,
                max_hr      REAL,
                avg_power   REAL,
                max_power   REAL,
                np          REAL,      -- normalized power
                tss         REAL,      -- training stress score
                ctl         REAL,
                atl         REAL,
                tsb         REAL,
                feel        INTEGER,   -- RPE 1-10
                calories    REAL,
                athlete_note TEXT,     -- athlete's own words, from the intervals.icu chat thread
                raw_json    TEXT       -- full API response
            );

            CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
            CREATE INDEX IF NOT EXISTS idx_activities_type ON activities(type);

            CREATE TABLE IF NOT EXISTS wellness (
                date        TEXT PRIMARY KEY,
                ctl         REAL,
                atl         REAL,
                tsb         REAL,
                rhr         REAL,   -- resting heart rate
                hrv         REAL,   -- HRV (ms)
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
                category    TEXT,   -- A, B, C
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
                type        TEXT NOT NULL,  -- vacation, illness, injury, life_stress, gear_change, other
                severity    TEXT,           -- mild, moderate, severe
                note        TEXT,
                logged_at   TEXT NOT NULL,
                logged_via  TEXT NOT NULL   -- checkin, chat, cli
            );

            CREATE INDEX IF NOT EXISTS idx_life_events_start ON life_events(start_date);

            CREATE TABLE IF NOT EXISTS subjective_feel (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                feel_score  INTEGER,        -- 1-10
                energy      INTEGER,        -- 1-10
                motivation  INTEGER,        -- 1-10
                legs        INTEGER,        -- 1-10 (1=dead, 10=great)
                notes       TEXT,
                logged_at   TEXT NOT NULL,
                logged_via  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_subjective_feel_date ON subjective_feel(date);

            CREATE TABLE IF NOT EXISTS goals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                target_date TEXT,
                priority    TEXT,           -- primary, secondary
                status      TEXT NOT NULL DEFAULT 'active',  -- active, achieved, dropped
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coaching_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                date        TEXT NOT NULL,
                type        TEXT NOT NULL,  -- checkin, session_summary
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
        """)
    conn.close()

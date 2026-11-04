"""
The rolling horizon: a few weeks of dated sessions, re-derived and stored.

This is what replaces a 32-week dated plan. Three weeks of prescriptions exist at a time, and
they are regenerated from wherever the athlete actually is rather than marched through. Nothing
tapers and nothing ends.

**The freeze is the whole safety property.** Regeneration only touches rows dated
`today + FREEZE_DAYS` or later. Today and tomorrow are immutable, so a sync that runs on Friday
evening cannot change Saturday's long ride, and past weeks are never rewritten — which is what
makes an outcome auditable. Without it, `prescribed_sessions` would record what the plan *now*
thinks you should have done, which is worthless: "you were asked for 3 rides and 205 minutes at
T2, you rode 2 and 124" is only answerable if the ask was frozen at the time.

Two write rules follow from that, and they are not the same rule:

- Rows **from the cutoff onward** are deleted and rewritten. They are the future; they are
  allowed to change.
- Rows **before the cutoff** are inserted only if missing (`DO NOTHING`). That fills in the
  current week on a first run without ever editing a session already prescribed. `DO UPDATE`
  here would quietly defeat the freeze, which is the easiest way to get this wrong.

**The tier does not change across the horizon.** Weeks 2 and 3 are built at the same tier as
week 1, because the gate only moves when a week closes and those weeks have not happened. What
does advance is rotation position. A horizon that pre-ramped the later weeks would be the
calendar plan again, wearing a smaller number.
"""

import json
from datetime import date, timedelta

from src.analysis.dates import monday_of
from src.clock import utc_now_iso
from src.planning.blocks import (
    PLAN_NAME, ROTATION, advance_position, expand_week, plan_week_row,
)
from src.planning.constants import GOAL_DATE, PLAN_START_MONDAY
from src.planning.progression_store import current_state

# The current week plus the next two. Long enough to see a shape, short enough that it is always
# a projection rather than a promise.
HORIZON_WEEKS = 3

# Today and tomorrow are immutable. Two days rather than one so an evening sync cannot change
# the next morning's session, whichever side of midnight it lands on.
FREEZE_DAYS = 2


def external_id(plan_name: str, on: str, role: str) -> str:
    """
    A stable id for the remote calendar event this session will become.

    Keyed on exactly the columns that make a session unique locally
    (`UNIQUE(plan_name, date, role)`), so re-pushing a horizon updates events rather than
    duplicating them — which is the one thing an export has to get right. Deliberately readable:
    when something goes wrong on intervals.icu, the id should say which session it is.
    """
    slug = "".join(c if c.isalnum() else "-" for c in plan_name.lower()).strip("-")
    return f"{slug}-{on}-{role}"


def horizon(conn, today: date | None = None, plan_name: str = PLAN_NAME,
            weeks: int = HORIZON_WEEKS, start_tier: int = 1,
            persist_state: bool = True) -> dict:
    """
    Build the horizon without writing it. Returns the state, the sessions and the week rows.

    Separate from `store_horizon` so the thing being stored can be inspected and diffed in a
    test, and so `scripts/plan_build.py` can preview exactly what a sync would write rather
    than something built by a parallel code path.
    """
    today = today or date.today()
    state = current_state(conn, today, plan_name, start_tier, persist=persist_state)

    tier = state["tier"]
    block_index, week_in_block = state["block_index"], state["week_in_block"]
    sessions: list[dict] = []
    week_rows: list[dict] = []

    for offset in range(weeks):
        monday = monday_of(today) + timedelta(weeks=offset)
        block = ROTATION[block_index]
        # Only the first week can be a re-entry week: the gate decided that about the week now
        # starting, and it has said nothing about the two after it.
        week_type = state["week_type"] if offset == 0 else "normal"
        sessions += expand_week(block, week_in_block, tier, monday, plan_name,
                                block_index, week_type)
        week_rows.append(plan_week_row(
            monday, block, block_index, week_in_block,
            plan_week_number=_plan_week_number(monday), tier=tier, week_type=week_type,
            plan_name=plan_name, goal_date=GOAL_DATE, plan_start=PLAN_START_MONDAY))
        block_index, week_in_block = advance_position(block_index, week_in_block, "continue")

    return {"state": state, "sessions": sessions, "week_rows": week_rows,
            "cutoff": (today + timedelta(days=FREEZE_DAYS)).isoformat()}


def _plan_week_number(monday: date) -> int:
    """Weeks since the plan started, 1-based. Replaces a hand-maintained counter."""
    start = monday_of(date.fromisoformat(PLAN_START_MONDAY))
    return ((monday - start).days // 7) + 1


def store_horizon(conn, today: date | None = None, plan_name: str = PLAN_NAME,
                  weeks: int = HORIZON_WEEKS, start_tier: int = 1) -> dict:
    """
    Regenerate and store the horizon. Idempotent, and never rewrites a frozen session.

    Returns counts of what moved, so a sync log says something useful rather than "ok".
    """
    today = today or date.today()
    built = horizon(conn, today, plan_name, weeks, start_tier)
    cutoff = built["cutoff"]

    # Scoped delete. The unconditional `DELETE FROM prescribed_sessions WHERE plan_name = ?`
    # this replaces would have wiped the record of every past ask on every sync.
    deleted = conn.execute(
        "DELETE FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (plan_name, cutoff),
    ).rowcount

    # Has the frozen window already been prescribed? If so, nothing before the cutoff is
    # written at all.
    #
    # Deciding this per *window* rather than per row is the point. `UNIQUE(plan_name, date,
    # role)` means an insert-if-missing only protects a row that already exists under the same
    # role — so when the block changed, a frozen day silently gained a *second* session under a
    # role it had not carried before. Adding a workout to tomorrow is as disruptive as editing
    # it, and a frozen rest day must stay a rest day. A test caught this; the symptom was one
    # unexplained backfill on a run that should have written nothing.
    window_start = monday_of(today).isoformat()
    already = conn.execute(
        "SELECT 1 FROM prescribed_sessions WHERE plan_name = ? AND date >= ? AND date < ? "
        "LIMIT 1", (plan_name, window_start, cutoff)).fetchone() is not None

    future = frozen = 0
    for s in built["sessions"]:
        if s["date"] >= cutoff:
            _upsert_session(conn, s, overwrite=True)
            future += 1
        elif not already:
            # First run for this window: fill it in, or the current week is half-missing.
            #
            # `overwrite=False` cannot actually fire here — the guard above means no row exists
            # in this window, so there is nothing to conflict with. It stays because if that
            # guard is ever loosened, this degrades to the old insert-if-missing behaviour
            # rather than to silently overwriting frozen sessions. Reading *this* line as the
            # thing that protects the freeze is what hid the bug in the first place.
            frozen += _upsert_session(conn, s, overwrite=False)

    weeks_written = sum(
        _upsert_week(conn, row, overwrite=row["week_start_date"] >= cutoff)
        for row in built["week_rows"]
    )

    return {"cutoff": cutoff, "deleted": deleted, "written": future,
            "backfilled": frozen, "weeks": weeks_written,
            "tier": built["state"]["tier"], "week_type": built["state"]["week_type"],
            "sessions": built["sessions"]}


def _upsert_session(conn, s: dict, overwrite: bool) -> int:
    conflict = ("""
        DO UPDATE SET
            weekday = excluded.weekday, block_index = excluded.block_index,
            block_name = excluded.block_name, week_in_block = excluded.week_in_block,
            is_recovery_week = excluded.is_recovery_week, archetype = excluded.archetype,
            variant = excluded.variant, prescription = excluded.prescription,
            minutes = excluded.minutes, tss = excluded.tss,
            intensity_factor = excluded.intensity_factor, params_json = excluded.params_json,
            volume_tier = excluded.volume_tier, week_type = excluded.week_type
    """ if overwrite else "DO NOTHING")
    cur = conn.execute(f"""
        INSERT INTO prescribed_sessions (
            plan_name, date, weekday, block_index, block_name, week_in_block,
            is_recovery_week, role, archetype, variant, prescription, minutes, tss,
            intensity_factor, params_json, volume_tier, week_type, external_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_name, date, role) {conflict}
    """, (s["plan_name"], s["date"], s["weekday"], s["block_index"], s["block_name"],
          s["week_in_block"], int(s["is_recovery_week"]), s["role"], s["archetype"],
          s["variant"], s["prescription"], s["minutes"], s["tss"], s["intensity_factor"],
          json.dumps(s["params"]), s["tier"], s["week_type"],
          external_id(s["plan_name"], s["date"], s["role"]), utc_now_iso()))
    return cur.rowcount


def _upsert_week(conn, row: dict, overwrite: bool) -> int:
    conflict = ("""
        DO UPDATE SET
            week_end_date = excluded.week_end_date, week_type = excluded.week_type,
            phase = excluded.phase, phase_number = excluded.phase_number,
            phase_week_number = excluded.phase_week_number,
            plan_week_number = excluded.plan_week_number, volume_tier = excluded.volume_tier
    """ if overwrite else "DO NOTHING")
    cur = conn.execute(f"""
        INSERT INTO training_plan_weeks (
            plan_name, plan_start_date, plan_end_date, week_start_date, week_end_date,
            week_type, phase, phase_number, phase_week_number, plan_week_number, volume_tier
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_name, week_start_date) {conflict}
    """, (row["plan_name"], row["plan_start_date"], row["plan_end_date"],
          row["week_start_date"], row["week_end_date"], row["week_type"], row["phase"],
          row["phase_number"], row["phase_week_number"], row["plan_week_number"],
          row["volume_tier"]))
    return cur.rowcount

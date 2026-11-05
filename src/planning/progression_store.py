"""
The gate's memory: reading and writing `plan_progression`.

`planning/progression.py` is pure by design — no database, no I/O — so the whole gate can be
replayed against a year of real history in a test. This module is the other half: it persists
each decision once, when a week closes, and rebuilds the gate's input from what was stored.

**Why persist at all, when the gate could be re-derived on every read.** It could: the inputs
come from `activities` and the functions are deterministic. But a prescription that re-derives
itself is one that can change under the athlete mid-week — a late-syncing ride on Thursday would
re-grade the week behind it and move the tier while Saturday's long ride is already on the
calendar. And the decision is the thing worth auditing ("why am I at T3"), not the arithmetic
that produced it. So: derive the inputs, persist the decision.

**Where the record starts.** At `plan.start_monday`, not at the beginning of history. Running
the gate over earlier weeks is a counterfactual — no gate existed then — and mixing those rows
in with real decisions would destroy the one property an append-only log has. The pre-plan
replay still happens, in memory, for the single purpose of choosing the rung to start on, and is
labelled as such wherever it surfaces.

**Idempotence.** `record_week` is `INSERT ... ON CONFLICT DO NOTHING` against
`UNIQUE(plan_name, week_start)`. A closed week is decided exactly once and never re-decided,
however many times a sync runs.
"""

from datetime import date, timedelta

from src.analysis.activities import week_volume
from src.analysis.dates import last_completed_week, monday_of, mondays_between
from src.clock import today_local, utc_now_iso
from src.planning.blocks import ROTATION_LOOP_FROM, advance_position
from src.planning import plans
from src.planning.constants import (
    configured_slug, plan_name as configured_plan_name, plan_start_monday,
)
from src.planning.progression import classify_week, next_state

# How far back the pre-plan replay looks when nothing is persisted yet. Long enough to cover the
# longest gap in the reference history plus the climb out of it.
LOOKBACK_WEEKS = 26


def rows(conn, plan_name: str | None = None) -> list[dict]:
    """Every decided week for this plan, oldest first."""
    plan_name = plan_name or configured_plan_name()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_progression WHERE plan_name = ? ORDER BY week_start",
        (plan_name,),
    )]


def _graded_from_row(row: dict) -> dict:
    """
    Rebuild a `classify_week` dict from a stored row.

    `tier_applied` rather than `tier_next`: `classify_week` grades a week against the tier that
    was prescribed *for that week*, and re-grading history against the current tier would make
    the gate irreproducible. That is why the column exists rather than being inferred from the
    previous row — and it also makes the first row self-contained.
    """
    return {
        "week_start": row["week_start"],
        "tier": row["tier_applied"],
        "outcome": row["outcome"],
        "ride_min": row["ride_minutes"],
        "ride_days": row["ride_days"],
        "habit_held": row["strength_logged"] >= 1,
    }


def record_week(conn, graded: dict, decision, block_index: int, week_in_block: int,
                strength_logged: int, plan_name: str | None = None) -> bool:
    """
    Store one closed week's decision. Returns False if that week was already decided.

    The caller supplies `block_index`/`week_in_block` for the *next* week, already advanced by
    `blocks.advance_position` — the rotation is that module's business, not the gate's.
    """
    plan_name = plan_name or configured_plan_name()
    cur = conn.execute("""
        INSERT INTO plan_progression (
            plan_name, plan_id, week_start, ride_minutes, ride_days, strength_logged,
            outcome, tier_applied, verdict, tier_next, week_type_next, rotation,
            block_index, week_in_block, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(plan_name, week_start) DO NOTHING
    """, (plan_name,
          plans.ensure(conn, plans.resolve(conn, plan_name, configured_slug()),
                       plan_name, started_on=plan_start_monday()).id,
          graded["week_start"], graded["ride_min"], graded["ride_days"],
          strength_logged, graded["outcome"], graded["tier"], decision.verdict, decision.tier,
          decision.week_type, decision.rotation, block_index, week_in_block,
          decision.reason, utc_now_iso()))
    return cur.rowcount > 0


def _replay_before_plan(conn, first_monday: date,
                        start_tier: int) -> tuple[int, str, list[dict]]:
    """
    The counterfactual: where would the gate have put the athlete by `first_monday`.

    Returns (tier, the rotation verdict of the last decision, the graded weeks). Nothing is
    written. Used only when `plan_progression` is empty, to choose a starting rung rather than
    assume one — the reference history's last eight weeks were 2.26h, 0, 0, 2.71h, 2.01h, 0,
    2.91h, 2.07h, so any fixed starting tier would be wrong in one direction or the other.

    The graded weeks come back so a caller can *show* the evidence for the starting tier. They
    are a counterfactual and must be labelled as one wherever they surface.
    """
    first = first_monday - timedelta(weeks=LOOKBACK_WEEKS)
    weeks = [week_volume(conn, m) for m in mondays_between(first, first_monday)
             if m < first_monday]
    tier, history, rotation = start_tier, [], "continue"
    for week in weeks:
        history.append(classify_week(week, tier))
        decision = next_state(history, tier)
        tier, rotation = decision.tier, decision.rotation
    return tier, rotation, history


def _seed(conn, plan_start: date, start_tier: int) -> tuple[int, int, int, str, list[dict]]:
    """Starting tier and rotation position when no week has been decided yet."""
    tier, rotation, replayed = _replay_before_plan(conn, plan_start, start_tier)
    # The Consistency block is the entry point for a cold start and where a long absence
    # restarts. An athlete who has been riding is returning from something rather than from
    # nothing, and four weeks of pure endurance is its own abandonment risk.
    block_index = 0 if rotation == "restart_rotation" else ROTATION_LOOP_FROM
    return tier, block_index, 1, "pre-plan replay", replayed


def catch_up(conn, today: date | None = None, plan_name: str | None = None,
             start_tier: int = 1, persist: bool = True) -> dict:
    """
    Decide every week that has closed since the last decided one, and store it.

    Walks **Mondays, not rows**. A week with no activity produces no rows at all, so iterating
    over what the database happens to contain silently omits exactly the weeks that matter — 12
    of the 62 weeks in the history that motivated this design. `week_volume` zero-fills instead.

    Safe to call on every sync: already-decided weeks are skipped by the unique constraint, and
    the current week is never graded because it has not finished.

    `persist=False` computes the same answer and writes nothing. That is what a `--as-of` what-if
    needs: asking "where would I be in March" must not file March's decisions as though they had
    happened, and the unique constraint would then lock those fictional rows in permanently.
    """
    today = today or today_local()
    plan_name = plan_name or configured_plan_name()
    plan_start = monday_of(date.fromisoformat(plan_start_monday()))
    last_closed = last_completed_week(today)   # already a Monday

    stored = rows(conn, plan_name)
    if stored:
        last = stored[-1]
        resume = date.fromisoformat(last["week_start"]) + timedelta(weeks=1)
        tier = last["tier_next"]
        block_index, week_in_block = last["block_index"], last["week_in_block"]
        seeded_from, replayed = "stored", []
    else:
        resume = plan_start
        tier, block_index, week_in_block, seeded_from, replayed = _seed(
            conn, plan_start, start_tier)

    history = [_graded_from_row(r) for r in stored]
    recorded = []
    # `mondays_between` is inclusive at both ends, and returns [] when resume is past
    # last_closed — which is the normal state between a catch-up and the next Monday.
    for monday in mondays_between(max(resume, plan_start), last_closed):
        week = week_volume(conn, monday)
        graded = classify_week(week, tier)
        history.append(graded)
        decision = next_state(history, tier)
        block_index, week_in_block = advance_position(
            block_index, week_in_block, decision.rotation)
        if persist:
            record_week(conn, graded, decision, block_index, week_in_block,
                        week["strength_sessions"], plan_name)
        tier = decision.tier
        recorded.append({**graded, "verdict": decision.verdict, "tier_next": tier,
                         "week_type_next": decision.week_type, "reason": decision.reason})

    decided = history[-1] if history else None
    last_decision = recorded[-1] if recorded else (stored[-1] if stored else None)
    return {
        "tier": tier,
        "block_index": block_index,
        "week_in_block": week_in_block,
        # The week being built takes its type from the decision that closed the week before it.
        # With nothing decided, the plan's first week is an ordinary week, not a re-entry.
        "week_type": (last_decision["week_type_next"] if last_decision else "normal"),
        "reason": (last_decision["reason"] if last_decision
                   else f"T{tier} carried in from the weeks before the plan started"),
        "decided_through": decided["week_start"] if decided else None,
        "seeded_from": seeded_from,
        "recorded": recorded,
        "decided_weeks": len(stored) + len(recorded),
        # Decided weeks when there are any, otherwise the counterfactual that
        # chose the starting rung. `seeded_from` says which.
        "recent": (history or replayed)[-6:],
    }


def current_state(conn, today: date | None = None, plan_name: str | None = None,
                  start_tier: int = 1, persist: bool = True) -> dict:
    """
    Everything the generator needs to build this week: tier, rotation position, week type.

    A catch-up runs first, so a sync that has not happened since a week closed cannot leave the
    generator building at a stale tier. The answer is identical with `persist=False`; only the
    writing stops.
    """
    return catch_up(conn, today, plan_name, start_tier, persist)

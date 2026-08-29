"""
Reads the hand-seeded training_plan_weeks table.

Cycles are derived from `week_type = 'rest'`, not from arithmetic on the week number: a rest
week closes out the block before it by definition, so it survives plan irregularities (a
5-week build, a taper that breaks the rhythm) that `plan_week_number % 4` would get wrong.
Rolling 28-day windows were rejected outright — they drift out of alignment with the real
blocks almost immediately.
"""

from datetime import date, timedelta
from src.db.schema import get_connection


def _rows(plan_name: str | None = None) -> list[dict]:
    conn = get_connection()
    if plan_name:
        rows = conn.execute(
            "SELECT * FROM training_plan_weeks WHERE plan_name = ? ORDER BY week_start_date",
            (plan_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM training_plan_weeks ORDER BY week_start_date"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def current_week(on: date | None = None, plan_name: str | None = None) -> dict | None:
    """The plan week containing `on` (default today), or None if outside any seeded plan."""
    d = (on or date.today()).isoformat()
    for r in _rows(plan_name):
        if r["week_start_date"] <= d <= r["week_end_date"]:
            return r
    return None


def cycles(plan_name: str | None = None) -> list[dict]:
    """
    Split the plan into mesocycles: each run of weeks up to and including a rest week.

    Weeks after the final rest week form a trailing cycle with `closed=False` — that's the
    normal state for a plan in progress, not an error. A phase with no rest week seeded
    produces no boundary at all, so its weeks fold into the next cycle; seed_training_plan.py
    warns when that's the case.
    """
    out: list[dict] = []
    current: list[dict] = []
    for r in _rows(plan_name):
        current.append(r)
        if r["week_type"] == "rest":
            out.append(_cycle(current, closed=True))
            current = []
    if current:
        out.append(_cycle(current, closed=False))
    return out


def _cycle(weeks: list[dict], closed: bool) -> dict:
    return {
        "start_date": weeks[0]["week_start_date"],
        "end_date": weeks[-1]["week_end_date"],
        "weeks": len(weeks),
        # The phase a cycle "belongs to" is the one most of its weeks sit in — a cycle can
        # straddle a phase change if a rest week doesn't land on the phase boundary.
        "phase": max({w["phase"] for w in weeks},
                     key=lambda p: sum(1 for w in weeks if w["phase"] == p)),
        "plan_week_range": (weeks[0]["plan_week_number"], weeks[-1]["plan_week_number"]),
        "closed": closed,
        "week_types": [w["week_type"] for w in weeks],
    }


def current_cycle(on: date | None = None, plan_name: str | None = None) -> dict | None:
    d = (on or date.today()).isoformat()
    for c in cycles(plan_name):
        if c["start_date"] <= d <= c["end_date"]:
            return c
    return None


def just_completed_cycle(on: date | None = None, plan_name: str | None = None) -> dict | None:
    """
    The cycle that ended yesterday, if one did — i.e. `on` is the Monday after a rest week.

    This is the trigger for the 4-week cycle review: it returns something exactly once per
    cycle, on the day the new one starts, so a caller can fire a review without tracking state.
    """
    d = on or date.today()
    for c in cycles(plan_name):
        if c["closed"] and date.fromisoformat(c["end_date"]) == d - timedelta(days=1):
            return c
    return None


def cycle_data_complete(plan_name: str | None = None) -> bool:
    """
    True only if every non-rest phase has at least one rest week seeded.

    When a phase has none, its weeks silently fold into the next cycle and every derived
    figure — cycle length, weeks-to-next-rest — becomes wrong rather than merely absent.
    Callers should suppress cycle-derived output rather than present it, since a plausible
    wrong number is worse than no number.
    """
    by_phase: dict[int, list[dict]] = {}
    for r in _rows(plan_name):
        by_phase.setdefault(r["phase_number"], []).append(r)
    return all(
        any(w["week_type"] == "rest" for w in weeks)
        for weeks in by_phase.values()
    )


def plan_summary(on: date | None = None, plan_name: str | None = None) -> dict | None:
    """Everything the coaching context needs about where the athlete is in the plan."""
    week = current_week(on, plan_name)
    if not week:
        return None
    cycle = current_cycle(on, plan_name)
    all_weeks = _rows(week["plan_name"])
    upcoming_rest = next(
        (w for w in all_weeks
         if w["week_type"] == "rest" and w["week_start_date"] > week["week_start_date"]),
        None,
    )
    complete = cycle_data_complete(week["plan_name"])
    return {
        "plan_name": week["plan_name"],
        "cycle_data_complete": complete,
        "plan_start_date": week["plan_start_date"],
        "plan_end_date": week["plan_end_date"],
        "total_weeks": len(all_weeks),
        "week": week,
        # Cycle-derived fields are None rather than wrong when rest weeks are incompletely
        # seeded. The week/phase fields above stay valid either way — they come straight from
        # the plan's phase view and don't depend on rest-week placement.
        "cycle": cycle if complete else None,
        "next_rest_week": (
            upcoming_rest["week_start_date"] if upcoming_rest and complete else None
        ),
        "weeks_to_rest": (
            (date.fromisoformat(upcoming_rest["week_start_date"])
             - date.fromisoformat(week["week_start_date"])).days // 7
            if upcoming_rest and complete else None
        ),
    }

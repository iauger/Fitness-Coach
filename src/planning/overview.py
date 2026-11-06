"""
Everything the plan screen shows, in one read: where the gate has put you and why, how this week
is going against what would move you, the committed sessions, the shape beyond them, the goals.

Read-only by construction: `current_state` runs with `persist=False`, because opening a screen
must never record a training decision. Every number comes from the same functions the writer
and the coaching report use (the stored ladder, `classify_week`, `plan_outline`), so this page
cannot say 3.5 hours while the calendar says 3.25.
"""

from datetime import date, timedelta

from src.analysis.activities import week_volume
from src.analysis.dates import monday_of
from src import goals as goals_store
from src.planning import plans, tiers_store
from src.planning.constants import plan_name
from src.planning.outline import from_state as plan_outline
from src.planning.progression import classify_week, gate_thresholds
from src.planning.progression_store import current_state
from src.planning.roller import FREEZE_DAYS

HORIZON_DAYS = 21


def _rung(index: int, t) -> dict:
    return {"index": index, "hours": round(t.target_min / 60, 2), "minutes": t.target_min,
            "rides": t.rides, "long_min": t.long_min, "hard": t.hard,
            "hard_min": t.hard_min, "lit_min": t.lit_min}


def sessions(conn, today: date, days: int = HORIZON_DAYS) -> list[dict]:
    """The committed horizon, each session marked frozen (will not be rewritten) and whether
    it has been sent to the calendar."""
    first = today.isoformat()
    last = (today + timedelta(days=days)).isoformat()
    frozen_before = (today + timedelta(days=FREEZE_DAYS)).isoformat()
    out = []
    for r in conn.execute(
            "SELECT date, weekday, role, archetype, variant, prescription, minutes, tss, "
            "week_type, pushed_at FROM prescribed_sessions "
            "WHERE plan_name = ? AND date BETWEEN ? AND ? ORDER BY date, role",
            (plan_name(), first, last)):
        row = dict(r)
        row["frozen"] = row["date"] < frozen_before
        row["on_calendar"] = row.pop("pushed_at") is not None
        out.append(row)
    return out


def overview(conn, today: date) -> dict:
    plan = plans.by_name(conn, plan_name())
    ladder = tiers_store.load(conn, plan.id) if plan else None
    if not ladder:
        return {"ready": False, "plan_name": plan_name(),
                "reason": "There is no tier ladder yet, so there is no plan to show. "
                          "Derive one on the Ladder screen."}

    state = current_state(conn, today, persist=False, tiers=ladder)
    tier = state["tier"]
    spec = ladder[tier]
    monday = monday_of(today)
    volume = week_volume(conn, monday)
    so_far = classify_week(volume, tier, ladder)
    outline = plan_outline(state, today, weeks=12, tiers=ladder)
    here = next((w for w in outline["weeks"] if w["week_start"] == monday.isoformat()), None)

    return {
        "ready": True,
        "plan_name": plan_name(),
        "today": today.isoformat(),
        "tier": _rung(tier, spec),
        "top_tier": max(ladder),
        "ladder": [_rung(i, ladder[i]) for i in sorted(ladder)],
        "week_type": state["week_type"],
        "block": here["block"] if here else None,
        "week_in_block": state["week_in_block"],
        "recovery_week": bool(here and here["recovery"]),
        "reason": state["reason"],
        "seeded_from": state["seeded_from"],
        "decided_weeks": state["decided_weeks"],
        "gate": gate_thresholds(spec),
        "this_week": {
            "week_start": monday.isoformat(),
            "ride_min": round(volume["ride_min"]),
            "ride_days": volume["ride_days"],
            "strength_sessions": volume["strength_sessions"],
            "outcome_so_far": so_far["outcome"],
        },
        "recent": state["recent"],
        "sessions": sessions(conn, today),
        "frozen_before": (today + timedelta(days=FREEZE_DAYS)).isoformat(),
        "outline": outline,
        "goals": [{"description": d, "date": when.isoformat(), "priority": p,
                   "days": (when - today).days} for d, when, p in goals_store.dated(conn)],
    }

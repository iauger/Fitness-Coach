"""
The coach is told the gate's rule exactly, from the gate's own numbers.

Found by asking it: told only that "the tier moves when a completed week meets what was asked",
it explained to the athlete that they needed to hit the target "three times in a row". The gate
needs one met week. Mutants: the rule left out of the snapshot; thresholds hard-coded rather
than taken from `gate_thresholds`; the wording drifting from the constants it describes.
"""

import pytest

import src.analysis.report as R
from src.planning.progression import (
    ADVANCE_MET_WEEKS, MET_FRACTION, PARTIAL_FRACTION, TIERS, classify_week, gate_thresholds,
)


def render(rolling_plan: dict) -> str:
    return R.coaching_context_text({
        "generated": "2026-11-05",
        "athlete": {"name": "A", "goals": [], "stats": {}},
        "timeline": {"goals": []},
        "fitness": {"current": {}, "trend": {}, "peak_ever": {}, "pct_of_peak": None,
                    "weekly_history": []},
        "recovery": {"hrv": {}, "sleep": {}, "resting_hr": {}, "flags": []},
        "training": {"recent_activities": [], "sport_distribution_90d": {},
                     "weekly_load_12w": [], "annual_volume": [], "plan_adherence_8w": {},
                     "recent_planned_workouts": [], "plan_position": None,
                     "earlier_week_summaries": [], "rolling_plan": rolling_plan},
        "memory": {},
    })


def plan_for(tier: int, with_gate: bool = True) -> dict:
    spec = TIERS[tier]
    plan = {"tier": tier, "tier_hours": spec.target_hours, "week_type": "normal",
            "reason": "x", "decided_through": None, "upcoming": [], "outgrown": None,
            "outline": {"summary": "s"}}
    if with_gate:
        plan["gate"] = gate_thresholds(spec)
    return plan


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(R, "load_correction_text", lambda *a, **k: "")
    monkeypatch.setattr(R, "derived_text", lambda *a, **k: "")


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_thresholds_are_what_classify_week_grades(tier):
    """The coach is told N minutes counts; the gate must agree at exactly N and N-1."""
    g = gate_thresholds(TIERS[tier])
    week = {"week_start": "2026-11-02", "ride_min": g["met_min"], "ride_days": g["rides"],
            "strength_sessions": 0}
    assert classify_week(week, tier)["outcome"] == "met"
    assert classify_week({**week, "ride_min": g["met_min"] - 1}, tier)["outcome"] == "partial"
    assert classify_week({**week, "ride_min": g["partial_min"] - 1}, tier)["outcome"] == "missed"
    assert classify_week({**week, "ride_min": g["partial_min"]}, tier)["outcome"] == "partial"


def test_the_snapshot_states_the_rule_with_this_tiers_numbers():
    g = gate_thresholds(TIERS[2])
    text = render(plan_for(2))
    assert f"MET at {g['met_min']}+ ride minutes and {g['rides']}+ ride days" in text
    assert f"PARTIAL: the tier holds" in text and f"Under {g['partial_min']} is MISSED" in text


def test_it_says_one_met_week_and_forbids_describing_a_streak():
    text = render(plan_for(2))
    assert f"{ADVANCE_MET_WEEKS} met week moves the athlete up one rung" in text
    assert "not a streak of several weeks" in text


def test_the_wording_follows_the_constants(monkeypatch):
    """Mutant: the numbers typed into the prose. Change the fraction and the text must follow."""
    import src.planning.progression as P
    monkeypatch.setattr(P, "MET_FRACTION", 0.5)
    g = gate_thresholds(TIERS[2])
    assert g["met_min"] == -(-TIERS[2].target_min // 2)          # ceil of half
    assert f"MET at {g['met_min']}+" in render({**plan_for(2), "gate": g})


def test_the_strength_condition_is_named():
    text = render(plan_for(2))
    assert "strength in 3 of the last 4 weeks" in text


def test_a_plan_with_no_gate_still_renders():
    """The snapshot degrades rather than failing: it is advisory context."""
    text = render(plan_for(2, with_gate=False))
    assert "THE ROLLING PLAN" in text and "The rule, exactly" not in text


def test_the_report_builder_includes_the_gate(tmp_path, monkeypatch):
    """Mutant: the key dropped where the plan dict is built. The text tests hand-build the dict
    and could not see it."""
    import functools
    import sqlite3
    from datetime import date

    from conftest import seed_ladder
    from src.db import schema
    from src.db.migrations import migrate
    from src.planning.roller import store_horizon

    path = tmp_path / "r.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    store_horizon(conn, date(2026, 11, 3))
    conn.commit()
    conn.close()
    monkeypatch.setattr(R, "read_connection", functools.partial(schema.read_connection, path))
    plan = R._rolling_plan(date(2026, 11, 3))
    assert plan and plan["gate"] == gate_thresholds(TIERS[plan["tier"]])

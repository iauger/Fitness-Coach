"""
Noticing when someone has ridden past their own ladder.

The loop this closes is self-reinforcing and was wide open: the plan asks for three days, so
three days get ridden, so a re-derivation finds three days, so `plan.max_ride_days: 7` can be
set and never once be reached by the system's own operation. Someone could sit at the cap for
a year riding five days a week and keep being handed three.

Every test here is about *not* nagging as much as about noticing. A detector that fires on a
good fortnight, or on someone climbing back from a gap, is worse than one that never fires —
it teaches the athlete to ignore it, and this is the one place the plan asks them to go and
change something themselves.
"""

from src.planning.outgrown import (
    HELD_CAP_WEEKS, MIN_LADDER_AGE_WEEKS, VOLUME_MARGIN, assess,
)
from src.planning.progression import Tier

LADDER = {
    0: Tier(0, rides=2, long_min=75, hard=0, hard_min=0, lit_min=50),
    1: Tier(1, rides=3, long_min=75, hard=1, hard_min=55, lit_min=65),
    2: Tier(2, rides=3, long_min=90, hard=1, hard_min=55, lit_min=65),
    3: Tier(3, rides=3, long_min=105, hard=1, hard_min=60, lit_min=65),
    4: Tier(4, rides=3, long_min=120, hard=1, hard_min=60, lit_min=65),
}
CAP = LADDER[4]
OLD_ENOUGH = MIN_LADDER_AGE_WEEKS + 4


def wk(ride_min: float, ride_days: int) -> dict:
    return {"week_start": "2026-11-02", "ride_min": ride_min, "ride_days": ride_days,
            "longest_ride_min": 90.0, "strength_sessions": 2,
            "training_days": max(ride_days, 2), "any_activity": ride_min > 0}


def met_at_cap(n: int) -> list[dict]:
    return [{"outcome": "met", "tier_applied": 4} for _ in range(n)]


def check(weeks, decided=(), max_ride_days=7, age=OLD_ENOUGH):
    return assess(list(weeks), LADDER, list(decided), max_ride_days, age)


# ── the headline signal ───────────────────────────────────────────────────────

def test_riding_more_days_than_the_ladder_asks_for_is_flagged():
    """
    The one thing a re-derivation cannot discover on its own, because the plan never asks for
    the extra day — so unless something looks at what was actually ridden, availability can
    change and nothing will ever notice.
    """
    found = check([wk(400, 5)] * 8)
    assert found and found.outgrown
    assert "ride_days" in found.signals
    assert "bootstrap_plan.py" in found.summary


def test_riding_more_volume_than_the_top_rung_is_flagged():
    found = check([wk(CAP.target_min * 1.4, 3)] * 8)
    assert found.outgrown
    assert "volume" in found.signals


def test_riding_exactly_what_the_ladder_asks_is_not_flagged():
    assert not check([wk(CAP.target_min, 3)] * 8).outgrown


def test_a_margin_of_error_does_not_count_as_outgrowing():
    """
    `hard_min` is a nominal planning duration and the real session runs 52-82min, so the cap's
    target is approximate by a few minutes. Firing on that would be noise.
    """
    just_under = CAP.target_min * (VOLUME_MARGIN - 0.02)
    assert not check([wk(just_under, 3)] * 8).outgrown


# ── holding the cap is context, not a recommendation ─────────────────────────

def test_holding_the_top_rung_is_reported_but_is_not_outgrowing():
    """
    Sitting at the cap is a perfectly good steady state. The design says going higher is a
    decision rather than a consequence of a good streak, so this is raised as a question and
    never as a prompt to re-derive.
    """
    found = check([wk(CAP.target_min, 3)] * 8, met_at_cap(HELD_CAP_WEEKS))
    assert found.signals == ("held_cap",)
    assert not found.outgrown
    assert "bootstrap_plan.py" not in found.summary
    assert found.summary


def test_a_broken_streak_at_the_cap_is_not_holding_it():
    decided = met_at_cap(3) + [{"outcome": "partial", "tier_applied": 4}] + met_at_cap(2)
    found = check([wk(CAP.target_min, 3)] * 8, decided)
    assert "held_cap" not in found.signals


def test_met_weeks_below_the_cap_do_not_count():
    below = [{"outcome": "met", "tier_applied": 2} for _ in range(6)]
    assert "held_cap" not in check([wk(CAP.target_min, 3)] * 8, below).signals


# ── not nagging ───────────────────────────────────────────────────────────────

def test_a_freshly_derived_ladder_is_left_alone():
    """
    Re-deriving every few weeks would make `tier_applied` mean a different thing in every row
    of the decision log, which is the record the whole gate is built on.
    """
    assert not check([wk(400, 5)] * 8, age=MIN_LADDER_AGE_WEEKS - 1).outgrown


def test_someone_returning_from_a_gap_is_never_flagged():
    """
    The opposite situation, and the same scarcity of data describes both. Firing here would
    tell someone climbing back in that they had outgrown their plan.

    The single week here is deliberately a big one — five days, well over the cap. Without the
    too-thin-to-judge guard it fires on that one week, and an earlier version of this test
    used a modest week that would not have fired either way, so the guard could be deleted
    with the suite still green.
    """
    returning = [wk(0, 0)] * 7 + [wk(500, 5)]
    assert not check(returning).outgrown
    assert check(returning).signals == ()


def test_one_strong_fortnight_is_not_enough():
    assert not check([wk(CAP.target_min, 3)] * 6 + [wk(500, 5)] * 2).outgrown


def test_no_history_and_no_ladder_are_both_quiet():
    assert not assess([], LADDER, [], 7, OLD_ENOUGH)
    assert not assess([wk(400, 5)] * 8, {}, [], 7, OLD_ENOUGH)


# ── when re-deriving will not help ───────────────────────────────────────────

def test_availability_already_at_the_ceiling_says_so_instead():
    """
    `bootstrap` clamps the derived day count to `plan.max_ride_days`, so telling someone to
    re-derive when the ceiling is already what the ladder uses sends them round a loop that
    changes nothing. The config is the binding constraint, and that is what gets named.
    """
    found = check([wk(CAP.target_min, 5)] * 8, max_ride_days=3)
    assert found.outgrown and found.blocked_by_config
    assert "max_ride_days" in found.summary


def test_blocked_on_days_but_not_on_volume_still_recommends_re_deriving():
    """
    Both signals firing with the ceiling reached means half the problem is fixable now: the
    rungs can lengthen even though the week cannot widen. Saying only "raise the ceiling"
    would leave that on the table.
    """
    found = check([wk(CAP.target_min * 1.5, 5)] * 8, max_ride_days=3)
    assert found.blocked_by_config
    assert "max_ride_days" in found.summary
    assert "bootstrap_plan.py" in found.summary


def test_the_numbers_behind_the_judgement_are_reported():
    """A sentence a coach repeats is only as good as the figures underneath it."""
    found = check([wk(420, 5)] * 8, met_at_cap(5))
    assert found.detail["recent_ride_days"] == 5
    assert found.detail["ladder_ride_days"] == CAP.rides
    assert found.detail["met_weeks_at_cap"] == 5
    assert found.detail["max_ride_days"] == 7


# ── it has to reach the coach ────────────────────────────────────────────────

def _context_with(plan_extra: dict, monkeypatch) -> str:
    """
    A minimal coaching context, rendered without a database.

    `coaching_context_text` reads the database for two blocks of its own even when handed a
    context — the stored correction and the derived-metrics text — so those are stubbed. The
    real-database guard in conftest found that; the first version of this test reached
    `data/fitness.db`.
    """
    import src.analysis.report as R
    from src.analysis.report import coaching_context_text

    monkeypatch.setattr(R, "load_correction_text", lambda *a, **k: "")
    monkeypatch.setattr(R, "derived_text", lambda *a, **k: "")

    ctx = {
        "generated": "2026-11-05",
        "athlete": {"name": "A", "goals": [], "stats": {}},
        "timeline": {"goals": []},
        "fitness": {"current": {}, "trend": {}, "peak_ever": {}, "pct_of_peak": None,
                    "weekly_history": []},
        "recovery": {"hrv": {}, "sleep": {}, "resting_hr": {}, "flags": []},
        "training": {"recent_activities": [], "sport_distribution_90d": {},
                     "weekly_load_12w": [], "annual_volume": [], "plan_adherence_8w": {},
                     "recent_planned_workouts": [], "plan_position": None,
                     "earlier_week_summaries": [],
                     "rolling_plan": {
                         "tier": 4, "tier_hours": 4.08, "week_type": "normal",
                         "reason": "at the cap", "decided_through": "2026-11-01",
                         "upcoming": [],
                         "outline": {"summary": "Committed: 3 weeks at T4."},
                         **plan_extra}},
        "memory": {},
    }
    return coaching_context_text(ctx)


def test_the_coach_is_told_when_the_athlete_has_outgrown_the_ladder(monkeypatch):
    """
    The detector is worthless if it stops at the context dict. What makes this close the loop
    is that the sentence lands in the text the model reads at a check-in.
    """
    found = check([wk(420, 5)] * 8, met_at_cap(5))
    text = _context_with({"outgrown": {
        "outgrown": found.outgrown, "blocked_by_config": found.blocked_by_config,
        "signals": list(found.signals), "summary": found.summary,
        "detail": found.detail}}, monkeypatch)

    assert "LADDER CHECK" in text
    assert "bootstrap_plan.py" in text
    assert "5 days a week" in text


def test_a_plan_that_has_not_been_outgrown_adds_nothing(monkeypatch):
    """No section at all rather than an empty one — the coach reads every line it is given."""
    assert "LADDER CHECK" not in _context_with({"outgrown": None}, monkeypatch)
    assert "LADDER CHECK" not in _context_with({}, monkeypatch)


def test_the_report_actually_runs_the_check(tmp_path, monkeypatch):
    """
    End to end through `_rolling_plan`, because everything above this tests the detector and
    the renderer with the join between them assumed.

    A mutant that made `_rolling_plan` never call the check — leaving `outgrown` permanently
    None — passed every other test in this file. The detector would have been correct, fully
    covered, and never run.
    """
    import sqlite3
    from datetime import date, timedelta

    import src.analysis.report as R
    from src.db.migrations import migrate
    from src.planning import plans, tiers_store

    path = tmp_path / "r.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    today = date(2026, 11, 5)
    plan = plans.ensure(conn, "s", "S", started_on="2026-01-05")
    rows = [(plan.id, t.index, t.rides, t.long_min, t.hard, t.hard_min, t.lit_min,
             "{}", "[]", "2026-01-05T00:00:00") for t in LADDER.values()]
    conn.executemany(
        "INSERT INTO tiers (plan_id, tier_index, rides, long_min, hard, hard_min, lit_min, "
        "derived_from, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    # Five rides a week for eight weeks, against a ladder built for three.
    for w in range(8):
        monday = today - timedelta(weeks=8 - w)
        for d in range(5):
            conn.execute(
                "INSERT INTO activities (id, date, name, type, moving_time) VALUES (?,?,?,?,?)",
                (f"a{w}-{d}", (monday + timedelta(days=d)).isoformat(),
                 "ride", "Ride", 90 * 60))
    conn.commit()

    monkeypatch.setattr(R, "read_connection", lambda *a, **k: _yielding(conn))
    monkeypatch.setattr(R, "plan_name", lambda: "S")
    monkeypatch.setattr(R, "current_state", lambda *a, **k: {
        "tier": 4, "block_index": 1, "week_in_block": 1, "week_type": "normal",
        "reason": "at the cap", "decided_through": "2026-11-01", "decided_weeks": 8,
        "seeded_from": "stored", "recent": []})

    result = R._rolling_plan(today)
    assert result is not None, "_rolling_plan returned None, so the check cannot have run"
    assert result["outgrown"], "the ladder check did not run"
    assert "ride_days" in result["outgrown"]["signals"]
    conn.close()


def _yielding(conn):
    from contextlib import contextmanager

    @contextmanager
    def opener():
        yield conn
    return opener()


def test_the_advice_names_the_setting_that_is_actually_the_limit():
    """
    Same defect as in bootstrap: with `plan.available_days` binding, "raise
    plan.max_ride_days" is advice that does nothing. Covers both advice branches, since each
    builds its own sentence. Mutant this catches: either branch hardcoding the old name.
    """
    days_only = assess([wk(CAP.target_min, 5)] * 8, LADDER, [], 3, OLD_ENOUGH,
                       ceiling_setting="plan.available_days")
    both = assess([wk(CAP.target_min * 1.5, 5)] * 8, LADDER, [], 3, OLD_ENOUGH,
                  ceiling_setting="plan.available_days")
    for found in (days_only, both):
        assert found.blocked_by_config
        assert "plan.available_days" in found.summary, found.summary
        assert "plan.max_ride_days" not in found.summary, found.summary

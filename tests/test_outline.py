"""
Tests for the twelve-week outline.

The outline's whole job is to show shape without making a promise, so these are mostly about
what it must *not* claim:

- Weeks inside the committed horizon are not projections and must not be marked as such.
- No week beyond the horizon may be presented without its assumption attached.
- A recovery week must never be counted as a promotion opportunity, or the projection grows
  the tier out of a rest.
- The strength precondition on entering the 3.5-4h band has to be stated, because a tier curve
  that silently assumes it reads as though riding alone gets you there.

Pure functions, so no database.
"""

from datetime import date

import pytest

from src.planning.outline import (
    DEFAULT_MET_IN_FOUR, OUTLINE_WEEKS, outline, summarise,
)
from src.planning.progression import CAP_TIER, STRENGTH_GATED_TIER, TIERS

MONDAY = date(2026, 11, 2)


def shared_conn(conn):
    """
    A `read_connection`-shaped context manager that yields a connection it does not own.

    Tests hand the code a connection they keep using afterwards. The real
    `read_connection` closes on exit — correctly, that is the whole point of it — so
    patching it with the real thing would close the test's own handle mid-test. Patching
    `get_connection` instead no longer works: the readers were converted to the context
    manager, so that seam is not on the path any more.
    """
    from contextlib import contextmanager

    @contextmanager
    def opener(*a, **k):
        yield conn
    return opener


def build(**kw):
    args = {"start_monday": MONDAY, "tier": 2, "block_index": 1, "week_in_block": 1}
    args.update(kw)
    return outline(**args)


def test_the_outline_runs_for_the_requested_weeks_from_a_monday():
    rows = build(weeks=OUTLINE_WEEKS)
    assert len(rows) == OUTLINE_WEEKS
    assert rows[0].week_start == MONDAY.isoformat()
    assert all(date.fromisoformat(r.week_start).weekday() == 0 for r in rows)


def test_horizon_weeks_are_committed_and_assume_nothing():
    """
    Inside the horizon the tier is what the gate actually decided.

    Marking those weeks as projections, or letting the assumption move the tier inside them,
    would misrepresent dated sessions that already exist in the database.
    """
    rows = build(horizon_weeks=3)
    committed = rows[:3]
    assert all(not r.projected for r in committed)
    assert all(r.met_assumed is None for r in committed)
    assert len({r.tier for r in committed}) == 1


def test_everything_past_the_horizon_is_marked_projected():
    rows = build(horizon_weeks=3)
    assert all(r.projected for r in rows[3:])


def test_a_recovery_week_is_never_a_promotion_opportunity():
    """It asks for less by design, so counting it would project growth out of a rest."""
    rows = build(weeks=OUTLINE_WEEKS)
    assert any(r.recovery for r in rows), "no recovery week in range — test proves nothing"
    assert all(r.met_assumed is None for r in rows if r.recovery)


def test_the_tier_climbs_only_on_an_assumed_met_week():
    rows = build(weeks=OUTLINE_WEEKS)
    for before, after in zip(rows, rows[1:]):
        if after.tier > before.tier:
            assert before.met_assumed is True, (
                f"tier rose after a week marked {before.met_assumed}")


def test_the_tier_never_exceeds_the_ceiling():
    rows = build(weeks=40, met_in_four=4)
    assert max(r.tier for r in rows) == CAP_TIER


def test_assuming_every_week_missed_holds_the_tier():
    rows = build(weeks=OUTLINE_WEEKS, met_in_four=0)
    assert {r.tier for r in rows} == {2}


def test_hours_always_match_the_tier():
    for r in build(weeks=OUTLINE_WEEKS):
        assert r.hours == TIERS[r.tier].target_hours


# ── the summary carries its premise ───────────────────────────────────────────

def test_the_summary_states_the_assumption_beside_the_conclusion():
    """
    Put the assumption in a footnote and the number travels without it.

    The failure this guards against is the projection being quoted as a schedule — which is
    how a 32-week dated ramp got believed in the first place.
    """
    text = summarise(build(weeks=OUTLINE_WEEKS), DEFAULT_MET_IN_FOUR)
    assert f"{DEFAULT_MET_IN_FOUR} of 4 weeks met" in text
    assert "projection" in text
    assert "not a schedule" in text


def test_the_summary_separates_committed_weeks_from_projected_ones():
    text = summarise(build(weeks=OUTLINE_WEEKS, horizon_weeks=3))
    assert "Committed: 3 weeks" in text
    assert "shape only" in text


def test_crossing_into_the_banded_tier_states_the_strength_precondition():
    """
    Entering the 3.5-4h band costs the anchor habit.

    Unsaid, the tier curve reads as though riding alone gets you there — and strength has not
    been logged since 2026-09-02, so this is the condition most likely to bind.
    """
    rows = build(tier=STRENGTH_GATED_TIER - 1, weeks=OUTLINE_WEEKS)
    assert max(r.tier for r in rows) >= STRENGTH_GATED_TIER
    text = summarise(rows, DEFAULT_MET_IN_FOUR, strength_recently_held=False)
    assert "strength" in text.lower()
    assert f"stops at T{STRENGTH_GATED_TIER - 1}" in text


def test_the_strength_caveat_is_softer_when_the_habit_is_being_held():
    rows = build(tier=STRENGTH_GATED_TIER - 1, weeks=OUTLINE_WEEKS)
    text = summarise(rows, DEFAULT_MET_IN_FOUR, strength_recently_held=True)
    assert "strength" in text.lower()
    assert "stops at" not in text


def test_a_projection_that_starts_at_the_ceiling_says_so():
    rows = build(tier=CAP_TIER, weeks=OUTLINE_WEEKS)
    text = summarise(rows)
    assert "ceiling" in text
    assert "not a consequence of a good streak" in text


def test_an_empty_outline_does_not_crash_the_summary():
    assert summarise([]) == "No outline available."


# ── read-side callers must not write ──────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path):
    """A migrated, empty database, so these tests do not read the athlete's real one."""
    import sqlite3
    from src.db.migrations import migrate

    path = tmp_path / "o.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_the_report_asks_the_gate_not_to_persist(monkeypatch, temp_db):
    """
    Asserted on the call, not on the database.

    Today a write from here happens to be harmless because `read_connection` never commits —
    but that is the connection type protecting us, not the intent being expressed. If someone
    later switches it to `write_connection`, building a report would start filing permanent,
    unique-constrained training decisions from a code path nobody thinks of as a writer.
    """
    import src.analysis.report as R

    seen = {}

    # `tiers` is accepted because the readers now pass the plan's stored ladder. Without it
    # the call raises TypeError, _rolling_plan swallows that into a None result, and the spy
    # records nothing — which is exactly how this test failed when the ladder was threaded.
    def spy(conn, today=None, plan_name=None, start_tier=1, persist=True, tiers=None):
        seen["persist"] = persist
        return {"tier": 2, "block_index": 1, "week_in_block": 1, "week_type": "normal",
                "reason": "spied", "decided_through": None, "decided_weeks": 0,
                "seeded_from": "spy", "recent": []}

    monkeypatch.setattr(R, "current_state", spy)
    monkeypatch.setattr(R, "read_connection", shared_conn(temp_db))
    R._rolling_plan(date(2026, 11, 3))
    assert seen.get("persist") is False


def test_the_plan_api_asks_the_gate_not_to_persist(monkeypatch, temp_db):
    """Same contract, the other read-side caller. A GET must never record a decision."""
    import src.api.app as A

    calls = []

    # `tiers` is accepted because the readers now pass the plan's stored ladder. Without it
    # the call raises TypeError, _rolling_plan swallows that into a None result, and the spy
    # records nothing — which is exactly how this test failed when the ladder was threaded.
    def spy(conn, today=None, plan_name=None, start_tier=1, persist=True, tiers=None):
        calls.append(persist)
        return {"tier": 2, "block_index": 1, "week_in_block": 1, "week_type": "normal",
                "reason": "spied", "decided_through": None, "decided_weeks": 0,
                "seeded_from": "spy", "recent": []}

    monkeypatch.setattr(A, "current_state", spy)
    monkeypatch.setattr(A, "read_connection", shared_conn(temp_db))
    A.plan_state(date(2026, 11, 3))
    A.plan_outline_route(date(2026, 11, 3), weeks=4)
    assert calls and all(p is False for p in calls)


# ── the calendar must show the habit the plan is anchored on ──────────────────

def test_a_session_with_no_training_load_still_renders_on_the_calendar():
    """
    Strength days were invisible, and the filter that hid them looked correct.

    `planned_tss IS NOT NULL` was a fine proxy for "real workout" while the table held
    TrainerRoad's feed, where a null meant mobility. The rolling plan writes strength with a
    null load *deliberately* — compliance reads a non-null as "bike workout with power
    targets", and a zero would score every strength day as skipped. Two strength sessions a
    week at every tier is the anchor habit, and the calendar was drawing the week as two rides
    and five blanks.
    """
    from src.visualizations.calendar_view import _planned_html

    strength = {"name": "40min - full body, compound lifts", "planned_tss": None,
                "planned_duration_min": 40.0, "workout_type": "strength",
                "compliance_status": None, "compliance_pct": None}
    html = _planned_html(strength)
    assert "full body" in html
    assert "40min" in html
    assert "None" not in html

    ride = {"name": "60min @ 62% FTP", "planned_tss": 38.0, "planned_duration_min": 60.0,
            "workout_type": "lit", "compliance_status": None, "compliance_pct": None}
    assert "38 TSS" in _planned_html(ride)


def test_a_session_with_neither_load_nor_duration_does_not_render_none():
    from src.visualizations.calendar_view import _planned_html

    html = _planned_html({"name": "Something", "planned_tss": None,
                          "planned_duration_min": None, "compliance_status": None,
                          "compliance_pct": None})
    assert "None" not in html and "Something" in html


def test_the_calendar_query_loads_sessions_that_have_no_training_load(tmp_path, monkeypatch):
    """
    The filter, not just the renderer.

    Fixing `_planned_html` alone would leave strength days correct-looking and still absent,
    because the row never reaches it — the `planned_tss IS NOT NULL` clause dropped them in
    SQL. A mutant restoring that clause passed every other test here.
    """
    import sqlite3
    import src.visualizations.calendar_view as CV
    from src.db.migrations import migrate

    path = tmp_path / "cal.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO planned_workouts (id, date, name, planned_tss, "
                 "planned_duration_min, workout_type) VALUES (?,?,?,?,?,?)",
                 ("s1", "2026-11-04", "40min - full body", None, 40.0, "strength"))
    conn.execute("INSERT INTO planned_workouts (id, date, name, planned_tss, "
                 "planned_duration_min, workout_type) VALUES (?,?,?,?,?,?)",
                 ("r1", "2026-11-05", "60min @ 62% FTP", 38.0, 60.0, "lit"))
    conn.commit()
    monkeypatch.setattr(CV, "read_connection", shared_conn(conn))

    days = CV._load_data(date(2026, 11, 2), date(2026, 11, 8))
    names = [p["name"] for d in days.values() for p in d.get("planned", [])]
    conn.close()

    assert "40min - full body" in names, "strength day never reached the renderer"
    assert "60min @ 62% FTP" in names

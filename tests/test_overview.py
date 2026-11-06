"""
The plan screen's read: one dict, built from the same functions the writer uses.

Mutants these exist to catch: a "what moves you" threshold that disagrees with `classify_week`
(the screen would promise a promotion the gate then refuses); the freeze boundary off by one
(a session marked editable that the roller will not touch, or the reverse); a screen that
records a training decision by being opened; a session shown as on the calendar when it was
never pushed; and "this week" counting a different week from the gate.
"""

import math
import sqlite3
from datetime import date, timedelta

import pytest

from conftest import seed_ladder
from src.db.migrations import migrate
from src.planning.constants import dated_goals
from src.planning.overview import overview
from src.planning.progression import MET_FRACTION, TIERS, classify_week
from src.planning.roller import FREEZE_DAYS, store_horizon

TODAY = date(2026, 11, 3)          # a Tuesday inside the plan's first week


def connect(tmp_path):
    path = tmp_path / "o.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path)
    seed_ladder(conn)
    store_horizon(conn, TODAY)
    yield conn
    conn.close()


def ride(conn, day: date, minutes: int, kind: str = "Ride"):
    conn.execute("INSERT INTO activities (id, date, type, moving_time) VALUES (?, ?, ?, ?)",
                 (f"a{day.isoformat()}{minutes}", day.isoformat(), kind, minutes * 60))


def test_without_a_ladder_it_says_so_rather_than_inventing_a_plan(tmp_path):
    conn = connect(tmp_path)
    out = overview(conn, TODAY)
    assert out["ready"] is False and "ladder" in out["reason"].lower()
    assert "tier" not in out
    conn.close()


def test_the_tier_is_the_stored_rung_it_names(db):
    out = overview(db, TODAY)
    spec = TIERS[out["tier"]["index"]]
    assert (out["tier"]["minutes"], out["tier"]["rides"]) == (spec.target_min, spec.rides)
    assert out["tier"]["hours"] == round(spec.target_min / 60, 2)
    assert [r["index"] for r in out["ladder"]] == sorted(TIERS) and out["top_tier"] == max(TIERS)


@pytest.mark.parametrize("tier", sorted(TIERS))
def test_the_gate_numbers_agree_with_classify_week_at_the_boundary(db, monkeypatch, tier):
    """
    Mutant: ceil -> floor, or 0.9 hard-coded. The screen says "ride N minutes to advance"; the
    gate must grade exactly N as met and N-1 as not, or the screen is promising something false.
    """
    from src.planning import overview as mod
    monkeypatch.setattr(mod, "current_state", lambda *a, **k: {
        "tier": tier, "block_index": 1, "week_in_block": 1, "week_type": "normal",
        "reason": "forced", "seeded_from": "forced", "decided_weeks": 0, "recent": [],
        "decided_through": None, "recorded": []})
    gate = overview(db, TODAY)["gate"]
    at = {"week_start": "2026-11-02", "ride_min": gate["met_min"], "ride_days": gate["rides"],
          "strength_sessions": 0}
    below = {**at, "ride_min": gate["met_min"] - 1}
    short = {**at, "ride_days": gate["rides"] - 1}
    assert classify_week(at, tier)["outcome"] == "met"
    assert classify_week(below, tier)["outcome"] == "partial"
    assert classify_week(short, tier)["outcome"] == "partial"
    assert gate["met_min"] == math.ceil(MET_FRACTION * TIERS[tier].target_min)


def test_this_week_counts_what_was_ridden_in_the_gates_week(db):
    ride(db, date(2026, 11, 2), 60)               # Monday: this week
    ride(db, date(2026, 11, 3), 60)               # Tuesday: this week
    ride(db, date(2026, 10, 31), 120)             # last Saturday: not this week
    ride(db, date(2026, 11, 3), 40, kind="WeightTraining")   # a lift is not a ride
    week = overview(db, TODAY)["this_week"]
    assert week["week_start"] == "2026-11-02"
    assert (week["ride_min"], week["ride_days"], week["strength_sessions"]) == (120, 2, 1)


def test_outcome_so_far_is_the_gates_own_grade(db):
    ride(db, date(2026, 11, 2), 60)
    ride(db, date(2026, 11, 3), 60)
    out = overview(db, TODAY)
    expected = classify_week({"week_start": "2026-11-02", "ride_min": 120, "ride_days": 2,
                              "strength_sessions": 1},
                             out["tier"]["index"])["outcome"]
    assert out["this_week"]["outcome_so_far"] == expected


def test_the_freeze_boundary_matches_the_rollers(db):
    """Mutant: <= for <. Today+1 is frozen, today+FREEZE_DAYS is the first the roller rewrites."""
    out = overview(db, TODAY)
    cutoff = (TODAY + timedelta(days=FREEZE_DAYS)).isoformat()
    assert out["frozen_before"] == cutoff
    for s in out["sessions"]:
        assert s["frozen"] == (s["date"] < cutoff), s["date"]
    assert any(s["frozen"] for s in out["sessions"]) and any(not s["frozen"] for s in out["sessions"])


def test_a_session_is_on_the_calendar_only_if_it_was_pushed(db):
    db.execute("UPDATE prescribed_sessions SET pushed_at = '2026-11-03T10:00:00Z' "
               "WHERE date = (SELECT MIN(date) FROM prescribed_sessions WHERE date >= ?)",
               (TODAY.isoformat(),))
    out = overview(db, TODAY)
    flags = [s["on_calendar"] for s in out["sessions"]]
    assert flags.count(True) >= 1 and flags.count(False) >= 1
    assert all(isinstance(f, bool) for f in flags) and "pushed_at" not in out["sessions"][0]


def test_opening_the_screen_records_no_decision(db):
    """Mutant: persist=True. A GET must never write a training decision."""
    before = db.execute("SELECT COUNT(*) FROM plan_progression").fetchone()[0]
    overview(db, date(2026, 11, 20))              # a later week, so there is something to catch up
    assert db.execute("SELECT COUNT(*) FROM plan_progression").fetchone()[0] == before


def test_goals_count_down_from_today_and_are_in_date_order(db):
    out = overview(db, TODAY)
    assert [g["description"] for g in out["goals"]] == [g[0] for g in dated_goals()]
    for g in out["goals"]:
        assert g["days"] == (date.fromisoformat(g["date"]) - TODAY).days
    assert [g["date"] for g in out["goals"]] == sorted(g["date"] for g in out["goals"])


def test_the_outline_and_the_block_come_from_the_same_week(db):
    out = overview(db, TODAY)
    this = next(w for w in out["outline"]["weeks"] if w["week_start"] == "2026-11-02")
    assert out["block"] == this["block"] and out["recovery_week"] == this["recovery"]


def test_a_session_on_the_cutoff_day_is_not_frozen(tmp_path):
    """
    Mutant: `<=` for `<` on the freeze boundary. Only bites when a session lands exactly on
    today + FREEZE_DAYS, which depends on the weekday, so every weekday is tried and at least
    one must have such a session or this proves nothing.
    """
    hit = 0
    for offset in range(7):
        today = TODAY + timedelta(days=offset)
        path = tmp_path / f"d{offset}.db"
        migrate(path)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        seed_ladder(conn)
        store_horizon(conn, today)
        out = overview(conn, today)
        for s in out["sessions"]:
            if s["date"] == out["frozen_before"]:
                hit += 1
                assert s["frozen"] is False
        conn.close()
    assert hit, "no session ever landed on the cutoff day; the boundary is untested"


def test_the_route_serves_the_overview(db, monkeypatch):
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    import src.api.app as web

    # Sync routes run in a worker thread, so the route needs its own connection to the file.
    db.commit()
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    shared = sqlite3.connect(path, check_same_thread=False)
    shared.row_factory = sqlite3.Row

    @contextmanager
    def same():
        yield shared
    monkeypatch.setattr(web, "read_connection", same)
    r = TestClient(web.app, base_url="http://localhost:8800").get(
        "/api/plan/overview", params={"on": TODAY.isoformat()})
    assert r.status_code == 200
    assert r.json() == overview(db, TODAY)

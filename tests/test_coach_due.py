"""
When a check-in is due.

Mutants these exist to catch: a closed week that does not make it due; a covered week that
still does (nagging); the minimum gap ignored; a progression event from before the last
check-in counted again; another plan's decisions or blocks counted; a block review offered the
day the block ends, or after it was filed.
"""

import sqlite3
from datetime import date

import pytest

from src.coach import due
from src.db.migrations import migrate
from src.planning.constants import plan_name


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "d.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def checkin(conn, day: str, created: str | None = None):
    conn.execute("INSERT INTO coaching_log (session_id, date, type, content, created_at) "
                 "VALUES ('s', ?, 'checkin', 'x', ?)", (day, created or f"{day}T12:00:00"))


def decision(conn, week: str, verdict: str, created: str, tier_applied=2, tier_next=3,
             week_type="normal", plan: str | None = None):
    conn.execute(
        "INSERT INTO plan_progression (plan_name, week_start, ride_minutes, ride_days, "
        "strength_logged, outcome, tier_applied, verdict, tier_next, week_type_next, rotation, "
        "block_index, week_in_block, reason, created_at) VALUES (?,?,0,0,0,'met',?,?,?,?,0,1,1,'r',?)",
        (plan or plan_name(), week, tier_applied, verdict, tier_next, week_type, created))


# -- weekly -----------------------------------------------------------------------------------

def test_never_having_checked_in_is_overdue(db):
    s = due.checkin_status(db, date(2026, 11, 6))
    assert s["state"] == "overdue" and "not done a weekly check-in" in s["reasons"][0]
    assert s["last_date"] is None and s["days_since"] is None


def test_a_week_that_closed_after_the_last_check_in_makes_it_due(db):
    checkin(db, "2026-11-09")                       # Monday
    s = due.checkin_status(db, date(2026, 11, 16))  # the next Monday: Nov 9-15 has closed
    assert s["state"] == "due" and s["weeks_closed"] == 1 and s["days_since"] == 7
    assert "A week has closed" in s["reasons"][0]


def test_a_check_in_covers_the_weeks_that_ended_before_it(db):
    """Mutant: comparing week *start*. A Wednesday check-in covers the week that ended Sunday."""
    checkin(db, "2026-11-11")                       # Wednesday, after week Nov 2-8 closed
    s = due.checkin_status(db, date(2026, 11, 13))
    assert s["state"] == "ok" and s["reasons"] == [] and s["weeks_closed"] == 0


def test_a_check_in_on_the_closing_sunday_covers_that_week(db):
    checkin(db, "2026-11-08")                       # the Sunday itself
    assert due.checkin_status(db, date(2026, 11, 10))["state"] == "ok"


def test_two_closed_weeks_is_overdue(db):
    checkin(db, "2026-11-09")
    s = due.checkin_status(db, date(2026, 11, 23))
    assert s["state"] == "overdue" and s["weeks_closed"] == 2
    assert "2 or more weeks" in s["reasons"][0]


def test_fourteen_days_is_overdue_on_its_own(db, monkeypatch):
    """The two thresholds are separate rules; either alone should do it."""
    monkeypatch.setattr(due, "OVERDUE_WEEKS", 99)
    checkin(db, "2026-11-02")
    assert due.checkin_status(db, date(2026, 11, 16))["state"] == "overdue"
    assert due.checkin_status(db, date(2026, 11, 15))["state"] == "due"


def test_it_never_prompts_straight_after_a_check_in(db):
    """Mutant: MIN_GAP ignored. A decision recorded after the check-in must not nag the next day."""
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-02", "advance", "2026-11-09T10:00:00")
    s = due.checkin_status(db, date(2026, 11, 10))
    assert s["state"] == "ok" and s["reasons"] == []


# -- progression ------------------------------------------------------------------------------

def test_moving_up_is_a_reason_even_when_no_week_has_closed(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-02", "advance", "2026-11-09T10:00:00")
    s = due.checkin_status(db, date(2026, 11, 11))
    assert s["state"] == "due" and any("moved up from T2 to T3" in r for r in s["reasons"])


def test_a_decision_from_before_the_last_check_in_is_not_counted_again(db):
    """Mutant: no created_at filter. The same promotion would prompt every week."""
    decision(db, "2026-11-02", "advance", "2026-11-09T08:00:00")
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    s = due.checkin_status(db, date(2026, 11, 11))
    assert s["state"] == "ok"


def test_stepping_down_and_a_missed_week_are_reasons(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-09", "step_back", "2026-11-16T07:00:00", tier_applied=3, tier_next=2)
    decision(db, "2026-11-02", "hold", "2026-11-16T07:01:00", week_type="reentry")
    s = due.checkin_status(db, date(2026, 11, 16))
    text = " ".join(s["reasons"])
    assert "eased you from T3 to T2" in text and "A week was missed (2026-11-02)" in text


def test_another_plans_decisions_are_ignored(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-02", "advance", "2026-11-10T10:00:00", plan="Some Other Plan")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "ok"


def test_a_hold_is_not_worth_a_prompt(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-02", "hold", "2026-11-10T10:00:00")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "ok"


# -- what the athlete has told the coach --------------------------------------------------------

def told(conn, kind, severity, day, topic="", state="new"):
    conn.execute("INSERT INTO signals (observed_at, on_date, kind, topic, severity, state, quote, source) "
                 "VALUES ('x', ?, ?, ?, ?, ?, 'q', 'manual')", (day, kind, topic, severity, state))


def test_something_needing_action_makes_a_check_in_due_even_when_no_week_has_closed(db):
    """Mutant: signals not consulted. Someone who has just said they are ill is told "all good"."""
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    told(db, "illness", 3, "2026-11-11")
    s = due.checkin_status(db, date(2026, 11, 12))
    assert s["state"] == "due"
    assert s["reasons"] == ["You told the coach about illness (needs action) on 2026-11-11."]


def test_a_level_two_raised_since_the_last_check_in_counts_and_one_raised_before_it_does_not(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    told(db, "injury_pain", 2, "2026-11-10", topic="left knee")
    told(db, "fatigue", 2, "2026-11-05")
    s = due.checkin_status(db, date(2026, 11, 12))
    assert s["state"] == "due" and len(s["reasons"]) == 1 and "left knee" in s["reasons"][0]


def test_a_level_two_already_discussed_is_not_a_reason_again(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    told(db, "fatigue", 2, "2026-11-05")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "ok"


def test_a_level_three_stays_a_reason_until_it_leaves_the_watch_list(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    told(db, "burnout", 3, "2026-11-05")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "due"


def test_a_resolved_signal_is_no_reason(db):
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    told(db, "illness", 3, "2026-11-11", state="resolved")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "ok"


def test_a_signal_does_not_nag_straight_after_a_check_in(db):
    checkin(db, "2026-11-12", "2026-11-12T09:00:00")
    told(db, "illness", 3, "2026-11-12")
    assert due.checkin_status(db, date(2026, 11, 13))["state"] == "ok"


def test_signals_add_to_the_other_reasons_rather_than_replace_them(db):
    checkin(db, "2026-11-09")
    told(db, "illness", 3, "2026-11-14")
    s = due.checkin_status(db, date(2026, 11, 16))
    assert len(s["reasons"]) == 2 and s["reasons"][0].startswith("A week has closed")


# -- block review -----------------------------------------------------------------------------

def cyc(start="2026-11-02", end="2026-11-22", closed=True, weeks=3, plan=None):
    return {"plan_name": plan or plan_name(), "start_date": start, "end_date": end,
            "closed": closed, "weeks": weeks, "phase": "sit"}


def test_no_blocks_means_nothing_to_review(db):
    s = due.review_status(db, date(2026, 11, 6), cycle_list=[])
    assert s["state"] == "ok" and s["cycle"] is None


def test_an_open_block_says_when_it_closes(db):
    s = due.review_status(db, date(2026, 11, 6), cycle_list=[cyc(closed=False)])
    assert s["state"] == "ok" and s["next_close"] == "2026-11-22"
    assert "2026-11-22" in s["reasons"][0]


def test_a_block_whose_recovery_week_is_already_seeded_is_still_running(db):
    """
    Mutant: trusting the `closed` flag. The rolling plan seeds only its horizon, so the block
    being ridden has its recovery week in view and reads as closed while it is still running.
    """
    s = due.review_status(db, date(2026, 11, 6), cycle_list=[cyc(closed=True)])
    assert s["state"] == "ok" and s["next_close"] == "2026-11-22" and s["cycle"] is None


def test_a_closed_unreviewed_block_is_due(db):
    s = due.review_status(db, date(2026, 11, 24), cycle_list=[cyc()])
    assert s["state"] == "due" and s["days_since_close"] == 2
    assert s["cycle"] == {"start": "2026-11-02", "end": "2026-11-22", "weeks": 3, "phase": "sit"}


def test_the_review_is_not_offered_on_the_day_the_block_ends(db):
    """Mutant: <= for <. The last week has not finished, so there is nothing to review yet."""
    assert due.review_status(db, date(2026, 11, 22), cycle_list=[cyc()])["state"] == "ok"
    assert due.review_status(db, date(2026, 11, 23), cycle_list=[cyc()])["state"] == "due"


def test_two_weeks_after_the_close_it_is_overdue(db):
    assert due.review_status(db, date(2026, 12, 5), cycle_list=[cyc()])["state"] == "due"
    assert due.review_status(db, date(2026, 12, 6), cycle_list=[cyc()])["state"] == "overdue"


def test_a_filed_review_clears_it(db):
    db.execute("INSERT INTO cycle_reviews (plan_name, cycle_start, cycle_end, metrics_json, "
               "created_at) VALUES (?, '2026-11-02', '2026-11-22', '{}', 'x')", (plan_name(),))
    assert due.review_status(db, date(2026, 11, 30), cycle_list=[cyc()])["state"] == "ok"


def test_only_the_latest_closed_block_is_offered(db):
    older = cyc("2026-10-05", "2026-11-01", weeks=4)
    s = due.review_status(db, date(2026, 11, 24), cycle_list=[older, cyc()])
    assert s["cycle"]["start"] == "2026-11-02"
    db.execute("INSERT INTO cycle_reviews (plan_name, cycle_start, cycle_end, metrics_json, "
               "created_at) VALUES (?, '2026-11-02', '2026-11-22', '{}', 'x')", (plan_name(),))
    assert due.review_status(db, date(2026, 11, 24), cycle_list=[older, cyc()])["state"] == "ok"


def test_a_superseded_plans_blocks_are_not_offered(db):
    other = cyc("2026-09-14", "2026-10-11", plan="TrainerRoad 2026-27")
    assert due.review_status(db, date(2026, 11, 24), cycle_list=[other])["state"] == "ok"


# -- together ---------------------------------------------------------------------------------

def test_attention_counts_what_needs_doing(db):
    s = due.status(db, date(2026, 11, 24), cycle_list=[cyc()])
    assert s["checkin"]["state"] == "overdue" and s["review"]["state"] == "due"
    assert s["attention"] == 2
    checkin(db, "2026-11-23")
    assert due.status(db, date(2026, 11, 24), cycle_list=[cyc()])["attention"] == 1


def test_a_decision_stamped_at_the_same_instant_as_the_check_in_is_not_counted(db):
    """Mutant: >= for >. The check-in itself runs a sync that records the decision, in the same
    second; counting it would prompt straight after the check-in that already covered it."""
    checkin(db, "2026-11-09", "2026-11-09T09:00:00")
    decision(db, "2026-11-02", "advance", "2026-11-09T09:00:00")
    assert due.checkin_status(db, date(2026, 11, 12))["state"] == "ok"


def test_with_no_check_in_ever_the_closed_week_count_is_the_overdue_threshold(db):
    assert due.checkin_status(db, date(2026, 11, 6))["weeks_closed"] == due.OVERDUE_WEEKS

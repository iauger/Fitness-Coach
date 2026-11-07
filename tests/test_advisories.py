"""
The plan-advisory seam: what the signals would do, shown and not applied.

The point of the seam is that it can be switched on later without a rewrite, *and* that until then it
changes nothing. So the first group pins the second half over the athlete's whole reference year:
with no advisory, an unapplied one, or one that asks for more, every decision is identical to the
gate's own. Mutants: an unapplied advisory honoured; `consider_harder` honoured when applied;
an applied hold that changes anything but an advance; an advisory born applied; a "harder" suggestion
alongside an easier one, or in a re-entry week.
"""

import json
import pathlib

import pytest

from src.planning import advisories as adv
from src.planning import progression as prog

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "history_2025-09_2026-11.json"


def thread(kind, severity=2, recurring=False, topic="", ids=(1,)):
    return {"kind": kind, "severity": severity, "recurring": recurring, "topic": topic, "ids": list(ids)}


def actions(watch, week_type="normal"):
    return [a["action"] for a in adv.plan_advisories(watch, {"week_type": week_type})]


# -- what each kind argues for ------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["illness", "injury_pain", "burnout"])
def test_a_health_signal_needing_action_argues_for_easing_the_week(kind):
    assert actions([thread(kind, 3)]) == ["ease"]


@pytest.mark.parametrize("kind", ["illness", "injury_pain", "burnout", "workouts_too_hard",
                                  "motivation_low", "missed_sessions"])
def test_strain_that_is_affecting_training_argues_for_holding_the_tier(kind):
    assert actions([thread(kind, 2)]) == ["hold"]


@pytest.mark.parametrize("kind", ["poor_sleep", "fatigue", "stress", "family_caregiving", "work_event"])
def test_load_outside_training_argues_for_recovery_when_serious_or_recurring(kind):
    assert actions([thread(kind, 2)]) == ["add_recovery"]
    assert actions([thread(kind, 1, recurring=True)]) == ["add_recovery"]
    assert actions([thread(kind, 1)]) == []


def test_room_to_do_more_needs_a_recurring_signal():
    assert actions([thread("workouts_too_easy", 1, recurring=True)]) == ["consider_harder"]
    assert actions([thread("workouts_too_easy", 1)]) == []
    assert actions([thread("feeling_strong", 0, recurring=True)]) == ["consider_harder"]


def test_things_with_no_bearing_on_load_argue_for_nothing():
    assert actions([thread(k, 3, recurring=True) for k in ("travel", "nutrition", "gear", "preference",
                                                          "wants_more", "goal_change")]) == []


def test_a_serious_signal_is_judged_by_its_strongest_action_only():
    assert actions([thread("illness", 3)]) == ["ease"]                    # not also "hold"


def test_nothing_asks_for_more_while_anything_asks_for_less():
    """Mutant: no suppression. The plan would be told to hold back and push harder at once."""
    watch = [thread("workouts_too_easy", 1, recurring=True), thread("poor_sleep", 2, ids=(2,))]
    assert actions(watch) == ["add_recovery"]


def test_nothing_asks_for_more_in_a_re_entry_week():
    assert actions([thread("workouts_too_easy", 1, recurring=True)], week_type="reentry") == []


def test_advisories_are_ordered_easiest_first_and_carry_every_mention_behind_them():
    watch = [thread("poor_sleep", 2, ids=(4, 2)), thread("illness", 3, ids=(9,)),
             thread("stress", 2, ids=(2, 7))]
    got = adv.plan_advisories(watch, {"week_type": "normal"})
    assert [a["action"] for a in got] == ["ease", "add_recovery"]
    assert got[1]["signal_ids"] == [2, 4, 7]


def test_a_reason_names_what_it_is_about():
    (a,) = adv.plan_advisories([thread("injury_pain", 3, topic="left knee")], {})
    assert "injury pain (left knee)" in a["reason"] and "ease the week" in a["reason"]


def test_every_advisory_is_born_not_applied_and_says_whether_it_is_easier():
    watch = [thread("illness", 3), thread("workouts_too_easy", 1, recurring=True, ids=(5,)),
             thread("poor_sleep", 2, ids=(6,))]
    for a in adv.plan_advisories(watch, {}):
        assert a["applied"] is False and a["easier"] == (a["action"] != "consider_harder")
    assert adv.EASIER == {"ease", "hold", "add_recovery"}


def test_an_empty_watch_list_gives_no_advisories():
    assert adv.plan_advisories([], {}) == []


# -- the seam in the gate ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def weeks():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["weeks"]


def decisions(weeks, advisories):
    """The gate over the whole reference year, tier carried forward as the real roller does."""
    tier, history, out = 1, [], []
    for week in weeks:
        history.append(prog.classify_week(week, tier))
        d = prog.next_state(history, tier, advisories)
        out.append(d)
        tier = d.tier
    return out


def advisory(action, applied):
    return {"action": action, "applied": applied, "reason": "r", "signal_ids": [1], "easier": True, "label": ""}


def test_with_nothing_applied_every_decision_over_the_year_is_the_gates_own(weeks):
    """The test the seam exists for. Mutant: an unapplied advisory that changes a decision."""
    baseline = decisions(weeks, None)
    assert any(d.verdict == "advance" for d in baseline)          # the year really does exercise it
    every_action_unapplied = [advisory(a, False) for a in adv.ACTIONS]
    assert decisions(weeks, []) == baseline
    assert decisions(weeks, every_action_unapplied) == baseline
    assert [prog.next_state([], 1)] == [prog.next_state([], 1, None)]


def test_the_replay_still_matches_the_gates_own_decisions(weeks):
    assert [r["next_tier"] for r in prog.replay(weeks)] == [d.tier for d in decisions(weeks, None)]


def test_an_applied_hold_changes_an_advance_into_a_hold_and_nothing_else(weeks):
    base = decisions(weeks, None)
    held = decisions(weeks, [advisory("hold", True)])
    # tiers diverge after the first held advance, so compare the first difference only
    first = next(i for i, d in enumerate(base) if d.verdict == "advance")
    assert held[:first] == base[:first]
    assert held[first].verdict == "hold" and held[first].tier == base[first].tier - 1
    assert "held by what you told the coach" in held[first].reason


def test_an_applied_hold_leaves_a_decision_that_was_not_an_advance_alone():
    history = [{"outcome": "missed", "week_start": "2026-01-05"}]
    assert prog.next_state(history, 2, [advisory("hold", True)]) == prog.next_state(history, 2)


@pytest.mark.parametrize("action", ["consider_harder", "ease", "add_recovery"])
def test_only_a_hold_is_honoured_today_and_an_action_that_raises_load_never_is(weeks, action):
    """Mutant: any applied advisory taken at its word."""
    assert decisions(weeks, [advisory(action, True)]) == decisions(weeks, None)

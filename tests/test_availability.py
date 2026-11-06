"""
Which days the week is built on.

`plan.available_days` and `plan.long_ride_day` let someone say which weekdays they can train,
where before the long ride was always Saturday and the hard session always Tuesday. The
generator that produced that week was tested only against an athlete whose life happens to
match it — their real rides fall on Tue, Thu and Sat — so a Monday/Wednesday/Friday rider
would have been handed a Saturday long ride and nothing in the suite would have noticed.

Three properties carry this, and each is checked at a different level:

1. **Nothing changes for anyone who sets nothing.** Held to a verbatim copy of the previous
   implementation across every combination, not to a handful of remembered cases.
2. **Nothing is ever placed on a day that is not available**, checked exhaustively over every
   subset of the week rather than over the examples that came to mind.
3. **The roller honours it end to end**, because a generator that is right and a roller that
   calls it without the settings is exactly the reader/writer split this project keeps
   finding.
"""

import re
import sqlite3
from datetime import date, timedelta
from itertools import combinations
from pathlib import Path

import pytest

from conftest import seed_ladder

import src.config as C
from src.db.migrations import migrate
from src.planning.blocks import _apart, build_pattern, plan_rotation, rotation_for
from src.planning.roller import store_horizon

SAT, SUN = 5, 6


# ── the implementation this replaces, verbatim ───────────────────────────────

def legacy_strength_days(rides, count):
    from itertools import combinations as combos

    if count <= 0:
        return []
    candidates = [d for d in range(7) if d not in (5, 1)]
    if count > len(candidates):
        raise ValueError("cannot place")

    def gap(days):
        if len(days) < 2:
            return 7
        ordered = sorted(days)
        wrap = (ordered[0] + 7) - ordered[-1]
        return min(min(b - a for a, b in zip(ordered, ordered[1:])), wrap)

    def score(days):
        rest_days = sum(1 for d in days if d not in rides)
        follows_ride = sum(1 for d in days if (d - 1) % 7 in rides)
        return rest_days, gap(days), follows_ride

    return sorted(max(combos(candidates, count), key=score))


def legacy_build_pattern(ride_days, strength_days=2, hard=True):
    rides = [5]
    if ride_days > 1:
        rides.append(1)
    for day in (3, 6, 2, 0, 4):
        if len(rides) >= ride_days:
            break
        rides.append(day)
    rides = sorted(rides[:ride_days])
    pattern = []
    for day in rides:
        if day == 5:
            pattern.append((day, "long"))
        elif day == 1 and hard and ride_days > 1:
            pattern.append((day, "hard"))
        else:
            pattern.append((day, "lit"))
    pattern += [(day, "strength") for day in legacy_strength_days(rides, strength_days)]
    return sorted(pattern)


@pytest.mark.parametrize("ride_days", range(1, 8))
@pytest.mark.parametrize("lifts", range(0, 6))
@pytest.mark.parametrize("hard", [True, False])
def test_setting_nothing_builds_exactly_the_week_it_always_did(ride_days, lifts, hard):
    """
    This template decides every athlete's week, so "I think it is the same" is not enough.
    Mutants this catches: the hard-session tie-break flipping to the later day (Wed for Tue),
    the spacing rule changing which day the first easy ride takes, and the preference order
    being reordered.
    """
    assert build_pattern(ride_days, lifts, hard) == legacy_build_pattern(ride_days, lifts, hard)


@pytest.mark.parametrize("ride_days", range(1, 8))
def test_all_seven_days_listed_is_the_same_as_listing_none(ride_days):
    """`None` and `(0..6)` mean the same week; a config that lists every day must not differ."""
    assert (build_pattern(ride_days, 2, True, days=tuple(range(7)))
            == build_pattern(ride_days, 2, True))


# ── placement, over every subset of the week ─────────────────────────────────

POOLS = [c for k in range(2, 8) for c in combinations(range(7), k)]


def test_nothing_is_ever_placed_outside_the_available_days():
    """
    Every subset of two to seven days, every ride count it can hold, zero to two lifts, with
    and without a hard session — and each result checked against the rules rather than against
    a remembered answer. A swallowed ValueError would hide a generator that refuses cases it
    should handle, so refusals are asserted to be exactly the impossible ones.

    Mutants this catches: lift candidates drawn from all seven days instead of the pool;
    the hard day chosen from outside it; the closest-to-the-long-ride day preferred over the
    furthest.
    """
    checked = 0
    for pool in POOLS:
        for rides in range(1, len(pool) + 1):
            for lifts in range(0, 3):
                for hard in (True, False):
                    where = f"pool={pool} rides={rides} lifts={lifts} hard={hard}"
                    if lifts > len(pool) - 2:
                        with pytest.raises(ValueError):
                            build_pattern(rides, lifts, hard, days=pool)
                        continue
                    pattern = build_pattern(rides, lifts, hard, days=pool)
                    checked += 1

                    assert all(d in pool for d, _ in pattern), where
                    ride_slots = [(d, r) for d, r in pattern if r != "strength"]
                    lift_days = [d for d, r in pattern if r == "strength"]
                    assert len(ride_slots) == rides, where
                    assert len({d for d, _ in ride_slots}) == rides, where
                    assert len(lift_days) == lifts == len(set(lift_days)), where

                    long_days = [d for d, r in ride_slots if r == "long"]
                    hard_days = [d for d, r in ride_slots if r == "hard"]
                    assert len(long_days) == 1, where
                    assert len(hard_days) == (1 if hard and rides > 1 else 0), where
                    assert not set(lift_days) & set(long_days + hard_days), where

                    # Not next to the long ride when any day that is not next to it was there.
                    if hard_days:
                        farther = [d for d in pool if d != long_days[0]
                                   and _apart(d, long_days[0]) >= 2]
                        if farther:
                            assert _apart(hard_days[0], long_days[0]) >= 2, where
    assert checked > 1500, f"only {checked} cases ran; the loop is not exercising the space"


def test_the_lift_days_are_the_same_whether_or_not_the_week_has_a_hard_session():
    """
    An easy block builds the same lift days as a hard block: the hard slot's day is held back
    from lifts either way, which is what the week has always done.
    """
    for pool in POOLS:
        if len(pool) < 3:
            continue
        for rides in (2, min(3, len(pool))):
            with_hard = build_pattern(rides, 1, True, days=pool)
            without = build_pattern(rides, 1, False, days=pool)
            assert ([d for d, r in with_hard if r == "strength"]
                    == [d for d, r in without if r == "strength"])


# ── where the long ride and the hard session go ──────────────────────────────

def test_the_long_ride_is_saturday_when_saturday_is_available():
    assert (SAT, "long") in build_pattern(3, 2, days=(0, 1, 3, 5, 6))


@pytest.mark.parametrize("pool, expected", [
    ((0, 2, 4, 6), SUN),          # no Saturday: the weekend's other day
    ((0, 2, 3), 3),               # no weekend: working back from it, Thursday before Monday
    ((0, 1, 2), 2),               # Wednesday before Tuesday before Monday
    ((1, 3, 4), 4),               # Friday, ahead of the weekdays further from the weekend
])
def test_without_saturday_the_long_ride_goes_to_the_nearest_day_to_the_weekend(pool, expected):
    pattern = build_pattern(3, 0, days=pool)
    assert [d for d, r in pattern if r == "long"] == [expected]


def test_the_long_ride_can_be_pinned():
    """Mutant this catches: ignoring `long_day`."""
    pattern = build_pattern(3, 0, days=(0, 2, 4, 5, 6), long_day=2)
    assert [d for d, r in pattern if r == "long"] == [2]


def test_a_pinned_long_ride_outside_the_available_days_is_refused():
    with pytest.raises(ValueError, match="not one of the available days"):
        build_pattern(3, 0, days=(0, 2, 4), long_day=5)


def test_the_hard_session_sits_early_after_the_long_ride_and_far_from_the_next():
    """
    Saturday long ride gives Tuesday, which is where this template has always put it; moving
    the long ride moves the hard session with it. Mutants: preferring the day closest to the
    long ride, and breaking the tie toward the later day.
    """
    def hard_day(long_day):
        return [d for d, r in build_pattern(3, 0, long_day=long_day) if r == "hard"][0]

    assert hard_day(SAT) == 1       # Tuesday
    assert hard_day(SUN) == 2       # Wednesday
    assert hard_day(0) == 3         # Monday long ride: Thursday


def test_easy_rides_are_spread_out_from_the_rides_already_chosen():
    """
    Long ride Monday, so the hard session lands Thursday, and the third ride is the day
    furthest from both — Saturday — rather than the first on the preference list, which is
    Sunday and sits beside the long ride. Mutant this catches: ranking by the preference list
    alone, which every default-week test passes because there the two orders agree.
    """
    pattern = build_pattern(3, 0, days=tuple(range(7)), long_day=0)
    assert [d for d, _ in pattern] == [0, 3, 5]


# ── refusing rather than squeezing ───────────────────────────────────────────

def test_a_week_wider_than_the_available_days_is_refused_and_says_which():
    """Mutant this catches: removing the size check, which leaves a bare `min() of empty`."""
    with pytest.raises(ValueError, match=r"only 3 are available \(Tue, Thu, Sat\)"):
        build_pattern(4, 0, days=(1, 3, 5))


def test_more_lifts_than_the_available_days_can_hold_is_refused():
    with pytest.raises(ValueError, match="only 1 other available day"):
        build_pattern(2, 2, days=(1, 3, 5))


def test_a_repeated_or_impossible_weekday_is_refused():
    with pytest.raises(ValueError, match="repeat"):
        build_pattern(2, 0, days=(1, 1, 3))
    with pytest.raises(ValueError, match="0-6"):
        build_pattern(2, 0, days=(1, 7))


# ── the configuration ────────────────────────────────────────────────────────

BASE = "athlete:\n  name: X\nplan:\n  name: P\n  start_monday: 2026-11-02\n"


def plan_block(extra: str) -> str:
    return BASE + "".join(f"  {line}\n" for line in extra.splitlines())


def test_day_names_are_accepted_in_either_form_and_any_case():
    cfg = C.parse(plan_block("available_days: [Mon, tuesday, WED, thu]\nstrength_days: 2"))
    assert cfg.plan.available_days == ("Mon", "tuesday", "WED", "thu")


@pytest.mark.parametrize("extra, message", [
    ("available_days: [tue, funday]", r"available_days\[1\].*'funday' is not a weekday"),
    ("available_days: [tue, thu, tue, sat]", "listed twice"),
    ("available_days: [sat]", "fewer than the lowest rung"),
    ("available_days: [tue, thu, sat]", "room for 1 lift"),
    ("available_days: [tue, thu, sat, sun]\nlong_ride_day: mon", "not one of"),
    ("long_ride_day: someday", r"long_ride_day.*not a weekday"),
])
def test_a_bad_day_setting_is_refused_at_load_naming_the_setting(extra, message):
    """
    Mutants this catches: dropping the lift-room check (a three-day week quietly failing at
    its first roll instead of here), and dropping the long-ride membership check.
    """
    with pytest.raises(C.ConfigError, match=message):
        C.parse(plan_block(extra))


def test_three_days_is_enough_when_the_lifts_fit():
    C.parse(plan_block("available_days: [tue, thu, sat]\nstrength_days: 1"))


def test_a_pinned_long_ride_needs_no_available_days():
    cfg = C.parse(plan_block("long_ride_day: sun"))
    assert cfg.plan.available_days is None and cfg.plan.long_ride_day == "sun"


def test_a_misspelt_key_is_named_rather_than_ignored():
    """The strict loader's whole job: `available_day` must not become a setting nobody reads."""
    with pytest.raises(C.ConfigError, match="available_day"):
        C.parse(plan_block("available_day: [tue, thu]"))


@pytest.fixture
def use_config(tmp_path, monkeypatch):
    def apply(extra: str = ""):
        path = tmp_path / "cfg.yaml"
        path.write_text(plan_block(extra), encoding="utf-8")
        monkeypatch.setattr(C, "CONFIG_PATH", path)
        C.reset()
    yield apply
    C.reset()


def test_the_constants_read_the_configured_days(use_config):
    from src.planning.constants import available_days, long_ride_day

    use_config("available_days: [Sun, tue, THU, sat]\nlong_ride_day: sun")
    assert available_days() == (1, 3, 5, 6)
    assert long_ride_day() == SUN


def test_the_constants_say_nothing_when_nothing_is_configured(use_config):
    from src.planning.constants import available_days, long_ride_day

    use_config()
    assert available_days() is None and long_ride_day() is None


# ── the ride-day ceiling takes both limits into account ──────────────────────

@pytest.mark.parametrize("extra, ceiling, setting", [
    ("max_ride_days: 7\navailable_days: [tue, thu, sat, sun]", 4, "plan.available_days"),
    ("max_ride_days: 3\navailable_days: [tue, thu, sat, sun]", 3, "plan.max_ride_days"),
    ("max_ride_days: 4\navailable_days: [tue, thu, sat, sun]", 4, "plan.max_ride_days"),
    ("max_ride_days: 5", 5, "plan.max_ride_days"),
    ("", 7, "plan.max_ride_days"),
])
def test_the_ride_day_ceiling_is_the_lower_of_the_two_limits(use_config, extra, ceiling,
                                                            setting):
    """
    Mutant this catches: `max_ride_days()` returning the config field alone, which lets a
    ladder be derived for five days in a week with four available — a week that cannot be
    placed. The setting name matters as much as the number: it is what a message tells the
    athlete to edit.
    """
    from src.planning.constants import max_ride_days, ride_day_setting

    use_config(extra)
    assert max_ride_days() == ceiling
    assert ride_day_setting() == setting


# ── the roller honours it end to end ─────────────────────────────────────────

PLAN = "test rolling"
TODAY = date(2026, 11, 3)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    yield conn
    conn.close()


def stored_weekdays(conn, since: str = "0000-00-00") -> set[int]:
    return {r["weekday"] for r in conn.execute(
        "SELECT weekday FROM prescribed_sessions WHERE plan_name = ? AND date >= ?",
        (PLAN, since))}


def test_without_settings_the_stored_plan_uses_the_usual_days(db, use_config):
    use_config()
    store_horizon(db, TODAY, PLAN)
    # Not asserting Thursday: the tier this plan starts at is a two-ride week, and a lower tier
    # dropping an easy ride is `pattern_for` working, not this feature failing.
    assert {r["weekday"] for r in db.execute(
        "SELECT weekday FROM prescribed_sessions WHERE role = 'long'")} == {SAT}
    assert 1 in stored_weekdays(db), "no Tuesday session in the default week"


def test_a_stored_plan_uses_only_the_available_days(db, use_config):
    """
    The roller and the preview each built the week from their own arguments before
    `plan_rotation`, so a generator that honoured availability and a roller that did not was
    reachable. Mutant this catches: the roller calling `rotation_for` directly.
    """
    use_config("available_days: [mon, wed, fri, sun]")
    store_horizon(db, TODAY, PLAN)
    days = stored_weekdays(db)
    assert days and days <= {0, 2, 4, 6}, f"sessions landed on weekdays {sorted(days)}"
    assert {r["weekday"] for r in db.execute(
        "SELECT weekday FROM prescribed_sessions WHERE role = 'long'")} == {SUN}


def test_changing_the_days_moves_the_future_and_leaves_the_frozen_days_alone(db, use_config):
    """
    The regeneration case, and the freeze: sessions in the next two days stay where they were
    promised, and everything beyond them moves. Without the second assertion this would pass
    for a roller that changed nothing at all.
    """
    use_config()
    store_horizon(db, TODAY, PLAN)
    frozen_before = {(r["date"], r["role"]) for r in db.execute(
        "SELECT date, role FROM prescribed_sessions WHERE date < ?",
        ((TODAY + timedelta(days=2)).isoformat(),))}
    assert frozen_before

    use_config("available_days: [mon, wed, fri, sun]")
    store_horizon(db, TODAY, PLAN)

    cutoff = (TODAY + timedelta(days=2)).isoformat()
    future = stored_weekdays(db, since=cutoff)
    assert future <= {0, 2, 4, 6}, f"stale sessions left on weekdays {sorted(future)}"
    assert future & {0, 2, 4, 6}, "the future was not regenerated at all"
    frozen_after = {(r["date"], r["role"]) for r in db.execute(
        "SELECT date, role FROM prescribed_sessions WHERE date < ?", (cutoff,))}
    assert frozen_after == frozen_before


def test_a_ladder_wider_than_the_days_left_is_refused_with_a_message(db, use_config):
    """
    The hourly roll cannot place a three-ride week in two days, and the useful outcome is a
    sentence naming the days rather than a stack trace from `min()`.
    """
    use_config("available_days: [sat, sun]\nstrength_days: 0")
    with pytest.raises(ValueError, match=r"3 ride days but only 2 are available \(Sat, Sun\)"):
        store_horizon(db, TODAY, PLAN)


# ── nothing builds the week except plan_rotation ─────────────────────────────

def test_the_week_is_built_in_one_place():
    """
    `rotation_for` takes its settings as arguments; `plan_rotation` is the one function that
    reads them from configuration. A caller using the former builds a week that ignores the
    athlete's days, and does so silently — the calendar and the preview would agree with each
    other and disagree with the settings, which is the hardest version of this to notice.
    """
    root = Path(__file__).parent.parent
    offenders = []
    for folder in ("src", "scripts"):
        for path in (root / folder).rglob("*.py"):
            if path.name == "blocks.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\brotation_for\(", line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, "call plan_rotation instead:\n  " + "\n  ".join(offenders)


def test_plan_rotation_reads_the_configuration(use_config):
    use_config("available_days: [mon, wed, fri, sun]\nlong_ride_day: fri")
    for block in plan_rotation(3):
        assert [d for d, r in block.pattern if r == "long"] == [4]
        assert {d for d, _ in block.pattern} <= {0, 2, 4, 6}


def test_rotation_for_alone_ignores_the_configuration(use_config):
    """The other half of the contract: it takes arguments, so a test of it is not at the
    mercy of whichever config file is loaded."""
    use_config("available_days: [mon, wed, fri, sun]")
    assert [b.pattern for b in rotation_for(3)] == [b.pattern for b in rotation_for(3, 2)]
    assert 5 in {d for d, _ in rotation_for(3)[0].pattern}


# ── the coach is told, so it does not suggest a day that does not exist ──────

def test_the_coach_profile_carries_the_available_days_only_when_configured(use_config):
    """
    Absent must stay absent: "can train any day" is a claim the athlete never made. Mutant this
    catches: reporting all seven days when nothing is set.
    """
    from src.analysis.report import athlete_profile

    use_config()
    unset = athlete_profile()
    assert unset["available_days"] is None and unset["long_ride_day"] is None

    use_config("available_days: [Tue, thu, SAT, sun]\nlong_ride_day: sun")
    who = athlete_profile()
    assert who["available_days"] == ["Tue", "Thu", "Sat", "Sun"]
    assert who["long_ride_day"] == "Sun"


def test_the_coaching_text_says_which_days_the_athlete_can_train(use_config, monkeypatch):
    import src.analysis.report as R

    monkeypatch.setattr(R, "load_correction_text", lambda *a, **k: "")
    monkeypatch.setattr(R, "derived_text", lambda *a, **k: "")

    def render(athlete: dict) -> str:
        return R.coaching_context_text({
            "generated": "2026-11-05",
            "athlete": {"name": "A", "goals": [], "stats": {}, **athlete},
            "timeline": {"goals": []},
            "fitness": {"current": {}, "trend": {}, "peak_ever": {}, "pct_of_peak": None,
                        "weekly_history": []},
            "recovery": {"hrv": {}, "sleep": {}, "resting_hr": {}, "flags": []},
            "training": {"recent_activities": [], "sport_distribution_90d": {},
                         "weekly_load_12w": [], "annual_volume": [], "plan_adherence_8w": {},
                         "recent_planned_workouts": [], "plan_position": None,
                         "earlier_week_summaries": [], "rolling_plan": None},
            "memory": {},
        })

    text = render({"available_days": ["Tue", "Thu", "Sat"], "long_ride_day": "Sat"})
    assert "Can train on: Tue, Thu, Sat" in text
    assert "Long ride day: Sat" in text
    assert "Can train on" not in render({}), "an unset day list must not appear at all"

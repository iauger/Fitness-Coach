"""
How long a ride may be, and a plan that is built inside it.

`plan.max_ride_min` and `plan.day_max_ride_min` say the longest a single ride can be. The
design question is what a plan does when it meets that limit, and the answer this file defends
is: **derive inside it, never clamp to it afterwards.** A rung is graded against 90% of its
minutes, so a ride quietly shortened to fit leaves a rung the gate expects and the calendar
cannot deliver — the athlete meets everything the plan asked and holds at the same tier for
ever, with nothing saying why.

So the ladder climbs to what the limits allow and stops, the climb takes up the easy rides once
the long ride is at its limit (one thing changing per rung), and when there is no room at all
the derivation refuses, naming the setting and the amount.
"""

import sqlite3
from datetime import date

import pytest

from conftest import seed_ladder

import src.config as C
from src.db.migrations import migrate
from src.planning import blocks as B
from src.planning.blocks import (
    build_pattern, check_fit, expand_week, fitting_variants, hard_variant_minutes,
    rotation_for, unfittable_blocks,
)
from src.planning.bootstrap import BootstrapError, RideLimits, derive
from src.planning.progression import TIERS
from src.planning.roller import store_horizon

BAND = (3.5, 4.0)
CEILING = 120
SAT = 5


def weeks(n=12, ride_min=195.0, ride_days=3, longest=75.0):
    return [{"week_start": f"2026-{1 + i // 5:02d}-{1 + (i % 5) * 6:02d}", "ride_min": ride_min,
             "ride_days": ride_days, "longest_ride_min": longest, "strength_sessions": 2,
             "training_days": ride_days, "any_activity": True} for i in range(n)]


def limited(limit, **kw):
    return derive(weeks(**kw), BAND, CEILING, 3, limits_for=lambda r: RideLimits(limit, limit, limit))


# ── the configuration ────────────────────────────────────────────────────────

BASE = "athlete:\n  name: X\nplan:\n  name: P\n  start_monday: 2026-11-02\n"


def plan_block(extra: str) -> str:
    return BASE + "".join(f"  {line}\n" for line in extra.splitlines())


@pytest.fixture
def use_config(tmp_path, monkeypatch):
    def apply(extra: str = ""):
        path = tmp_path / "cfg.yaml"
        path.write_text(plan_block(extra), encoding="utf-8")
        monkeypatch.setattr(C, "CONFIG_PATH", path)
        C.reset()
    yield apply
    C.reset()


def test_the_limits_are_accepted_and_read_back(use_config):
    from src.planning.constants import ride_limits

    use_config("max_ride_min: 90\nday_max_ride_min: {sat: 150, Tuesday: 60}")
    limits = ride_limits()
    assert limits[SAT] == 150 and limits[1] == 60
    assert limits[0] == limits[2] == limits[4] == 90, "the plan-wide limit fills the other days"


def test_no_limits_is_an_empty_mapping_not_zero(use_config):
    """Absent means unlimited. A zero would mean the opposite, which is the mistake to avoid."""
    from src.planning.constants import ride_limits

    use_config()
    assert ride_limits() == {}


def test_a_day_limit_alone_leaves_the_other_days_unlimited(use_config):
    from src.planning.constants import ride_limits

    use_config("day_max_ride_min: {sat: 120}")
    assert ride_limits() == {SAT: 120}


@pytest.mark.parametrize("extra, message", [
    ("max_ride_min: 20", "shorter than any session"),
    ("day_max_ride_min: {sat: 20}", r"day_max_ride_min\.sat.*shorter than any session"),
    ("day_max_ride_min: {sat: 90.5}", r"expected a number of minutes"),
    ("day_max_ride_min: {sat: true}", r"expected a number of minutes"),
    ("day_max_ride_min: {funday: 90}", r"'funday' is not a weekday"),
    ("day_max_ride_min: {sat: 90, saturday: 100}", "same weekday is already given"),
    ("available_days: [tue, thu, sat, sun]\nday_max_ride_min: {mon: 90}", "not in plan.available_days"),
])
def test_a_bad_limit_is_refused_at_load_naming_the_setting(extra, message):
    """
    Mutants this catches: dropping the 30-minute floor (a day that can hold nothing while still
    listed as available), accepting a day that is not available (a limit that does nothing),
    and letting `sat` and `saturday` both through, where the second silently wins.
    """
    with pytest.raises(C.ConfigError, match=message):
        C.parse(plan_block(extra))


@pytest.mark.parametrize("extra, day, expected", [
    ("max_ride_min: 90", SAT, "plan.max_ride_min"),
    ("max_ride_min: 90\nday_max_ride_min: {sat: 150}", SAT, "plan.day_max_ride_min.sat"),
    ("max_ride_min: 90\nday_max_ride_min: {sat: 150}", 1, "plan.max_ride_min"),
])
def test_the_setting_named_in_a_message_is_the_one_that_governs_that_day(use_config, extra,
                                                                         day, expected):
    """Telling someone to raise `plan.max_ride_min` when their Saturday has its own entry sends
    them to edit a line Saturday ignores."""
    from src.planning.constants import limit_setting

    use_config(extra)
    assert limit_setting(day) == expected


# ── placement ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ride_days", range(2, 8))
def test_an_even_limit_changes_nothing_about_which_days_are_used(ride_days):
    """
    A plan-wide limit says nothing about *which* day, so every day is equally roomy and the
    week has to be the one it always was. Mutant this catches: limits leaking into the
    tie-breaks and moving the long ride or the hard session off Saturday and Tuesday.
    """
    even = {d: 90 for d in range(7)}
    assert (build_pattern(ride_days, 2, True, limits=even)
            == build_pattern(ride_days, 2, True))


def test_the_long_ride_goes_to_the_roomiest_day():
    """Saturday is the default only among equals. Mutant: preferring Saturday regardless."""
    pattern = build_pattern(3, 0, limits={SAT: 60, 6: 120})
    assert [d for d, r in pattern if r == "long"] == [6]


def test_a_limit_on_one_day_does_not_push_the_long_ride_off_it():
    """
    `{sat: 90}` says Saturday's ride is at most 90 minutes. It does not say the other six days
    have none, and reading it that way moved the long ride to Friday. Mutant this catches:
    ranking an unlisted day as unlimited, which the first version of this did.
    """
    pattern = build_pattern(3, 0, limits={SAT: 90})
    assert [d for d, r in pattern if r == "long"] == [SAT]


def test_with_every_day_listed_the_roomiest_wins():
    limits = {d: 60 for d in range(7)} | {2: 150}
    pattern = build_pattern(3, 0, limits=limits)
    assert [d for d, r in pattern if r == "long"] == [2]


def test_a_pinned_long_ride_beats_room():
    pattern = build_pattern(3, 0, long_day=SAT, limits={SAT: 60, 6: 120})
    assert [d for d, r in pattern if r == "long"] == [SAT]


def test_the_hard_session_avoids_a_day_too_short_to_hold_one():
    """
    Tuesday is where it has always gone, and a 35-minute Tuesday cannot hold any of the hard
    sessions — the shortest is 40. Wednesday is as far from the long ride.

    Listing only Tuesday is deliberate: every unlisted day then counts as exactly as roomy as
    Tuesday, so the room tie-break has nothing to say and *only the fit filter* can move the
    session. An earlier version of this test gave every other day 90 minutes, and either
    mechanism alone passed it — so deleting the filter went unnoticed.
    """
    pattern = build_pattern(3, 0, limits={1: 35})
    assert [d for d, r in pattern if r == "hard"] == [2]


def test_the_hard_session_prefers_the_roomier_of_two_equally_placed_days():
    """
    Tuesday and Wednesday are equally far from a Saturday long ride, and Tuesday could hold a
    hard session — 50 minutes fits the sprint sessions — but Wednesday has more room. The
    partner of the test above: here the fit filter has nothing to say, so only the room
    tie-break decides. Mutant this catches: dropping it, which falls back to Tuesday.
    """
    limits = {d: 90 for d in range(7)} | {1: 50}
    pattern = build_pattern(3, 0, limits=limits)
    assert [d for d, r in pattern if r == "hard"] == [2]


def test_if_no_day_can_hold_a_hard_session_one_is_still_placed():
    """Placement never fails over this; the session is turned into an easy ride at expansion."""
    pattern = build_pattern(3, 0, limits={d: 35 for d in range(7)})
    assert [d for d, r in pattern if r == "hard"]


# ── fitting a hard session to a day ──────────────────────────────────────────

def block_of(kind: str, limits: dict[int, int]):
    return next(b for b in rotation_for(3, 2, limits=limits) if b.block_type == kind)


def test_the_hard_session_lengths_are_measured_not_assumed():
    """
    The ladder's nominal hard session is 55-60 minutes; the real ones run 40-78. Deciding fit
    from the nominal would accept a 60-minute day for a 78-minute session, so fit is decided
    from the sessions themselves. Pinned to the figures that were measured.
    """
    minutes = hard_variant_minutes()
    assert min(minutes.values()) == 40
    assert round(max(minutes.values())) == 78
    assert round(minutes[("threshold", "sweet_spot")]) == 78


def test_only_the_variants_that_fit_are_offered():
    threshold = block_of("threshold", {})
    assert fitting_variants(threshold, None) == threshold.hard_variants
    assert set(fitting_variants(threshold, 62)) == {"continuous", "long_reps"}
    assert fitting_variants(threshold, 59) == []


def test_a_short_day_rotates_through_the_variants_that_fit_in_order():
    """Not skipping weeks: the same rotation, over fewer sessions."""
    threshold = block_of("threshold", {})
    fits = fitting_variants(threshold, 64)
    seen = [B._hard_session(threshold, week, 64).variant for week in range(1, 7)]
    assert seen == [fits[(week - 1) % len(fits)] for week in range(1, 7)]


def hard_slot(kind: str, limit: int | None):
    limits = {} if limit is None else {d: 200 for d in range(7)}
    block = block_of(kind, limits)
    hard_day = next(d for d, r in block.pattern if r == "hard")
    if limit is not None:
        block.ride_limits[hard_day] = limit
    week = expand_week(block, 1, 1, date(2026, 11, 2), "p", 1, tiers=TIERS)
    return [s for s in week if s["weekday"] == hard_day and s["role"] != "strength"]


def test_a_hard_slot_that_nothing_fits_becomes_an_easy_ride():
    """
    The same thing `pattern_for` does when a tier disallows the hard session. Mutant this
    catches: `expand_week` prescribing the session anyway, which hands someone a 60-minute
    threshold workout on a 50-minute day.
    """
    [session] = hard_slot("threshold", 50)
    assert session["role"] == "lit"
    assert session["archetype"] == "lit"
    assert session["minutes"] == TIERS[1].lit_min


def test_a_hard_slot_that_something_fits_stays_hard():
    [session] = hard_slot("sit", 50)
    assert session["role"] == "hard" and session["minutes"] <= 50


def test_unlimited_slots_are_exactly_as_before():
    [session] = hard_slot("threshold", None)
    assert session["role"] == "hard"


def test_the_blocks_that_lose_their_hard_session_are_reported():
    limits = {d: 90 for d in range(7)}
    rotation = rotation_for(3, 2, limits=limits)
    for block in rotation:
        hard_day = next((d for d, r in block.pattern if r == "hard"), None)
        if hard_day is not None:
            block.ride_limits[hard_day] = 50
    lost = {item["block"]: item for item in unfittable_blocks(rotation)}
    assert set(lost) == {"Threshold", "VO2"}, "sprints fit 50 minutes; these do not"
    assert lost["Threshold"]["needs"] == 60 and lost["VO2"]["needs"] == 52
    assert lost["Threshold"]["limit"] == 50


def test_nothing_is_reported_without_limits():
    assert unfittable_blocks(rotation_for(3)) == []


# ── refusing a ladder the limits cannot deliver ──────────────────────────────

def test_a_long_ride_beyond_its_days_limit_is_refused_naming_rung_day_and_setting(use_config):
    use_config("max_ride_min: 90")
    with pytest.raises(ValueError, match=r"T3's long ride is 105min but Sat's limit is 90min "
                                          r"\(plan.max_ride_min\)"):
        check_fit(B.plan_rotation(3), TIERS)


def test_an_easy_ride_beyond_a_days_limit_is_refused(use_config):
    use_config("max_ride_min: 120\nday_max_ride_min: {thu: 60}")
    with pytest.raises(ValueError, match=r"T4's easy ride is 65min but Thu's limit is 60min "
                                          r"\(plan.day_max_ride_min.thu\)"):
        check_fit(B.plan_rotation(3), TIERS)


def test_a_ladder_that_fits_is_not_refused(use_config):
    use_config("max_ride_min: 120")
    check_fit(B.plan_rotation(3), TIERS)


def test_no_limits_never_refuses(use_config):
    use_config()
    check_fit(B.plan_rotation(3), TIERS)


def test_a_limit_exactly_equal_to_the_ride_is_allowed(use_config):
    """Mutant this catches: `>=` for `>` in the comparison, which refuses a ride at its limit."""
    use_config("max_ride_min: 120")
    check_fit(B.plan_rotation(3), {4: TIERS[4]})


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    yield conn
    conn.close()


def test_the_roller_refuses_rather_than_clamping(db, use_config):
    """
    The deadlock this whole design avoids: a stored ladder asking for 120 minutes on a day
    limited to 90, quietly shortened, leaves a rung nobody can meet. Mutant this catches:
    removing `check_fit` from the roller, after which the roll succeeds and prescribes the
    120-minute ride anyway.
    """
    use_config("max_ride_min: 90")
    with pytest.raises(ValueError, match="limit is 90min"):
        store_horizon(db, date(2026, 11, 3), "test rolling")
    assert db.execute("SELECT COUNT(*) FROM prescribed_sessions").fetchone()[0] == 0


def test_the_roller_is_content_when_the_limits_allow_the_ladder(db, use_config):
    use_config("max_ride_min: 120")
    store_horizon(db, date(2026, 11, 3), "test rolling")
    assert db.execute("SELECT MAX(minutes) FROM prescribed_sessions "
                      "WHERE role != 'strength'").fetchone()[0] <= 120


# ── deriving inside the limits ───────────────────────────────────────────────

def test_a_limit_above_the_ladder_changes_nothing():
    """
    Mutant this catches: limits altering the ladder when they do not bind — the whole feature
    is meant to be invisible to anyone whose limits are looser than their riding needs.
    """
    plain = derive(weeks(), BAND, CEILING, 3)
    assert limited(200).tiers == plain.tiers
    assert limited(120).tiers == plain.tiers


def test_a_90_minute_limit_climbs_to_the_band_in_real_steps():
    """
    The case this was built for: three rides, ninety minutes at most. Climbing the long ride
    alone gave five-minute steps and a top of 3.6h against 4.5h of capacity.
    """
    ladder = limited(90)
    assert [(t.long_min, t.lit_min) for t in ladder.tiers[1:]] == [
        (75, 65), (90, 65), (90, 80), (90, 90)]
    assert ladder.cap.target_hours == 4.0


@pytest.mark.parametrize("limit", [80, 90, 100, 110])
def test_every_rung_changes_exactly_one_thing(limit):
    """
    The ladder's rule: a rung changes one thing, or the rung cannot be attributed. Mutants this
    catches: letting the long ride and the easy ride both move in one gap, and a gap where
    nothing moves (a dead rung — the bug banker's rounding once produced).
    """
    ladder = limited(limit)
    for lower, upper in zip(ladder.tiers[1:], ladder.tiers[2:]):
        moved = [lower.long_min != upper.long_min, lower.lit_min != upper.lit_min]
        assert sum(moved) == 1, (
            f"limit {limit}: T{lower.index}->T{upper.index} changed "
            f"{'both' if all(moved) else 'nothing'}")
        assert upper.target_min > lower.target_min


@pytest.mark.parametrize("limit", [80, 90, 100, 110, 120])
def test_no_ride_in_the_ladder_exceeds_the_limit(limit):
    for tier in limited(limit).tiers:
        assert tier.long_min <= limit and tier.lit_min <= limit, tier


def test_the_easy_rides_stop_growing_when_the_top_rung_reaches_the_band():
    """
    A generous limit would otherwise let them keep going: at 100 the top reached 4.33h against
    a band of 3.5-4.0. The band is intent. Mutant this catches: removing the band cap.
    """
    assert limited(100).cap.target_hours <= BAND[1]


def test_the_easy_rides_only_grow_when_a_limit_was_declared():
    """
    Without limits the ladder is what it has always been, so the easy ride never moves. The
    configured long-ride ceiling alone does not unlock this axis. Mutant: growing it always.
    """
    plain = derive(weeks(longest=75), BAND, 90, 3)
    assert {t.lit_min for t in plain.tiers[1:]} == {65}


def test_two_ride_weeks_have_no_easy_ride_to_grow():
    """With a long ride and a hard session and nothing else, there is no easy ride above T0."""
    ladder = derive(weeks(ride_min=130, ride_days=2, longest=60), BAND, CEILING, 2,
                    limits_for=lambda r: RideLimits(90, 90, 90))
    assert len({t.lit_min for t in ladder.tiers[1:]}) == 1


def test_the_ladder_says_when_the_easy_rides_are_taking_over_the_climb():
    assert any("easy rides lengthen" in n for n in limited(90).notes)
    assert not any("easy rides lengthen" in n for n in derive(weeks(), BAND, CEILING, 3).notes)


# ── refusing when there is no room ───────────────────────────────────────────

def test_no_room_to_climb_is_refused_with_the_setting_and_the_amount():
    """
    A 75-minute limit for someone already riding 75-minute long rides and 65-minute easy rides.
    This used to fail as "T4 does not ask for more than T3", which names neither the limit nor
    the fix. Mutant this catches: dropping the explicit check, which falls back to that message.
    """
    with pytest.raises(BootstrapError) as raised:
        limited(75)
    message = str(raised.value)
    assert "no room to climb" in message
    assert "plan.max_ride_min" in message
    assert "at least 80min" in message
    assert "plan.max_ride_days" in message, "adding a day is the other way out and should be said"


def test_the_refusal_quotes_what_you_ride_not_the_value_it_was_clamped_to():
    """A 60-minute limit on someone riding 75-minute long rides: the message must not claim
    they ride 60."""
    with pytest.raises(BootstrapError) as raised:
        limited(60)
    assert "75min and 65min" in str(raised.value)


def test_a_long_ride_limit_below_the_shortest_long_ride_is_refused():
    with pytest.raises(BootstrapError, match="shorter than the 45min"):
        derive(weeks(), BAND, CEILING, 3, limits_for=lambda r: RideLimits(long_cap=40))


def test_a_band_that_is_already_reached_is_named_rather_than_blamed_on_the_limit():
    """
    Riding a 90-minute long ride and 85-minute easy rides already fills a 3.5h band. Telling
    them to raise their ride limit would be wrong: nothing about the limit is in the way.
    """
    with pytest.raises(BootstrapError, match="inside your band") as raised:
        derive(weeks(ride_min=255.0, longest=90.0), (3.0, 3.5), CEILING, 3,
               limits_for=lambda r: RideLimits(90, 90, 90))
    assert "plan.sustainable_band_hours" in str(raised.value)


def test_a_limit_below_the_habitual_easy_ride_says_where_the_ladder_starts():
    ladder = derive(weeks(ride_min=210.0, longest=75.0), BAND, CEILING, 3,
                    limits_for=lambda r: RideLimits(90, 60, 90))
    assert ladder.tiers[1].lit_min == 60
    assert any("usually ride" in n and "60min" in n for n in ladder.notes)


def test_the_note_about_the_band_names_the_limit_that_is_binding():
    """Mutant: the band note hardcoding `plan.long_ride_ceiling_min` when a limit is the cause."""
    ladder = derive(weeks(), (5.0, 6.0), CEILING, 3,
                    limits_for=lambda r: RideLimits(90, 90, 90, long_setting="plan.max_ride_min"))
    assert any("plan.max_ride_min" in n and "band" in n for n in ladder.notes), ladder.notes


# ── what each limit applies to ───────────────────────────────────────────────

def test_plan_limits_read_the_days_the_rides_actually_land_on(use_config):
    """
    The long ride's limit is its own day's, not the plan's tightest; the easy rides' is the
    tightest of the rest. Mutant: applying one number to everything, which would cap a
    Saturday long ride at a Tuesday's limit.
    """
    use_config("max_ride_min: 90\nday_max_ride_min: {sat: 150, thu: 60}")
    limits = B.plan_limits(3)
    assert limits.long_cap == 150
    assert limits.easy_cap == 60
    assert limits.easy_setting == "plan.day_max_ride_min.thu"
    assert limits.long_setting == "plan.day_max_ride_min.sat"


def test_plan_limits_are_empty_when_nothing_is_set(use_config):
    use_config()
    assert not B.plan_limits(3).declared


def test_the_hard_day_counts_toward_the_easy_limit(use_config):
    """
    In a recovery or re-entry week the hard session becomes an easy ride of the tier's full
    easy length, so that day has to be able to hold one.

    The setup forces the case: Tuesday and Wednesday are the only days far enough from the
    Saturday long ride to host the hard session, and both are limited to 50 while Thursday has
    90, so the hard day is *the tightest day in the week*. An earlier version gave the hard day
    the most room, which made the assertion true whether or not that day was counted.
    Mutant this catches: leaving the hard day out of the easy limit.
    """
    use_config("max_ride_min: 90\nday_max_ride_min: {sat: 120, tue: 50, wed: 50}")
    pattern = build_pattern(3, 0, limits=B.ride_limits())
    hard_day = next(d for d, r in pattern if r == "hard")
    assert B.ride_limits()[hard_day] == 50, "the setup no longer makes the hard day the tightest"
    limits = B.plan_limits(3)
    assert limits.easy_cap == 50
    assert limits.hard_cap == 50


def test_the_hard_sessions_nominal_length_is_held_to_its_days_limit():
    """
    The ladder's nominal hard session is 55-60 minutes; a 45-minute day cannot hold that, and a
    rung whose target counts 55 for a session that is at most 45 asks for ten minutes nobody
    can ride. Mutant this catches: ignoring `hard_cap` in the derivation.
    """
    ladder = derive(weeks(), BAND, CEILING, 3,
                    limits_for=lambda r: RideLimits(long_cap=90, easy_cap=90, hard_cap=45))
    assert {t.hard_min for t in ladder.tiers[1:]} == {45}
    assert ladder.tiers[0].hard_min == 0, "the floor still carries no hard session"


# ── status.py ────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    from src import firstrun

    (tmp_path / "config").mkdir()
    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "config" / "athlete.yaml")
    C.reset()
    yield tmp_path
    C.reset()


def stored(repo, extra: str, lit: int | None = None, long_: int | None = None):
    path = repo / "f.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        seed_ladder(conn, "p", "P")
        if lit is not None:
            conn.execute("UPDATE tiers SET lit_min = ?, long_min = ?", (lit, long_))
    C.CONFIG_PATH.write_text(plan_block(extra), encoding="utf-8")
    C.reset()
    return conn


def test_status_blocks_when_a_tightened_limit_no_longer_fits_the_ladder(repo):
    """
    The unattended failure: ladder derived, config edited later. Without this the first sign
    is a calendar that quietly stopped updating. Mutant: `_week` ignoring limits.
    """
    from src import firstrun

    conn = stored(repo, "max_ride_min: 90")
    check = firstrun._week(conn)
    assert not check.ok and check.blocking
    assert "limit is 90min" in check.detail


def test_status_is_quiet_when_the_limits_fit(repo):
    from src import firstrun

    conn = stored(repo, "max_ride_min: 130")
    assert firstrun._week(conn).ok


def test_status_says_nothing_about_limits_when_none_are_set(repo):
    from src import firstrun

    conn = stored(repo, "")
    assert firstrun._week(conn) is None and firstrun._limits(conn) is None


def test_status_says_which_blocks_lose_their_hard_session(repo):
    """
    Non-blocking: it is a consequence of a setting the athlete chose. But silent is wrong,
    since the symptom is a calendar with no intervals in it. Mutant: `_limits` dropped.
    """
    from src import firstrun

    conn = stored(repo, "max_ride_min: 50", lit=45, long_=45)
    assert firstrun._week(conn).ok
    note = firstrun._limits(conn)
    assert note is not None and not note.blocking
    assert "Threshold" in note.detail and "VO2" in note.detail and "Sprints" not in note.detail
    assert "plan.max_ride_min" in note.fix and "60min" in note.fix

"""
A decision keeps the rungs it was graded against.

`plan_progression.tier_applied` exists so a week's decision can be read back against the
inputs it was made under — the same reasoning as `config_hash` on `plans` and the whole
append-only design of the decision log. That stopped being true the moment a ladder could be
re-derived: `tier_applied = 2` meant one week of training before the re-derivation and a
different one after, with nothing in the row to say which.

It became urgent rather than theoretical when the outgrown check started *recommending*
re-derivation. Telling an athlete to do something with a known unresolved consequence is worse
than not having noticed in the first place.

So ladders accumulate versions instead of being overwritten, and every decided week records
which version graded it.
"""

import sqlite3
from datetime import date

import pytest

from conftest import seed_ladder
from src.db.migrations import migrate
from src.planning import plans, tiers_store
from src.planning.bootstrap import derive
from src.planning.progression import TIERS
from src.planning.roller import store_horizon

BAND = (3.5, 4.0)


def wk(ride_min: float, ride_days: int, longest: float) -> dict:
    return {"week_start": "2026-11-02", "ride_min": ride_min, "ride_days": ride_days,
            "longest_ride_min": longest, "strength_sessions": 2,
            "training_days": ride_days, "any_activity": True}


def small() -> object:
    return derive([wk(180, 3, 60)] * 16, BAND, 120, 7)


def large() -> object:
    return derive([wk(450, 5, 140)] * 16, BAND, 200, 7)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "v.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── versions accumulate ───────────────────────────────────────────────────────

def test_the_first_ladder_is_version_one(db):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    assert tiers_store.current_version(db, plan.id) == 1


def test_re_deriving_adds_a_version_rather_than_overwriting(db):
    """
    Nothing is deleted. The old rungs have to still be there, or a decision recorded against
    them cannot be resolved — which is the entire point.
    """
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    tiers_store.save(db, plan.id, large(), replace=True)

    assert tiers_store.current_version(db, plan.id) == 2
    v1 = tiers_store.load(db, plan.id, version=1)
    v2 = tiers_store.load(db, plan.id, version=2)
    assert v1[4].long_min != v2[4].long_min
    assert tiers_store.load(db, plan.id) == v2, "the default load is the current version"


def test_an_unbootstrapped_plan_has_no_version(db):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    assert tiers_store.current_version(db, plan.id) is None
    assert tiers_store.load(db, plan.id) is None


def test_re_deriving_still_has_to_be_asked_for(db):
    """
    Safe for the record is not the same as free. A new version changes every session
    prescribed from the next roll onward, so it stays a flag.
    """
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    with pytest.raises(ValueError, match="--replace"):
        tiers_store.save(db, plan.id, large())


def test_the_refusal_names_the_version_that_already_exists(db):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    tiers_store.save(db, plan.id, large(), replace=True)
    with pytest.raises(ValueError, match="version 2"):
        tiers_store.save(db, plan.id, small())


def test_one_plans_versions_are_not_another_plans(db):
    a = plans.ensure(db, "a", "A", started_on="2026-11-02")
    b = plans.ensure(db, "b", "B", started_on="2026-11-02")
    tiers_store.save(db, a.id, small())
    tiers_store.save(db, a.id, large(), replace=True)
    tiers_store.save(db, b.id, small())
    assert tiers_store.current_version(db, a.id) == 2
    assert tiers_store.current_version(db, b.id) == 1


# ── a decided week remembers its ladder ───────────────────────────────────────

def test_a_decided_week_records_the_version_that_graded_it(db):
    seed_ladder(db, slug="s", name="S")
    store_horizon(db, date(2026, 11, 2), "S")
    store_horizon(db, date(2026, 11, 16), "S")       # closes weeks, so decisions get written

    rows = [dict(r) for r in db.execute("SELECT * FROM plan_progression")]
    assert rows, "no weeks were decided, so this proves nothing"
    assert all(r["ladder_version"] == 1 for r in rows), [r["ladder_version"] for r in rows]


def test_a_week_decided_under_an_older_ladder_keeps_pointing_at_it(db):
    """
    The property the whole change exists for. After re-deriving, the old row's `tier_applied`
    still resolves against the rungs that produced it rather than against today's.
    """
    plan = seed_ladder(db, slug="s", name="S")
    store_horizon(db, date(2026, 11, 2), "S")
    store_horizon(db, date(2026, 11, 16), "S")
    before = [dict(r) for r in db.execute(
        "SELECT * FROM plan_progression ORDER BY week_start")]

    tiers_store.save(db, plan.id, large(), replace=True)

    after = [dict(r) for r in db.execute(
        "SELECT * FROM plan_progression ORDER BY week_start")]
    assert [r["ladder_version"] for r in after] == [r["ladder_version"] for r in before]

    old_rungs = tiers_store.ladder_for(db, plan.id, after[0])
    new_rungs = tiers_store.load(db, plan.id)
    assert old_rungs[4].long_min != new_rungs[4].long_min, (
        "the two ladders must differ or this asserts nothing")
    assert old_rungs == tiers_store.load(db, plan.id, version=1)


def test_a_week_decided_before_any_ladder_resolves_to_the_built_in_rungs(db):
    """
    NULL has a meaning rather than being an absence: graded before this plan had a derived
    ladder, against the built-in rungs. Resolving such a row against today's ladder is exactly
    the misreading this column prevents.
    """
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, large())
    legacy = {"ladder_version": None, "tier_applied": 2}
    assert tiers_store.ladder_for(db, plan.id, legacy) == dict(TIERS)


def test_decisions_made_after_a_re_derivation_record_the_new_version(db):
    plan = seed_ladder(db, slug="s", name="S")
    store_horizon(db, date(2026, 11, 2), "S")
    tiers_store.save(db, plan.id, large(), replace=True)
    store_horizon(db, date(2026, 11, 16), "S")

    versions = {r[0] for r in db.execute(
        "SELECT DISTINCT ladder_version FROM plan_progression")}
    assert versions == {2}, (
        f"weeks closed after the re-derivation were graded by version 2, so that is what "
        f"they must record; got {versions}")


# ── provenance follows the version ───────────────────────────────────────────

def test_provenance_describes_the_current_version(db):
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    tiers_store.save(db, plan.id, large(), replace=True)
    found = tiers_store.provenance(db, plan.id)
    assert found["version"] == 2
    assert found["observed"]["recent_ride_days"] == 5


def test_the_age_check_measures_the_current_version_not_the_first(db):
    """
    The outgrown check leaves a recently derived ladder alone. Measuring from the *first*
    version would let it fire the week after a re-derivation, which is the one moment it has
    nothing useful to say.
    """
    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    db.executemany(
        "INSERT INTO tiers (plan_id, version, tier_index, rides, long_min, hard, hard_min, "
        "lit_min, derived_from, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(plan.id, 1, t.index, t.rides, t.long_min, t.hard, t.hard_min, t.lit_min,
          "{}", "[]", "2026-01-05T00:00:00") for t in TIERS.values()])
    tiers_store.save(db, plan.id, large(), replace=True)

    age = tiers_store.age_weeks(db, plan.id, date(2026, 11, 5))
    assert age is not None and age < 1, (
        f"age {age} weeks — measured from version 1 rather than the current one")


# ── the migration ─────────────────────────────────────────────────────────────

def test_the_migration_leaves_old_decisions_unversioned(db):
    """
    Rows decided before migration 10 were graded against the built-in rungs, because no
    derived ladder existed. Backfilling `ladder_version = 1` onto them would claim they were
    graded by a ladder that did not exist when they closed — which is the exact dishonesty
    this column was added to prevent, and it survived every other test here.
    """
    from src.db.migrations import _m010_ladder_versions

    db.execute("DELETE FROM plan_progression")
    db.execute("DELETE FROM tiers")
    db.execute("UPDATE plan_progression SET ladder_version = NULL")
    plan = plans.ensure(db, "legacy", "Legacy", started_on="2026-01-05")
    db.execute(
        "INSERT INTO plan_progression (plan_name, plan_id, week_start, ride_minutes, "
        "ride_days, strength_logged, outcome, tier_applied, verdict, tier_next, "
        "week_type_next, rotation, block_index, week_in_block, reason, created_at) "
        "VALUES ('Legacy', ?, '2026-01-05', 180, 3, 2, 'met', 2, 'advance', 3, 'normal', "
        "'continue', 1, 1, 'x', '2026-01-12')", (plan.id,))
    db.executemany(
        "INSERT INTO tiers (plan_id, version, tier_index, rides, long_min, hard, hard_min, "
        "lit_min, derived_from, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(plan.id, 1, t.index, t.rides, t.long_min, t.hard, t.hard_min, t.lit_min,
          "{}", "[]", "2026-06-01T00:00:00") for t in TIERS.values()])
    db.commit()

    _m010_ladder_versions(db)

    row = db.execute("SELECT ladder_version FROM plan_progression").fetchone()
    assert row["ladder_version"] is None, (
        "a week decided before any ladder existed was stamped with a version it was never "
        "graded against")
    assert tiers_store.ladder_for(db, plan.id, dict(row)) == dict(TIERS)


def test_the_migration_versions_existing_rungs_as_one(db):
    """Rungs that predate versioning are version 1, so the current ladder stays loadable."""
    from src.db.migrations import _m010_ladder_versions

    plan = plans.ensure(db, "p", "P", started_on="2026-11-02")
    tiers_store.save(db, plan.id, small())
    db.commit()

    _m010_ladder_versions(db)

    assert tiers_store.current_version(db, plan.id) == 1
    assert tiers_store.load(db, plan.id) is not None

"""
Tests for plan identity.

The whole feature is one property: **renaming a plan must not lose its history**. Everything
here either asserts that directly or guards something that would break it.

It is worth recording that the first implementation failed this. `ensure()` derived the slug
from the name, so after a rename the roller passed the *new* name, a new slug was computed,
nothing matched, and a second plan was inserted beside the original with the stored rows
repointed at it. No error, no orphans — just a plan silently split in two. The fix is that the
slug is supplied, never derived at runtime; `slug_for` generates one once, at creation.
"""

import sqlite3
from datetime import date

import pytest

from src.db.migrations import migrate
from src.planning import plans
from src.planning.roller import store_horizon

SLUG = "sustainable-rolling"
NAME = "Sustainable rolling"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in plans.LABELLED_TABLES}


# ── the property ──────────────────────────────────────────────────────────────

def test_renaming_a_plan_keeps_every_row(db):
    store_horizon(db, date(2026, 11, 2), NAME)
    before = counts(db)
    assert before["prescribed_sessions"] > 0

    plans.rename(db, SLUG, "My Training Plan")

    assert counts(db) == before
    assert plans.orphans(db) == dict.fromkeys(plans.LABELLED_TABLES, 0)


def test_rolling_after_a_rename_does_not_create_a_second_plan(db):
    """
    The bug the slug exists to prevent, and which the first implementation had.

    Deriving the slug from the name meant the roller, called with the new name, computed a new
    slug, matched nothing, and inserted a duplicate plan — repointing the stored rows at it.
    """
    store_horizon(db, date(2026, 11, 2), NAME)
    before = counts(db)

    plans.rename(db, SLUG, "My Training Plan")
    store_horizon(db, date(2026, 11, 2), "My Training Plan")

    assert len(plans.active(db)) == 1
    assert counts(db) == before
    assert {r["plan_id"] for r in db.execute("SELECT plan_id FROM prescribed_sessions")} == {1}


def test_the_slug_survives_a_rename(db):
    """It is what every external_id on the remote calendar was built from."""
    plans.ensure(db, SLUG, NAME, started_on="2026-11-02")
    renamed = plans.rename(db, SLUG, "Something Else")
    assert renamed.slug == SLUG
    assert renamed.name == "Something Else"


def test_the_denormalised_label_is_updated_in_the_same_transaction(db):
    """
    Readers still filter on plan_name — 116 places across 14 modules. If the label were left
    stale they would see a plan with no rows the instant it was renamed.
    """
    store_horizon(db, date(2026, 11, 2), NAME)
    plans.rename(db, SLUG, "My Training Plan")

    for table in plans.LABELLED_TABLES:
        names = {r[0] for r in db.execute(f"SELECT DISTINCT plan_name FROM {table}")}
        assert names <= {"My Training Plan"}, f"{table} kept a stale label: {names}"


def test_ensure_applies_a_name_change_without_a_separate_rename_call(db):
    """A rename made in configuration should reach the label on the next roll."""
    plans.ensure(db, SLUG, NAME, started_on="2026-11-02")
    updated = plans.ensure(db, SLUG, "Renamed In Config", started_on="2026-11-02")
    assert updated.name == "Renamed In Config"
    assert len(plans.active(db)) == 1


# ── identity generation ───────────────────────────────────────────────────────

def test_slug_for_matches_the_external_id_prefix():
    """
    These two derivations must agree.

    The external_ids already on the intervals.icu calendar are built from
    roller.external_id_prefix. If this module generated a different slug for the same name,
    a newly created plan would push events under ids that do not match the ones already there.
    """
    from src.planning.roller import external_id_prefix

    for name in ("Sustainable rolling", "TrainerRoad 2026-27", "A/B test plan!"):
        assert plans.slug_for(name) + "-" == external_id_prefix(name)


def test_a_plan_is_created_once_and_then_found(db):
    first = plans.ensure(db, SLUG, NAME, started_on="2026-11-02")
    again = plans.ensure(db, SLUG, NAME, started_on="2026-11-02")
    assert first.id == again.id
    assert len(plans.active(db)) == 1


def test_two_different_slugs_are_two_plans(db):
    plans.ensure(db, SLUG, NAME, started_on="2026-11-02")
    plans.ensure(db, "other-plan", "Other Plan", started_on="2027-01-04")
    assert {p.slug for p in plans.active(db)} == {SLUG, "other-plan"}


# ── config provenance ─────────────────────────────────────────────────────────

def test_the_config_hash_is_order_independent():
    """
    Otherwise the field records the order someone wrote their YAML in, not their settings.
    """
    a = plans.config_fingerprint({"band": [3.5, 4.0], "horizon": 3})
    b = plans.config_fingerprint({"horizon": 3, "band": [3.5, 4.0]})
    assert a == b
    assert a != plans.config_fingerprint({"horizon": 4, "band": [3.5, 4.0]})


def test_the_config_hash_is_recorded_and_updated(db):
    plans.ensure(db, SLUG, NAME, started_on="2026-11-02", config_hash="abc123")
    assert plans.by_slug(db, SLUG).config_hash == "abc123"
    plans.ensure(db, SLUG, NAME, started_on="2026-11-02", config_hash="def456")
    assert plans.by_slug(db, SLUG).config_hash == "def456"


# ── retirement, and what must not happen ──────────────────────────────────────

def test_retiring_a_plan_keeps_its_rows(db):
    """
    A superseded plan keeps its past — the rule the TrainerRoad cutover followed. Deleting it
    would take the adherence record the stored cycle reviews were written against.
    """
    store_horizon(db, date(2026, 11, 2), NAME)
    before = counts(db)

    retired = plans.retire(db, SLUG)

    assert retired.retired_at
    assert plans.active(db) == []
    assert counts(db) == before


def test_renaming_an_unknown_plan_raises_rather_than_creating_one(db):
    with pytest.raises(ValueError, match="no plan"):
        plans.rename(db, "never-existed", "New Name")


# ── every writer sets plan_id ─────────────────────────────────────────────────

def test_rolling_links_every_row_it_writes(db):
    """An unlinked row is invisible to anything that moves to plan_id later."""
    store_horizon(db, date(2026, 11, 2), NAME)
    store_horizon(db, date(2026, 11, 16), NAME)      # closes weeks, so progression fills too

    for table in plans.LABELLED_TABLES:
        total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert total > 0, f"{table} wrote nothing, so this proves nothing"
    assert plans.orphans(db) == dict.fromkeys(plans.LABELLED_TABLES, 0)


def test_the_migration_backfilled_rows_that_predate_the_table(db):
    """
    Rows written before migration 8 existed have to end up linked too, or the plan loses the
    history it was created to protect.
    """
    db.execute("PRAGMA user_version = 7")
    db.execute("DELETE FROM plans")
    db.execute(
        "INSERT INTO prescribed_sessions (plan_name, date, weekday, block_index, block_name, "
        "week_in_block, is_recovery_week, role, archetype, variant, prescription, "
        "params_json, created_at, plan_id) VALUES "
        "('Legacy Plan', '2026-01-05', 0, 0, 'B', 1, 0, 'long', 'lit', 'steady', 'x', "
        "'{}', '2026-01-01', NULL)")
    db.commit()

    from src.db.migrations import _m008_plans_table

    _m008_plans_table(db)

    row = db.execute("SELECT plan_id, plan_name FROM prescribed_sessions").fetchone()
    plan = plans.by_slug(db, "legacy-plan")
    assert plan is not None and row["plan_id"] == plan.id
    assert plan.started_on == "2026-01-05", "started_on should come from the earliest row"

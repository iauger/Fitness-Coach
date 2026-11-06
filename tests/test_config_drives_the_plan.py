"""
The point of step 3: the generator reads the athlete's file, not its own source.

Every test here changes a setting and asserts the generated plan changed with it. That shape
is deliberate. Asserting that `plan_name()` returns what the file says proves only that the
accessor works; asserting that a *stored session* carries it proves there is no copy of the
old constant still bound in a default argument somewhere — which is the specific way this
refactor could silently half-land.

It is not hypothetical. `PLAN_NAME` was read into eight default arguments of the form
`plan_name: str = PLAN_NAME`, and a default binds when the function is defined. Had the
constant simply been reassigned from config, every one of those would still hold whatever was
loaded at import, and no amount of `reset()` would reach them. The same mechanism is what made
a fixture's `schema.DB_PATH` patch do nothing and let the suite write to the real database.

The modules under test are imported by the time these run, so an import-time binding would
already have happened. That is the property being exercised.
"""

import sqlite3
import textwrap
from datetime import date

import pytest

from conftest import seed_ladder

import src.config
from src.analysis.report import athlete_profile
from src.db.migrations import migrate
from src.planning import plans
from src.planning.constants import goal_date, plan_name, plan_start_monday
from src.planning.roller import plan_week_number, store_horizon

OTHER = """
athlete:
  name: Someone Else
  timezone: Europe/London
  history_start: 2019
  primary_sport: running
  equipment: A bicycle
  notes: Works nights.
  tools:
    - intervals.icu

goals:
  - description: A hill climb
    date: 2028-04-10
    priority: primary

plan:
  name: Totally Different Plan
  start_monday: 2026-11-16
  goal_date: 2028-04-10

profile:
  ftp: 199.0
"""


@pytest.fixture
def other_config(tmp_path, monkeypatch):
    """A configuration sharing no value with the committed example."""
    path = tmp_path / "other.yaml"
    path.write_text(textwrap.dedent(OTHER), encoding="utf-8")
    monkeypatch.setattr(src.config, "CONFIG_PATH", path)
    src.config.reset()
    yield path
    src.config.reset()


@pytest.fixture
def db(tmp_path, other_config):
    """
    Depends on `other_config` deliberately: the ladder is seeded against the plan the config
    names, so the config has to be in place before the plan exists. Seeding under the example
    config and then rolling under this one would attach the roll to the wrong plan — which
    `plans.resolve` would do silently, since it prefers the one plan already in the database.
    """
    path = tmp_path / "p.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn, name="Totally Different Plan", started_on="2026-11-16")
    yield conn
    conn.close()


# ── the accessors ─────────────────────────────────────────────────────────────

def test_the_accessors_follow_the_file(other_config):
    assert plan_name() == "Totally Different Plan"
    assert plan_start_monday() == "2026-11-16"
    assert goal_date() == "2028-04-10"


def test_the_accessors_are_read_again_after_a_change(other_config, tmp_path):
    assert plan_name() == "Totally Different Plan"
    other_config.write_text(
        textwrap.dedent(OTHER).replace("Totally Different Plan", "Renamed Again"),
        encoding="utf-8")
    src.config.reset()
    assert plan_name() == "Renamed Again"


# ── the generated plan ────────────────────────────────────────────────────────

def test_every_stored_session_carries_the_configured_name(other_config, db):
    store_horizon(db, date(2026, 11, 23))
    names = {r[0] for r in db.execute("SELECT DISTINCT plan_name FROM prescribed_sessions")}
    assert names == {"Totally Different Plan"}, (
        "a default argument is still bound to the old constant")


def test_the_week_rows_carry_the_configured_goal_date(other_config, db):
    store_horizon(db, date(2026, 11, 23))
    ends = {r[0] for r in db.execute("SELECT DISTINCT plan_end_date FROM training_plan_weeks")}
    assert ends == {"2028-04-10"}


def test_week_numbering_counts_from_the_configured_start_monday(other_config):
    """
    Off by one here moves every decision, which is why the loader insists it is a Monday.
    """
    assert plan_week_number(date(2026, 11, 16)) == 1
    assert plan_week_number(date(2026, 11, 23)) == 2


def test_the_plan_row_takes_its_slug_from_the_configured_name(other_config, db):
    store_horizon(db, date(2026, 11, 23))
    active = plans.active(db)
    assert [p.slug for p in active] == ["totally-different-plan"]
    assert active[0].started_on == "2026-11-16"


def test_the_athlete_block_is_the_configured_one(other_config):
    who = athlete_profile()
    assert who["name"] == "Someone Else"
    assert who["notes"] == "Works nights."
    assert who["history_start"] == "2019"
    assert "goals" not in who          # goals live in the table now (src/goals.py)


def test_the_generators_resolve_the_name_themselves_when_not_given_one(other_config):
    """
    `expand_week` and `plan_week_row` each fall back to the configured name, and the fallback
    has to be resolved in the body rather than bound as a default.

    This is here because a mutant that restored `plan_name or "Sustainable rolling"` in
    `expand_week` survived the whole suite. Everything that calls it in anger passes a name
    explicitly, and the one test that did not was running under a config whose plan happened
    to be called "Sustainable rolling" — so it asserted the mutant's own answer. The fallback
    is only observable under a config that disagrees with the old constant.
    """
    from src.planning.blocks import ROTATION, expand_week, plan_week_row

    monday = date(2026, 11, 16)
    sessions = expand_week(ROTATION[0], 1, 1, monday)
    row = plan_week_row(monday, ROTATION[0], 1, 1, 1, tier=1)

    assert sessions, "no sessions, so this proves nothing"
    assert {s["plan_name"] for s in sessions} == {"Totally Different Plan"}
    assert row["plan_name"] == "Totally Different Plan"


def test_dated_goals_come_back_soonest_first(other_config, tmp_path, monkeypatch):
    """
    The report counts down to each one in order. Unsorted, the nearest event can be listed
    last, which is the one line of that block anyone reads first.
    """
    import sqlite3

    import src.config
    from src import goals as goals_store
    from src.db.migrations import migrate

    path = tmp_path / "goals.yaml"
    path.write_text(
        "athlete:\n  name: X\n"
        "goals:\n"
        "  - description: Late\n    date: 2028-09-01\n"
        "  - description: Early\n    date: 2027-03-01\n"
        "  - description: Middle\n    date: 2027-11-01\n"
        "  - description: Undated\n"
        "plan:\n  name: P\n  start_monday: 2026-11-02\n", encoding="utf-8")
    monkeypatch.setattr(src.config, "CONFIG_PATH", path)
    src.config.reset()

    # An empty goals table is a first run, so the configured goals stand in.
    db_path = tmp_path / "g.db"
    migrate(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert [g[0] for g in goals_store.dated(conn)] == ["Early", "Middle", "Late"]
    conn.close()

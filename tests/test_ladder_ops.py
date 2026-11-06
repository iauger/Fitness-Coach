"""
The ladder derive / compare / store path shared by `bootstrap_plan.py` and the UI.

Mutants these exist to catch: a preview that writes; a diff that reports fields that did not
change (or misses ones that did); "same" decided by version rather than by rungs; a store that
overwrites instead of adding a version; and a failed derivation raised out of a preview, which
the screen would have shown as a stack trace instead of a reason.
"""

import sqlite3
from contextlib import contextmanager

import pytest

from src.db.migrations import migrate
from src.planning import ladder_ops, tiers_store
from src.planning.ladder_ops import LadderError, changes, preview, store

BAND = (3.5, 4.0)


def wk(ride_min: float, ride_days: int, longest: float) -> dict:
    return {"week_start": "2026-11-02", "ride_min": ride_min, "ride_days": ride_days,
            "longest_ride_min": longest, "strength_sessions": 2,
            "training_days": ride_days, "any_activity": True}


class Conn(sqlite3.Connection):
    """A connection that can carry the history a test wants the derivation to read."""


SMALL = [wk(180, 3, 60)] * 16
LARGE = [wk(240, 4, 80)] * 16      # a stronger recent history: different rungs


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "l.db"
    migrate(path)
    conn = sqlite3.connect(path, factory=Conn)
    conn.row_factory = sqlite3.Row

    @contextmanager
    def same_conn():
        yield conn
        conn.commit()

    monkeypatch.setattr(ladder_ops, "read_connection", same_conn)
    monkeypatch.setattr(ladder_ops, "write_connection", same_conn)
    monkeypatch.setattr(ladder_ops, "migrate_db", lambda: None)
    history = {"weeks": SMALL}
    monkeypatch.setattr(tiers_store, "weeks_for", lambda conn, through, n=78: history["weeks"])
    conn.history = history
    yield conn
    conn.close()


def rung(tier, **kw):
    base = {"tier": tier, "rides": 3, "long_min": 90, "hard": 1, "hard_min": 55,
            "lit_min": 65, "hours": 3.5}
    return {**base, **kw}


# -- the diff itself --------------------------------------------------------------------------

def test_only_the_fields_that_differ_are_reported():
    was = {"version": 1, "rungs": [rung(0), rung(1, long_min=100)]}
    now = [rung(0), rung(1, long_min=90)]
    assert changes(was, now) == [
        {"tier": 1, "field": "long_min", "label": "long ride (min)", "was": 100, "now": 90}]


def test_identical_ladders_have_no_changes():
    was = {"version": 3, "rungs": [rung(0), rung(1)]}
    assert changes(was, [rung(0), rung(1)]) == []


def test_a_rung_that_did_not_exist_is_all_new():
    out = changes({"version": 1, "rungs": [rung(0)]}, [rung(0), rung(1)])
    assert {c["field"] for c in out} == set(ladder_ops.FIELDS)
    assert all(c["tier"] == 1 and c["was"] is None for c in out)


def test_no_stored_ladder_means_everything_is_new():
    assert len(changes(None, [rung(0), rung(1)])) == 2 * len(ladder_ops.FIELDS)


# -- preview ----------------------------------------------------------------------------------

def test_a_preview_with_nothing_stored(db):
    p = preview()
    assert p["error"] is None and p["stored"] is None and p["same"] is False
    assert [r["tier"] for r in p["derived"]] == [0, 1, 2, 3, 4]
    assert p["changes"] and p["observed"]["recent_hours"] > 0


def test_a_preview_writes_nothing(db):
    """Mutant: preview calls store. It is behind a GET, so a page load would re-derive."""
    before = db.execute("select count(*) from tiers").fetchone()[0]
    preview()
    assert db.execute("select count(*) from tiers").fetchone()[0] == before == 0


def test_after_storing_the_preview_says_nothing_has_changed(db):
    store()
    p = preview()
    assert p["same"] is True and p["changes"] == [] and p["stored"]["version"] == 1


def test_same_is_decided_by_the_rungs_not_the_version(db):
    """Mutant: same = stored exists. Different history must show as a change."""
    store()
    db.history["weeks"] = LARGE
    p = preview()
    assert p["same"] is False and p["changes"]
    assert p["stored"]["version"] == 1


def test_every_reported_change_matches_the_two_ladders(db):
    store()
    db.history["weeks"] = LARGE
    p = preview()
    was = {r["tier"]: r for r in p["stored"]["rungs"]}
    now = {r["tier"]: r for r in p["derived"]}
    for c in p["changes"]:
        assert was[c["tier"]][c["field"]] == c["was"] != c["now"] == now[c["tier"]][c["field"]]
    # and nothing that differs is left out
    differing = {(t, f) for t in now for f in ladder_ops.FIELDS if was[t][f] != now[t][f]}
    assert differing == {(c["tier"], c["field"]) for c in p["changes"]}


def test_too_little_history_is_a_reason_not_an_exception(db):
    db.history["weeks"] = []
    p = preview()
    assert p["error"] and "derived" not in p


# -- store ------------------------------------------------------------------------------------

def test_a_first_store_is_version_one(db):
    done = store()
    assert (done["version"], done["written"]) == (1, 5)


def test_storing_over_an_existing_ladder_needs_replace(db):
    """Mutant: store always replaces. The CLI has always needed --replace for that."""
    store()
    with pytest.raises(LadderError):
        store()
    assert preview()["stored"]["version"] == 1


def test_a_replace_adds_a_version_and_keeps_the_old_rungs(db):
    store()
    db.history["weeks"] = LARGE
    assert store(replace=True)["version"] == 2
    plan_id = db.execute("select id from plans").fetchone()[0]
    assert tiers_store.load(db, plan_id, version=1)[0].rides != \
        tiers_store.load(db, plan_id, version=2)[0].rides or \
        tiers_store.load(db, plan_id, version=1)[4].long_min != \
        tiers_store.load(db, plan_id, version=2)[4].long_min

"""
The one sport table, and the two things derived from it.

Written after a mutation run on the merge: dropping a cycling type from the table, and making
`types_in` ignore its argument, both survived the whole suite. Those are the two ways this
module can be wrong, and neither raises — an unrecognised type falls to "other" and a ride
quietly stops being a ride.
"""

import sqlite3

import pytest

from src.sports import CYCLING_TYPES, OTHER, SPORT_GROUPS, sport_group, types_in


def test_every_cycling_type_is_recognised_as_cycling():
    """
    Named explicitly rather than derived from the table, so this fails if one is removed.

    A derived assertion would agree with the table whatever the table said, which is exactly
    the tautology mutation testing keeps finding in this project.
    """
    for t in ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide"):
        assert sport_group(t) == "cycling", t
    assert set(CYCLING_TYPES) == {"Ride", "VirtualRide", "GravelRide",
                                  "MountainBikeRide", "EBikeRide"}


def test_the_other_groups_are_recognised_too():
    for t, group in (("Run", "running"), ("TrailRun", "running"), ("Swim", "swimming"),
                     ("WeightTraining", "strength"), ("Workout", "strength"),
                     ("Yoga", "yoga"), ("Pilates", "yoga"),
                     ("Walk", "walk"), ("Hike", "hike")):
        assert sport_group(t) == group, t


def test_an_unknown_or_missing_type_is_other():
    """
    The quiet failure this module exists to make loud. `None` reaches here from the database
    whenever intervals.icu omits a type.
    """
    assert sport_group("Kitesurf") == OTHER
    assert sport_group(None) == OTHER
    assert sport_group("") == OTHER


def test_types_in_returns_only_that_group():
    assert "Run" not in types_in("cycling")
    assert set(types_in("running")) == {"Run", "VirtualRun", "TrailRun"}
    assert types_in("nonexistent") == ()


def test_every_type_in_the_table_maps_to_a_group_types_in_can_find():
    """A group reachable by `sport_group` but not by `types_in` would be a silent asymmetry."""
    for t, group in SPORT_GROUPS.items():
        assert t in types_in(group), t


# ── the duplicate check reads groups, not types ───────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE activities (id TEXT PRIMARY KEY, date TEXT, "
              "moving_time INTEGER, type TEXT)")
    return c


def test_a_duplicate_is_the_same_sport_not_the_same_type(conn):
    """
    A Zwift ride arriving twice shows up as `Ride` from one source and `VirtualRide` from
    another. Comparing types rather than groups would let both through, which is the whole
    reason this check groups at all.
    """
    from src.db.store import _has_near_duplicate

    conn.execute("INSERT INTO activities VALUES ('a', '2026-11-04', 3600, 'VirtualRide')")
    assert _has_near_duplicate(conn, "2026-11-04", 3610, "Ride", exclude_id="b")


def test_two_different_sports_of_the_same_length_are_not_duplicates(conn):
    from src.db.store import _has_near_duplicate

    conn.execute("INSERT INTO activities VALUES ('a', '2026-11-04', 3600, 'Run')")
    assert not _has_near_duplicate(conn, "2026-11-04", 3610, "Ride", exclude_id="b")

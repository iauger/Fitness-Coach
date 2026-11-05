"""
What counts as which sport. One table, because there were six.

`SPORT_GROUPS` was copied verbatim into `analysis/activities.py`, `db/store.py`,
`visualizations/calendar_view.py` and `scripts/dedupe_activities.py`, and derived twice more:
`compliance.CYCLING_TYPES` listed the five cycling types by hand, and
`import_strava_archive.STRAVA_TYPE_MAP` was an identity map over the same sixteen keys with a
comment saying "matching activities.py".

All six were still in step when they were merged — verified, not assumed, by comparing the
dicts at runtime before deleting any of them. That they agreed is the reason the merge was
safe and not the reason it was unnecessary: nothing made them agree. A new activity type
reaches intervals.icu whenever Garmin adds one, and adding it to four dicts and remembering
two derivations is not a thing anyone does reliably. The failure would be quiet in a
particular way — an unrecognised type falls to `"other"`, so a ride would vanish from a
distribution chart, be excluded from a duplicate check, and be ungrouped on the calendar,
each independently, with no error anywhere.

A leaf module, stdlib only, because `db/store.py` is one of the consumers: anything this
module imported, `store` would then import too.
"""

# intervals.icu activity type -> the group this project reasons in.
SPORT_GROUPS = {
    "Ride": "cycling", "VirtualRide": "cycling", "GravelRide": "cycling",
    "MountainBikeRide": "cycling", "EBikeRide": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
    "Swim": "swimming", "OpenWaterSwim": "swimming",
    "WeightTraining": "strength", "Workout": "strength",
    "Yoga": "yoga", "Pilates": "yoga",
    "Walk": "walk", "Hike": "hike",
}

# Anything unrecognised. Named rather than repeated as a literal default in six places, since
# it is also a colour key and a group a chart can legitimately show.
OTHER = "other"


def sport_group(activity_type: str | None) -> str:
    """The group an activity type belongs to, or `OTHER`."""
    return SPORT_GROUPS.get(activity_type or "", OTHER)


def types_in(group: str) -> tuple[str, ...]:
    """
    Every activity type in a group, in table order.

    Derived rather than listed, so a new cycling type is recognised by the compliance matcher
    the moment it is added above — which is the half of the duplication that was least likely
    to be noticed, because a hand-written list of five does not look like a copy of anything.
    """
    return tuple(t for t, g in SPORT_GROUPS.items() if g == group)


CYCLING_TYPES = types_in("cycling")

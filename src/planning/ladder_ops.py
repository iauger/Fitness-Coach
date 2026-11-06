"""
Derive, compare and store the tier ladder. The one implementation behind
`scripts/bootstrap_plan.py` and the UI's ladder screen.

It lived in the script, which is why the UI could not have used it without a second copy, and a
second copy of "how the ladder is derived" is exactly how a screen and a command end up showing
different rungs. Both now call `derive_now` and `store`.

`preview` is what makes a re-derive safe to do from a screen: the ladder decides every session
for months, so what changes (which rung, which field, from what to what) is shown before it is
kept.
"""

from src.clock import today_local
from src.config import config
from src.db.schema import migrate_db, read_connection, write_connection
from src.planning import plans, tiers_store
from src.planning.blocks import plan_limits
from src.planning.bootstrap import BootstrapError, Ladder, derive
from src.planning.constants import (
    configured_slug, max_ride_days, plan_name as configured_plan_name, ride_day_setting,
)

FIELDS = ("rides", "long_min", "hard", "hard_min", "lit_min")
LABELS = {"rides": "rides a week", "long_min": "long ride (min)", "hard": "hard sessions",
          "hard_min": "hard session (min)", "lit_min": "easy ride (min)"}


class LadderError(Exception):
    pass


def derive_now(conn, weeks: int = 78) -> Ladder:
    """Derive from the synced history under the current configuration. Raises BootstrapError."""
    cfg = config()
    return derive(tiers_store.weeks_for(conn, today_local(), weeks),
                  cfg.plan.sustainable_band_hours, cfg.plan.long_ride_ceiling_min,
                  max_ride_days(), ride_day_setting(), limits_for=plan_limits)


def _row(index, t) -> dict:
    """A rung as plain numbers. Works for both a derived rung and a stored `Tier`."""
    return {"tier": index, **{f: getattr(t, f) for f in FIELDS},
            "hours": round(t.target_min / 60, 2)}


def stored(conn) -> dict | None:
    plan = plans.by_name(conn, configured_plan_name())
    if plan is None:
        return None
    version = tiers_store.current_version(conn, plan.id)
    rungs = tiers_store.load(conn, plan.id) if version else None
    if not rungs:
        return None
    return {"version": version, "rungs": [_row(i, rungs[i]) for i in sorted(rungs)]}


def changes(was: dict | None, now: list[dict]) -> list[dict]:
    """Every field that differs, rung by rung; a rung that did not exist counts as all new."""
    old = {r["tier"]: r for r in (was["rungs"] if was else [])}
    out = []
    for r in now:
        before = old.get(r["tier"])
        for f in FIELDS:
            if before is None or before[f] != r[f]:
                out.append({"tier": r["tier"], "field": f, "label": LABELS[f],
                            "was": None if before is None else before[f], "now": r[f]})
    return out


def preview(weeks: int = 78) -> dict:
    with read_connection() as conn:
        current = stored(conn)
        try:
            ladder = derive_now(conn, weeks)
        except BootstrapError as exc:
            return {"error": str(exc), "stored": current}
    derived = [_row(t.index, t) for t in ladder.tiers]
    o = ladder.observed
    diff = changes(current, derived)
    return {
        "error": None,
        "stored": current,
        "derived": derived,
        "changes": diff,
        "same": current is not None and not diff,
        "notes": list(ladder.notes),
        "observed": {
            "window": [o.window_start, o.window_end],
            "weeks_with_a_ride": [o.ridden_weeks, o.weeks_seen],
            "recent_hours": round(o.recent_volume_min / 60, 1),
            "recent_days": round(o.recent_ride_days),
            "recent_long_min": round(o.recent_long_min),
            "sustained_hours": round(o.sustained_volume_min / 60, 1),
            "sustained_since": o.sustained_window_start,
        },
    }


def store(replace: bool = False, weeks: int = 78) -> dict:
    """Derive and keep it. A re-derive is a new version; past decisions keep their old one."""
    migrate_db()
    cfg = config()
    name = configured_plan_name()
    with read_connection() as conn:
        ladder = derive_now(conn, weeks)              # BootstrapError propagates
    with write_connection() as conn:
        slug = plans.resolve(conn, name, configured_slug())
        plan = plans.ensure(conn, slug, name, started_on=cfg.plan.start_monday.isoformat(),
                            goal_date=cfg.plan.goal_date.isoformat()
                            if cfg.plan.goal_date else None)
        try:
            written = tiers_store.save(conn, plan.id, ladder, replace=replace)
        except ValueError as exc:
            raise LadderError(str(exc)) from None
        version = tiers_store.current_version(conn, plan.id)
    return {"written": written, "version": version, "ladder": ladder}

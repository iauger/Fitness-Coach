"""
The stored tier ladder: reading it, writing it, and refusing to guess when it is absent.

`progression.TIERS` is still here and still the fallback. What changes is that a plan can have
its own ladder, derived from its athlete's history, and that **the absence of one is a state
the generator can see** rather than a silence it fills in.

That distinction is the whole point of the module. The failure this is written against is
specific: a second athlete clones the repo, the `tiers` table is empty, the roller falls back
to one particular person's rungs, and nothing ever says so. They would get a plan — a plausible
one, with no error anywhere — built on a stranger's measured practice, and the natural reading
of a plan that fits badly is that the plan is wrong about *them*. So `require` raises, with a
message naming the command that fixes it.

**Why the ladder is stored rather than derived on read.** Same argument as `plan_progression`:
a prescription that re-derives itself from history would jitter as history changes, and a
mid-week sync could lengthen the session being ridden that evening. Derive the inputs, persist
the decision.
"""

import json
from datetime import date, timedelta

from src.clock import utc_now_iso
from src.planning.bootstrap import Ladder
from src.planning.progression import TIERS, Tier


class NotBootstrapped(Exception):
    """No ladder for this plan, with a message saying how to make one."""


def _tier(row) -> Tier:
    return Tier(index=row["tier_index"], rides=row["rides"], long_min=row["long_min"],
                hard=row["hard"], hard_min=row["hard_min"], lit_min=row["lit_min"])


def load(conn, plan_id: int) -> dict[int, Tier] | None:
    """The plan's ladder as `progression.TIERS` is shaped, or None if it has none."""
    rows = conn.execute(
        "SELECT * FROM tiers WHERE plan_id = ? ORDER BY tier_index", (plan_id,)).fetchall()
    return {r["tier_index"]: _tier(r) for r in rows} if rows else None


def require(conn, plan_id: int, plan_name: str = "") -> dict[int, Tier]:
    """
    The plan's ladder, or an exception explaining how to get one.

    Deliberately not `load(...) or TIERS`. That one-liner is the bug: it generates a complete,
    believable plan from another athlete's rungs and reports nothing, and because it never
    fails there is no later moment at which anyone discovers it.
    """
    ladder = load(conn, plan_id)
    if ladder is None:
        raise NotBootstrapped(
            f"no tier ladder for {plan_name or f'plan {plan_id}'}. The ladder is derived from "
            f"your own synced riding, so there is nothing to generate from until it has been:\n"
            f"    python scripts/bootstrap_plan.py\n"
            f"Run scripts/fetch_history.py first if you have not synced yet.")
    return ladder


def age_weeks(conn, plan_id: int, today: date) -> float | None:
    """
    How long ago this ladder was derived, in weeks.

    Read by the outgrown check, which leaves a recent ladder alone: re-deriving every few
    weeks would make `tier_applied` mean a different thing in every row of the decision log.
    """
    row = conn.execute(
        "SELECT MIN(created_at) FROM tiers WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        derived = date.fromisoformat(str(row[0])[:10])
    except ValueError:
        return None
    return max(0.0, (today - derived).days / 7)


def provenance(conn, plan_id: int) -> dict | None:
    """What the bootstrap observed, for explaining a ladder after the fact."""
    row = conn.execute(
        "SELECT derived_from, notes, created_at FROM tiers WHERE plan_id = ? "
        "ORDER BY tier_index LIMIT 1", (plan_id,)).fetchone()
    if row is None:
        return None
    return {"observed": json.loads(row["derived_from"]),
            "notes": json.loads(row["notes"] or "[]"),
            "derived_at": row["created_at"]}


def save(conn, plan_id: int, ladder: Ladder, replace: bool = False) -> int:
    """
    Write a derived ladder. Returns the number of rungs written.

    `replace=False` refuses to overwrite an existing ladder, because re-deriving is not a
    neutral act: the gate's stored decisions in `plan_progression` were graded against the
    rungs that were in force when each week closed, and swapping the ladder underneath them
    makes `tier_applied` mean something different for rows before and after. A re-bootstrap is
    a deliberate choice, so it is a flag.
    """
    if not replace and load(conn, plan_id) is not None:
        raise ValueError(
            f"plan {plan_id} already has a ladder. Re-deriving changes what the tiers already "
            f"recorded in plan_progression meant, so pass --replace if that is intended.")

    conn.execute("DELETE FROM tiers WHERE plan_id = ?", (plan_id,))
    now = utc_now_iso()
    observed = json.dumps(ladder.observed.as_dict(), sort_keys=True)
    notes = json.dumps(list(ladder.notes))
    conn.executemany(
        "INSERT INTO tiers (plan_id, tier_index, rides, long_min, hard, hard_min, lit_min, "
        "derived_from, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(plan_id, t.index, t.rides, t.long_min, t.hard, t.hard_min, t.lit_min,
          observed, notes, now) for t in ladder.tiers])
    return len(ladder.tiers)


def weeks_for(conn, through: date, weeks_back: int = 78) -> list[dict]:
    """
    The synced weekly volume a bootstrap reads, oldest first, zero-filled.

    Zero-filled matters more than it looks: a week with no activity produces no rows, so a
    `GROUP BY week` omits precisely the weeks that motivated this whole design — 21 of 62 in
    the reference history. `week_volume` fills them, and a derivation that never saw a missed
    week would be reading a history in which the athlete never missed one.
    """
    from src.analysis.activities import week_volume
    from src.analysis.dates import monday_of

    end = monday_of(through)
    return [week_volume(conn, end - timedelta(weeks=i))
            for i in range(weeks_back, 0, -1)]


def fallback() -> dict[int, Tier]:
    """
    The hand-written ladder, for tests and for reasoning about the design.

    Not reachable from the generator — `require` is what the generator calls. Kept because
    every invariant test and the whole progression test suite is written against these rungs,
    and because it is the ladder the derivation was validated by reproducing.
    """
    return dict(TIERS)

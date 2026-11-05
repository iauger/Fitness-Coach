"""
Plan identity: a slug that never changes, and a name that may.

`plan_name` used to be the key. Five readers joined on it, and that was fine while the name
lived in a Python constant nobody edited. It stops being fine the moment the name comes from a
configuration file, because renaming a plan there orphans every stored row — silently, with no
error, leaving a plan that appears to have no history.

So the `plans` table holds a generated `slug` as the identity and a `name` as a label. This
module owns the distinction, and in particular owns `rename`, which is the one operation that
has to touch both.

**Why `plan_name` still exists on three tables.** It is referenced 116 times across 14 modules.
Rewriting all of those in the same change that introduced the table would have produced a diff
nobody could review. It is now a *cached label*, not the key: every write goes through here, so
it cannot drift, and a later migration drops it once the readers have moved to `plan_id`.

**Why `config_hash`.** Once configuration is editable, "which settings produced this
prescription" becomes unanswerable, because re-deriving it later answers today's question
rather than the one that was asked. Same reasoning as `tier_applied` on `plan_progression`:
store the inputs a decision was made under.
"""

import hashlib
import json
from dataclasses import dataclass

from src.clock import utc_now_iso

# The three tables carrying a denormalised plan_name alongside plan_id.
LABELLED_TABLES = ("prescribed_sessions", "training_plan_weeks", "plan_progression")


@dataclass(frozen=True)
class PlanRow:
    id: int
    slug: str
    name: str
    started_on: str
    goal_date: str | None = None
    config_hash: str | None = None
    created_at: str = ""
    retired_at: str | None = None


def slug_for(name: str) -> str:
    """
    The stable identity derived from a name, once, when the plan is first created.

    Identical to `roller.external_id_prefix`'s derivation on purpose: the `external_id`s
    already pushed to the intervals.icu calendar are built from it, and a different rule here
    would orphan live calendar events on the next push.
    """
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


def config_fingerprint(payload: dict) -> str:
    """
    A short, stable hash of the settings a plan was built under.

    Sorted keys so the same settings always hash the same regardless of how the dict was
    assembled — otherwise the field records nothing but the order someone happened to write
    their YAML in.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _row(r) -> PlanRow:
    return PlanRow(**{k: r[k] for k in PlanRow.__dataclass_fields__ if k in r.keys()})


def by_slug(conn, slug: str) -> PlanRow | None:
    r = conn.execute("SELECT * FROM plans WHERE slug = ?", (slug,)).fetchone()
    return _row(r) if r else None


def by_name(conn, name: str) -> PlanRow | None:
    r = conn.execute("SELECT * FROM plans WHERE name = ?", (name,)).fetchone()
    return _row(r) if r else None


def active(conn) -> list[PlanRow]:
    """Plans that have not been retired, oldest first."""
    return [_row(r) for r in conn.execute(
        "SELECT * FROM plans WHERE retired_at IS NULL ORDER BY started_on, id")]


def ensure(conn, slug: str, name: str, started_on: str, goal_date: str | None = None,
           config_hash: str | None = None) -> PlanRow:
    """
    Fetch the plan with this slug, creating it if it does not exist.

    **The slug is supplied, never derived from the name here.** Deriving it was a real bug: a
    renamed plan passed by its new name produced a new slug, matched nothing, and inserted a
    duplicate next to the original — the exact failure the slug exists to prevent. `slug_for`
    is for generating one *once*, when a plan is first created.

    The stored name is refreshed from `name`, so a rename made in configuration reaches the
    label without a separate call.
    """
    found = by_slug(conn, slug)
    if found:
        if name != found.name:
            return rename(conn, slug, name)
        if config_hash and config_hash != found.config_hash:
            conn.execute("UPDATE plans SET config_hash = ? WHERE id = ?",
                         (config_hash, found.id))
            found = by_slug(conn, slug)
        return found

    conn.execute(
        "INSERT INTO plans (slug, name, started_on, goal_date, config_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (slug, name, started_on, goal_date, config_hash, utc_now_iso()))
    return by_slug(conn, slug)


def rename(conn, slug: str, new_name: str) -> PlanRow:
    """
    Change a plan's label without changing its identity.

    The whole point of the slug. The denormalised `plan_name` on the three labelled tables is
    updated in the same transaction, so a reader still filtering on the name sees the new one
    immediately and nothing is left pointing at a name that no longer exists.

    The slug is **not** recomputed. It is the identity, and it is what the `external_id`s on
    the remote calendar were built from — regenerating it would orphan every pushed event.
    """
    plan = by_slug(conn, slug)
    if plan is None:
        raise ValueError(f"no plan with slug {slug!r}")
    if new_name == plan.name:
        return plan

    conn.execute("UPDATE plans SET name = ? WHERE id = ?", (new_name, plan.id))
    for table in LABELLED_TABLES:
        conn.execute(f"UPDATE {table} SET plan_name = ? WHERE plan_id = ?",
                     (new_name, plan.id))
    return by_slug(conn, slug)


def retire(conn, slug: str) -> PlanRow:
    """
    Mark a plan finished. Its rows stay.

    A superseded plan keeps its past — the same rule the TrainerRoad cutover followed. Deleting
    it would take the adherence record the stored cycle reviews were written against.
    """
    plan = by_slug(conn, slug)
    if plan is None:
        raise ValueError(f"no plan with slug {slug!r}")
    conn.execute("UPDATE plans SET retired_at = ? WHERE id = ?", (utc_now_iso(), plan.id))
    return by_slug(conn, slug)


def orphans(conn) -> dict[str, int]:
    """
    Rows whose `plan_id` points at nothing, per table.

    A health check rather than a repair. Should always be zero: the migration backfilled every
    existing row and every writer sets it. A non-zero count means something wrote a plan row
    without going through here.
    """
    out = {}
    for table in LABELLED_TABLES:
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE plan_id IS NULL "
            f"OR plan_id NOT IN (SELECT id FROM plans)").fetchone()[0]
    return out


def resolve(conn, name: str, slug: str | None = None) -> str:
    """
    The slug to use for this plan, given a configured name and an optional configured slug.

    Three sources, in this order, and the order is the whole point:

    1. **The configured slug**, when one is set. Explicit identity always wins.
    2. **The slug already in the database**, when there is exactly one plan. This is what makes
       renaming safe for someone who never set a slug: config says the plan is now called
       "My Training Plan", the database says the plan it has is `sustainable-rolling`, and the
       rename reaches the label without creating a second plan.
    3. **Derived from the name**, only when there is no plan at all — a genuine first run.

    Step 1 fixed this bug once already, by hardcoding `PLAN_SLUG`. Reading the name from a
    configuration file reopens it, because a file invites editing, so the fix has to survive
    without a hardcoded answer. Deriving from the name is now reachable only when there is
    nothing to orphan.

    With two or more plans and no configured slug there is no safe guess, so it raises rather
    than picking one — silently picking is exactly the failure being prevented.
    """
    if slug:
        return slug

    existing = active(conn)
    if len(existing) == 1:
        return existing[0].slug
    if not existing:
        return slug_for(name)

    named = by_name(conn, name)
    if named:
        return named.slug
    raise ValueError(
        f"{len(existing)} active plans and no plan.slug configured, so there is no way to tell "
        f"which one {name!r} means. Set plan.slug in the configuration file to one of: "
        f"{', '.join(p.slug for p in existing)}")

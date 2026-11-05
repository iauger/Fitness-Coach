"""
Plan-wide identifiers, read from the athlete's configuration file.

This was five module-level string constants until step 3 of the config refactor. They are now
functions over `src.config`, and the shape change is deliberate rather than incidental.

**Why functions and not module constants assigned from config.** A constant assigned at import
time is read into every default argument it appears in — `plan_name: str = PLAN_NAME` binds
once, when the module is first imported, and no later `reset()` or different config file can
reach it. That is the same defect that made a test fixture's `schema.DB_PATH` patch do nothing
and let tests write to the real database. So the defaults became `None`, resolved inside the
function body, and the only way to read a plan name is to call for it.

**Why this module still imports nothing from the project except config.** Three readers in
`src.analysis` need to filter `training_plan_weeks` by plan name (`cycle.py`, `derive.py`,
`weekly.py`), and `planning/blocks.py` imports `analysis/derive.py` — so a name living in
`blocks` would close an import cycle. `src.config` is a leaf for exactly the same reason, which
is what makes it safe to import here.

**What is not here: the slug.** Identity needs the database, not just the file, because config
holds a name that may be edited and the slug must not follow it. `plans.resolve` owns that, and
`configured_slug()` is only the override it consults first.
"""

from datetime import date

from src.config import config


def plan_name() -> str:
    """
    The plan's label. Free to change; nothing joins on it as an identity any more.

    No year in the default name, because the plan has no end date. A year-stamped second name
    is what made the prescribed -> planned join impossible before — `plan_build` wrote
    "Sustainable 2026-27" while `seed_training_plan` used "TrainerRoad 2026-27" — and that join
    is what `current_week`, `cycles`, `plan_summary`, `cycle_metrics` and `week_metrics` key on.
    """
    return config().plan.name


def configured_slug() -> str | None:
    """
    An explicitly configured plan slug, or None to let `plans.resolve` decide.

    Almost always None. Set it only when two plans exist and the name is ambiguous, because a
    slug written by hand is a slug that can be written wrongly — and a wrong one orphans every
    event already on the intervals.icu calendar, since the `external_id`s were built from it.
    """
    return config().plan.slug


def plan_start_monday() -> str:
    """
    The Monday the rolling plan became the plan of record, ISO.

    `plan_progression` holds decided weeks from here on and nothing before: replaying the gate
    over earlier history is a *counterfactual* (no gate was running then), and storing
    counterfactuals in an append-only decision log would make it impossible to tell a decision
    that was acted on from one that was merely computed. The earlier replay is used only to
    pick the starting rung.

    `src/db/migrations.py` keeps its own frozen copy of this date, deliberately and now
    necessarily: a migration that read it from a config file would stop replaying to the same
    database the moment someone edited the file.
    """
    return config().plan.start_monday.isoformat()


def goal_date() -> str | None:
    """
    The primary goal's date, ISO, or None when none is configured.

    Held as a plan setting rather than read from the `goals` table so a stored plan row's end
    date cannot drift when goals are edited, and deliberately *not* a taper target — nothing in
    the rotation peaks for it. `plan_end_date` on a `training_plan_weeks` row is labelled as a
    goal, not as a dated commitment.
    """
    d = config().plan.goal_date
    return d.isoformat() if d else None


def dated_goals() -> list[tuple[str, date, str]]:
    """
    Configured goals that have a date, soonest first, as `(description, date, priority)`.

    Replaces a hardcoded `GRAVEL_DATE`. The report used to name two events it had been told
    about in source; it now counts down to whatever the athlete listed, which is the only
    version of that line that means anything to a second user.
    """
    goals = [(g.description, g.date, g.priority) for g in config().goals if g.date]
    return sorted(goals, key=lambda g: g[1])

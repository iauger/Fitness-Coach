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


def available_days() -> tuple[int, ...] | None:
    """
    The weekdays training can happen on, 0 (Monday) to 6 (Sunday), or None for every day.

    `None` is not the same as a list of seven, though both mean the same week: it is what lets
    a config that says nothing generate exactly what it always has. Parsed here rather than in
    the generator so `blocks.py` deals in integers and never in the spellings someone typed.
    """
    from src.config import weekday

    named = config().plan.available_days
    if named is None:
        return None
    return tuple(sorted(weekday(d, "plan.available_days") for d in named))


def long_ride_day() -> int | None:
    """The weekday the long ride is pinned to, or None to let the generator choose."""
    from src.config import weekday

    named = config().plan.long_ride_day
    return None if named is None else weekday(named, "plan.long_ride_day")


def ride_limits() -> dict[int, int]:
    """
    The longest ride allowed on each weekday, for the days that have a limit. `{}` means none.

    A day's own entry beats the plan-wide `max_ride_min`; a day with neither is simply absent,
    not zero — absent means unlimited, and a limit of zero would mean the opposite.
    """
    from src.config import weekday

    plan = config().plan
    limits: dict[int, int] = {}
    if plan.max_ride_min is not None:
        limits = {day: plan.max_ride_min for day in range(7)}
    for name, minutes in (plan.day_max_ride_min or {}).items():
        limits[weekday(name, "plan.day_max_ride_min")] = minutes
    return limits


def limit_setting(day: int) -> str:
    """
    The setting to tell the athlete to raise for this weekday's limit.

    A message that says "raise plan.max_ride_min" to someone whose Saturday has its own entry
    sends them to edit a line that Saturday ignores.
    """
    from src.config import WEEKDAYS, weekday

    for name in (config().plan.day_max_ride_min or {}):
        if weekday(name, "plan.day_max_ride_min") == day:
            return f"plan.day_max_ride_min.{WEEKDAYS[day]}"
    return "plan.max_ride_min"


def max_ride_days() -> int:
    """
    The most days a week the ladder may ask for. A ceiling, never a target.

    History decides how many ride days a tier asks for; this only stops that climbing past the
    days that exist. Two things can be that limit — `plan.max_ride_days`, and the number of days
    in `plan.available_days` — and the lower wins, because a ladder built for four days in a
    week with three available would prescribe a week that cannot be placed. Seven is permitted
    by the first and is a bad idea for most people; that is a judgement for the athlete.

    Every reader that asks how many days there are goes through here. Reading the config field
    directly would honour one limit and ignore the other, which is the same split between a
    reader and a writer that the ladder had three times.
    """
    cap = config().plan.max_ride_days
    days = available_days()
    return cap if days is None else min(cap, len(days))


def ride_day_setting() -> str:
    """
    Which setting is the one stopping the ride-day count climbing, for a message that has to
    tell the athlete what to change. Naming the wrong one sends them to edit a line that does
    nothing.
    """
    days = available_days()
    if days is not None and len(days) < config().plan.max_ride_days:
        return "plan.available_days"
    return "plan.max_ride_days"


def strength_days() -> int:
    """Lifting days per week. Never trimmed by a tier, recovery week or re-entry week."""
    return config().plan.strength_days


def push_calendar() -> bool:
    """
    Whether to push the horizon to the intervals.icu calendar.

    Off is the setting that makes a second copy of this project safe to run against the same
    account: the plan slug and therefore every `external_id` would be identical, so a sandbox
    would quietly overwrite the real calendar rather than sitting beside it.
    """
    return config().integrations.intervals_icu.push_calendar

"""
Week boundaries — one definition, imported everywhere.

A training week is Monday-to-Sunday throughout this project: `training_plan_weeks.week_start_date`,
`weekly_summaries.week_start` and every rollup key on it. The arithmetic is one line, which is
exactly why it had been written out four separate times (`analysis/weekly.py`, `analysis/derive.py`,
`analysis/activities.py`, `planning/blocks.py`). The rolling plan generator keys prescriptions,
outcomes and tier decisions off these boundaries, so an off-by-one in any one copy would silently
credit a ride to the wrong week and move a tier.

This module is a leaf on purpose — no imports beyond the stdlib. `analysis/weekly.py` sits on top
of `analysis/cycle.py` and `analysis/training_plan.py`, so making it the canonical home would have
pulled that chain into `analysis/derive.py`, which `planning/blocks.py` imports.
"""

from datetime import date, timedelta


def monday_of(d: date) -> date:
    """The Monday of the week containing `d`. Idempotent on a Monday."""
    return d - timedelta(days=d.weekday())


def sunday_of(d: date) -> date:
    """The Sunday closing the week containing `d`."""
    return monday_of(d) + timedelta(days=6)


def last_completed_week(on: date | None = None) -> date:
    """Monday of the most recent week that has fully elapsed."""
    return monday_of(on or date.today()) - timedelta(weeks=1)


def mondays_between(start: date, end: date) -> list[date]:
    """
    Every Monday from the week containing `start` to the week containing `end`, inclusive.

    The reason this exists rather than letting callers group rows by week: a week with no logged
    activity produces no rows at all, so `GROUP BY week_start` silently omits it. Twelve of the
    62 weeks in the history this generator was designed against are empty, and those weeks are
    the signal — they are what the re-entry rule exists to detect. Anything walking weeks must
    walk the calendar, not the data.
    """
    cursor = monday_of(start)
    last = monday_of(end)
    out = []
    while cursor <= last:
        out.append(cursor)
        cursor += timedelta(weeks=1)
    return out

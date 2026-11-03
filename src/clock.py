"""
Time, in the two flavours this project actually needs.

A true leaf — stdlib only — so every layer can import it without creating a cycle.

Two distinctions that were being conflated:

**Audit timestamps vs. training dates.** `created_at` and `logged_at` record when a row was
written, and UTC is right for those. A check-in date, a session date or a plan week is about
*the athlete's day*, and must be local: `datetime.utcnow().date()` files an 8pm US Eastern
check-in under tomorrow, where it is then compared against activity dates that intervals.icu
reports in local time and against Monday boundaries computed from `date.today()`.

**Naive vs. aware.** `datetime.utcnow()` has been deprecated since 3.12 and this runs on 3.14,
but the obvious replacement changes the stored format: `datetime.now(UTC).isoformat()` appends
`+00:00`, and every existing row in `prescribed_sessions`, `cycle_reviews`, `weekly_summaries`
and `subjective_feel` is naive. Mixing the two shapes in one column breaks ordering and any
`fromisoformat` consumer that assumes naive. `utc_now_iso()` keeps the existing format while
dropping the deprecated call.
"""

from datetime import date, datetime, timezone


def utc_now() -> datetime:
    """Current UTC time, naive — the format already stored throughout the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """Audit timestamp for a `created_at` / `logged_at` column."""
    return utc_now().isoformat()


def today_local() -> date:
    """
    The athlete's today.

    Use for anything a human would call a date: a check-in, a session, a plan week. Never
    `utc_now().date()`, which rolls over mid-evening in the Americas.
    """
    return date.today()


def today_local_iso() -> str:
    return today_local().isoformat()

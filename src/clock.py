"""
Time, in the two flavours this project actually needs.

A true leaf — stdlib only — so every layer can import it without creating a cycle.

Two distinctions that were being conflated:

**Audit timestamps vs. training dates.** `created_at` and `logged_at` record when a row was
written, and UTC is right for those. A check-in date, a session date or a plan week is about
*the athlete's day*, and must be local: `datetime.utcnow().date()` files an 8pm US Eastern
check-in under tomorrow, where it is then compared against activity dates that intervals.icu
reports in local time and against Monday boundaries computed from `date.today()`.

**UTC underneath, the athlete's timezone on top.** Storage is UTC and always was. What
"today" means is a separate question, and it is the athlete's, not the server's:
`date.today()` returns the day where the *process* is running, which is correct on a laptop
and wrong on a server left in UTC, where an evening check-in in the Americas files under
tomorrow and is then compared against activity dates intervals.icu reports in local time.
`today_local()` now reads `athlete.timezone` from the configuration, defaulting to UTC.

**Naive vs. aware.** `datetime.utcnow()` has been deprecated since 3.12 and this runs on 3.14,
but the obvious replacement changes the stored format: `datetime.now(UTC).isoformat()` appends
`+00:00`, and every existing row in `prescribed_sessions`, `cycle_reviews`, `weekly_summaries`
and `subjective_feel` is naive. Mixing the two shapes in one column breaks ordering and any
`fromisoformat` consumer that assumes naive. `utc_now_iso()` keeps the existing format while
dropping the deprecated call.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# Default when nothing is configured, and the fallback whenever the configuration cannot be
# read. UTC is the honest choice for "we do not know where the athlete is".
DEFAULT_TIMEZONE = "UTC"


def utc_now() -> datetime:
    """Current UTC time, naive — the format already stored throughout the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """Audit timestamp for a `created_at` / `logged_at` column."""
    return utc_now().isoformat()


def athlete_timezone() -> ZoneInfo:
    """
    The configured timezone, or UTC.

    Deliberately forgiving, and deliberately lazy. This module is imported by the migration
    runner, so a configuration file that is missing, unreadable or invalid must not be able to
    stop the schema upgrading or a timestamp being written — the fallback is UTC and the work
    continues. Config problems are reported loudly where config is actually read; here they
    would only take down everything else.
    """
    try:
        from src.config import CONFIG_PATH, load

        # create_if_missing=False on purpose. Asking what day it is must not have the side
        # effect of writing a configuration file -- generating one belongs to a deliberate
        # first run, not to a timestamp. Without this, importing anything that touches the
        # clock silently created config/athlete.yaml from the example.
        return ZoneInfo(load(CONFIG_PATH, create_if_missing=False).athlete.timezone)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def today_local() -> date:
    """
    The athlete's today, in their configured timezone.

    Use for anything a human would call a date: a check-in, a session, a plan week. Never
    `utc_now().date()`, which rolls over mid-evening in the Americas, and no longer
    `date.today()`, which is the server's day rather than the athlete's.
    """
    return datetime.now(athlete_timezone()).date()


def today_local_iso() -> str:
    return today_local().isoformat()

"""
The unattended maintenance cycle: pull, roll, reconcile, push.

This exists because of the failure it is named after. The six-week data gap that started this
rebuild was not a modelling problem — nobody ran the sync. A coach reasoning from six-week-old
data gives advice about a person who no longer exists, and the first thing to automate is
therefore not the coaching, it is the freshness underneath it.

Order matters and is not arbitrary:

1. **Pull** activities and wellness. Everything below reads them.
2. **Roll** the horizon. The gate grades *measured* weekly volume, so rolling before the pull
   would decide a closed week from data this run was about to fetch — and a decision, once
   filed, is never revisited.
3. **Reconcile** local rows against remote events by `external_id`, before pushing anything.
   The push deletes by stored id; a session that has lost its id cannot clean up the event it
   created, it just creates another beside it. Reconciling first is what stops an unattended
   push from becoming an unattended duplicator.
4. **Push**, and only if something actually changed. A horizon that has not moved does not need
   fifteen deletes and fifteen creates every hour.

Steps 1-2 happen inside `incremental_sync`. Steps 3-4 are here.

**One cycle at a time, across processes.** The scheduler inside `serve.py` and a Windows
Scheduled Task can both fire, and SQLite in WAL mode tolerates many readers and exactly one
writer — two concurrent cycles produce "database is locked" at best and a half-applied horizon
at worst. The lock is a file because it has to hold between separate processes, which a
threading primitive cannot do.
"""

import os
import time
from contextlib import contextmanager
from datetime import date

from src.clock import today_local, utc_now_iso
from src.db.schema import DB_PATH, read_connection, write_connection
from src.planning.constants import plan_name as configured_plan_name, push_calendar

LOCK_PATH = DB_PATH.parent / "maintenance.lock"

# A cycle is a handful of HTTP requests and some SQLite writes; a minute is already generous.
# Past this the holder is assumed dead — a process killed mid-cycle leaves the file behind, and
# without an expiry that would disable all future runs silently, which is precisely the failure
# mode this module exists to prevent.
LOCK_STALE_SECONDS = 600


class AlreadyRunning(RuntimeError):
    """Another maintenance cycle holds the lock."""


@contextmanager
def _exclusive():
    """A cross-process lock, taken atomically, released on every path out."""
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age > LOCK_STALE_SECONDS:
            LOCK_PATH.unlink(missing_ok=True)
        else:
            raise AlreadyRunning(f"lock held for {age:.0f}s by {_holder()}")
    try:
        # O_EXCL so two processes racing here cannot both win.
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AlreadyRunning("lock taken concurrently") from exc
    try:
        os.write(fd, f"{os.getpid()} {utc_now_iso()}".encode())
        os.close(fd)
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _holder() -> str:
    try:
        return LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def needs_push(conn, today: date, plan_name: str | None = None) -> bool:
    """
    True when the remote calendar is out of step with the stored plan.

    Deliberately cheap and local. A session that has never been pushed has no
    `remote_event_id`; a session whose content changed has had its `pushed_at` cleared by the
    roller's upsert, which compares the columns the remote event actually shows. Anything else
    means the calendar already says what we would say, and pushing would delete fifteen events
    only to recreate them identically — every hour, forever.
    """
    plan_name = plan_name or configured_plan_name()
    row = conn.execute(
        "SELECT COUNT(*) n FROM prescribed_sessions "
        "WHERE plan_name = ? AND date >= ? "
        "  AND (remote_event_id IS NULL OR pushed_at IS NULL)",
        (plan_name, today.isoformat()),
    ).fetchone()
    return row["n"] > 0


def run_cycle(today: date | None = None, push_enabled: bool | None = None,
              plan_name: str | None = None, silent: bool = True) -> dict:
    """
    One full maintenance pass. Safe to call on a timer.

    Never raises for an expected failure: the network is down, the API is rate-limited, a
    scheduled run overlaps another. Those are recorded in `errors` and the next run tries
    again. An unattended job that dies on a transient failure stops being unattended.

    `push_enabled` defaults to `integrations.intervals_icu.push_calendar`. That setting was
    declared in `config.py` and documented in the committed example, and **read by nothing** —
    a field stating an intent the code did not implement, which is worse than no field. Found
    while looking for a safe way to run a second copy of this project against the same
    intervals.icu account: turning the push off was the obvious precaution and would have
    done nothing, and a second copy pushes the same `external_id`s, so it would have
    overwritten the real calendar.
    """
    today = today or today_local()
    plan_name = plan_name or configured_plan_name()
    if push_enabled is None:
        push_enabled = push_calendar()
        off_because = "push_calendar is off"
    else:
        off_because = "asked not to"
    result: dict = {"ran_at": utc_now_iso(), "errors": []}

    try:
        with _exclusive():
            from src.sync import incremental_sync

            result["sync"] = incremental_sync(silent=silent)
            result["errors"] += result["sync"].get("errors", [])

            if not push_enabled:
                result["push"] = {"skipped": off_because}
                return result

            try:
                result["push"] = _reconcile_and_push(today, plan_name)
            except Exception as exc:
                result["errors"].append(f"push: {exc}")
                result["push"] = {"error": str(exc)}
    except AlreadyRunning as exc:
        # Not an error worth alarming about: the other runner is doing the work.
        result["skipped"] = str(exc)
    except Exception as exc:
        result["errors"].append(f"cycle: {exc}")
    return result


def _reconcile_and_push(today: date, plan_name: str) -> dict:
    from src.intervals.client import IntervalsClient
    from src.planning.export import push, reconcile

    client = IntervalsClient()
    with write_connection() as conn:
        linked = reconcile(conn, client, today, plan_name)

    with read_connection() as conn:
        if not needs_push(conn, today, plan_name):
            return {**linked, "pushed": False, "reason": "calendar already matches the plan"}

    with write_connection() as conn:
        pushed = push(conn, client, today, plan_name, dry_run=False)
    return {**linked, "pushed": True,
            **{k: v for k, v in pushed.items() if k not in ("payload", "sessions")}}


def summarise(result: dict) -> str:
    """One line for a log the athlete might actually read."""
    if result.get("skipped"):
        return f"[maintenance] skipped — {result['skipped']}"
    sync = result.get("sync") or {}
    plan = sync.get("plan") or {}
    push = result.get("push") or {}
    bits = [
        f"activities {sync.get('activities', 0)}",
        f"wellness {sync.get('wellness', 0)}",
        f"plan T{plan.get('tier', '?')}",
    ]
    if push.get("pushed"):
        bits.append(f"pushed {push.get('created', 0)} (replaced {push.get('deleted', 0)})")
    elif push.get("relinked"):
        bits.append(f"relinked {push['relinked']}")
    elif push.get("skipped"):
        # Named rather than folded into "calendar unchanged". Those two look identical in a
        # log and mean opposite things: one is a push that found nothing to do, the other is
        # a push that never ran. Someone wondering why their sessions are not on their watch
        # needs to be able to tell which.
        bits.append(f"no push ({push['skipped']})")
    else:
        bits.append("calendar unchanged")
    line = f"[maintenance] {', '.join(bits)}"
    if result.get("errors"):
        line += f"  ERRORS: {'; '.join(result['errors'])}"
    return line

"""
The signal ledger: what the athlete has said about how life and training are going.

Every row is one *mention*, dated and graded, and rows are only ever added. A "thread" (the same
knee complaint over three weeks) is not stored: it is worked out when read, by grouping mentions
of the same kind and topic that fall within `THREAD_GAP_DAYS` of each other. That keeps writes
trivial and puts every judgement in pure functions below, which is where they can be tested.

Severity is graded by whoever records the signal (0 a passing mention, 1 worth knowing, 2
affecting training, 3 needs action now) and then *adjusted by code*: three mentions of the same
thing inside two weeks go up a level, and a mention that is not repeated fades from view. The
model never decides how long something stays on the coach's mind.

Nothing here changes a prescription. `watch_list` is what the coach is told and what the plan
screen will show; turning any of it into a plan change is a separate, deliberate step.
"""

from datetime import date, timedelta

from src.clock import today_local, today_local_iso, utc_now_iso
from src.db.schema import read_connection, write_connection

KINDS = (
    "illness", "injury_pain", "poor_sleep", "fatigue",
    "workouts_too_hard", "workouts_too_easy", "missed_sessions", "feeling_strong",
    "motivation_low", "motivation_high", "burnout",
    "work_event", "family_caregiving", "travel", "schedule_change", "stress",
    "wants_more", "wants_less", "goal_change", "preference",
    "nutrition", "gear", "other",
)
STATES = ("new", "ongoing", "improving", "resolved")
SEVERITY_LABELS = {0: "passing", 1: "worth knowing", 2: "affecting training", 3: "needs action"}

THREAD_GAP_DAYS = 28        # mentions further apart than this are separate threads
ESCALATE_COUNT = 3          # this many mentions ...
ESCALATE_WITHIN_DAYS = 14   # ... inside this window raise the severity one level
FADE_DAYS = 21              # a level-2 mention nobody repeated leaves the watch list after this
SILENT_DAYS = 28            # level 3, and anything repeated or ongoing, leaves after this silence

QUOTE_MAX = 280
TOPIC_MAX = 60


class SignalError(ValueError):
    """A signal that cannot be recorded as given."""


def _date(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise SignalError(f"{field} must be YYYY-MM-DD, not {value!r}") from None


def clean(kind: str, *, topic: str = "", severity: int = 1, state: str = "new", quote: str = "",
          on_date=None, start_date=None, end_date=None) -> dict:
    """
    Validate one signal, returning the fields as they will be stored.

    A kind we do not know becomes `other` rather than an error: the model will invent kinds, and
    the share of `other` is the signal that the list needs a new entry. Everything else that is
    wrong is refused, because a mis-graded or mis-dated row is worse than a missing one.
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        kind = "other"
    if isinstance(severity, bool) or not isinstance(severity, int) or not 0 <= severity <= 3:
        raise SignalError(f"severity must be an integer 0-3, not {severity!r}")
    state = (state or "new").strip().lower()
    if state not in STATES:
        raise SignalError(f"state must be one of {', '.join(STATES)}, not {state!r}")
    start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
    if start and end and end < start:
        raise SignalError("end_date is before start_date")
    return {
        "kind": kind,
        "topic": " ".join((topic or "").lower().split())[:TOPIC_MAX],
        "severity": severity,
        "state": state,
        "quote": " ".join((quote or "").split())[:QUOTE_MAX],
        "on_date": _date(on_date, "on_date") or today_local_iso(),
        "start_date": start,
        "end_date": end,
    }


def record(kind: str, *, source: str = "manual", source_ref: str | None = None,
           extractor_version: str | None = None, observed_at: str | None = None, **fields) -> int:
    """Add one mention and return its id. `fields` are those of `clean`."""
    row = clean(kind, **fields)
    with write_connection() as conn:
        cur = conn.execute(
            "INSERT INTO signals (observed_at, on_date, kind, topic, severity, state, quote, "
            "start_date, end_date, source, source_ref, extractor_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (observed_at or utc_now_iso(), row["on_date"], row["kind"], row["topic"],
             row["severity"], row["state"], row["quote"], row["start_date"], row["end_date"],
             source, source_ref, extractor_version))
        return cur.lastrowid


def dismiss(signal_id: int, reason: str = "") -> bool:
    """Soft-delete a signal. The row stays, so a wrong one can become a test case."""
    with write_connection() as conn:
        cur = conn.execute(
            "UPDATE signals SET dismissed_at = ?, dismissed_reason = ? "
            "WHERE id = ? AND dismissed_at IS NULL", (utc_now_iso(), reason.strip(), signal_id))
        return cur.rowcount == 1


def restore(signal_id: int) -> bool:
    with write_connection() as conn:
        cur = conn.execute("UPDATE signals SET dismissed_at = NULL, dismissed_reason = NULL "
                           "WHERE id = ? AND dismissed_at IS NOT NULL", (signal_id,))
        return cur.rowcount == 1


def _rows(*, since: str | None = None, until: str | None = None, kind: str | None = None,
          topic: str | None = None, min_severity: int = 0, dismissed: bool = False,
          conn=None) -> list[dict]:
    where, args = ["severity >= ?"], [min_severity]
    if not dismissed:
        where.append("dismissed_at IS NULL")
    if since:
        where.append("on_date >= ?")
        args.append(since)
    if until:
        where.append("on_date <= ?")
        args.append(until)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if topic:
        where.append("topic LIKE ?")
        args.append(f"%{topic.strip().lower()}%")
    sql = f"SELECT * FROM signals WHERE {' AND '.join(where)} ORDER BY on_date, id"
    if conn is not None:                  # the caller's own connection (the check-in status uses one)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    with read_connection() as own:
        return [dict(r) for r in own.execute(sql, args).fetchall()]


# -- threads and the watch list: pure, so the judgement is testable ------------------------------

def _days(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _escalated(dates: list[str]) -> bool:
    """Whether ESCALATE_COUNT mentions fall inside ESCALATE_WITHIN_DAYS of one another."""
    n = ESCALATE_COUNT
    return any(_days(dates[i + n - 1], dates[i]) <= ESCALATE_WITHIN_DAYS
               for i in range(len(dates) - n + 1))


def threads(rows: list[dict], today: str) -> list[dict]:
    """
    Group mentions into threads and grade each as of `today`.

    `severity` is the latest mention's grade, plus one if the thread has flared (see `_escalated`),
    capped at 3. `open` says whether it should still be on the coach's mind:

    - resolved (by its latest mention): never;
    - level 3, or any thread repeated or ongoing at level 2: until SILENT_DAYS without a mention;
    - level 2 mentioned once: until FADE_DAYS, then gone unless it is ongoing;
    - level 1: only while it keeps coming back;
    - level 0: never. A passing mention is kept in the ledger and not carried.
    """
    groups: dict[tuple[str, str], list[list[dict]]] = {}
    for r in sorted(rows, key=lambda r: (r["on_date"], r["id"])):
        runs = groups.setdefault((r["kind"], r["topic"]), [])
        if runs and _days(r["on_date"], runs[-1][-1]["on_date"]) <= THREAD_GAP_DAYS:
            runs[-1].append(r)
        else:
            runs.append([r])
    out = []
    for runs in groups.values():
        for run in runs:
            last, dates = run[-1], [m["on_date"] for m in run]
            flared = _escalated(dates)
            severity = min(3, last["severity"] + (1 if flared else 0))
            silent = _days(today, last["on_date"])         # negative for a date ahead: still fresh
            repeated = len(run) > 1
            if last["state"] == "resolved" or severity == 0:
                is_open = False
            elif severity == 3:
                is_open = silent <= SILENT_DAYS
            elif severity == 2:
                is_open = (silent <= SILENT_DAYS if repeated or last["state"] == "ongoing"
                           else silent <= FADE_DAYS)
            else:
                is_open = repeated and silent <= SILENT_DAYS
            out.append({
                "kind": last["kind"], "topic": last["topic"], "severity": severity,
                "graded": last["severity"], "escalated": flared, "state": last["state"],
                "count": len(run), "recurring": repeated,
                "first_seen": dates[0], "last_seen": dates[-1], "quote": last["quote"],
                "ids": [m["id"] for m in run], "open": is_open,
            })
    return out


def all_threads(today: str | None = None, conn=None) -> list[dict]:
    """Every thread in the ledger (dismissed rows excluded), graded as of `today`."""
    return threads(_rows(conn=conn), today or today_local().isoformat())


def watch_list(today: str | None = None, conn=None) -> list[dict]:
    """Open threads that are serious (level 2+) or recurring, most pressing first."""
    return watch_list_from(all_threads(today, conn))


def watch_list_from(grouped: list[dict]) -> list[dict]:
    shown = [t for t in grouped if t["open"] and (t["severity"] >= 2 or t["recurring"])]
    shown.sort(key=lambda t: (-t["severity"], -t["recurring"], -t["count"],
                              -date.fromisoformat(t["last_seen"]).toordinal()))
    return shown


def recent_threads(today: str | None = None) -> list[dict]:
    """
    Threads the extractor should know about so it can continue them rather than start a duplicate:
    anything not resolved and mentioned within SILENT_DAYS, whatever its level. Wider than the
    watch list on purpose: a level-1 knee twinge is not worth the coach's attention, but it is
    worth recognising when the athlete mentions it again.
    """
    today = today or today_local().isoformat()
    return [t for t in threads(_rows(), today)
            if t["state"] != "resolved" and _days(today, t["last_seen"]) <= SILENT_DAYS]


def recorded(source: str, source_ref: str) -> bool:
    """Whether anything has already been taken from this source (dismissed or not)."""
    with read_connection() as conn:
        return conn.execute("SELECT 1 FROM signals WHERE source = ? AND source_ref = ? LIMIT 1",
                            (source, source_ref)).fetchone() is not None


# -- reads for the coach, the screens and the tool ----------------------------------------------

def timeline(*, since: str | None = None, until: str | None = None, kind: str | None = None,
             topic: str | None = None, min_severity: int = 0, limit: int = 50,
             dismissed: bool = False) -> list[dict]:
    """Mentions, newest first."""
    found = _rows(since=since, until=until, kind=kind, topic=topic,
                  min_severity=min_severity, dismissed=dismissed)
    return list(reversed(found))[:max(1, limit)]


def summary(*, since: str | None = None, until: str | None = None, kind: str | None = None,
            topic: str | None = None, min_severity: int = 0, conn=None) -> list[dict]:
    """One line per kind and topic: how often, how bad, when. Worst first."""
    groups: dict[tuple[str, str], dict] = {}
    for r in _rows(since=since, until=until, kind=kind, topic=topic, min_severity=min_severity, conn=conn):
        g = groups.setdefault((r["kind"], r["topic"]), {
            "kind": r["kind"], "topic": r["topic"], "mentions": 0, "max_severity": 0,
            "first_seen": r["on_date"], "last_seen": r["on_date"]})
        g["mentions"] += 1
        g["max_severity"] = max(g["max_severity"], r["severity"])
        g["last_seen"] = max(g["last_seen"], r["on_date"])
        g["first_seen"] = min(g["first_seen"], r["on_date"])
    return sorted(groups.values(), key=lambda g: (-g["max_severity"], -g["mentions"], g["kind"]))


def digest(today: str | None = None, days: int = 28, conn=None) -> str:
    """One line on the recent window. Reads fine when the ledger is empty, which it will be."""
    today = today or today_local().isoformat()
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    per_kind: dict[str, list[int]] = {}          # kind -> [mentions, peak severity]
    for g in summary(since=since, until=today, min_severity=1, conn=conn):
        mentions, peak = per_kind.setdefault(g["kind"], [0, 0])
        per_kind[g["kind"]] = [mentions + g["mentions"], max(peak, g["max_severity"])]
    if not per_kind:
        return f"Nothing logged in the last {days} days."
    parts = [f"{k.replace('_', ' ')} x{n} (peak {p})"
             for k, (n, p) in sorted(per_kind.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))]
    return f"Last {days} days: " + ", ".join(parts) + "."

"""
The athlete's goals. One source: the `goals` table.

They used to live in two places that drifted. The config file's `goals:` list fed the plan
screen and the report's countdown; the table fed the coach's goal list and is what the coach
edits when someone says "add a goal" in chat. By the time this was noticed the two disagreed
about the gravel race (2027-06-18 and opt-in in one, 2027-05-01 and secondary in the other),
so the screen and the coach counted down to different days.

The table is the source of truth now, for the same reason `athlete_profile` is: it can be
edited from the app and from a conversation, and a config file cannot. The config's `goals:` is
a first-run seed (see `from_config`), used only while the table has no rows at all, so a new
install shows its configured goals and the first save moves them into the table.

Every function takes the connection, so a screen, the coach and the report all read the same
rows through the same code. Nothing else reads `config().goals`; a test holds that line.
"""

from datetime import date, datetime, timezone

from src.config import config

PRIORITIES = ("primary", "secondary", "opt_in")
_PRIORITY_ORDER = {p: i for i, p in enumerate(PRIORITIES)}


class GoalError(ValueError):
    """A goal that cannot be stored, with the reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(description, target_date, priority) -> tuple[str, str | None, str]:
    text = (description or "").strip()
    if not text:
        raise GoalError("a goal needs a description")
    if priority not in PRIORITIES:
        raise GoalError(f"priority {priority!r} is not one of {', '.join(PRIORITIES)}")
    if target_date in (None, ""):
        return text, None, priority
    try:
        return text, date.fromisoformat(str(target_date)).isoformat(), priority
    except ValueError:
        raise GoalError(f"{target_date!r} is not a date (expected YYYY-MM-DD)") from None


def from_config() -> list[dict]:
    """The configured goals, in the shape of a stored one but with no id: the first-run seed."""
    return [{"id": None, "description": g.description,
             "date": g.date.isoformat() if g.date else None, "priority": g.priority}
            for g in config().goals]


def active(conn) -> list[dict]:
    """
    Goals that are still live, soonest first, undated ones after, ties by importance.

    A table with *no rows at all* means a first run, so the configured goals stand in. A table
    whose goals have all been dropped or achieved is a deliberate empty list and stays empty;
    falling back there would bring back goals someone had just removed.
    """
    rows = conn.execute("SELECT id, description, target_date, priority, status FROM goals").fetchall()
    if not rows:
        live = from_config()
    else:
        live = [{"id": r["id"], "description": r["description"], "date": r["target_date"],
                 "priority": r["priority"] or "secondary"} for r in rows if r["status"] == "active"]
    live.sort(key=lambda g: (g["date"] is None, g["date"] or "",
                             _PRIORITY_ORDER.get(g["priority"], len(PRIORITIES)), g["id"] or 0))
    return live


def dated(conn) -> list[tuple[str, date, str]]:
    """Live goals that have a date, soonest first, as `(description, date, priority)`."""
    return [(g["description"], date.fromisoformat(g["date"]), g["priority"])
            for g in active(conn) if g["date"]]


def add(conn, description, target_date=None, priority="secondary") -> int:
    text, when, prio = _check(description, target_date, priority)
    now = _now()
    cur = conn.execute(
        "INSERT INTO goals (description, target_date, priority, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'active', ?, ?)", (text, when, prio, now, now))
    return cur.lastrowid


def update(conn, goal_id: int, **fields) -> None:
    """
    Change the named fields of one goal and leave the rest alone.

    Only what is passed changes. The coach's old `update` went through
    `COALESCE(?, priority)` with a default of "secondary", which made the COALESCE a no-op:
    renaming a goal silently reset its priority.
    """
    row = conn.execute("SELECT description, target_date, priority FROM goals WHERE id = ?",
                       (goal_id,)).fetchone()
    if row is None:
        raise GoalError(f"no goal with id {goal_id}")
    text, when, prio = _check(fields.get("description", row["description"]),
                              fields.get("date", row["target_date"]),
                              fields.get("priority", row["priority"] or "secondary"))
    conn.execute("UPDATE goals SET description = ?, target_date = ?, priority = ?, updated_at = ? "
                 "WHERE id = ?", (text, when, prio, _now(), goal_id))


def set_status(conn, goal_id: int, status: str) -> None:
    if status not in ("active", "achieved", "dropped"):
        raise GoalError(f"unknown status {status!r}")
    conn.execute("UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
                 (status, _now(), goal_id))


def sync(conn, goals: list[dict]) -> list[dict]:
    """
    Make the live goals equal to this list: the edit a screen makes.

    A goal with an `id` is updated, one without is added, and a live goal that is not in the
    list is dropped (not deleted: it stays in the table as `dropped`, which is what the coach
    sees as history). Everything is checked before anything is written, so one bad row leaves
    the table as it was.
    """
    cleaned = []
    for i, g in enumerate(goals):
        try:
            cleaned.append((g.get("id"), *_check(g.get("description"), g.get("date"),
                                                 g.get("priority", "secondary"))))
        except GoalError as exc:
            raise GoalError(f"goal {i + 1}: {exc}") from None

    existing = {r["id"] for r in conn.execute("SELECT id FROM goals WHERE status = 'active'")}
    keep = {gid for gid, *_ in cleaned if gid is not None}
    unknown = keep - existing
    if unknown:
        raise GoalError(f"no live goal with id {sorted(unknown)[0]}")

    for gid, text, when, prio in cleaned:
        if gid is None:
            add(conn, text, when, prio)
        else:
            update(conn, gid, description=text, date=when, priority=prio)
    for gid in existing - keep:
        set_status(conn, gid, "dropped")
    return active(conn)

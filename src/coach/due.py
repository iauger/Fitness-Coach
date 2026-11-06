"""
When a check-in is worth having: the weekly one and the block review.

Nothing here calls the model. It reads what is already recorded and says whether the athlete
would get something from a check-in now, and why, so the app can show a status instead of
relying on remembering to ask. The old rule was "run it when you think of it", which is how the
last weekly check-in came to be 65 days ago.

**Weekly.** Due when a week has closed since the last check-in; overdue when two have, or two
weeks have passed, or there has never been one. A progression event (the gate moved the tier up
or down, or sent the athlete to a re-entry week) is its own reason, because those are the weeks
most worth talking about. Never within `MIN_GAP_DAYS` of the last one, whatever the trigger:
a status that nags the day after a check-in teaches people to ignore it.

**Block review.** Due when a block of the *current* plan has closed with no review filed. Only
the most recent closed block is offered (the review compares against the previous one, so an
older skipped one is history). Superseded plans are ignored: the TrainerRoad blocks are not
something to review now. Before the block closes it says when that will be.

Each reason is a sentence for the athlete, in the order the page shows them.
"""

from datetime import date, timedelta

from src.analysis.dates import last_completed_week
from src.planning.constants import plan_name

# A week that closed after the last check-in makes the next one due; this many days (or this many
# closed weeks) without one makes it overdue.
OVERDUE_DAYS = 14
OVERDUE_WEEKS = 2
# Never prompt again this soon after a check-in, whatever else has happened.
MIN_GAP_DAYS = 2
# A block review this many days after the block closed is overdue.
REVIEW_OVERDUE_DAYS = 14


def _last_checkin(conn) -> tuple[date, str] | None:
    row = conn.execute("SELECT date, created_at FROM coaching_log WHERE type = 'checkin' "
                       "ORDER BY date DESC, created_at DESC LIMIT 1").fetchone()
    return (date.fromisoformat(row["date"]), row["created_at"]) if row else None


def _weeks_closed_since(last: date | None, today: date) -> int:
    """How many whole Monday-to-Sunday weeks have ended after `last` (all of them, capped)."""
    newest = last_completed_week(today)                    # Monday of the latest closed week
    if last is None:
        return OVERDUE_WEEKS
    count, monday = 0, newest
    while monday + timedelta(days=6) > last and count < OVERDUE_WEEKS:
        count += 1
        monday -= timedelta(weeks=1)
    return count


def _progression_since(conn, created_after: str | None) -> list[str]:
    """Plain-language notes for gate decisions recorded since the last check-in."""
    sql = ("SELECT week_start, verdict, tier_applied, tier_next, week_type_next, reason "
           "FROM plan_progression WHERE plan_name = ?")
    args: list = [plan_name()]
    if created_after:
        sql += " AND created_at > ?"
        args.append(created_after)
    notes = []
    for r in conn.execute(sql + " ORDER BY week_start", args):
        if r["verdict"] == "advance":
            notes.append(f"You moved up from T{r['tier_applied']} to T{r['tier_next']} "
                         f"after the week of {r['week_start']}.")
        elif r["verdict"] == "step_back":
            notes.append(f"The plan eased you from T{r['tier_applied']} to T{r['tier_next']} "
                         f"after the week of {r['week_start']}.")
        elif r["week_type_next"] == "reentry":
            notes.append(f"A week was missed ({r['week_start']}), so the next one comes back easy.")
    return notes


def checkin_status(conn, today: date) -> dict:
    last = _last_checkin(conn)
    last_date, last_created = (last if last else (None, None))
    days_since = (today - last_date).days if last_date else None
    closed = _weeks_closed_since(last_date, today)
    reasons: list[str] = []

    if last_date is None:
        reasons.append("You have not done a weekly check-in yet.")
    elif closed >= OVERDUE_WEEKS:
        reasons.append(f"It has been {days_since} days since your last check-in, and "
                       f"{OVERDUE_WEEKS} or more weeks have closed.")
    elif closed:
        reasons.append(f"A week has closed since your last check-in ({days_since} "
                       f"day{'s' if days_since != 1 else ''} ago).")

    events = _progression_since(conn, last_created)
    too_soon = days_since is not None and days_since < MIN_GAP_DAYS
    if too_soon:
        state, reasons, events = "ok", [], []
    elif last_date is None or (days_since or 0) >= OVERDUE_DAYS or closed >= OVERDUE_WEEKS:
        state = "overdue"
    elif closed or events:
        state = "due"
    else:
        state = "ok"
    if state != "ok":
        reasons += events

    return {"kind": "checkin", "state": state, "reasons": reasons,
            "last_date": last_date.isoformat() if last_date else None,
            "days_since": days_since, "weeks_closed": closed,
            "week_to_cover": last_completed_week(today).isoformat()}


def review_status(conn, today: date, cycle_list: list[dict] | None = None) -> dict:
    if cycle_list is None:
        from src.analysis.training_plan import cycles
        cycle_list = cycles(plan_name())
    ours = [c for c in cycle_list if c.get("plan_name", plan_name()) == plan_name()]
    closed = [c for c in ours if c["closed"] and c["end_date"] < today.isoformat()]
    # The block still running, whether or not its recovery week is seeded yet. The rolling plan
    # seeds only its three-week horizon, so a block whose last week is already in view reads as
    # "closed" the whole time it is being ridden: the end date, not the flag, says it is over.
    openc = next((c for c in ours if c["end_date"] >= today.isoformat()), None)

    out = {"kind": "cycle", "state": "ok", "reasons": [], "cycle": None, "days_since_close": None,
           "next_close": None}
    if openc is not None:
        out["next_close"] = openc["end_date"]
    if not closed:
        if openc is not None:
            out["reasons"] = [f"Your current block runs to {openc['end_date']}; the review "
                              f"becomes available once it closes."]
        return out

    latest = closed[-1]
    reviewed = conn.execute("SELECT 1 FROM cycle_reviews WHERE plan_name = ? AND cycle_start = ?",
                            (latest["plan_name"], latest["start_date"])).fetchone()
    if reviewed:
        return out
    since = (today - date.fromisoformat(latest["end_date"])).days
    out.update(
        state="overdue" if since >= REVIEW_OVERDUE_DAYS else "due",
        days_since_close=since,
        cycle={"start": latest["start_date"], "end": latest["end_date"],
               "weeks": latest["weeks"], "phase": latest.get("phase")},
        reasons=[f"Your {latest['weeks']}-week block ({latest['start_date']} to "
                 f"{latest['end_date']}) closed {since} day{'s' if since != 1 else ''} ago and has "
                 f"no review yet."])
    return out


def status(conn, today: date, cycle_list: list[dict] | None = None) -> dict:
    checkin = checkin_status(conn, today)
    review = review_status(conn, today, cycle_list)
    return {"checkin": checkin, "review": review,
            "attention": sum(1 for s in (checkin, review) if s["state"] != "ok")}

"""
What the coach is told about the athlete's own account of things.

The ledger (`signals.py`) holds every mention; this turns it into a short block of the briefing:
the watch list, a one-line digest of the last four weeks, and the long-running themes that are
not on the watch list. About 400 tokens when it is full, and a few lines when it is empty, which
for months it will be.

It is built from code, not by a model. No model writes a summary of the athlete that the next model
then reads as fact: the picture is derived from threads, so it changes the moment a thread does.

The header says what the block is, because the coach must treat it differently from the data above
it: filed automatically from the athlete's words, possibly wrong, theirs to delete, and never the
last word where it disagrees with the numbers.
"""

from datetime import date

from src import signals
from src.clock import today_local
from src.planning.advisories import plan_advisories

WATCH_MAX = 6
THEMES_MAX = 4
QUOTE_CHARS = 100
THEME_MIN_MENTIONS = 3
THEME_MIN_SPAN_DAYS = 28
THEME_RECENT_DAYS = 90

HEADER = ("WHAT THE ATHLETE HAS TOLD YOU (filed automatically from their own words, so it can be "
          "wrong; they can see and delete every entry. Say \"you mentioned...\", never diagnose. "
          "Where this disagrees with the data above, the data wins.)")


def age(day: str, today: str) -> str:
    n = (date.fromisoformat(today) - date.fromisoformat(day)).days
    if n <= 0:
        return "today"
    return "yesterday" if n == 1 else f"{n} days ago"


def _name(t: dict) -> str:
    kind = t["kind"].replace("_", " ")
    return f"{kind} / {t['topic']}" if t["topic"] else kind


def _quote(t: dict) -> str:
    q = t["quote"]
    return f' "{q[:QUOTE_CHARS]}{"..." if len(q) > QUOTE_CHARS else ""}"' if q else ""


def watch_line(t: dict, today: str) -> str:
    rising = ", rising" if t["escalated"] else ""
    mentions = f"{t['count']} mention{'s' if t['count'] != 1 else ''}"
    span = t["first_seen"] if t["first_seen"] == t["last_seen"] else f"{t['first_seen']} to {t['last_seen']}"
    return (f"  - {_name(t)}: {signals.SEVERITY_LABELS[t['severity']]}{rising}, {t['state']}; "
            f"{mentions}, {span}, last {age(t['last_seen'], today)}.{_quote(t)}")


def themes(all_threads: list[dict], today: str) -> list[dict]:
    """
    Long-running threads that are not already on the watch list: mentioned often, or over a long
    stretch, and not resolved. This is the derived picture of the athlete; nothing writes it.
    """
    on_watch = {tuple(t["ids"]) for t in signals.watch_list_from(all_threads)}
    out = []
    for t in all_threads:
        if t["state"] == "resolved" or tuple(t["ids"]) in on_watch:
            continue
        span = (date.fromisoformat(t["last_seen"]) - date.fromisoformat(t["first_seen"])).days
        recent = (date.fromisoformat(today) - date.fromisoformat(t["last_seen"])).days <= THEME_RECENT_DAYS
        if recent and (t["count"] >= THEME_MIN_MENTIONS or span >= THEME_MIN_SPAN_DAYS):
            out.append(t)
    return sorted(out, key=lambda t: (-t["count"], t["first_seen"]))[:THEMES_MAX]


def advisory_lines(advisories: list[dict]) -> list[str]:
    """What the signals would do to the plan, and a plain statement that they have not."""
    if not advisories:
        return []
    return (["  If these were allowed to change the plan they would (NOT applied; the plan is unchanged, "
             "and you must not tell the athlete it has changed):"]
            + [f"  - {a['reason']}" for a in advisories])


def strip(today: str, week_type: str | None = None, conn=None) -> dict:
    """The same picture for the Plan screen: the watch list and what it would do, not applied."""
    watch = signals.watch_list(today, conn)
    return {"watch": [{**t, "label": signals.SEVERITY_LABELS[t["severity"]], "age": age(t["last_seen"], today)}
                      for t in watch[:WATCH_MAX]],
            "advisories": plan_advisories(watch, {"week_type": week_type}),
            "digest": signals.digest(today, conn=conn)}


def block(today: str | None = None, week_type: str | None = None) -> str:
    today = today or today_local().isoformat()
    every = signals.all_threads(today)
    full_watch = signals.watch_list_from(every)
    watch = full_watch[:WATCH_MAX]
    lines = [HEADER]
    if watch:
        lines.append("  Watch list (most pressing first):")
        lines += [watch_line(t, today) for t in watch]
    else:
        lines.append("  Watch list: nothing open.")
    lines += advisory_lines(plan_advisories(full_watch, {"week_type": week_type}))
    lines.append(f"  {signals.digest(today)}")
    picture = themes(every, today)
    if picture:
        lines.append("  Long-running themes:")
        lines += [f"  - {_name(t)}: {t['count']} mentions since {t['first_seen']}, last "
                  f"{age(t['last_seen'], today)} ({t['state']})" for t in picture]
    return "\n".join(lines)

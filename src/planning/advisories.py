"""
What the athlete's own account would do to the plan, if it were allowed to.

It is not allowed to. `plan_advisories` reads the watch list and says what each open signal would
argue for, with the reason and the ids of the mentions behind it, and every advisory carries
`applied: False`. They are shown (on the Plan screen, in the coach's briefing) as "what the
signals would do, not applied", which is also how their quality is judged before anything acts: a
wrongly graded "burnout" must not reshape a plan, and the only way to know how often that
happens is to watch what it would have done.

The way back in is deliberately narrow. `progression.next_state` accepts advisories, and honours
one only if it is marked `applied` *and* is an action that holds the athlete where they are. Nothing
marks one applied today; turning an action on later is one config entry per action
(`plan.signal_effects`, absent until something reads it). Two rules that will still hold then:
an action that raises load (`consider_harder`) can never be applied from a single signal, and
every applied advisory is written beside the decision it changed.

Pure: no database, no clock. The watch list and the gate state come in as arguments.
"""

ACTIONS = ("ease", "hold", "add_recovery", "consider_harder")

# Actions that make the plan easier or keep it where it is. Only these may ever be applied on a
# single signal's say-so; `consider_harder` asks for more, and a good week is not a reason.
EASIER = {"ease", "hold", "add_recovery"}

_HEALTH = {"illness", "injury_pain", "burnout"}
_STRAIN = {"illness", "injury_pain", "burnout", "workouts_too_hard", "motivation_low", "missed_sessions"}
_LOAD = {"poor_sleep", "fatigue", "stress", "family_caregiving", "work_event"}
_ROOM = {"workouts_too_easy", "feeling_strong"}

_TEXT = {
    "ease": "ease the week",
    "hold": "hold the tier this week rather than step up",
    "add_recovery": "add recovery",
    "consider_harder": "consider asking for more",
}


def _wants(thread: dict) -> str | None:
    kind, severity = thread["kind"], thread["severity"]
    if kind in _HEALTH and severity >= 3:
        return "ease"
    if kind in _STRAIN and severity >= 2:
        return "hold"
    if kind in _LOAD and (severity >= 2 or thread["recurring"]):
        return "add_recovery"
    if kind in _ROOM and thread["recurring"]:
        return "consider_harder"
    return None


def plan_advisories(watch: list[dict], gate: dict) -> list[dict]:
    """
    One advisory per action that at least one open thread argues for.

    `watch` is `signals.watch_list`; `gate` is the gate's state (`week_type` is read: nothing
    asks for more in a re-entry week). Each thread argues for its strongest action only, and
    `consider_harder` is dropped whenever anything argues for an easier one.
    """
    grouped: dict[str, list[dict]] = {a: [] for a in ACTIONS}
    for thread in watch:
        action = _wants(thread)
        if action:
            grouped[action].append(thread)
    if any(grouped[a] for a in EASIER) or gate.get("week_type") == "reentry":
        grouped["consider_harder"] = []
    out = []
    for action in ACTIONS:
        threads = grouped[action]
        if not threads:
            continue
        names = ", ".join(sorted({t["kind"].replace("_", " ") + (f" ({t['topic']})" if t["topic"] else "")
                                  for t in threads}))
        out.append({"action": action, "easier": action in EASIER, "applied": False,
                    "label": _TEXT[action], "reason": f"{names}: would {_TEXT[action]}.",
                    "signal_ids": sorted({i for t in threads for i in t["ids"]})})
    return out

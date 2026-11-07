"""
Tool definitions and implementations for the coaching agent.
Each tool has a schema (sent to Claude) and an execute function (called locally).
"""

import json
from datetime import date
from src import signals
from src.coach.suggest import suggest_change
from src.db.schema import read_connection, write_connection
from src.clock import today_local, today_local_iso, utc_now_iso

NOW = utc_now_iso


# ── Tool schemas (sent to Claude) ─────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "log_life_event",
        "description": (
            "Log a life event that affects training context — vacation, illness, injury, "
            "life stress, or gear change. Call this when the athlete mentions something "
            "that explains a training gap or affects load capacity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["vacation", "illness", "injury", "life_stress", "gear_change", "other"],
                    "description": "Category of the life event",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format (omit if ongoing or unknown)",
                },
                "severity": {
                    "type": "string",
                    "enum": ["mild", "moderate", "severe"],
                    "description": "How much this affected training capacity",
                },
                "note": {
                    "type": "string",
                    "description": "Brief description in the athlete's own words",
                },
            },
            "required": ["type", "start_date"],
        },
    },
    {
        "name": "log_subjective_feel",
        "description": (
            "Log how the athlete is feeling right now as numbers on a 1-10 scale: overall, "
            "energy, motivation, leg freshness. Call this when the athlete gives you such a "
            "rating. What they say in words is filed on its own when the conversation ends."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feel_score": {
                    "type": "integer",
                    "description": "Overall feeling 1-10 (1=terrible, 10=amazing)",
                },
                "energy": {
                    "type": "integer",
                    "description": "Energy level 1-10",
                },
                "motivation": {
                    "type": "integer",
                    "description": "Motivation to train 1-10",
                },
                "legs": {
                    "type": "integer",
                    "description": "Leg freshness 1-10 (1=dead, 10=fresh)",
                },
                "notes": {
                    "type": "string",
                    "description": "Any additional context in the athlete's words",
                },
                "date": {
                    "type": "string",
                    "description": "Date this applies to in YYYY-MM-DD format (defaults to today)",
                },
            },
            "required": ["feel_score"],
        },
    },
    {
        "name": "query_history",
        "description": (
            "Query the athlete's historical training and wellness data. Use this to answer "
            "specific questions about past periods — e.g. peak CTL windows, what training "
            "looked like at a specific time, HRV during a past illness, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": [
                        "ctl_range",
                        "activities_in_period",
                        "wellness_in_period",
                        "peak_ctl_periods",
                        "life_events",
                        "subjective_feel",
                        "goals",
                        "plan_compliance",
                    ],
                    "description": "What to query",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date YYYY-MM-DD",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date YYYY-MM-DD",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 20)",
                },
            },
            "required": ["query_type"],
        },
    },
    {
        "name": "query_signals",
        "description": (
            "Look up what the athlete has told you about how they are doing: illness, injury or "
            "pain, sleep, fatigue, motivation, work and family demands, travel, how sessions "
            "felt, nutrition, gear, and wishes for more or less training. Each entry is dated and "
            "graded 0-3 (0 passing, 1 worth knowing, 2 affecting training, 3 needs action). The "
            "briefing carries only the watch list and the last four weeks; use this for anything "
            "longer: 'has the knee come up before?', 'how has sleep been since September?'. "
            "These are the athlete's words as filed, not measurements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["summary", "timeline", "watch"],
                    "description": ("summary: one line per kind and topic with counts; timeline: the "
                                    "individual mentions, newest first; watch: the open threads "
                                    "worth attention now. Default summary."),
                },
                "kind": {"type": "string", "enum": list(signals.KINDS), "description": "Only this kind"},
                "topic": {"type": "string", "description": "Only topics containing this text, e.g. 'knee'"},
                "min_severity": {"type": "integer", "minimum": 0, "maximum": 3,
                                 "description": "Only this severity or worse (default 1)"},
                "start_date": {"type": "string", "description": "From this date YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "To this date YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Max rows (default 20)"},
            },
        },
    },
    {
        "name": "suggest_change",
        "description": (
            "Offer the athlete a button to change one plan limit: how many days a week the plan "
            "may ask them to ride, or the longest ride on any day. Use it only when they have "
            "asked for more or less, or when what they have told you and the data clearly point "
            "that way (they say the sessions feel too easy and the limit is what holds them "
            "back). One suggestion at a time. Nothing changes until they press Apply, and the "
            "ladder is then re-derived on its own screen where they review it. Their current "
            "limits are in the snapshot; the tool refuses a value that is the current one or "
            "that cannot fit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "setting": {"type": "string", "enum": ["plan.max_ride_days", "plan.max_ride_min"],
                            "description": "Which limit"},
                "value": {"type": "integer", "description": "The new value (days, or minutes)"},
                "reason": {"type": "string",
                           "description": "One or two sentences for the athlete, shown beside the button"},
            },
            "required": ["setting", "value", "reason"],
        },
    },
    {
        "name": "calculate_ramp_target",
        "description": (
            "Calculate the weekly CTL ramp rate required to reach a target CTL by a goal date, "
            "and assess whether it's physiologically realistic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_ctl": {
                    "type": "number",
                    "description": "Desired CTL to achieve",
                },
                "target_date": {
                    "type": "string",
                    "description": "Goal date in YYYY-MM-DD format",
                },
            },
            "required": ["target_ctl", "target_date"],
        },
    },
    {
        "name": "update_goal",
        "description": (
            "Add, update, or mark a goal as achieved or dropped. Use when the athlete "
            "mentions a new target event, changes a goal date, or achieves something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "achieve", "drop"],
                },
                "description": {
                    "type": "string",
                    "description": "Goal description",
                },
                "target_date": {
                    "type": "string",
                    "description": "Target date YYYY-MM-DD",
                },
                "priority": {
                    "type": "string",
                    "enum": ["primary", "secondary"],
                },
                "goal_id": {
                    "type": "integer",
                    "description": "Existing goal ID for update/achieve/drop actions",
                },
            },
            "required": ["action"],
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

# The tool keeps its original vocabulary; the ledger has its own (src/signals.py).
_LIFE_EVENT_KINDS = {"vacation": "travel", "illness": "illness", "injury": "injury_pain",
                     "life_stress": "stress", "gear_change": "gear", "other": "other"}
_LIFE_EVENT_SEVERITY = {"mild": 1, "moderate": 2, "severe": 3}


def log_life_event(type: str, start_date: str, end_date: str = None,
                   severity: str = None, note: str = None,
                   logged_via: str = "chat") -> dict:
    """Record a life event. It is a signal like any other; the ledger is the one place they live."""
    from src import signals
    try:
        signals.record(
            _LIFE_EVENT_KINDS.get(type, "other"), source="coach_tool", source_ref=logged_via,
            severity=_LIFE_EVENT_SEVERITY.get(severity, 1), quote=note or "",
            on_date=start_date, start_date=start_date, end_date=end_date,
            state="resolved" if end_date and end_date <= today_local_iso() else "ongoing")
    except signals.SignalError as e:
        return {"error": str(e)}
    end_str = f" to {end_date}" if end_date else ""
    return {"status": "logged", "event": f"{type}{end_str}: {note or ''}"}


def log_subjective_feel(feel_score: int, energy: int = None, motivation: int = None,
                        legs: int = None, notes: str = None,
                        date: str = None, logged_via: str = "chat") -> dict:
    target_date = date or today_local_iso()
    with write_connection() as conn:
        conn.execute("""
            INSERT INTO subjective_feel
            (date, feel_score, energy, motivation, legs, notes, logged_at, logged_via)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (target_date, feel_score, energy, motivation, legs, notes, NOW(), logged_via))
    return {"status": "logged", "date": target_date, "feel_score": feel_score}


def query_history(query_type: str, start_date: str = None, end_date: str = None,
                  limit: int = 20) -> dict:
    """
    Answer a question about a past window, and say so when the answer is incomplete.

    The three period queries used `.fetchmany(limit)` over a query ordered oldest-first, so
    they returned the *beginning* of the window rather than a representative sample, and
    reported `count` as though that were all there was. Asking about the last three months
    returned 20 of 32 activities spanning 2026-08-04 to 2026-09-08 — the first five weeks of
    thirteen, stopping immediately before the September dropout. The model had no way to tell,
    so it would have concluded training was consistent and never seen the gap it was asked
    about.

    Now the limit is applied in SQL against the newest rows, the result is handed back
    oldest-first for readability, and `total` plus `truncated` say what was left out.
    """
    with read_connection() as conn:
        end = end_date or today_local().isoformat()
        start = start_date or "2017-01-01"

        total = None

        if query_type == "ctl_range":
            total = conn.execute(
                "SELECT COUNT(*) n FROM wellness WHERE date BETWEEN ? AND ? "
                "AND ctl IS NOT NULL", (start, end)).fetchone()["n"]
            rows = conn.execute("""
                SELECT date, ctl, atl, round(ctl-atl,1) as tsb
                FROM wellness WHERE date BETWEEN ? AND ? AND ctl IS NOT NULL
                ORDER BY date DESC LIMIT ?
            """, (start, end, limit)).fetchall()
            result = [dict(r) for r in reversed(rows)]

        elif query_type == "activities_in_period":
            total = conn.execute(
                "SELECT COUNT(*) n FROM activities WHERE date BETWEEN ? AND ?",
                (start, end)).fetchone()["n"]
            rows = conn.execute("""
                SELECT date, name, type, round(moving_time/60.0,0) as duration_min,
                       round(distance/1000.0,1) as distance_km, avg_hr
                FROM activities WHERE date BETWEEN ? AND ?
                ORDER BY date DESC LIMIT ?
            """, (start, end, limit)).fetchall()
            result = [dict(r) for r in reversed(rows)]

        elif query_type == "wellness_in_period":
            total = conn.execute(
                "SELECT COUNT(*) n FROM wellness WHERE date BETWEEN ? AND ?",
                (start, end)).fetchone()["n"]
            rows = conn.execute("""
                SELECT date, ctl, atl, rhr, hrv, sleep_hrs, sleep_score
                FROM wellness WHERE date BETWEEN ? AND ?
                ORDER BY date DESC LIMIT ?
            """, (start, end, limit)).fetchall()
            result = [dict(r) for r in reversed(rows)]

        elif query_type == "peak_ctl_periods":
            rows = conn.execute("""
                SELECT date, ctl, atl, round(ctl-atl,1) as tsb
                FROM wellness WHERE ctl IS NOT NULL
                ORDER BY ctl DESC LIMIT ?
            """, (limit,)).fetchall()
            result = [dict(r) for r in rows]

        elif query_type == "life_events":
            from src import signals
            result = [{"date": r["on_date"], "end_date": r["end_date"], "kind": r["kind"],
                       "topic": r["topic"], "severity": r["severity"], "state": r["state"],
                       "note": r["quote"]} for r in signals.timeline(limit=limit)]

        elif query_type == "subjective_feel":
            rows = conn.execute("""
                SELECT date, feel_score, energy, motivation, legs, notes
                FROM subjective_feel WHERE date BETWEEN ? AND ?
                ORDER BY date DESC LIMIT ?
            """, (start, end, limit)).fetchall()
            result = [dict(r) for r in rows]

        elif query_type == "goals":
            rows = conn.execute("""
                SELECT id, description, target_date, priority, status
                FROM goals ORDER BY priority, target_date
            """).fetchall()
            result = [dict(r) for r in rows]

        elif query_type == "plan_compliance":
            rows = conn.execute("""
                SELECT date, name, planned_tss, planned_duration_min,
                       compliance_status, compliance_pct
                FROM planned_workouts WHERE date BETWEEN ? AND ?
                ORDER BY date DESC LIMIT ?
            """, (start, end, limit)).fetchall()
            result = [dict(r) for r in rows]

        else:
            result = []

    out = {"query_type": query_type, "rows": result, "count": len(result)}
    if total is not None and total > len(result):
        # Stated in the payload the model reads, not only in a log. A silently truncated
        # window is the one failure here that produces confident wrong answers.
        out["total"] = total
        out["truncated"] = True
        out["note"] = (f"showing the {len(result)} most recent of {total}; "
                       f"raise limit or narrow the dates for the rest")
    return out


def calculate_ramp_target(target_ctl: float, target_date: str) -> dict:
    with read_connection() as conn:
        row = conn.execute("""
            SELECT ctl FROM wellness WHERE ctl IS NOT NULL ORDER BY date DESC LIMIT 1
        """).fetchone()

    current_ctl = row["ctl"] if row else 0
    today = today_local()
    goal = date.fromisoformat(target_date)
    weeks = max((goal - today).days / 7, 1)
    ctl_gap = target_ctl - current_ctl
    weekly_ramp = ctl_gap / weeks

    if weekly_ramp <= 2:
        assessment = "conservative and very achievable"
    elif weekly_ramp <= 4:
        assessment = "solid and realistic with consistent training"
    elif weekly_ramp <= 6:
        assessment = "aggressive — requires near-perfect consistency, no gaps"
    else:
        assessment = "unrealistic — consider extending the timeline or lowering the target"

    return {
        "current_ctl": round(current_ctl, 1),
        "target_ctl": target_ctl,
        "target_date": target_date,
        "weeks_available": round(weeks, 1),
        "required_weekly_ramp": round(weekly_ramp, 2),
        "assessment": assessment,
    }


def update_goal(action: str, description: str = None, target_date: str = None,
                priority: str = None, goal_id: int = None) -> dict:
    """
    Add, change, achieve or drop a goal. The rules live in `src/goals.py`, shared with the
    app's goals screen, so the two cannot disagree about what a goal is.

    `priority` defaults to None, not "secondary": an update only changes what it is given. With
    a "secondary" default, renaming a goal quietly reset its priority.
    """
    from src import goals

    try:
        with write_connection() as conn:
            if action == "add":
                goals.add(conn, description, target_date, priority or "secondary")
                return {"status": "added", "description": description}

            elif action == "update" and goal_id:
                changes = {k: v for k, v in (("description", description),
                                              ("date", target_date), ("priority", priority))
                           if v is not None}
                goals.update(conn, goal_id, **changes)
                return {"status": "updated", "goal_id": goal_id}

            elif action in ("achieve", "drop") and goal_id:
                # Spelled out rather than `action + "d"`, which produced "dropd" and put a typo
                # in the status column for anything later filtering on it.
                status = {"achieve": "achieved", "drop": "dropped"}[action]
                goals.set_status(conn, goal_id, status)
                return {"status": status, "goal_id": goal_id}
    except goals.GoalError as exc:
        return {"status": "error", "error": str(exc)}

    return {"status": "no_op"}


def query_signals(view: str = "summary", kind: str = None, topic: str = None,
                  min_severity: int = 1, start_date: str = None, end_date: str = None,
                  limit: int = 20) -> dict:
    """
    Read the ledger. Says how many rows matched and whether the list was cut, for the reason
    `query_history` does: a model shown the first 20 of 32 concludes there were 20.
    """
    if view == "watch":
        rows = signals.watch_list()
        return {"view": view, "total": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}
    filters = dict(since=start_date, until=end_date, kind=kind, topic=topic, min_severity=min_severity)
    if view == "timeline":
        rows = signals.timeline(limit=100000, **filters)
        shown = [{k: r[k] for k in ("on_date", "kind", "topic", "severity", "state", "quote",
                                    "start_date", "end_date")} for r in rows[:limit]]
        return {"view": view, "total": len(rows), "truncated": len(rows) > limit, "rows": shown}
    rows = signals.summary(**filters)
    return {"view": "summary", "total": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit]}


# ── Dispatch ───────────────────────────────────────────────────────────────────

TOOL_FN_MAP = {
    "log_life_event": log_life_event,
    "log_subjective_feel": log_subjective_feel,
    "query_history": query_history,
    "query_signals": query_signals,
    "suggest_change": suggest_change,
    "calculate_ramp_target": calculate_ramp_target,
    "update_goal": update_goal,
}


def execute_tool(name: str, inputs: dict) -> str:
    fn = TOOL_FN_MAP.get(name)
    if not fn:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        result = fn(**inputs)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

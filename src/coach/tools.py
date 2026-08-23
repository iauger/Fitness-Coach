"""
Tool definitions and implementations for the coaching agent.
Each tool has a schema (sent to Claude) and an execute function (called locally).
"""

import json
from datetime import datetime, date
from src.db.schema import get_connection

NOW = lambda: datetime.utcnow().isoformat()


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
            "Log how the athlete is feeling right now — energy, motivation, leg freshness, "
            "and TrainerRoad workout compliance. Call this when the athlete describes their "
            "current physical or mental state, or reports on their training week."
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
                "tr_compliance": {
                    "type": "string",
                    "enum": ["completed_all", "skipped_some", "modified", "rest_week", "not_on_plan"],
                    "description": "TrainerRoad workout compliance for the week",
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

def log_life_event(type: str, start_date: str, end_date: str = None,
                   severity: str = None, note: str = None,
                   logged_via: str = "chat") -> dict:
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO life_events (start_date, end_date, type, severity, note, logged_at, logged_via)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (start_date, end_date, type, severity, note, NOW(), logged_via))
    conn.close()
    end_str = f" to {end_date}" if end_date else ""
    return {"status": "logged", "event": f"{type}{end_str}: {note or ''}"}


def log_subjective_feel(feel_score: int, energy: int = None, motivation: int = None,
                        legs: int = None, tr_compliance: str = None, notes: str = None,
                        date: str = None, logged_via: str = "chat") -> dict:
    target_date = date or datetime.utcnow().date().isoformat()
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO subjective_feel
            (date, feel_score, energy, motivation, legs, tr_compliance, notes, logged_at, logged_via)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (target_date, feel_score, energy, motivation, legs, tr_compliance, notes, NOW(), logged_via))
    conn.close()
    return {"status": "logged", "date": target_date, "feel_score": feel_score,
            "tr_compliance": tr_compliance}


def query_history(query_type: str, start_date: str = None, end_date: str = None,
                  limit: int = 20) -> dict:
    conn = get_connection()
    end = end_date or date.today().isoformat()
    start = start_date or "2017-01-01"

    if query_type == "ctl_range":
        rows = conn.execute("""
            SELECT date, ctl, atl, round(ctl-atl,1) as tsb
            FROM wellness WHERE date BETWEEN ? AND ? AND ctl IS NOT NULL
            ORDER BY date
        """, (start, end)).fetchmany(limit)
        result = [dict(r) for r in rows]

    elif query_type == "activities_in_period":
        rows = conn.execute("""
            SELECT date, name, type, round(moving_time/60.0,0) as duration_min,
                   round(distance/1000.0,1) as distance_km, avg_hr
            FROM activities WHERE date BETWEEN ? AND ?
            ORDER BY date
        """, (start, end)).fetchmany(limit)
        result = [dict(r) for r in rows]

    elif query_type == "wellness_in_period":
        rows = conn.execute("""
            SELECT date, ctl, atl, rhr, hrv, sleep_hrs, sleep_score
            FROM wellness WHERE date BETWEEN ? AND ?
            ORDER BY date
        """, (start, end)).fetchmany(limit)
        result = [dict(r) for r in rows]

    elif query_type == "peak_ctl_periods":
        rows = conn.execute("""
            SELECT date, ctl, atl, round(ctl-atl,1) as tsb
            FROM wellness WHERE ctl IS NOT NULL
            ORDER BY ctl DESC LIMIT ?
        """, (limit,)).fetchall()
        result = [dict(r) for r in rows]

    elif query_type == "life_events":
        rows = conn.execute("""
            SELECT start_date, end_date, type, severity, note, logged_via
            FROM life_events ORDER BY start_date DESC LIMIT ?
        """, (limit,)).fetchall()
        result = [dict(r) for r in rows]

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

    conn.close()
    return {"query_type": query_type, "rows": result, "count": len(result)}


def calculate_ramp_target(target_ctl: float, target_date: str) -> dict:
    conn = get_connection()
    row = conn.execute("""
        SELECT ctl FROM wellness WHERE ctl IS NOT NULL ORDER BY date DESC LIMIT 1
    """).fetchone()
    conn.close()

    current_ctl = row["ctl"] if row else 0
    today = date.today()
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
                priority: str = "secondary", goal_id: int = None) -> dict:
    conn = get_connection()
    now = NOW()
    with conn:
        if action == "add":
            conn.execute("""
                INSERT INTO goals (description, target_date, priority, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
            """, (description, target_date, priority, now, now))
            return {"status": "added", "description": description}

        elif action == "update" and goal_id:
            conn.execute("""
                UPDATE goals SET description=COALESCE(?,description),
                target_date=COALESCE(?,target_date), priority=COALESCE(?,priority),
                updated_at=? WHERE id=?
            """, (description, target_date, priority, now, goal_id))
            return {"status": "updated", "goal_id": goal_id}

        elif action in ("achieve", "drop") and goal_id:
            conn.execute("""
                UPDATE goals SET status=?, updated_at=? WHERE id=?
            """, (action + "d", now, goal_id))
            return {"status": action + "d", "goal_id": goal_id}

    conn.close()
    return {"status": "no_op"}


# ── Dispatch ───────────────────────────────────────────────────────────────────

TOOL_FN_MAP = {
    "log_life_event": log_life_event,
    "log_subjective_feel": log_subjective_feel,
    "query_history": query_history,
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

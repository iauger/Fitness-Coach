"""
Deterministic rollup of a completed mesocycle, plus storage of the review built from it.

Everything quantitative is computed here in SQL and handed to the model as fact. Asking an LLM
to aggregate 28 days of rows is both more expensive and less reliable than computing it, and
the numbers are what a cycle review turns on — CTL ramp, adherence, whether the rest week
actually cleared the fatigue.

The rest week is included in the cycle by design: judging whether recovery worked is half the
point of reviewing a block. It's reported separately from the build weeks throughout so a
deliberately easy week doesn't dilute the read on the hard ones.
"""

import json
from datetime import date, datetime
from src.db.schema import get_connection
from src.analysis.training_plan import cycles


def _weeks_in_cycle(conn, cycle: dict) -> list[dict]:
    rows = conn.execute("""
        SELECT * FROM training_plan_weeks
        WHERE week_start_date >= ? AND week_end_date <= ?
        ORDER BY week_start_date
    """, (cycle["start_date"], cycle["end_date"])).fetchall()
    return [dict(r) for r in rows]


def _load_for_range(conn, start: str, end: str) -> dict:
    planned = conn.execute("""
        SELECT COALESCE(SUM(planned_tss), 0) tss, COUNT(*) n
        FROM planned_workouts WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
    """, (start, end)).fetchone()
    actual = conn.execute("""
        SELECT COALESCE(SUM(tss), 0) tss, COUNT(*) n
        FROM activities WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL
    """, (start, end)).fetchone()
    return {
        "planned_tss": round(planned["tss"], 1),
        "planned_sessions": planned["n"],
        "actual_tss": round(actual["tss"], 1),
        "activities": actual["n"],
    }


def _recovery_for_range(conn, start: str, end: str) -> dict:
    r = conn.execute("""
        SELECT AVG(hrv) hrv, AVG(rhr) rhr, AVG(sleep_hrs) sleep, COUNT(hrv) n
        FROM wellness WHERE date BETWEEN ? AND ?
    """, (start, end)).fetchone()
    return {
        "hrv": round(r["hrv"], 1) if r["hrv"] is not None else None,
        "rhr": round(r["rhr"], 1) if r["rhr"] is not None else None,
        "sleep_hrs": round(r["sleep"], 1) if r["sleep"] is not None else None,
        "days_with_hrv": r["n"],
    }


def cycle_metrics(cycle: dict) -> dict:
    """Full deterministic rollup for one cycle from training_plan.cycles()."""
    conn = get_connection()
    start, end = cycle["start_date"], cycle["end_date"]
    weeks = _weeks_in_cycle(conn, cycle)
    build = [w for w in weeks if w["week_type"] != "rest"]
    rest = [w for w in weeks if w["week_type"] == "rest"]

    # ── per-week breakdown
    per_week = []
    for w in weeks:
        load = _load_for_range(conn, w["week_start_date"], w["week_end_date"])
        rec = _recovery_for_range(conn, w["week_start_date"], w["week_end_date"])
        ctl_end = conn.execute(
            # wellness.tsb is null on every row — intervals.icu sends ctl and atl but never
            # tsb, so it's derived here exactly as fitness.py does.
            "SELECT ctl, atl, ctl - atl AS tsb FROM wellness "
            "WHERE date <= ? AND ctl IS NOT NULL AND atl IS NOT NULL ORDER BY date DESC LIMIT 1",
            (w["week_end_date"],)
        ).fetchone()
        per_week.append({
            "week_start": w["week_start_date"],
            "plan_week": w["plan_week_number"],
            "week_type": w["week_type"],
            **load,
            "hrv": rec["hrv"], "rhr": rec["rhr"], "sleep_hrs": rec["sleep_hrs"],
            "ctl_end": round(ctl_end["ctl"], 1) if ctl_end and ctl_end["ctl"] else None,
            "tsb_end": round(ctl_end["tsb"], 1) if ctl_end and ctl_end["tsb"] is not None else None,
        })

    # ── fitness movement across the block
    first = conn.execute(
        "SELECT ctl, atl, ctl - atl AS tsb FROM wellness "
        "WHERE date <= ? AND ctl IS NOT NULL AND atl IS NOT NULL ORDER BY date DESC LIMIT 1",
        (start,)
    ).fetchone()
    last = conn.execute(
        "SELECT ctl, atl, ctl - atl AS tsb FROM wellness "
        "WHERE date <= ? AND ctl IS NOT NULL AND atl IS NOT NULL ORDER BY date DESC LIMIT 1",
        (end,)
    ).fetchone()
    ctl_start = round(first["ctl"], 1) if first and first["ctl"] is not None else None
    ctl_end = round(last["ctl"], 1) if last and last["ctl"] is not None else None
    n_weeks = len(weeks) or 1
    fitness = {
        "ctl_start": ctl_start,
        "ctl_end": ctl_end,
        "ctl_delta": round(ctl_end - ctl_start, 1) if None not in (ctl_start, ctl_end) else None,
        "ctl_ramp_per_week": (round((ctl_end - ctl_start) / n_weeks, 2)
                              if None not in (ctl_start, ctl_end) else None),
        "tsb_start": round(first["tsb"], 1) if first and first["tsb"] is not None else None,
        "tsb_end": round(last["tsb"], 1) if last and last["tsb"] is not None else None,
    }

    # ── compliance, overall and by workout type
    comp = conn.execute("""
        SELECT compliance_status s, COUNT(*) n FROM planned_workouts
        WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
        GROUP BY compliance_status
    """, (start, end)).fetchall()
    counts = {r["s"] or "unscored": r["n"] for r in comp}
    scored = sum(v for k, v in counts.items() if k != "unscored")
    compliance = {
        **counts,
        "scored": scored,
        "adherence_pct": (round(counts.get("completed", 0) / scored * 100, 1)
                          if scored else None),
    }
    by_type = [
        {"workout_type": r["workout_type"] or "unclassified", "planned": r["n"],
         "completed": r["done"], "avg_pct": round(r["pct"], 1) if r["pct"] else None}
        for r in conn.execute("""
            SELECT workout_type, COUNT(*) n,
                   SUM(CASE WHEN compliance_status='completed' THEN 1 ELSE 0 END) done,
                   AVG(compliance_pct) pct
            FROM planned_workouts
            WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
            GROUP BY workout_type ORDER BY n DESC
        """, (start, end)).fetchall()
    ]

    # ── recovery: build weeks vs the rest week. This comparison is the whole reason the
    # rest week is inside the cycle rather than reported on its own.
    def span(ws):
        return (ws[0]["week_start_date"], ws[-1]["week_end_date"]) if ws else None
    build_span, rest_span = span(build), span(rest)
    recovery = {
        "build_weeks": _recovery_for_range(conn, *build_span) if build_span else None,
        "rest_week": _recovery_for_range(conn, *rest_span) if rest_span else None,
    }
    if recovery["build_weeks"] and recovery["rest_week"]:
        b, r = recovery["build_weeks"], recovery["rest_week"]
        recovery["rest_week_delta"] = {
            k: (round(r[k] - b[k], 1) if None not in (r[k], b[k]) else None)
            for k in ("hrv", "rhr", "sleep_hrs")
        }

    # ── every session with subjective data attached
    sessions = [
        {"date": r["date"], "name": r["name"], "type": r["type"],
         "tss": r["tss"], "rpe": r["feel"], "note": r["athlete_note"]}
        for r in conn.execute("""
            SELECT date, name, type, tss, feel, athlete_note FROM activities
            WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL
            ORDER BY date
        """, (start, end)).fetchall()
    ]

    conn.close()
    totals = {
        "planned_tss": round(sum(w["planned_tss"] for w in per_week), 1),
        "actual_tss": round(sum(w["actual_tss"] for w in per_week), 1),
    }
    return {
        "cycle": {
            "start_date": start, "end_date": end, "weeks": len(weeks),
            "phase": cycle["phase"], "plan_week_range": cycle["plan_week_range"],
            "week_types": cycle["week_types"],
        },
        "per_week": per_week,
        "totals": totals,
        "fitness": fitness,
        "compliance": compliance,
        "by_workout_type": by_type,
        "recovery": recovery,
        "sessions": sessions,
    }


def previous_review(before_start: str) -> dict | None:
    """
    The most recent stored review starting before `before_start`.

    Cycle-over-cycle comparison is the highest-value thing a review can offer, and doing it
    from stored numbers rather than from the model's recall is why reviews are structured rows.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM cycle_reviews WHERE cycle_start < ? ORDER BY cycle_start DESC LIMIT 1
    """, (before_start,)).fetchone()
    conn.close()
    return dict(row) if row else None


def latest_review() -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM cycle_reviews ORDER BY cycle_start DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_review(plan_name: str, metrics: dict, content: str, model: str) -> None:
    """Upsert on (plan_name, cycle_start) so re-running a review replaces rather than duplicates."""
    c, f, comp = metrics["cycle"], metrics["fitness"], metrics["compliance"]
    rec = metrics["recovery"]
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO cycle_reviews (
                plan_name, cycle_start, cycle_end, phase, plan_week_start, plan_week_end,
                weeks, ctl_start, ctl_end, ctl_ramp_per_week, tsb_end, planned_tss, actual_tss,
                adherence_pct, hrv_build, hrv_rest, metrics_json, content, model, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(plan_name, cycle_start) DO UPDATE SET
                cycle_end=excluded.cycle_end, phase=excluded.phase,
                plan_week_start=excluded.plan_week_start, plan_week_end=excluded.plan_week_end,
                weeks=excluded.weeks, ctl_start=excluded.ctl_start, ctl_end=excluded.ctl_end,
                ctl_ramp_per_week=excluded.ctl_ramp_per_week, tsb_end=excluded.tsb_end,
                planned_tss=excluded.planned_tss, actual_tss=excluded.actual_tss,
                adherence_pct=excluded.adherence_pct, hrv_build=excluded.hrv_build,
                hrv_rest=excluded.hrv_rest, metrics_json=excluded.metrics_json,
                content=excluded.content, model=excluded.model, created_at=excluded.created_at
        """, (
            plan_name, c["start_date"], c["end_date"], c["phase"],
            c["plan_week_range"][0], c["plan_week_range"][1], c["weeks"],
            f["ctl_start"], f["ctl_end"], f["ctl_ramp_per_week"], f["tsb_end"],
            metrics["totals"]["planned_tss"], metrics["totals"]["actual_tss"],
            comp.get("adherence_pct"),
            (rec["build_weeks"] or {}).get("hrv"), (rec["rest_week"] or {}).get("hrv"),
            json.dumps(metrics), content, model, datetime.utcnow().isoformat(),
        ))
    conn.close()


def cycle_context_text(metrics: dict, prev: dict | None = None) -> str:
    """Render the rollup for the model. Facts only — the interpretation is the model's job."""
    c, f, comp = metrics["cycle"], metrics["fitness"], metrics["compliance"]
    lines = [
        f"CYCLE UNDER REVIEW — plan weeks {c['plan_week_range'][0]}-{c['plan_week_range'][1]}, "
        f"{c['phase']} phase",
        f"  {c['start_date']} to {c['end_date']}  ({c['weeks']} weeks: "
        f"{', '.join(c['week_types'])})",
        "",
        "WEEK BY WEEK",
        f"  {'week':<12} {'type':<10} {'planned':>8} {'actual':>7} {'CTL':>6} {'TSB':>6} "
        f"{'HRV':>6} {'RHR':>5} {'sleep':>6}",
    ]
    for w in metrics["per_week"]:
        lines.append(
            f"  {w['week_start']:<12} {w['week_type']:<10} {w['planned_tss']:>8.0f} "
            f"{w['actual_tss']:>7.0f} {str(w['ctl_end'] or '-'):>6} {str(w['tsb_end'] or '-'):>6} "
            f"{str(w['hrv'] or '-'):>6} {str(w['rhr'] or '-'):>5} {str(w['sleep_hrs'] or '-'):>6}"
        )

    lines += [
        "",
        "FITNESS ACROSS THE BLOCK",
        f"  CTL {f['ctl_start']} -> {f['ctl_end']}  (delta {f['ctl_delta']}, "
        f"{f['ctl_ramp_per_week']} CTL/week)",
        f"  TSB {f['tsb_start']} -> {f['tsb_end']}",
        f"  Load: {metrics['totals']['actual_tss']:.0f} TSS actual vs "
        f"{metrics['totals']['planned_tss']:.0f} planned",
        "",
        "PLAN ADHERENCE (power sessions only — strength/mobility are never scored)",
        f"  completed {comp.get('completed', 0)} | partial {comp.get('partial', 0)} | "
        f"skipped {comp.get('skipped', 0)} | adherence {comp.get('adherence_pct')}% "
        f"of {comp.get('scored', 0)} scored sessions",
    ]
    # The per-type "planned" count includes sessions with no compliance_status, so it can
    # exceed the scored total above. Saying so prevents the two blocks reading as contradictory.
    if comp.get("unscored"):
        lines.append(f"  {comp['unscored']} planned power sessions are unscored (no matching "
                     f"activity recorded) and are excluded from the adherence figure")
    for t in metrics["by_workout_type"]:
        lines.append(f"    {t['workout_type']:<14} {t['completed']}/{t['planned']} completed"
                     + (f", avg {t['avg_pct']}% of prescribed load" if t["avg_pct"] else ""))

    rec = metrics["recovery"]
    if rec.get("build_weeks"):
        b = rec["build_weeks"]
        lines += ["", "RECOVERY — build weeks vs the rest week",
                  f"  build weeks: HRV {b['hrv']}, RHR {b['rhr']}, sleep {b['sleep_hrs']}h"]
        if rec.get("rest_week"):
            r = rec["rest_week"]
            if r["days_with_hrv"]:
                lines.append(f"  rest week:   HRV {r['hrv']}, RHR {r['rhr']}, "
                             f"sleep {r['sleep_hrs']}h")
            else:
                lines.append("  rest week:   no wellness data recorded")
        d = rec.get("rest_week_delta") or {}
        # Only render the deltas that actually computed — a partially-missing rest week should
        # drop the missing metrics, not print None or a raw dict.
        parts = [f"{label} {d[k]:+}" for k, label in
                 (("hrv", "HRV"), ("rhr", "RHR"), ("sleep_hrs", "sleep"))
                 if d.get(k) is not None]
        if parts:
            lines.append(f"  change over the rest week: {', '.join(parts)}")

    logged = [s for s in metrics["sessions"] if s["rpe"] or s["note"]]
    if logged:
        lines += ["", "SESSIONS WITH RPE OR NOTES"]
        for s in logged:
            rpe = f"  RPE {int(s['rpe'])}/10" if s["rpe"] else ""
            lines.append(f"  {s['date']}  {s['name']}{rpe}")
            if s["note"]:
                for para in s["note"].split("\n\n"):
                    lines.append(f"      note: {para}")

    if prev:
        lines += [
            "",
            "PREVIOUS CYCLE (for comparison — these are stored figures, not estimates)",
            f"  {prev['cycle_start']} to {prev['cycle_end']}, {prev['phase']} phase, "
            f"{prev['weeks']} weeks",
            f"  CTL {prev['ctl_start']} -> {prev['ctl_end']} ({prev['ctl_ramp_per_week']} CTL/week)"
            f"  |  adherence {prev['adherence_pct']}%  |  "
            f"{prev['actual_tss']:.0f} TSS actual vs {prev['planned_tss']:.0f} planned",
            f"  HRV: {prev['hrv_build']} in build weeks, {prev['hrv_rest']} in the rest week",
        ]

    return "\n".join(lines)


def cycle_for_review(on: date | None = None) -> dict | None:
    """
    The cycle a review run on `on` should cover: the one that closed most recently.

    Not just_completed_cycle() — that fires only on the exact Monday after a rest week, which
    is right as a trigger but wrong as a lookup, since a review run a few days late should
    still find its cycle.
    """
    # Clamped to today, never `on`. Passing a future --as-of would otherwise return a cycle
    # whose later weeks have no activities yet, and every absent week reads identically to a
    # skipped one: a live test with --as-of two weeks ahead produced a confident review saying
    # two-thirds of the plan had been missed. A cycle that hasn't finished can't be reviewed.
    d = min(on or date.today(), date.today()).isoformat()
    closed = [c for c in cycles() if c["closed"] and c["end_date"] < d]
    return closed[-1] if closed else None

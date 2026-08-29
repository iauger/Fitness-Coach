"""
Per-week rollup and storage — the weekly counterpart to cycle.py.

Everything quantitative is computed here. The one thing that needs a model is compressing a
week's free-text notes into a sentence or two, and that lives in coach/summarize.py so this
module stays free of API dependencies and can run offline.

Why a table rather than computing on read, when derive.py deliberately doesn't persist:
a weekly summary carries a narrative written at the time, and the figures beside it have to
stay the ones that narrative was describing. That is the same test cycle_reviews passes and
derive.py's metrics fail — see the "what gets computed, what gets stored" rule in ROADMAP.
"""

import json
from datetime import date, datetime, timedelta
from src.db.schema import get_connection
from src.analysis.cycle import _load_for_range, _recovery_for_range


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def last_completed_week(on: date | None = None) -> date:
    """Monday of the most recent week that has fully elapsed."""
    return monday_of(on or date.today()) - timedelta(weeks=1)


def week_metrics(week_start: date) -> dict:
    """Deterministic rollup for one week. No model involved."""
    conn = get_connection()
    try:
        start, end = week_start.isoformat(), (week_start + timedelta(days=6)).isoformat()
        load = _load_for_range(conn, start, end)
        rec = _recovery_for_range(conn, start, end)

        wk = conn.execute(
            "SELECT plan_week_number, week_type, phase FROM training_plan_weeks "
            "WHERE week_start_date = ?", (start,)).fetchone()

        pmc = conn.execute(
            "SELECT ctl, atl, ctl - atl AS tsb FROM wellness WHERE date <= ? "
            "AND ctl IS NOT NULL AND atl IS NOT NULL ORDER BY date DESC LIMIT 1",
            (end,)).fetchone()

        comp = conn.execute("""
            SELECT compliance_status s, COUNT(*) n FROM planned_workouts
            WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
            GROUP BY compliance_status
        """, (start, end)).fetchall()
        counts = {r["s"] or "unscored": r["n"] for r in comp}
        scored = sum(v for k, v in counts.items() if k != "unscored")

        sessions = [
            {"date": r["date"], "name": r["name"], "type": r["type"],
             "rpe": r["feel"], "note": r["athlete_note"]}
            for r in conn.execute("""
                SELECT date, name, type, feel, athlete_note FROM activities
                WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL ORDER BY date
            """, (start, end)).fetchall()
        ]
        rpes = [s["rpe"] for s in sessions if s["rpe"]]

        subjective = [
            dict(r) for r in conn.execute(
                "SELECT date, feel_score, energy, legs, tr_compliance, notes FROM subjective_feel "
                "WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)).fetchall()
        ]

        return {
            "week_start": start,
            "week_end": end,
            "plan_week_number": wk["plan_week_number"] if wk else None,
            "week_type": wk["week_type"] if wk else None,
            "phase": wk["phase"] if wk else None,
            "planned_tss": load["planned_tss"],
            "actual_tss": load["actual_tss"],
            "sessions_count": load["activities"],
            "compliance": counts,
            "adherence_pct": (round(counts.get("completed", 0) / scored * 100, 1)
                              if scored else None),
            "ctl_end": round(pmc["ctl"], 1) if pmc else None,
            "tsb_end": round(pmc["tsb"], 1) if pmc else None,
            "hrv": rec["hrv"], "rhr": rec["rhr"], "sleep_hrs": rec["sleep_hrs"],
            "mean_rpe": round(sum(rpes) / len(rpes), 1) if rpes else None,
            "sessions": sessions,
            "subjective": subjective,
        }
    finally:
        conn.close()


def has_qualitative_content(m: dict) -> bool:
    """Whether this week has anything a model would need to compress."""
    return any(s.get("note") for s in m["sessions"]) or any(
        s.get("notes") for s in m["subjective"])


def save_summary(m: dict, narrative: str | None, model: str | None) -> None:
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO weekly_summaries (
                week_start, week_end, plan_week_number, week_type, phase, planned_tss,
                actual_tss, sessions, adherence_pct, ctl_end, tsb_end, hrv, rhr, sleep_hrs,
                mean_rpe, metrics_json, narrative, model, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(week_start) DO UPDATE SET
                week_end=excluded.week_end, plan_week_number=excluded.plan_week_number,
                week_type=excluded.week_type, phase=excluded.phase,
                planned_tss=excluded.planned_tss, actual_tss=excluded.actual_tss,
                sessions=excluded.sessions, adherence_pct=excluded.adherence_pct,
                ctl_end=excluded.ctl_end, tsb_end=excluded.tsb_end, hrv=excluded.hrv,
                rhr=excluded.rhr, sleep_hrs=excluded.sleep_hrs, mean_rpe=excluded.mean_rpe,
                metrics_json=excluded.metrics_json, narrative=excluded.narrative,
                model=excluded.model, created_at=excluded.created_at
        """, (
            m["week_start"], m["week_end"], m["plan_week_number"], m["week_type"], m["phase"],
            m["planned_tss"], m["actual_tss"], m["sessions_count"], m["adherence_pct"],
            m["ctl_end"], m["tsb_end"], m["hrv"], m["rhr"], m["sleep_hrs"], m["mean_rpe"],
            json.dumps(m), narrative, model, datetime.utcnow().isoformat(),
        ))
    conn.close()


def summaries_between(start: str, end: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM weekly_summaries WHERE week_start BETWEEN ? AND ? ORDER BY week_start",
        (start, end)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def summarized_weeks() -> set[str]:
    conn = get_connection()
    rows = conn.execute("SELECT week_start FROM weekly_summaries").fetchall()
    conn.close()
    return {r["week_start"] for r in rows}


def summary_lines(summaries: list[dict]) -> list[str]:
    """Render stored weekly summaries for the snapshot, tersely."""
    if not summaries:
        return []
    lines = []
    for s in summaries:
        head = (f"  {s['week_start']} to {s['week_end']}"
                + (f"  (plan week {s['plan_week_number']}, {s['week_type']})"
                   if s["plan_week_number"] else ""))
        lines.append(head)
        stats = [f"{s['actual_tss']:.0f} of {s['planned_tss']:.0f} TSS"
                 if s["planned_tss"] else f"{s['actual_tss']:.0f} TSS",
                 f"{s['sessions']} sessions"]
        if s["adherence_pct"] is not None:
            stats.append(f"{s['adherence_pct']:.0f}% adherence")
        if s["ctl_end"] is not None:
            stats.append(f"CTL {s['ctl_end']}, TSB {s['tsb_end']}")
        if s["mean_rpe"]:
            stats.append(f"mean RPE {s['mean_rpe']}")
        if s["hrv"]:
            stats.append(f"HRV {s['hrv']}, sleep {s['sleep_hrs']}h")
        lines.append(f"    {'  |  '.join(stats)}")
        if s["narrative"]:
            lines.append(f"    {s['narrative']}")
    return lines

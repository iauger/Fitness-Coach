"""
PMC (Performance Management Chart) analysis.
Reads from the wellness table where CTL/ATL are stored per day.
"""

import json
from datetime import date, timedelta
from statistics import mean, stdev
from src.db.schema import get_connection


def _wellness_rows(days: int = 365) -> list[dict]:
    end = date.today()
    start = end - timedelta(days=days)
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, ctl, atl, rhr, hrv, sleep_hrs, sleep_score, steps, raw_json
        FROM wellness
        WHERE date >= ? AND date <= ? AND ctl IS NOT NULL
        ORDER BY date ASC
    """, (start.isoformat(), end.isoformat())).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def current_fitness() -> dict:
    """Latest CTL/ATL/TSB and ramp rate."""
    conn = get_connection()
    row = conn.execute("""
        SELECT date, ctl, atl, raw_json
        FROM wellness
        WHERE ctl IS NOT NULL
        ORDER BY date DESC LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return {}
    r = dict(row)
    raw = json.loads(r["raw_json"])
    ctl = r["ctl"]
    atl = r["atl"]
    return {
        "date": r["date"],
        "ctl": round(ctl, 1),
        "atl": round(atl, 1),
        "tsb": round(ctl - atl, 1),
        "ramp_rate": round(raw.get("rampRate", 0), 2),
        "eftp": raw.get("sportInfo", [{}])[0].get("eftp") if raw.get("sportInfo") else None,
    }


def ctl_history(weeks: int = 52) -> list[dict]:
    """Weekly CTL snapshots for trend analysis."""
    rows = _wellness_rows(days=weeks * 7)
    if not rows:
        return []
    # sample end-of-week (Sunday) values
    weekly = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        week_end = d + timedelta(days=(6 - d.weekday()))
        weekly[week_end.isoformat()] = {
            "week_ending": week_end.isoformat(),
            "ctl": round(r["ctl"], 1),
            "atl": round(r["atl"], 1),
            "tsb": round(r["ctl"] - r["atl"], 1),
        }
    return sorted(weekly.values(), key=lambda x: x["week_ending"])


def peak_ctl(years: int = 9) -> dict:
    """Historical CTL peak — baseline for what this athlete can reach."""
    conn = get_connection()
    row = conn.execute("""
        SELECT date, ctl FROM wellness
        WHERE ctl IS NOT NULL AND date >= ?
        ORDER BY ctl DESC LIMIT 1
    """, ((date.today() - timedelta(days=years * 365)).isoformat(),)).fetchone()
    conn.close()
    if not row:
        return {}
    return {"date": row["date"], "ctl": round(row["ctl"], 1)}


def fitness_gaps() -> list[dict]:
    """Identify periods where CTL dropped significantly — training gaps."""
    rows = _wellness_rows(days=365 * 9)
    gaps = []
    prev_ctl = None
    gap_start = None

    for r in rows:
        ctl = r["ctl"]
        d = r["date"]
        if prev_ctl is not None:
            drop = prev_ctl - ctl
            if drop > 10 and gap_start is None:
                gap_start = d
            elif drop <= 0 and gap_start is not None:
                gaps.append({
                    "start": gap_start,
                    "end": d,
                    "ctl_drop": round(prev_ctl - ctl, 1),
                })
                gap_start = None
        prev_ctl = ctl

    return gaps[-5:]  # most recent 5 significant gaps


def ctl_trend(weeks: int = 8) -> dict:
    """Direction and magnitude of CTL change over recent weeks."""
    rows = _wellness_rows(days=weeks * 7)
    if len(rows) < 7:
        return {}
    first_ctl = rows[0]["ctl"]
    last_ctl = rows[-1]["ctl"]
    change = last_ctl - first_ctl
    direction = "building" if change > 2 else "declining" if change < -2 else "stable"
    return {
        "weeks": weeks,
        "start_ctl": round(first_ctl, 1),
        "end_ctl": round(last_ctl, 1),
        "change": round(change, 1),
        "direction": direction,
    }

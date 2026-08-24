"""
Recovery and wellness signal analysis — HRV, sleep, resting HR trends.
"""

import json
from datetime import date, timedelta
from statistics import mean, stdev
from src.db.schema import get_connection


def _recent_wellness(days: int) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, rhr, hrv, sleep_hrs, sleep_score, steps
        FROM wellness
        WHERE date >= ? ORDER BY date ASC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def hrv_summary(days: int = 30) -> dict:
    """HRV trend over recent period. HRV dropping = accumulated stress."""
    rows = [r for r in _recent_wellness(days) if r["hrv"] is not None]
    if not rows:
        return {}
    values = [r["hrv"] for r in rows]
    recent_7 = [r["hrv"] for r in rows[-7:]]
    prev_7 = [r["hrv"] for r in rows[-14:-7]] if len(rows) >= 14 else values[:7]
    trend = "declining" if mean(recent_7) < mean(prev_7) - 3 else \
            "improving" if mean(recent_7) > mean(prev_7) + 3 else "stable"
    return {
        "avg_30d": round(mean(values), 1),
        "avg_7d": round(mean(recent_7), 1),
        "trend": trend,
        "low": round(min(values), 1),
        "high": round(max(values), 1),
    }


def sleep_summary(days: int = 30) -> dict:
    """Sleep hours and score averages."""
    rows = [r for r in _recent_wellness(days) if r["sleep_hrs"] is not None]
    if not rows:
        return {}
    hours = [r["sleep_hrs"] for r in rows]
    scores = [r["sleep_score"] for r in rows if r["sleep_score"] is not None]
    recent_7h = [r["sleep_hrs"] for r in rows[-7:]]
    return {
        "avg_hours_30d": round(mean(hours), 1),
        "avg_hours_7d": round(mean(recent_7h), 1),
        "avg_score_30d": round(mean(scores), 1) if scores else None,
        "nights_under_7h": sum(1 for h in hours if h < 7),
        "nights_under_6h": sum(1 for h in hours if h < 6),
    }


def rhr_trend(days: int = 30) -> dict:
    """Resting heart rate trend. RHR rising with load = accumulated fatigue."""
    rows = [r for r in _recent_wellness(days) if r["rhr"] is not None]
    if not rows:
        return {}
    values = [r["rhr"] for r in rows]
    recent_7 = [r["rhr"] for r in rows[-7:] if r["rhr"]]
    prev_7 = [r["rhr"] for r in rows[-14:-7] if r["rhr"]] if len(rows) >= 14 else values[:7]
    trend = "elevated" if mean(recent_7) > mean(prev_7) + 2 else \
            "lowering" if mean(recent_7) < mean(prev_7) - 2 else "stable"
    return {
        "avg_30d": round(mean(values), 1),
        "avg_7d": round(mean(recent_7), 1),
        "trend": trend,
    }


def recovery_flags(days: int = 30) -> list[str]:
    """
    Plain-language flags for concerning recovery patterns. These strings are injected verbatim
    into the coach's system prompt, so their stated windows have to be true.

    All three summaries are pulled over the same `days` window because their output keys are
    named `avg_30d` regardless of the window requested — calling them with 14 or 7 (as this did
    until Session 14) meant the flag text claimed "30d avg" for a 14-day mean and "last 30 days"
    for a 7-day count. The 7-day sub-averages are unaffected: they're computed from the tail of
    whatever window is fetched, so a 30-day pull still yields a correct avg_7d.
    """
    flags = []
    hrv = hrv_summary(days=days)
    sleep = sleep_summary(days=days)
    rhr = rhr_trend(days=days)

    if hrv.get("trend") == "declining":
        flags.append(f"HRV trending down (7d avg {hrv.get('avg_7d')} vs 30d avg {hrv.get('avg_30d')})")
    if sleep.get("avg_hours_7d", 8) < 6.5:
        flags.append(f"Sleep short this week — averaging {sleep.get('avg_hours_7d')}h/night")
    if sleep.get("nights_under_6h", 0) >= 3:
        flags.append(f"{sleep.get('nights_under_6h')} nights under 6h in last 30 days")
    if rhr.get("trend") == "elevated":
        flags.append(f"Resting HR elevated (7d avg {rhr.get('avg_7d')} bpm vs 30d avg {rhr.get('avg_30d')} bpm)")

    return flags

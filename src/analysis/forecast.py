"""
Deterministic CTL/ATL/TSB projection — no ML, just the same EWMA recursion intervals.icu
already uses to compute CTL/ATL, run forward using planned_workouts' future TSS instead of
actual. Horizon is whatever planned_workouts actually covers (TrainerRoad is adaptive and
typically only publishes the near-term block, not the full season) — this is a near-term
"where does the current calendar put me" projection, not a long-range race-day forecast.
"""

from datetime import date, timedelta
from src.db.schema import get_connection

CTL_DAYS = 42
ATL_DAYS = 7


def project_fitness() -> list[dict]:
    """Day-by-day projected CTL/ATL/TSB from the latest actual value through the furthest
    date with planned TSS data. Non-power planned sessions (planned_tss IS NULL — strength,
    mobility) contribute 0 load, consistent with how the rest of the system already treats
    them. Returns [] if there's no actual baseline or no future planned data to project from."""
    conn = get_connection()
    latest = conn.execute("""
        SELECT date, ctl, atl FROM wellness WHERE ctl IS NOT NULL ORDER BY date DESC LIMIT 1
    """).fetchone()
    if not latest:
        conn.close()
        return []

    planned = conn.execute("""
        SELECT date, planned_tss FROM planned_workouts WHERE date > ? ORDER BY date
    """, (latest["date"],)).fetchall()
    conn.close()
    if not planned:
        return []

    tss_by_date: dict[str, float] = {}
    for p in planned:
        tss_by_date[p["date"]] = tss_by_date.get(p["date"], 0) + (p["planned_tss"] or 0)

    ctl, atl = latest["ctl"], latest["atl"]
    d = date.fromisoformat(latest["date"])
    last_planned_date = date.fromisoformat(max(tss_by_date.keys()))

    projected = []
    while d < last_planned_date:
        d += timedelta(days=1)
        tss = tss_by_date.get(d.isoformat(), 0)
        ctl = ctl + (tss - ctl) / CTL_DAYS
        atl = atl + (tss - atl) / ATL_DAYS
        projected.append({
            "date": d.isoformat(),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
        })
    return projected

"""
Week-grid training calendar — health stats + activities + plan adherence per day.
Modeled on intervals.icu's weekly calendar view, minus weather (not useful to us),
plus our own plan-compliance overlay (which intervals.icu's stock view doesn't have).

Raw HTML/CSS grid, not Plotly — this is a layout problem, not a chart.
"""

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from src.db.schema import get_connection
from src.visualizations.theme import COLORS

CHARTS_DIR = Path(__file__).parent.parent.parent / "data" / "charts"

COMPLIANCE_COLORS = {
    "completed": "#34d399",
    "partial":   "#fbbf24",
    "skipped":   "#f87171",
    None:        "#6b7280",   # not yet scored — future or pending
}


def _fmt_duration(minutes: float | None) -> str:
    if not minutes:
        return ""
    m = int(round(minutes))
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fmt_hms(seconds: float | None) -> str:
    if not seconds:
        return ""
    return _fmt_duration(seconds / 60)


def _activity_load(raw_json: str) -> float | None:
    raw = json.loads(raw_json or "{}")
    return raw.get("icu_training_load") or raw.get("power_load") or raw.get("hr_load")


def _week_bounds(weeks: int) -> tuple[date, date]:
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(weeks=weeks - 1)
    end = this_monday + timedelta(days=6)  # Sunday of the current week
    return start, end


def _load_data(start: date, end: date) -> dict[str, dict]:
    conn = get_connection()

    wellness = {
        r["date"]: dict(r)
        for r in conn.execute("""
            SELECT date, sleep_hrs, sleep_score, rhr, hrv, weight_kg, steps
            FROM wellness WHERE date BETWEEN ? AND ?
        """, (start.isoformat(), end.isoformat())).fetchall()
    }

    activities = defaultdict(list)
    for r in conn.execute("""
        SELECT date, name, type, moving_time, avg_hr, avg_power, tss, raw_json
        FROM activities
        WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL
        ORDER BY date, moving_time DESC
    """, (start.isoformat(), end.isoformat())).fetchall():
        row = dict(r)
        row["load"] = row["tss"] or _activity_load(row.pop("raw_json"))
        activities[row["date"]].append(row)

    planned = defaultdict(list)
    for r in conn.execute("""
        SELECT date, name, planned_tss, planned_duration_min, compliance_status, compliance_pct
        FROM planned_workouts WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL
        ORDER BY date
    """, (start.isoformat(), end.isoformat())).fetchall():
        planned[r["date"]].append(dict(r))

    conn.close()

    days = {}
    cur = start
    while cur <= end:
        d = cur.isoformat()
        days[d] = {
            "date": d,
            "wellness": wellness.get(d),
            "activities": activities.get(d, []),
            "planned": planned.get(d, []),
        }
        cur += timedelta(days=1)
    return days


SPORT_GROUPS = {
    "Ride": "cycling", "VirtualRide": "cycling", "GravelRide": "cycling",
    "MountainBikeRide": "cycling", "EBikeRide": "cycling",
    "Run": "running", "VirtualRun": "running", "TrailRun": "running",
    "Swim": "swimming", "OpenWaterSwim": "swimming",
    "WeightTraining": "strength", "Workout": "strength",
    "Yoga": "yoga", "Pilates": "yoga",
    "Walk": "walk", "Hike": "hike",
}


def _sport_color(activity_type: str | None) -> str:
    return COLORS.get(SPORT_GROUPS.get(activity_type or "", "other"), COLORS["other"])


def _wellness_html(w: dict | None) -> str:
    if not w:
        return ""
    parts = []
    if w.get("sleep_hrs"):
        score = f" Q{round(w['sleep_score'])}" if w.get("sleep_score") else ""
        parts.append(f'<span class="stat">😴 {_fmt_duration(w["sleep_hrs"] * 60)}{score}</span>')
    if w.get("rhr"):
        parts.append(f'<span class="stat">❤ {round(w["rhr"])}</span>')
    if w.get("hrv"):
        parts.append(f'<span class="stat">📈 {round(w["hrv"])}ms</span>')
    if w.get("steps"):
        parts.append(f'<span class="stat">👣 {w["steps"]:,}</span>')
    if w.get("weight_kg"):
        lbs = w["weight_kg"] * 2.2046226218
        parts.append(f'<span class="stat">⚖ {lbs:.0f}lb</span>')
    return f'<div class="wellness-row">{"".join(parts)}</div>' if parts else ""


def _activity_html(a: dict) -> str:
    color = _sport_color(a["type"])
    dur = _fmt_hms(a["moving_time"])
    bits = []
    if a.get("avg_hr"):
        bits.append(f'{round(a["avg_hr"])}bpm')
    if a.get("avg_power"):
        bits.append(f'{round(a["avg_power"])}w')
    if a.get("load"):
        bits.append(f'Load {round(a["load"])}')
    detail = "  ".join(bits)
    return f"""
        <div class="activity" style="border-left-color:{color}">
          <div class="activity-dur">{dur}</div>
          <div class="activity-detail">{detail}</div>
          <div class="activity-name">{a["name"] or a["type"] or "Activity"}</div>
        </div>"""


def _planned_html(p: dict) -> str:
    color = COMPLIANCE_COLORS.get(p["compliance_status"])
    pct = f' &middot; {p["compliance_pct"]:.0f}%' if p.get("compliance_pct") is not None else ""
    return f"""
        <div class="planned" style="border-color:{color}; color:{color}">
          Plan: {p["name"]} ({round(p["planned_tss"])} TSS){pct}
        </div>"""


def _day_cell_html(day: dict, today_str: str) -> str:
    d = date.fromisoformat(day["date"])
    is_today = day["date"] == today_str
    is_future = day["date"] > today_str

    activities_html = "".join(_activity_html(a) for a in day["activities"])
    planned_html = "".join(_planned_html(p) for p in day["planned"])
    wellness_html = _wellness_html(day["wellness"])

    body = wellness_html + planned_html + activities_html
    if not body:
        body = '<div class="empty">&mdash;</div>'

    cls = "day" + (" today" if is_today else "") + (" future" if is_future else "")
    return f"""
      <div class="{cls}">
        <div class="day-header">{d.strftime("%a %d %b")}</div>
        {body}
      </div>"""


def build_calendar_html(weeks: int = 6) -> str:
    start, end = _week_bounds(weeks)
    days = _load_data(start, end)
    today_str = date.today().isoformat()

    week_rows = []
    cur = start
    while cur <= end:
        cells = "".join(_day_cell_html(days[(cur + timedelta(days=i)).isoformat()], today_str)
                        for i in range(7))
        week_rows.append(f'<div class="week">{cells}</div>')
        cur += timedelta(days=7)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Training Calendar — Ian Auger</title>
  <link rel="icon" href="data:image/svg+xml,&lt;svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22&gt;&lt;text y=%22.9em%22 font-size=%2290%22&gt;📅&lt;/text&gt;&lt;/svg&gt;">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: {COLORS["bg"]};
      color: {COLORS["text"]};
      font-family: Inter, system-ui, sans-serif;
      padding: 1.5rem;
    }}
    h1 {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; }}
    .week {{
      display: grid;
      grid-template-columns: repeat(7, 1fr);
      gap: 0.5rem;
      margin-bottom: 0.5rem;
    }}
    .day {{
      background: {COLORS["surface"]};
      border: 1px solid {COLORS["border"]};
      border-radius: 8px;
      padding: 0.5rem;
      min-height: 130px;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
    }}
    .day.today {{ border-color: {COLORS["ctl"]}; border-width: 2px; }}
    .day.future {{ opacity: 0.7; }}
    .day-header {{
      font-size: 0.7rem;
      font-weight: 600;
      color: {COLORS["subtext"]};
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .wellness-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      font-size: 0.68rem;
      color: {COLORS["subtext"]};
    }}
    .stat {{ white-space: nowrap; }}
    .activity {{
      background: rgba(255,255,255,0.04);
      border-left: 3px solid;
      border-radius: 4px;
      padding: 0.3rem 0.4rem;
    }}
    .activity-dur {{ font-size: 0.75rem; font-weight: 600; }}
    .activity-detail {{ font-size: 0.68rem; color: {COLORS["subtext"]}; }}
    .activity-name {{ font-size: 0.68rem; color: {COLORS["text"]}; opacity: 0.85; }}
    .planned {{
      font-size: 0.65rem;
      border: 1px dashed;
      border-radius: 4px;
      padding: 0.2rem 0.4rem;
    }}
    .empty {{ color: {COLORS["border"]}; font-size: 0.75rem; }}
  </style>
</head>
<body>
  <h1>Training Calendar — {start.strftime("%b %d")} to {end.strftime("%b %d, %Y")}</h1>
  {"".join(week_rows)}
</body>
</html>"""


def write_calendar(weeks: int = 6, open_browser: bool = False) -> Path:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    out = CHARTS_DIR / "calendar.html"
    out.write_text(build_calendar_html(weeks=weeks), encoding="utf-8")
    if open_browser:
        import webbrowser
        webbrowser.open(out.as_uri())
    return out

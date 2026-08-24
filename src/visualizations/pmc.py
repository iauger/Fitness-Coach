"""
Performance Management Chart — modeled on intervals.icu's 4-panel PMC:
  1. Fitness (CTL) / Fatigue (ATL), shaded area between, event markers
  2. Form (TSB), colored by zone: Fresh / Grey Zone / Optimal / High Risk
  3. Ramp rate — weekly CTL change, colored by sign
  4. FTP over time — manually-tracked actual FTP (stepped) vs. intervals.icu's eFTP estimate

Zone thresholds match the methodology already documented in src/coach/prompt.py, so the chart
and the coach's own reasoning about TSB agree with each other.
"""

import json
from datetime import date, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from src.db.schema import get_connection
from src.athlete.profile import metric_history
from src.analysis.forecast import project_fitness
from src.visualizations.theme import COLORS, LAYOUT, AXIS

FRESH_MIN = 5      # TSB >= 5: Fresh
GREY_MIN = -5      # -5 <= TSB < 5: Grey Zone
OPTIMAL_MIN = -30  # -30 <= TSB < -5: Optimal; below -30: High Risk

ZONE_COLORS = {
    "fresh":   COLORS["form_fresh"],
    "grey":    COLORS["form_grey"],
    "optimal": COLORS["form_optimal"],
    "risk":    COLORS["form_risk"],
}


def _zone(tsb: float) -> str:
    if tsb >= FRESH_MIN:
        return "fresh"
    if tsb >= GREY_MIN:
        return "grey"
    if tsb >= OPTIMAL_MIN:
        return "optimal"
    return "risk"


def _load_data(weeks: int | None):
    """weeks=None pulls full history — used to bake in enough data for the dashboard's
    client-side date-range filter to zoom within, rather than re-fetching per range."""
    conn = get_connection()
    if weeks is None:
        rows = conn.execute("""
            SELECT date, ctl, atl, round(ctl - atl, 1) as tsb, raw_json
            FROM wellness WHERE ctl IS NOT NULL ORDER BY date
        """).fetchall()
        events = conn.execute("""
            SELECT date, name, category FROM events
            WHERE category IN ('A', 'B', 'C') ORDER BY date
        """).fetchall()
    else:
        since = (date.today() - timedelta(weeks=weeks)).isoformat()
        rows = conn.execute("""
            SELECT date, ctl, atl, round(ctl - atl, 1) as tsb, raw_json
            FROM wellness
            WHERE date >= ? AND ctl IS NOT NULL
            ORDER BY date
        """, (since,)).fetchall()
        events = conn.execute("""
            SELECT date, name, category FROM events
            WHERE date >= ? AND category IN ('A', 'B', 'C')
            ORDER BY date
        """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows], [dict(e) for e in events]


def _eftp_series(rows: list[dict]) -> list[float | None]:
    """Daily eFTP, forward-filled across rest days that don't carry a fresh estimate."""
    values = []
    last = None
    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        sport_info = raw.get("sportInfo") or []
        eftp = sport_info[0].get("eftp") if sport_info else None
        if eftp:
            last = eftp
        values.append(last)
    return values


def _weekly_ramp(rows: list[dict]) -> tuple[list[str], list[float]]:
    """Weekly CTL change — sample the last CTL value of each week, diff consecutive weeks."""
    weekly: dict[str, float] = {}
    for r in rows:
        d = date.fromisoformat(r["date"])
        week_end = (d + timedelta(days=6 - d.weekday())).isoformat()
        weekly[week_end] = r["ctl"]  # rows are date-ascending, so last write per week wins
    weeks_sorted = sorted(weekly.items())
    ramp_dates = [w for w, _ in weeks_sorted[1:]]
    ramp_values = [round(weeks_sorted[i][1] - weeks_sorted[i - 1][1], 1)
                   for i in range(1, len(weeks_sorted))]
    return ramp_dates, ramp_values


def _zone_segments(dates: list[str], tsb: list[float]):
    """Split (dates, tsb) into contiguous same-zone runs, sharing boundary points so the
    rendered line stays visually continuous across zone-color changes."""
    if not tsb:
        return []
    zones = [_zone(v) for v in tsb]
    segments = []
    start = 0
    for i in range(1, len(zones)):
        if zones[i] != zones[start]:
            segments.append((start, i, zones[start]))
            start = i
    segments.append((start, len(zones) - 1, zones[start]))
    return segments


def build(weeks: int | None = None) -> go.Figure:
    rows, events = _load_data(weeks)
    if not rows:
        return go.Figure()

    dates = [r["date"] for r in rows]
    ctl = [r["ctl"] for r in rows]
    atl = [r["atl"] for r in rows]
    tsb = [r["tsb"] for r in rows]
    eftp = _eftp_series(rows)
    ramp_dates, ramp_values = _weekly_ramp(rows)
    ftp_history = metric_history("ftp")
    projected = project_fitness()

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.38, 0.24, 0.16, 0.22],
        vertical_spacing=0.035,
        subplot_titles=("Fitness &amp; Fatigue", "Form", "Weekly Ramp Rate", "FTP"),
    )

    # ── Panel 1: CTL / ATL, shaded area between ──────────────────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=atl, name="ATL (Fatigue)",
        line=dict(color=COLORS["atl"], width=1.5, dash="dot"),
        hovertemplate="%{y:.1f}",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=dates, y=ctl, name="CTL (Fitness)",
        line=dict(color=COLORS["ctl"], width=2.5),
        fill="tonexty", fillcolor="rgba(96,165,250,0.12)",
        hovertemplate="%{y:.1f}",
    ), row=1, col=1)

    for e in events:
        color = {"A": "#fbbf24", "B": "#94a3b8", "C": "#6b7280"}.get(e["category"], "#94a3b8")
        fig.add_shape(
            type="line", xref="x", yref="y domain",
            x0=e["date"], x1=e["date"], y0=0, y1=1,
            line=dict(color=color, width=1, dash="dash"), row=1, col=1,
        )
        fig.add_annotation(
            x=e["date"], y=1, yref="y domain",
            text=e["name"][:18], showarrow=False,
            font=dict(size=8, color=color),
            xanchor="left", yanchor="top", textangle=-60, row=1, col=1,
        )

    # ── Projected CTL/ATL/TSB — deterministic EWMA rollout using planned_workouts' future
    # TSS, not a model. Horizon is whatever's actually on the calendar (TrainerRoad only
    # publishes the near-term block), so this is near-term, not a race-day forecast.
    if projected:
        proj_dates = [dates[-1]] + [p["date"] for p in projected]
        proj_ctl = [ctl[-1]] + [p["ctl"] for p in projected]
        proj_atl = [atl[-1]] + [p["atl"] for p in projected]
        proj_tsb = [tsb[-1]] + [p["tsb"] for p in projected]

        fig.add_trace(go.Scatter(
            x=proj_dates, y=proj_atl, name="ATL (projected)",
            line=dict(color=COLORS["atl"], width=1.5, dash="dot"), opacity=0.5,
            hovertemplate="%{y:.1f} (projected)",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=proj_dates, y=proj_ctl, name="CTL (projected)",
            line=dict(color=COLORS["ctl"], width=2.5, dash="dash"), opacity=0.6,
            hovertemplate="%{y:.1f} (projected)",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=proj_dates, y=proj_tsb, name="Form (projected)",
            line=dict(color=COLORS["subtext"], width=2, dash="dash"),
            hovertemplate="%{y:.1f} (projected)",
        ), row=2, col=1)

        for r in (1, 2):
            fig.add_shape(
                type="line", xref="x", yref="y domain",
                x0=dates[-1], x1=dates[-1], y0=0, y1=1,
                line=dict(color=COLORS["subtext"], width=1, dash="dot"), row=r, col=1,
            )

    # ── Panel 2: Form, zone-colored line + background bands ─────────────────
    tsb_min = min(tsb + [-30])
    tsb_max = max(tsb + [30])
    pad = 5
    bands = [
        ("risk",    tsb_min - pad, OPTIMAL_MIN),
        ("optimal", OPTIMAL_MIN,   GREY_MIN),
        ("grey",    GREY_MIN,      FRESH_MIN),
        ("fresh",   FRESH_MIN,     tsb_max + pad),
    ]
    for zone, y0, y1 in bands:
        # exclude_empty_subplots defaults to True, which silently drops shapes on any
        # row with no traces yet — row 2's own traces aren't added until after this loop.
        fig.add_hrect(y0=y0, y1=y1, fillcolor=ZONE_COLORS[zone], opacity=0.18,
                      line_width=0, row=2, col=1, exclude_empty_subplots=False)

    # dummy traces purely for a clean 4-entry legend (real segments below have showlegend=False)
    for zone, label in [("fresh", "Fresh"), ("grey", "Grey Zone"),
                        ("optimal", "Optimal"), ("risk", "High Risk")]:
        fig.add_trace(go.Scatter(
            x=[dates[0]], y=[None], mode="lines",
            line=dict(color=ZONE_COLORS[zone], width=2.5),
            name=label, legendgroup="form",
        ), row=2, col=1)

    for start, end, zone in _zone_segments(dates, tsb):
        fig.add_trace(go.Scatter(
            x=dates[start:end + 1], y=tsb[start:end + 1],
            mode="lines", line=dict(color=ZONE_COLORS[zone], width=2.5),
            showlegend=False, hovertemplate="%{y:.1f}",
        ), row=2, col=1)

    fig.add_hline(y=0, line_width=1, line_color=COLORS["subtext"], row=2, col=1)

    # ── Panel 3: weekly ramp rate ─────────────────────────────────────────────
    ramp_colors = [COLORS["ramp_up"] if v >= 0 else COLORS["ramp_down"] for v in ramp_values]
    fig.add_trace(go.Bar(
        x=ramp_dates, y=ramp_values, name="Ramp (CTL/wk)",
        marker_color=ramp_colors, showlegend=False,
        hovertemplate="%{y:.1f} CTL/wk",
    ), row=3, col=1)
    fig.add_hline(y=0, line_width=1, line_color=COLORS["subtext"], row=3, col=1)

    # ── Panel 4: FTP (actual, stepped) vs. eFTP (estimate) ───────────────────
    fig.add_trace(go.Scatter(
        x=dates, y=eftp, name="eFTP (estimate)",
        line=dict(color=COLORS["ftp_estimate"], width=1.5),
        hovertemplate="%{y:.0f}W",
    ), row=4, col=1)

    if ftp_history:
        ftp_x = [h["effective_date"] for h in ftp_history] + [dates[-1]]
        ftp_y = [h["value"] for h in ftp_history] + [ftp_history[-1]["value"]]
        fig.add_trace(go.Scatter(
            x=ftp_x, y=ftp_y, name="FTP (tracked)",
            line=dict(color=COLORS["ftp_actual"], width=2, shape="hv"),
            hovertemplate="%{y:.0f}W",
        ), row=4, col=1)

    fig.update_layout(
        **{k: v for k, v in LAYOUT.items() if k not in ("xaxis", "yaxis", "margin", "legend")},
        title=dict(text="Performance Management Chart", font=dict(size=15)),
        xaxis=dict(**AXIS), xaxis2=dict(**AXIS), xaxis3=dict(**AXIS), xaxis4=dict(**AXIS),
        yaxis=dict(title="CTL / ATL", **AXIS),
        yaxis2=dict(title="Form", **AXIS),
        yaxis3=dict(title="CTL/wk", **AXIS),
        yaxis4=dict(title="Watts", **AXIS),
        height=880,
        margin=dict(l=48, r=24, t=56, b=60),
        legend=dict(**{**LAYOUT["legend"], "y": -0.04}),
    )
    return fig

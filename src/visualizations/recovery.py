"""
Recovery dashboard — HRV trend + sleep hours, dual panel, 30 days.
"""

from datetime import date, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from src.db.schema import get_connection
from src.visualizations.theme import COLORS, LAYOUT, AXIS


def _rolling_avg(values: list, window: int = 7) -> list:
    result = []
    for i, v in enumerate(values):
        vals = [x for x in values[max(0, i - window + 1):i + 1] if x is not None]
        result.append(round(sum(vals) / len(vals), 1) if vals else None)
    return result


def _load_data(days: int | None):
    """days=None pulls full history — see pmc.py's _load_data for why."""
    conn = get_connection()
    if days is None:
        rows = conn.execute("""
            SELECT date, hrv, sleep_hrs, sleep_score FROM wellness ORDER BY date
        """).fetchall()
    else:
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT date, hrv, sleep_hrs, sleep_score
            FROM wellness WHERE date >= ? ORDER BY date
        """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build(days: int | None = None) -> go.Figure:
    rows = _load_data(days)
    if not rows:
        return go.Figure()

    dates     = [r["date"] for r in rows]
    hrv       = [r["hrv"] for r in rows]
    sleep_hrs = [r["sleep_hrs"] for r in rows]
    hrv_avg   = _rolling_avg(hrv, 7)

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.50],
        vertical_spacing=0.10,
        subplot_titles=["HRV (ms)  —  7-day rolling avg", "Sleep (hours)"],
    )

    # HRV raw dots
    fig.add_trace(go.Scatter(
        x=dates, y=hrv, name="HRV daily",
        mode="markers",
        marker=dict(color=COLORS["hrv"], size=6, opacity=0.4),
        hovertemplate="HRV: %{y:.0f} ms",
        showlegend=False,
    ), row=1, col=1)

    # HRV 7-day rolling avg line
    fig.add_trace(go.Scatter(
        x=dates, y=hrv_avg, name="HRV 7d avg",
        line=dict(color=COLORS["hrv"], width=2.5),
        hovertemplate="7d avg: %{y:.1f} ms",
        showlegend=False,
    ), row=1, col=1)

    # Sleep bars — green ≥7h, yellow 6–7h, red <6h
    sleep_colors = [
        COLORS["tsb_pos"] if (h or 0) >= 7 else
        COLORS["atl"]     if (h or 0) < 6 else
        "#fbbf24"
        for h in sleep_hrs
    ]
    fig.add_trace(go.Bar(
        x=dates, y=sleep_hrs, name="Sleep",
        marker_color=sleep_colors,
        hovertemplate="Sleep: %{y:.1f}h",
        showlegend=False,
    ), row=2, col=1)

    # 7h reference line
    fig.add_hline(
        y=7, line_width=1, line_dash="dash",
        line_color=COLORS["subtext"], row=2, col=1,
        annotation_text="7h target",
        annotation_font_size=9,
        annotation_font_color=COLORS["subtext"],
        annotation_position="top right",
    )

    fig.update_layout(
        **LAYOUT,
        title=dict(text="Recovery Signals — HRV & Sleep", font=dict(size=15)),
        yaxis=dict(title="HRV (ms)", **AXIS),
        yaxis2=dict(title="Hours", **AXIS),
        xaxis2=dict(**AXIS),
        height=480,
    )
    return fig

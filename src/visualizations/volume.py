"""
Annual training volume — TSS and estimated hours by year, sourced from wellness.
(Strava-sourced activities are not available via API, so we use ctlLoad from wellness
which is computed server-side from all activities.)
"""

import plotly.graph_objects as go
from src.analysis.activities import yearly_volume
from src.visualizations.theme import COLORS, LAYOUT, AXIS
from src.clock import today_local


def build() -> go.Figure:
    data = yearly_volume()
    if not data:
        return go.Figure()

    # Read when drawn, not when imported: at import it logged a warning on a machine with no config
    # yet (so `generate_charts.py --help` created data/), and a long-running server kept its first year.
    current_year = str(today_local().year)

    years      = [d["year"] for d in data]
    tss_vals   = [d["tss"] for d in data]
    hours_est  = [d["hours_est"] for d in data]

    bar_colors = [
        COLORS["tsb_pos"] if y == current_year else COLORS["ctl"]
        for y in years
    ]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=years, y=tss_vals,
        name="Annual TSS",
        marker_color=bar_colors,
        hovertemplate="<b>%{x}</b><br>TSS: %{y:,.0f}<br>~%{customdata}h est.<extra></extra>",
        customdata=hours_est,
        yaxis="y",
    ))

    fig.add_trace(go.Scatter(
        x=years, y=hours_est,
        name="Est. Hours",
        mode="lines+markers",
        line=dict(color=COLORS["subtext"], width=1.5),
        marker=dict(size=6),
        hovertemplate="~%{y}h<extra></extra>",
        yaxis="y2",
    ))

    fig.update_layout(
        **LAYOUT,
        title=dict(text="Annual Training Load (TSS) — all activities", font=dict(size=15)),
        xaxis=dict(title="Year", type="category", **AXIS),
        yaxis=dict(title="Training Load (TSS)", **AXIS),
        yaxis2=dict(
            title="Est. Hours",
            overlaying="y", side="right",
            showgrid=False,
        ),
        height=360,
        bargap=0.25,
    )
    return fig

"""
Sport distribution — donut chart, last 90 days by training time.
"""

import plotly.graph_objects as go
from src.analysis.activities import sport_distribution
from src.visualizations.theme import COLORS, SPORT_COLOR


def build(days: int = 90) -> go.Figure:
    data = sport_distribution(days=days)
    if not data:
        return go.Figure()

    # filter out zero-time sports
    data = {k: v for k, v in data.items() if v["hours"] > 0}
    if not data:
        return go.Figure()

    labels = [s.capitalize() for s in data]
    values = [v["hours"] for v in data.values()]
    colors = [SPORT_COLOR(s) for s in data]

    fig = go.Figure(go.Pie(
        labels=labels,
        values=values,
        hole=0.55,
        marker=dict(colors=colors, line=dict(color="#111827", width=2)),
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b><br>%{value:.1f}h (%{percent})<extra></extra>",
        textfont=dict(size=12),
    ))

    fig.update_layout(
        paper_bgcolor=COLORS["bg"],
        font=dict(family="Inter, system-ui, sans-serif", color=COLORS["text"], size=12),
        title=dict(
            text=f"Sport Mix — Last {days} Days",
            font=dict(size=15),
        ),
        showlegend=False,
        margin=dict(l=16, r=16, t=52, b=32),
        height=360,
        autosize=False,
        annotations=[dict(
            text="by time",
            x=0.5, y=0.5,
            font_size=11,
            font_color=COLORS["subtext"],
            showarrow=False,
        )],
    )
    return fig

"""
Weekly training load by sport — stacked bar chart, 12 weeks.
"""

from datetime import date, timedelta
from collections import defaultdict
import plotly.graph_objects as go
from src.analysis.activities import weekly_load_by_sport
from src.visualizations.theme import COLORS, LAYOUT, AXIS, SPORT_COLOR

SPORT_ORDER = ["cycling", "running", "strength", "swimming", "yoga", "walk", "hike", "other"]


def build(weeks: int = 12) -> go.Figure:
    data = weekly_load_by_sport(weeks=weeks)
    if not data:
        return go.Figure()

    # collect all sport keys present
    all_sports = set()
    for w in data:
        all_sports.update(k for k in w if k not in ("week", "total"))

    sports = [s for s in SPORT_ORDER if s in all_sports] + \
             [s for s in all_sports if s not in SPORT_ORDER]

    weeks_labels = [w["week"] for w in data]
    fig = go.Figure()

    for sport in sports:
        values = [w.get(sport, 0) for w in data]
        fig.add_trace(go.Bar(
            name=sport.capitalize(),
            x=weeks_labels,
            y=values,
            marker_color=SPORT_COLOR(sport),
            hovertemplate=f"{sport.capitalize()}: %{{y:.0f}}<extra></extra>",
        ))

    fig.update_layout(
        **{k: v for k, v in LAYOUT.items() if k not in ("legend", "margin")},
        barmode="stack",
        title=dict(text="Weekly Training Load by Sport", font=dict(size=15)),
        xaxis=dict(title=None, tickangle=-30, **AXIS),
        yaxis=dict(title="Training Load (HRSS)", **AXIS),
        legend=dict(**{**LAYOUT["legend"], "y": -0.12}),
        margin=dict(l=48, r=24, t=48, b=70),
        height=380,
    )
    return fig

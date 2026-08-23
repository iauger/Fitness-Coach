"""Shared Plotly theme and color palette."""

COLORS = {
    "ctl":        "#60a5fa",   # blue
    "atl":        "#f87171",   # red
    "tsb_pos":    "#34d399",   # green
    "tsb_neg":    "#f87171",   # red
    "form_fresh":   "#7dd3fc",   # light blue — positive form, race-ready
    "form_grey":    "#94a3b8",   # neutral — training normally
    "form_optimal": "#34d399",   # green — productive fatigue-accumulating zone
    "form_risk":    "#f87171",   # red — overreach risk
    "ramp_up":      "#34d399",
    "ramp_down":    "#60a5fa",
    "ftp_actual":   "#c084fc",
    "ftp_estimate": "#fbbf24",
    "cycling":    "#60a5fa",
    "running":    "#34d399",
    "strength":   "#fb923c",
    "swimming":   "#a78bfa",
    "yoga":       "#f472b6",
    "walk":       "#94a3b8",
    "hike":       "#a3e635",
    "other":      "#64748b",
    "hrv":        "#818cf8",
    "sleep":      "#38bdf8",
    "rhr":        "#fb7185",
    "bg":         "#111827",
    "surface":    "#1f2937",
    "border":     "#374151",
    "text":       "#f9fafb",
    "subtext":    "#9ca3af",
}

LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor=COLORS["bg"],
    plot_bgcolor=COLORS["surface"],
    font=dict(family="Inter, system-ui, sans-serif", color=COLORS["text"], size=12),
    margin=dict(l=48, r=24, t=48, b=40),
    legend=dict(
        bgcolor="rgba(0,0,0,0)",
        borderwidth=0,
        orientation="h",
        yanchor="top", y=-0.08,
        xanchor="center", x=0.5,
    ),
    hovermode="x unified",
)

# Shared axis style — merge into per-chart xaxis/yaxis dicts
AXIS = dict(gridcolor=COLORS["border"], linecolor=COLORS["border"], showgrid=True)

SPORT_COLOR = lambda sport: COLORS.get(sport, COLORS["other"])

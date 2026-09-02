"""
Local HTTP server — phase 12A of the CLI-to-app migration.

Read-only by design. This phase proves the wrapping works and changes no behaviour: the pages it
serves are the same `build_dashboard()` / `build_calendar_html()` output the CLI writes to
`data/charts/`, and every JSON route is an existing pure function from `src/analysis` returned
verbatim. No route writes to the database. Write paths arrive in 12D, after real migrations
land in 12B.

Serving the pages live rather than from the generated files means the dashboard is never stale —
`scripts/generate_charts.py` remains for producing standalone files to share, but is no longer
the way to look at your own data.

Binds to 127.0.0.1 by default. This process holds the intervals.icu and Anthropic keys through
`.env` and has no authentication, so it must not listen on a routable interface. Remote access
is item 16 and is a VPN's job, not this server's.
"""

from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.cache import CACHE
from src.analysis.activities import recent_activities, sport_distribution, weekly_load_by_sport
from src.analysis.cycle import cycles, cycle_metrics, cycle_for_review, latest_review
from src.analysis.derive import derived_metrics
from src.analysis.fitness import current_fitness, ctl_trend, peak_ctl
from src.analysis.load import ctl_correction, corrected_activities
from src.analysis.report import build_coaching_context, coaching_context_text
from src.analysis.training_plan import plan_summary, current_week
from src.analysis.weekly import summaries_between
from src.db.schema import get_connection
from src.visualizations.calendar_view import build_calendar_html
from src.visualizations.dashboard import build_dashboard

app = FastAPI(
    title="Fitness Coach",
    description="Local read-only API over the training database (phase 12A).",
    version="12A",
)


# ── pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, tags=["pages"])
def dashboard() -> HTMLResponse:
    return HTMLResponse(CACHE.get("dashboard", build_dashboard))


@app.get("/calendar", response_class=HTMLResponse, tags=["pages"])
def calendar(weeks: int = Query(6, ge=1, le=52)) -> HTMLResponse:
    return HTMLResponse(
        CACHE.get(f"calendar:{weeks}", lambda: build_calendar_html(weeks=weeks))
    )


# The generated file links to "calendar.html"; keep that working so the served page and the
# standalone file behave identically rather than one of them having a dead header link.
@app.get("/calendar.html", include_in_schema=False)
def calendar_html_alias() -> RedirectResponse:
    return RedirectResponse("/calendar")


@app.get("/dashboard.html", include_in_schema=False)
def dashboard_html_alias() -> RedirectResponse:
    return RedirectResponse("/")


# ── coaching context ──────────────────────────────────────────────────────────

@app.get("/api/snapshot", tags=["coach"])
def snapshot() -> dict:
    """The structured context the coach receives."""
    return build_coaching_context()


@app.get("/api/snapshot.txt", response_class=HTMLResponse, tags=["coach"])
def snapshot_text() -> HTMLResponse:
    """The snapshot exactly as rendered into the system prompt — useful for eyeballing it."""
    return HTMLResponse(f"<pre>{coaching_context_text()}</pre>")


# ── analysis ──────────────────────────────────────────────────────────────────

@app.get("/api/fitness", tags=["analysis"])
def fitness(trend_weeks: int = Query(8, ge=1, le=104)) -> dict:
    return {
        "current": current_fitness(),
        "trend": ctl_trend(weeks=trend_weeks),
        "peak": peak_ctl(years=9),
    }


@app.get("/api/derived", tags=["analysis"])
def derived() -> dict:
    """Every comparison the coach would otherwise make by eye — see analysis/derive.py."""
    return derived_metrics()


@app.get("/api/activities", tags=["analysis"])
def activities(days: int = Query(28, ge=1, le=3650)) -> list[dict]:
    return recent_activities(days=days)


@app.get("/api/load", tags=["analysis"])
def load(weeks: int | None = Query(12, ge=1, le=520)) -> dict:
    return {
        "weekly_by_sport": weekly_load_by_sport(weeks=weeks),
        "sport_distribution_90d": sport_distribution(days=90),
    }


@app.get("/api/load-correction", tags=["analysis"])
def load_correction() -> dict:
    """Rides intervals.icu analysed against an FTP other than the tracked one."""
    conn = get_connection()
    try:
        return {
            "ctl_correction": ctl_correction(conn),
            "activities": corrected_activities(conn),
        }
    finally:
        conn.close()


# ── plan, cycles, weeks ───────────────────────────────────────────────────────

@app.get("/api/plan", tags=["plan"])
def plan() -> dict:
    return {"position": plan_summary(), "cycles": cycles()}


@app.get("/api/plan/week", tags=["plan"])
def plan_week(on: date | None = None) -> dict:
    week = current_week(on)
    if week is None:
        raise HTTPException(404, f"{on or date.today()} falls outside any seeded plan")
    return week


@app.get("/api/cycles/{start_date}", tags=["plan"])
def cycle_detail(start_date: date) -> dict:
    """Full deterministic rollup for the cycle beginning on `start_date`."""
    match = next((c for c in cycles() if c["start_date"] == start_date.isoformat()), None)
    if match is None:
        raise HTTPException(404, f"no cycle starts on {start_date}")
    return cycle_metrics(match)


@app.get("/api/reviews/latest", tags=["plan"])
def review_latest() -> dict | None:
    return latest_review()


@app.get("/api/reviews/pending", tags=["plan"])
def review_pending() -> dict | None:
    """The cycle a review would cover if run now, or null if none has closed."""
    return cycle_for_review()


@app.get("/api/weeks", tags=["plan"])
def weeks(start: date | None = None, end: date | None = None) -> list[dict]:
    lo = (start or date(2000, 1, 1)).isoformat()
    hi = (end or date.today()).isoformat()
    return summaries_between(lo, hi)


# ── operational ───────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["ops"])
def health() -> dict[str, Any]:
    conn = get_connection()
    try:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in ("activities", "wellness", "planned_workouts",
                      "training_plan_weeks", "weekly_summaries", "cycle_reviews")
        }
        latest = conn.execute(
            "SELECT MAX(date) d FROM activities").fetchone()["d"]
    finally:
        conn.close()
    return {"status": "ok", "phase": "12A", "row_counts": counts,
            "latest_activity": latest, "cache": CACHE.stats()}


@app.post("/api/cache/clear", tags=["ops"])
def cache_clear() -> dict:
    """
    Drop cached page builds. Not a database write — the only non-GET route in 12A, and it
    touches nothing but this process's memory.
    """
    CACHE.clear()
    return {"cleared": True}

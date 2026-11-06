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

import sqlite3
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.cache import CACHE
from src.api.guard import Guard, TOKEN, TOKEN_PATH
from src.api.chat import router as chat_router
from src.analysis.activities import recent_activities, sport_distribution, weekly_load_by_sport
from src.analysis.cycle import cycles, cycle_metrics, cycle_for_review, latest_review
from src.analysis.derive import derived_metrics
from src.analysis.fitness import current_fitness, ctl_trend, peak_ctl
from src.analysis.load import ctl_correction, corrected_activities
from src.analysis.report import build_coaching_context, coaching_context_text
from src.analysis.training_plan import plan_summary, current_week
from src.planning import plans, tiers_store
from src.planning.constants import plan_name
from src.planning.outline import from_state as plan_outline
from src.planning.progression import TIERS
from src.planning.progression_store import current_state
from src.analysis.weekly import summaries_between
from src.db.schema import get_connection, read_connection
from src.db.migrations import status as migration_status
from src.visualizations.calendar_view import build_calendar_html
from src.visualizations.dashboard import build_dashboard
from src.clock import today_local

# ── the background maintenance loop ───────────────────────────────────────────

# How often the server pulls, rolls and pushes while it is running. Hourly is well inside
# intervals.icu's limits for an API key (5000 requests a day, 2500 per rolling 15 minutes;
# a cycle is a handful), and matches how fast the inputs actually move — wellness lands once
# a morning, an activity within an hour or so of a ride.
SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))

# Set FITNESS_COACH_NO_SCHEDULER=1 to serve without it. Wanted while developing, where an
# unexpected write to the live calendar from a server you forgot was running is a nasty
# surprise.
SCHEDULER_ENABLED = os.environ.get("FITNESS_COACH_NO_SCHEDULER", "") != "1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Start the maintenance loop with the server, stop it with the server.

    In the app rather than in scripts/serve.py so it runs however the app is started, including
    under `uvicorn --reload`, which imports the app by string and never executes serve.py.

    It is deliberately safe for this to overlap the Windows Scheduled Task: `run_cycle` takes a
    cross-process file lock and the loser records a skip. Both exist because they fail
    differently — the task runs when the server does not, and the server keeps the dashboard
    fresh while someone is actually looking at it.
    """
    scheduler = None
    if SCHEDULER_ENABLED:
        from apscheduler.schedulers.background import BackgroundScheduler

        from src.maintenance import run_cycle, summarise

        def tick():
            # A scheduled job that raises kills its own schedule in some configurations, and
            # a maintenance loop that silently stops is the exact failure this replaced.
            try:
                print(summarise(run_cycle()))
            except Exception as exc:
                print(f"[maintenance] unhandled: {exc}")

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            tick, "interval", minutes=SYNC_INTERVAL_MINUTES, id="maintenance",
            # One at a time, and never a backlog of catch-up runs after the laptop wakes:
            # each cycle re-derives everything from scratch, so only the latest is useful.
            max_instances=1, coalesce=True, misfire_grace_time=300,
            next_run_time=datetime.now() + timedelta(seconds=30),
        )
        scheduler.start()
        print(f"  maintenance loop every {SYNC_INTERVAL_MINUTES}min "
              f"(first run in 30s; FITNESS_COACH_NO_SCHEDULER=1 to disable)")
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(
    title="Fitness Coach",
    description="Local API over the training database, with a background maintenance loop.",
    version="12C",
    lifespan=lifespan,
)

app.add_middleware(Guard)
app.include_router(chat_router)


@app.get(TOKEN_PATH, tags=["ops"])
def session_token() -> dict:
    """The write token for this server process. Readable only by this server's own pages:
    the Guard refuses other Hosts and Origins, and no CORS header is ever set."""
    return {"token": TOKEN}


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
        raise HTTPException(404, f"{on or today_local()} falls outside any seeded plan")
    return week


def _ladder(conn) -> dict | None:
    """
    The configured plan's stored tier ladder, or None.

    None rather than a raise: these are read routes, and a plan that has not been bootstrapped
    should still be able to report that it has not been bootstrapped. The *write* path is where
    the refusal belongs, and `roller.store_horizon` has it.
    """
    plan = plans.by_name(conn, plan_name())
    return tiers_store.load(conn, plan.id) if plan else None


@app.get("/api/plan/state", tags=["plan"])
def plan_state(on: date | None = None) -> dict:
    """
    Where the gate has put the athlete, and why.

    Read-only: the catch-up runs with `persist=False`, because a GET must never record a
    training decision. Writing happens on sync, which is where it belongs.
    """
    today = on or today_local()
    with read_connection() as conn:
        ladder = _ladder(conn)
        state = current_state(conn, today, persist=False, tiers=ladder)
    return {
        "plan_name": plan_name(),
        "tier": state["tier"],
        "tier_hours": (ladder or TIERS)[state["tier"]].target_hours,
        "week_type": state["week_type"],
        "block_index": state["block_index"],
        "week_in_block": state["week_in_block"],
        "reason": state["reason"],
        "decided_through": state["decided_through"],
        "decided_weeks": state["decided_weeks"],
        "seeded_from": state["seeded_from"],
    }


@app.get("/api/plan/sessions", tags=["plan"])
def plan_sessions(start: date | None = None, days: int = 21) -> dict:
    """The committed horizon: dated sessions as prescribed, newest-first by date."""
    first = start or today_local()
    last = first + timedelta(days=days)
    with read_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT date, weekday, role, archetype, variant, prescription, minutes, tss, "
            "intensity_factor, volume_tier, week_type, block_name, week_in_block, "
            "external_id, remote_event_id, pushed_at "
            "FROM prescribed_sessions WHERE plan_name = ? AND date BETWEEN ? AND ? "
            "ORDER BY date, role", (plan_name(), first.isoformat(), last.isoformat()))]
    return {"plan_name": plan_name(), "start": first.isoformat(), "end": last.isoformat(),
            "sessions": rows}


@app.get("/api/plan/outline", tags=["plan"])
def plan_outline_route(on: date | None = None, weeks: int = 12,
                       met_in_four: int = 3) -> dict:
    """
    Shape beyond the horizon, never stored.

    A dated twelve-week plan is what this rebuild removed, so every week past the horizon is
    marked `projected` and the assumption it rests on is returned alongside it rather than
    buried.
    """
    today = on or today_local()
    with read_connection() as conn:
        ladder = _ladder(conn)
        state = current_state(conn, today, persist=False, tiers=ladder)
    return plan_outline(state, today, weeks=weeks, met_in_four=met_in_four, tiers=ladder)


@app.get("/api/plan/progression", tags=["plan"])
def plan_progression(limit: int = 26) -> dict:
    """The decision log: one row per closed week, and why the tier moved."""
    with read_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT week_start, ride_minutes, ride_days, strength_logged, outcome, "
            "tier_applied, verdict, tier_next, week_type_next, rotation, reason "
            "FROM plan_progression WHERE plan_name = ? ORDER BY week_start DESC LIMIT ?",
            (plan_name(), limit))]
    return {"plan_name": plan_name(), "weeks": rows}


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
    hi = (end or today_local()).isoformat()
    return summaries_between(lo, hi)


# ── operational ───────────────────────────────────────────────────────────────

@app.get("/api/health", tags=["ops"])
def health() -> dict[str, Any]:
    # Every lookup here is best-effort. On an unmigrated or partly-migrated database a missing
    # table raised OperationalError and the endpoint 500'd *before* reporting the migration
    # status that would have explained why — which is precisely when health is worth calling.
    counts: dict[str, Any] = {}
    latest = None
    conn = get_connection()
    try:
        for t in ("activities", "wellness", "planned_workouts", "prescribed_sessions",
                  "training_plan_weeks", "weekly_summaries", "cycle_reviews",
                  "plan_progression"):
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            except sqlite3.Error as exc:
                counts[t] = f"unavailable: {exc}"
        try:
            latest = conn.execute("SELECT MAX(date) d FROM activities").fetchone()["d"]
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    st = migration_status()
    degraded = any(isinstance(v, str) for v in counts.values()) or st["pending"]
    return {"status": "degraded" if degraded else "ok", "phase": "12C", "row_counts": counts,
            "latest_activity": latest, "cache": CACHE.stats(),
            "schema": {"version": st["current_version"],
                       "latest": st["latest_version"],
                       "pending": st["pending"]}}


@app.post("/api/cache/clear", tags=["ops"])
def cache_clear() -> dict:
    """
    Drop cached page builds. Not a database write; it touches nothing but this process's memory. Still a
    write by method, so it needs the token like every other.
    """
    CACHE.clear()
    return {"cleared": True}


# ── chat test harness ─────────────────────────────────────────────────────────

CHAT_TEST_PAGE = """<!doctype html><meta charset=utf-8><title>coach stream test</title>
<style>body{font:14px/1.5 ui-monospace,monospace;max-width:60rem;margin:2rem auto;padding:0 1rem;
background:#111;color:#ddd}#log{white-space:pre-wrap;border:1px solid #333;padding:1rem;
min-height:20rem;background:#0b0b0b}.t{color:#8bd}.e{color:#f88}input{width:75%;padding:.5rem;
background:#1a1a1a;color:#ddd;border:1px solid #333}button{padding:.5rem 1rem}</style>
<h3>coach stream test <small id=sid></small></h3>
<div id=log></div><p><input id=q placeholder="ask the coach..." autofocus>
<button onclick=go()>send</button></p>
<script>
let session=null;
const log=document.getElementById('log');
function add(t,c){const s=document.createElement('span');if(c)s.className=c;s.textContent=t;
log.appendChild(s);log.scrollTop=log.scrollHeight;}
async function go(){
  const q=document.getElementById('q');const question=q.value.trim();if(!question)return;
  q.value='';add('

> '+question+'

');
  const tok=(await (await fetch('/api/session')).json()).token;
  const r=await fetch('/api/chat/ask',{method:'POST',headers:{'Content-Type':'application/json','X-Fitness-Token':tok},
    body:JSON.stringify({question,session_id:session})});
  const reader=r.body.getReader();const dec=new TextDecoder();let buf='';
  while(true){const {done,value}=await reader.read();if(done)break;
    buf+=dec.decode(value,{stream:true});const parts=buf.split('

');buf=parts.pop();
    for(const p of parts){if(!p.startsWith('data: '))continue;
      const ev=JSON.parse(p.slice(6));
      if(ev.type==='session'){session=ev.session_id;document.getElementById('sid').textContent=session;}
      else if(ev.type==='text')add(ev.text);
      else if(ev.type==='tool_start')add('
['+ev.name+' ...]
','t');
      else if(ev.type==='tool_end')add('['+ev.name+' done]
','t');
      else if(ev.type==='error')add('
!! '+ev.message+'
','e');
    }}
}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
</script>"""


@app.get("/chat-test", response_class=HTMLResponse, include_in_schema=False)
def chat_test() -> HTMLResponse:
    """
    Bare harness for verifying the SSE pipe. The real chat interface is phase 12E; this exists
    only to prove tokens and tool events arrive incrementally rather than in one lump.
    """
    return HTMLResponse(CHAT_TEST_PAGE)

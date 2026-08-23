# Fitness Coach — Roadmap

This is the durable north star + backlog. `devlog.md` is the session-by-session log of what
happened and why; this file is where "what are we actually building toward" lives so it
doesn't have to be re-derived every session. Update it when priorities shift, not every session.

---

## North Star

A single-pane personal coaching app: one page that shows what a coach would actually look at
(fitness, power, recovery, plan adherence) and lets you talk to the coach right there —
replacing the current CLI-plus-five-separate-services juggling (TrainerRoad, Garmin Connect,
Strava, Google Calendar, intervals.icu) with one integrated view checked once a day.

This isn't a new direction — it's Phase 6 from the original Session 1 plan ("UI — web dashboard
+ chat"). Naming it explicitly here is the fix for the last 8 sessions feeling ad hoc: each
session solved a real problem in front of it (calendar sync, FTP staleness, field-mapping bugs)
without a standing reference for how it fit the larger shape. Every feature below should trace
back to this sentence.

---

## Current state assessment (as of 2026-08-23, Session 13)

**Solid — build on this:**
- Data pipeline: intervals.icu sync (full history + incremental), Strava archive fallback +
  dedupe, Google Calendar → planned workouts, compliance matching against a manually-tracked
  FTP history (not intervals.icu's stale synced value). Confirmed resilient across three
  different upstream sources now (Garmin, Strava, Zwift) — Zwift-via-TR turned out to give full
  power-meter data quality, comparable to Garmin, so the Garmin Connect sync issue is a
  bottleneck for outdoor rides only, not indoor ones.
- Coaching logic: methodology prompt, tool-use loop, transcript/memory persistence. This is the
  actual product differentiator and it works today — CLI-only, but functionally solid.
- **PMC 4-panel chart (Session 11, verified Session 13)** — Fitness/Fatigue, zone-colored Form
  with visible background bands, weekly Ramp Rate, FTP-vs-eFTP. Built in Session 11 but shipped
  with three silent rendering bugs (embedded-view clipping, invisible zone bands, undersized
  expand-modal) that made it through two sessions unnoticed because it was never actually opened
  in a browser. Found and fixed in Session 13 using Playwright MCP for direct visual + DOM
  inspection — this is now the first chart in the project confirmed correct by more than reading
  the code that generates it.
- Sleep/HRV (`recovery.py`) and weekly load-by-sport (`load.py`) — good per direct user feedback,
  x-axis/legend collision in `load.py` also fixed Session 13.
- **Calendar view (Session 10)** — week-grid HTML/CSS page (`calendar_view.py`), health stats +
  sport-colored activity blocks + compliance badges per day, verified against real multi-source
  data (Garmin ride, Zwift ride, both scored correctly) and confirmed structurally sound again
  under Session 13's Playwright review. Standalone page, linked from the dashboard header — the
  "standalone vs. embedded" open question is resolved (see below).
- **Global date-range filter (Session 13)** — one control bar (YTD/1mo/3mo/6mo/1yr/All presets +
  lookback slider) drives PMC, Load, and Recovery in sync via client-side `Plotly.relayout()`.
  See item 7 in the feature roadmap below for the full design + implementation record.
- **First real git commit made, Session 13** — `src/`, `scripts/`, `requirements.txt`, config
  files. 13 sessions overdue; local commit only, not yet pushed.

**Ad hoc / debt — not blocking, but real:**
- Schema evolution is one `migrate_db()` function accreting ALTERs forever. Fine at current
  scale; worth a real migration pattern if this keeps growing.
- Dashboard is a static-generated HTML file (`generate_charts.py` writes `data/charts/dashboard.html`),
  not a running app. Chat panel is a disabled "coming soon" placeholder — there's no backend
  process, so "chat right there" (the north star) literally can't happen without one.
- Sync is manual (`scripts/sync.py` / `sync_calendar.py` run by hand). Not compatible with
  "check once a day."
- Visual/browser-based verification wasn't possible for 12 sessions (no working MCP browser
  tool), so every chart shipped since Session 9 was verified only by reading its generating code,
  not by looking at it. Playwright MCP is now connected and working, and a standing rule requiring
  it for any visual change is codified in `CLAUDE.md` (Session 13) — the date-range filter was
  built and fully verified under that rule the same session it was designed, closing the gap that
  let the PMC bugs above ship undetected for two sessions.

---

## Design references (intervals.icu) — what we're mimicking

Two screenshots reviewed 2026-08-19 set the visual target for the next tier of work. Recorded
here in detail so a future session can build against this without re-deriving it:

**Fitness graph (PMC), specifically the Form panel.** intervals.icu's version is a 4-panel
stacked chart sharing one x-axis:
1. Fitness (CTL, blue line — 42-day EWMA of load) and Fatigue (ATL, purple line — 7-day EWMA),
   shaded area between/under, with dots marking event days.
2. **Form % panel — the highlighted feature.** The line is colored dynamically by which zone
   it's currently in: Fresh (light blue, high positive form), Grey Zone (neutral band), Optimal
   Training Zone (green — the zone the user specifically wants visible), High Risk (red, deeply
   negative form / overreach). Background bands are colored to match. This is materially
   different from our current `pmc.py`, which renders TSB as plain red/green bars with no zone
   banding or "optimal zone" concept at all.
3. Ramp panel — bar chart of weekly CTL ramp rate, colored by sign.
4. Ride FTP panel — two lines: actual FTP setting over time (stepped, since it only changes on
   manual updates) and eFTP (smooth estimated-FTP trend from the critical-power model).
5. Right-side sidebar: large current-value callouts (Fitness, Fatigue, Form, Ramp, FTP, eFTP)
   for "today at a glance."

**Power curve (mean-max power) — descoped, Session 11.** Was reviewed as a design reference
(log-scale duration vs. power, multiple time-window curves, critical-power eFTP model) but
never built: it needs the per-second power stream per ride (a whole new intervals.icu
streams-endpoint client, not just the summary fields we store), and the user already gets this
natively from intervals.icu whenever they want it. Not worth a parallel implementation. Removed
from the feature roadmap; kept here only as a record of why it was considered and dropped.

---

## Feature roadmap, by tier

### Now — visualization parity (this tier is complete)

1. ~~**Calendar view**~~ — **done, Session 10.** Week-grid, health stats + activities +
   compliance badges. Standalone page vs. embedded: resolved (kept standalone, linked from the
   dashboard header) — see Open Questions below.
2. ~~**PMC upgrade**~~ — **done, Session 11; bugs found + fixed + visually verified, Session
   13.** Zone-colored Form panel with visible background bands, Ramp Rate panel, FTP-vs-eFTP
   panel. Built Session 11 but shipped clipped/invisible-bands/broken-modal; not caught until
   Session 13's Playwright-based review actually opened it in a browser. Now confirmed correct.
3. ~~**Drop `distribution.py`/`volume.py` from `dashboard.py`**~~ — **done, Session 11.**
   Confirmed by reading current `dashboard.py`: only `pmc`, `load`, `recovery` are imported/
   embedded. Modules left in place, unwired as planned.
4. ~~Power curve chart~~ — **descoped, Session 11.** Would need a whole new streams-endpoint
   API client plus a real decision on per-second data storage, for a chart intervals.icu already
   shows natively and the user can just check there directly when wanted. Not worth building a
   parallel implementation of something the source-of-truth tool already does well. Not coming
   back on this roadmap unless that calculus changes.

### Next — make it a real app, not a static file

5. Small local backend (FastAPI or Flask) serving the dashboard + a chat endpoint wired to
   `src/coach/session.py`. This is what actually unlocks the north star's "chat right there"
   instead of the current CLI-only `scripts/ask.py`.
6. Automated sync — scheduled task (cron / Windows Task Scheduler) running `incremental_sync()`
   daily instead of manual script runs.
7. ~~**Global date-range filter across PMC + Load + Recovery**~~ — **done, Session 13.**
   Design finalized then implemented and Playwright-verified same session. Decisions locked in
   with the user before coding:
   - **One shared control, not per-chart zoom.** A single control bar (presets + slider) drives
     all three charts in sync via `Plotly.relayout()` — closer to intervals.icu's page-level date
     picker than Plotly's native per-chart `rangeslider`/`rangeselector` (which would give three
     independent zoom states, not what was asked for).
   - **Presets:** YTD, 1mo, 3mo, 6mo, 1yr, All. Slider is a single lookback-duration control
     (today minus N days), not a custom dual-handle range component — every preset is a
     relative-to-today window, so a two-sided slider would be unused complexity. Clicking a
     preset moves the slider to match; dragging the slider off an exact preset value clears the
     active-button highlight.
   - **Recovery chart is in scope** — changes from a hardcoded 30-day window to filter-controlled,
     same as PMC/Load, for one consistent dashboard-wide filter.
   - **Default view on load: 3 months.** Full history is one click away via "All".
   - Calendar view is explicitly out of scope — it's a navigable week-grid, not a dense
     time-series, filtering doesn't apply the same way.

   **Shipped as:** `pmc.py`/`load.py`/`recovery.py`'s `weeks`/`days` params now accept `None` =
   full history; `dashboard.py` builds all three unbounded and injects a control bar (preset
   buttons + a lookback-days slider) above the PMC chart. One `applyRange(startIso)` JS function
   calls `Plotly.relayout()` on all three chart divs, setting every subplot row's x-axis range
   explicitly (`CHART_AXES` map in `dashboard.py`) rather than assuming `shared_xaxes` propagates
   a scripted relayout — confirmed via direct DOM inspection that it does not propagate reliably
   enough to rely on. All Session-13-identified risks verified via Playwright + `browser_evaluate`
   before calling this done: expand-modal correctly inherits the active filter range (checked
   `plotDiv.layout.xaxis.range` post-clone, not just eyeballed); "YTD"/"All" clamp correctly
   (`2017-01-01` earliest-data floor, computed from `MIN(date)` across `wellness`+`activities`);
   free slider-drag correctly clears the active-preset highlight when it doesn't land on an exact
   preset value. Zero console errors/warnings across every preset, the slider, and the modal.

### Later — currently just aspirational goals text, not built

8. Multi-sport recommendation logic (original Phase 5) — today this is only descriptive
   sport-distribution stats, no actual "you should do X this week" reasoning.
9. Schema migration cleanup, first-class tests, eventual hosting (original Phase 1 note: "local
   first, designed to be hostable later").

---

## Open questions

- ~~Calendar view: standalone page vs. embedded panel?~~ Resolved, Session 11: kept standalone
  (day-grid layout doesn't fit the Plotly chart-card pattern), linked from the dashboard header.
- Should visual/browser verification (Playwright MCP, working as of Session 13) become a standing
  step before calling any chart work "done," given that the PMC rebuild passed two sessions of
  code-level review while actually broken? Leaning yes — not formalized anywhere yet.

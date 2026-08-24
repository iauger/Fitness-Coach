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
  actual product differentiator and it works today — CLI-only, but functionally solid. **Now the
  active focus** (Session 13): with the dashboard in a good place, next work is making its output
  more granular and informative — see "Now" tier below.
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
- **Data-integrity hardening (Session 14, Phase 0)** — three fixes found by a full-repo audit,
  all verified: `upsert_activities()` converted from `INSERT OR REPLACE` to `ON CONFLICT DO
  UPDATE` (the old pattern would have destroyed item 7's athlete notes on every sync);
  `icu_rpe` field mapping corrected and 9 years of history backfilled from `raw_json`
  (`activities.feel` 24 → 154 populated); `seed_profile.py` no longer reverts changed metrics to
  its `DEFAULTS` on a bare re-run.
- **Deterministic CTL/ATL/TSB forecast (Session 13)** — near-term projection (bounded by however
  far `planned_workouts` actually extends, not a race-day forecast) extending the PMC chart past
  today. See item 11 below for the full record, including why the ML-based deviation feature it
  was evaluated alongside is currently parked (real data check, not a guess).

**Ad hoc / debt — not blocking, but real:**
- **`planned_workouts` is a rolling ~30-day-back/14-day-forward window, not a full-history
  table** (`sync_calendar.py`'s defaults) — only 40 rows exist as of Session 13, spanning
  2026-07-20 to 2026-09-05. This isn't a bug (confirmed by checking the actual data, not
  assumed) — worth remembering next time low counts here look surprising, so it doesn't get
  mistaken for a broken sync/matching pipeline again.
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
- **FTP: nothing in this repo is broken — the remaining work is upstream in intervals.icu.**
  Investigated in the Session 14 audit. Nothing in the codebase has ever written FTP from a sync,
  by design (Session 8 made it manual precisely because intervals.icu's value was stale). Live
  state: `athlete_profile` holds `238.0 @ 2026-08-19`, manually set and confirmed by the athlete
  as correct; synced `eftp` reads **260.0**, which is intervals.icu's critical-power *model
  estimate*, not a value anyone set. `_manual_tss()` therefore computes against the right number
  and compliance scores are **not** distorted. The PMC chart's two series ("FTP (tracked)" vs
  "eFTP (estimate)") are correctly showing a real divergence.
  **Open task, athlete-side not code-side:** get intervals.icu's own FTP *setting* (`icu_ftp` —
  distinct from the `eftp` estimate) corrected to 238. Once that lands, API-derived load values
  become trustworthy and `_manual_tss()`'s necessity is worth re-examining. Not before.

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

### Done — visualization + dashboard parity (Sessions 9–13)

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
5. ~~**Global date-range filter across PMC + Load + Recovery**~~ — **done, Session 13.**
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

### Now — AI coaching output: more granular, more informative

User call, 2026-08-23 (Session 13): data pipeline + dashboard are in a good place, so the coaching
layer itself — the actual product differentiator per the north star — is the next priority.
**Not designed yet.** Scoping this the same way the date-range filter was scoped: discovery before
coding, decisions confirmed with the user before writing anything. What's already known about the
current setup, to ground that discovery rather than starting from a blank page:

6. ~~**Base check-in prompt refinement**~~ — **done, Session 14.** See "What shipped" at the end
   of this item for the outcome; the decisions that drove it are kept below for the record.
   - **Direction confirmed: richer prose in the same conversational voice**, not a structured
     per-dimension breakdown. Loosen the ~300-word cap and "four short paragraphs" guidance; keep
     the no-headers/no-bullets/no-bold rule as-is.
   - **Model: staying on `claude-haiku-4-5`.** Explicitly deferred evaluating Sonnet until
     prompt-level changes have been tried and shown to be insufficient — exhaust the cheaper model
     first, especially now that call volume is about to increase with the per-workout loop below.
   - Add an explicit instruction to proactively reach for `query_history`'s `peak_ctl_periods`/
     `ctl_range` for comparative context (e.g. "how does this block compare to the athlete's last
     comparable build") — the tool already exists, the prompt just never told the coach to use it.
   - Bump `MAX_TOKENS` (currently 1024 in `session.py`) defensively so longer output under the
     loosened cap doesn't get cut off mid-sentence.
   - **Live evidence, 2026-08-23:** a real `scripts/ask.py` transcript this session showed the
     model ignoring its own format rules under open conversation — bold pseudo-headers, five
     bulleted-in-prose points, ~500-600 words, despite the explicit "no bold, no bullets, under
     300 words" instruction. Use this transcript as the concrete before-snapshot when verifying
     the change (re-ask the same question, compare).
   - **Bundle in a real correctness fix (Session 14 audit):** `recovery_flags()`
     (`wellness.py:76-92`) lies to the coach about its own time windows. It calls
     `hrv_summary(days=14)` / `rhr_trend(days=14)` / `sleep_summary(days=7)`, but those functions
     hardcode their output keys as `avg_30d` regardless of the `days` argument — so the flag
     strings read "vs 30d avg" when the window is 14 days, and "in last 30 days" when it's 7.
     These strings are injected verbatim into the system prompt via `report.py:217-220`. Cheap
     fix, squarely on-topic for coaching output quality.

   **What shipped (Session 14).** `prompt.py` restructured into composable blocks —
   `METHODOLOGY` (domain knowledge), `VOICE` (global style), and per-mode `CHECKIN_FORMAT` /
   `CONVERSATION_FORMAT`, assembled by `build_system_prompt(snapshot, mode)`.
   - **Root cause of the format violations was structural, not model capability.** The prompt
     applied one format rule — "four short paragraphs, under 300 words," written for a weekly
     check-in — to *every* conversational turn in `ask.py`. Splitting by mode fixed the mismatch.
   - **Ordering mattered more than wording.** With voice/format stated up front, ~90 lines of
     athlete data sat between the rule and the response, and Haiku reliably reverted to markdown
     headers and bold (reproduced twice). Moving the output constraints to *after* the data
     snapshot, plus a terse `HARD_FORMAT_REMINDER` in final position, fixed it — verified live:
     zero bold/headers/bullets/numbered lists in both modes. **This is why the Sonnet question
     stays deferred:** the failure was prompt structure, not the model.
   - Check-in now targets 500-700 words (measured 508 on a live run); conversation answers at
     whatever length the question warrants (measured 739 on an expansive question, 322 on an
     earlier narrower one). `MAX_TOKENS` 1024 → 2048.
   - Added blocks: specificity guidance (anchor claims to a named session/date/signal rather
     than listing more data), tool-use guidance (`query_history` for comparative history,
     `calculate_ramp_target` for feasibility, the logging tools as things happen — the prompt
     never mentioned tools existed), and RPE interpretation (see item 7).
   - **Two further context bugs found while verifying, both fixed:** `METHODOLOGY` hardcoded
     "historical peak is ~66" when the data says **94.8** — and derived a target range from it,
     so the coach was being handed a figure that contradicted its own snapshot; now it reads
     peak from the snapshot instead of carrying a number. And `report.py` labelled intervals.icu's
     `rampRate` as "CTL/day" when it is **CTL/week** (verified: the 7-day CTL delta matches it
     exactly), putting it at odds with every ramp threshold in `METHODOLOGY`.
   - Also filtered null-`moving_time` stub rows out of `recent_activities()` — the dead Strava
     stub was rendering as `Unknown  0min  None` in the snapshot, feeding the coach noise.
     `calendar_view.py` already filtered these (Session 10); the coaching path didn't.

7. **Per-workout logging loop.** Full design converged 2026-08-23 (Session 13) — see "Coaching
   interaction model" below for the three-loop framing this and items 8–10 all sit inside.
   - **Trigger for an immediate coach response: system-detected**, reusing the existing
     `compliance_status`/`compliance_pct` logic in `compliance.py` (already classifies completed/
     partial/skipped by comparing actual vs. planned TSS) rather than requiring the athlete to
     self-flag a deviation. Zero new athlete effort; fires automatically once an activity syncs.
     **Audit correction (Session 14): key off `compliance_pct`, NOT `compliance_status`.**
     `compliance.py:101-106` assigns `"partial"` in both the `elif` and the `else` branch, so
     `PARTIAL_PCT = 40` is dead code and a 5%-completed workout is statused identically to an
     84% one. Status alone cannot express severity.
   - **Known limitation (Session 14 audit):** the trigger is structurally cycling-only.
     `compliance.py` matches `planned_tss IS NOT NULL` against `CYCLING_TYPES`, so strength and
     mobility sessions will accept RPE + notes but will never fire an immediate coach response.
     Acceptable, but stated here so it isn't a surprise at build time.
   - **RPE: uniform 1-10 scale (1 = easiest) across every sport.** For power-based cycling, RPE
     can be compared against `planned_if`/`planned_tss` to catch effort/plan mismatches. Strength
     and other non-power sports have no such baseline — RPE there is just another subjective
     signal, read alongside the note rather than checked against a target.
   - **Strength-session notes: free text only**, no structured muscle-group field. The coach reads
     muscle-group focus straight out of the note text — keeps schema simple, consistent with
     keeping RPE/notes at the activity level generally (see below).
   - **Data layer: RPE + note live directly on `activities`**, not a separate table — applies
     uniformly across sports including races, no special-casing needed. RPE is always tethered to
     an existing activity by design (consistent with "objective data first, subjective layered on
     top" — if something never became an activity, it isn't part of this layer).
     **Audit corrections (Session 14), both now resolved:**
     - The earlier claim that `activities` "has no athlete-editable column at all" was wrong.
       `schema.py:83` has declared `feel INTEGER, -- RPE 1-10` since Session 2. Reuse it for RPE
       rather than adding a duplicate column; only `athlete_note TEXT` is genuinely new.
     - **Hard prerequisite that would have silently destroyed this feature — now fixed.**
       `upsert_activities()` used `INSERT OR REPLACE`, which SQLite implements as DELETE +
       INSERT: any column not named in the statement resets to NULL. Since `incremental_sync()`
       runs at the start of *every* coach session and re-pulls the last ~2 days, athlete notes
       would have been wiped within days of being written. Converted to `ON CONFLICT DO UPDATE`
       (Phase 0) listing only synced columns. **When adding `athlete_note`, do NOT add it to that
       SET list** — the comment in `store.py` says so explicitly.
   - **Entry point — intervals.icu native, now substantially de-risked.** intervals.icu already
     carries `perceived_exertion`, `icu_rpe`, `session_rpe`, `feel`, `description` in synced
     `raw_json`. Session 14 audit found `icu_rpe` populated on **154** activities while
     `store.py` mapped only `perceived_exertion` (null on all 2,288 records) — 130 real RPE
     values were being dropped, same bug class as Session 7. Mapping fixed and history
     backfilled from `raw_json` in Phase 0; `activities.feel` went 24 → 154 populated.
     - **Polarity question RESOLVED empirically — no manual test needed.** Grouping the
       backfilled values against real power data shows a cleanly monotonic relationship:
       RPE 1 → avg NP 165W / TSS 22; RPE 4 → 237W / 86; RPE 8 → 284W / 95. `icu_rpe` is
       **1 = easiest, 10 = hardest**, matching the desired scale.
     - Still worth a one-off real-world test before declaring intervals.icu the primary entry
       point: log a *note* there and confirm whether activity-level `description` is
       athlete-editable post-ride text (it's a different field from the TR workout-description
       text already used in `planned_workouts`). RPE itself no longer needs verifying.
   - **RPE now reaches the coach (Session 14).** `recent_activities()` didn't select `feel`, so
     RPE was invisible to `build_coaching_context()` regardless of how well-populated the column
     was — adding prompt guidance without this would have been inert. Now surfaced as `rpe` and
     rendered in the snapshot's RECENT ACTIVITIES lines. `prompt.py` carries the full
     TrainerRoad scale semantics (1-2 Easy … 9-10 All Out, anchored behaviourally on "could you
     do one more interval?") so the coach reads a 6 as "Hard — real effort" rather than a generic
     mid-scale number, plus guidance to read RPE against prescribed IF/TSS for bike work, treat
     it as standalone for strength, and not remark on its absence as a matter of course.
     Confirmed working on a live run: the coach cited "the Redondo +1 at RPE 5" and "Spickard +3
     at RPE 6" and reasoned about the missing RPE on strength sessions unprompted.
   - **A CLI tool for manual entry/testing gets built regardless** (`scripts/log_workout.py`-style,
     writes directly to our DB) — useful for dev/testing independent of whether intervals.icu ends
     up being the primary real-world entry point.
   - The dashboard/calendar "clickable activity card" UI idea from earlier discussion is a
     plausible eventual target but explicitly deferred — it's blocked on item 12's local backend
     (static HTML has no write path back to the DB) and may be superseded entirely if the
     intervals.icu-native path above verifies out.

8. **4-week training-cycle review** (not calendar-monthly — a real TrainerRoad 3-week-build /
   1-week-rest mesocycle). Distinct from the weekly check-in: looks at performance across the
   *full* cycle and forward-looking thoughts heading into the next one, not just the current week.
   - **New `training_plan_weeks` table**, one row per week: `plan_name` (identifies distinct
     plans), `plan_start_date`, `plan_end_date`, `week_start_date`, `week_type` (enum: base /
     build / specialty / rest), `plan_week_number`. Denormalized plan dates on every row is fine
     at this scale (a full plan is only tens of rows).
   - **Cycle boundary detection: derive from `week_type = 'rest'`**, not `plan_week_number % 4`.
     A rest week always closes out the preceding cycle by definition, so it's the more robust
     signal — correctly handles any plan irregularity (a 5-week build before a rest week, a taper
     that breaks the normal rhythm) that fixed modular arithmetic on the week number would get
     wrong. `plan_week_number` stays in the table regardless — still useful for display/reference
     ("week 12 of the plan") — just isn't what cycle-boundary logic depends on.
   - **Rolling 28-day windows were explicitly rejected** — doesn't match TR's actual 3-on-1-off
     structure, would silently drift out of alignment with the real training blocks.
   - **Seeded by hand**, same pattern as `athlete_profile`/FTP history — a
     `seed_training_plan.py`-style script run once whenever a new TR plan loads (current plan
     started this week, runs through 2027-06-18). Not automatically inferred from synced data.
     Note: model it on the **Phase 0-corrected** `seed_profile.py`, not the original — the
     original silently reverted changed values on a bare re-run.
   - **New evidence (Session 14 audit), worth using as a cross-check:** `events` already contains
     `category='NOTE', name='Recovery Week'` rows at **2026-07-20** and **2026-08-17** — exactly
     4 weeks apart, matching TR's 3-on/1-off cadence. These come from the *previous* plan and
     `NOTE` is a loose contract, so hand-seeding stays the primary path — but validating
     hand-entered `week_type='rest'` rows against these is close to free and would catch a typo'd
     date.
   - **Prerequisite (Session 14 audit):** `incremental_sync()` only syncs/matches planned workouts
     ~2-3 days back (`sync.py:119` uses `sync_window()`'s start; `scripts/sync_calendar.py`
     defaults to 14), and nothing ever re-matches older rows. A 4-week review needs a *complete*
     4 weeks of matched compliance data — widen that window before building this.

9. **Race/event handling.** Folded into the existing coaching methodology rather than a separate
   mode or separate schema — a race is still just an activity with RPE+notes, same loop as any
   other workout. What changes is *interpretation*: add guidance telling the coach that near a
   flagged event, high RPE is expected rather than a red flag, and that in the days before an
   event it should be reading TSB/freshness rather than pushing more load. Scoped to `events`
   categories **A and B only** — C entries usually aren't real races and don't need the different
   lens. No new schema for post-race debriefs (result, conditions, goal-met) — richer race context
   comes out conversationally through the same free-text note rather than forced structured
   fields, since races are infrequent (a couple times a year) and don't justify dedicated columns.

   **DEFERRED — the A/B filter currently matches nothing (Session 14 audit).** Live `events` data
   contains zero A/B/C rows: the categories present are `TARGET` (12, weekly TSS targets), `NOTE`
   (4), `SEASON_START` (3), `PLAN` (2) — intervals.icu is returning TrainerRoad plan metadata, not
   races. The same root cause makes `pmc.py:150-162` (event marker lines + annotations on the
   Fitness & Fatigue panel) a permanently dead code path that has never rendered a marker.
   Before this is buildable: determine whether races simply haven't been entered in intervals.icu
   yet, or whether the category vocabulary differs from what was assumed. With no races on the
   calendar before spring 2027 this is the lowest-urgency item in the tier — revisit when there's
   an actual race to reason about.

10. **Context summarization for check-ins** — a cost-reduction mechanism, not just a quality one.
    Extends a pattern that already exists: `session.py`'s `end_session()` already summarizes a
    chat session into `coaching_log` instead of replaying the full transcript next time; this
    applies the same idea one layer up, to the per-workout/weekly/cycle data the loops above will
    start generating.
    - **The cost mechanic:** without this, every check-in's context-build re-injects raw
      historical data for however far back it looks — a *recurring* cost paid on every future
      check-in that includes a given period in scope. Summarizing once converts that into a
      *one-time* cost (a small LLM call per period) that all future check-ins then reuse instead
      of re-reading the raw data. Savings compound the longer the app runs.
    - **Split what needs an LLM from what doesn't.** Quantitative data (CTL/ATL, compliance pct,
      RPE averages, completed/partial/skipped counts) is already compact as raw numbers — no LLM
      call needed, a plain stats rollup in Python handles it for free. Reserve the LLM call
      specifically for compressing the *qualitative* free-text notes into a short narrative
      ("Tuesday's intervals felt heavy two weeks running"). Keeps the summarization calls small
      and cheap rather than re-summarizing numbers that didn't need it.
    - **Trigger points reuse item 8's structure, no new automation needed:** end of week
      (generated as a byproduct of the weekly check-in call itself) and end of 4-week cycle
      (triggered by hitting a `week_type='rest'` row).
    - **Storage: new `coaching_log.type` values** (`weekly_summary`, `cycle_summary`) — no new
      table, reuses existing infra.
    - **Two blockers in `report.py` to fix FIRST (Session 14 audit)** — both would silently
      defeat this design if summaries were written before they're addressed:
      1. `report.py:295` hard-truncates injected coaching memory at 300 chars
         (`lines.append(f"  {note[:300]}")`) — summaries would be cut off mid-sentence.
      2. `_recent_coaching_memory()` (`report.py:19-22`) is `ORDER BY created_at DESC LIMIT 3`
         with **no type filter**. Adding two new types means summaries compete for those 3 slots
         and can fully evict `checkin`/`session_summary` rows — the opposite of the tiering
         described below, which assumes they coexist.
    - **Context tiering this enables:** current week gets full raw detail; prior weeks in the
      current cycle get their weekly summaries; older cycles get cycle-level summaries; anything
      older than that isn't injected by default but stays fully reachable via `query_history` on
      demand. Summarization shrinks default context, it doesn't delete the underlying raw data.

11. **Traditional ML / statistical forecasting** — raised 2026-08-23 (Session 13).
    - ~~**Deterministic CTL/ATL/TSB forecast**~~ — **done, Session 13, same day.** Not ML — the
      known EWMA recursion intervals.icu already uses (CTL: 42-day constant, ATL: 7-day), run
      forward using `planned_workouts`' future TSS instead of actual. New `src/analysis/
      forecast.py`, `project_fitness()`. **Real constraint found and designed around:**
      `planned_workouts` only extends ~13 days out even with the sync script's 14-day-forward
      default — TrainerRoad is adaptive and only publishes the near-term block, not the full
      season. So this is a near-term "where does the current calendar put me" projection, not a
      race-day forecast — deliberately not oversold as more than it is. Non-power planned
      sessions (`planned_tss IS NULL` — strength, mobility) default to 0 TSS contribution,
      consistent with how the rest of the system already undercounts that load.
      Surfaced as a dashed/muted continuation of `pmc.py`'s Fitness & Fatigue and Form panels
      past a "today" boundary marker, reusing the date-filter infrastructure from earlier this
      session (`dashboard.py` gained `_latest_data_date()` so every filter preset's range end
      extends to cover the projected tail instead of clipping it at literal today). Chart-tool
      only for now (no coach tool), per the user's call — verified via Playwright: zero console
      errors, projection renders correctly on the main dashboard and inherits correctly into the
      expand modal, Load/Recovery unaffected by the range-end change.
    - **Not yet built — candidate genuine-ML angles**, to evaluate once item 7 (per-workout RPE)
      is generating real data: (a) a simple regression predicting expected RPE from planned
      IF/TSS + current CTL/ATL/TSB, flagging actual RPE as an outlier when it deviates — a more
      informed version of item 7's deviation trigger than TSS-completion alone; (b) using
      `life_events` (type=illness/injury) as historical labels against HRV/RHR/ATL-ratio trends
      leading up to them, as a genuinely learned early-warning signal rather than the fixed
      thresholds `wellness.py` uses today. Tooling preference when either gets built:
      dependency-light — scikit-learn if/when a real model is warranted, not a heavier ML stack.
    - **Feasibility check on (a), 2026-08-23 — checked real numbers, not assumed.** Currently
      near-zero viable: `icu_rpe` has 154 values across 9 years but only 2 in the last 3 months —
      not an active logging habit today, whatever it was in the past. Separately,
      `planned_workouts` only has 40 rows total (`matched_activity_id` + `compliance_status` set
      on just 3), which turned out **not to be a pipeline bug** — `sync_calendar.py`'s rolling
      `--days-back 30 --days-forward 14` window plus the fact that the athlete's actual
      structured cycling plan (`Summer Gravel Fitness`) only started 2026-08-18 means there's
      only ~6 days of real structured-cycling history to have matched anything against yet.
      That part will resolve on its own as the plan runs (roughly 3-4 TSS-bearing rides/week
      observed so far) — no fix needed. The real gate is RPE logging actually happening
      consistently, which is an adoption question tied to item 7's intervals.icu-native entry
      point, not a data-pipeline problem. Rough estimate once both are healthy: 2-3 months of
      consistent logging before there's even a bare-minimum usable dataset (30-50 labeled rows)
      for a 3-4 feature regression. **Conclusion: park this specific angle, don't scope it
      further, until item 7 has been live for a few weeks and there's real data to look at.**
    - FTP not updating correctly from intervals.icu is a known **separate** issue the user is
      troubleshooting directly — not an ML problem, don't build anything here that depends on
      that signal until it's confirmed fixed.

### Next — make it a real app, not a static file

12. Small local backend (FastAPI or Flask) serving the dashboard + a chat endpoint wired to
   `src/coach/session.py`. This is what actually unlocks the north star's "chat right there"
   instead of the current CLI-only `scripts/ask.py`. Also the enabling dependency for item 7's
   dashboard/calendar UI text-fill, if that path is still wanted after the intervals.icu-native
   path is verified.
13. Automated sync — scheduled task (cron / Windows Task Scheduler) running `incremental_sync()`
   daily instead of manual script runs.

### Later — currently just aspirational goals text, not built

14. Multi-sport recommendation logic (original Phase 5) — today this is only descriptive
    sport-distribution stats, no actual "you should do X this week" reasoning.
15. Schema migration cleanup, first-class tests, eventual hosting (original Phase 1 note: "local
    first, designed to be hostable later").

---

## Coaching interaction model (Session 13 design)

The "more granular and informative" ask turned out to need more than a prompt edit — it needed an
actual model of how the athlete and coach interact, at what cadence, with what data. Converged on
three loops, each with different purpose, timing, and voice:

1. **Per-workout** — mostly about getting subjective data (RPE + notes) into the data landscape
   alongside the objective sync, *not* about chasing instantaneous feedback on every entry. The
   coach only responds immediately when something's flagged as deviated/failed (item 7) — routine
   logging is capture, not conversation.
2. **4-week cycle review** — the periodic aggregate loop, scoped to TR's actual mesocycle
   structure rather than calendar-monthly (item 8). Looks backward across the full cycle and
   forward into the next one.
3. **Race/event** — not a separate system, a different interpretive lens applied to the same
   per-workout loop near a flagged A/B event (item 9).

Item 10 (summarization) is the plumbing that lets loops 2 and 3 stay cheap as loop 1 generates
more data over time. Items 7–10 together are the actual spec for "Now" tier work going forward —
this section exists so that spec doesn't have to be re-derived from conversation history.

---

## Open questions

- ~~Calendar view: standalone page vs. embedded panel?~~ Resolved, Session 11: kept standalone
  (day-grid layout doesn't fit the Plotly chart-card pattern), linked from the dashboard header.
- Should visual/browser verification (Playwright MCP, working as of Session 13) become a standing
  step before calling any chart work "done," given that the PMC rebuild passed two sessions of
  code-level review while actually broken? Leaning yes — not formalized anywhere yet.

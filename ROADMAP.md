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

## Where this is now — 2026-11-06

The single source of truth for current state. Everything below the horizontal rule after this
section is **history**: the reasoning as it was at the time, kept because the arguments are
still worth reading, and superseded wherever it disagrees with this.

**Working and in daily use.** Sync from intervals.icu, the adherence gate, the rolling
three-week horizon, calendar push with delivery to Zwift confirmed on the device, the dashboard
and calendar, and the Claude coaching layer.

**The refactor is done.** Steps 0–7, plus two pieces that were not planned:

| | |
|---|---|
| 0 | `src/config.py`, strict at load |
| 1 | `plans` table — identity survives a rename |
| 2 | athlete timezone, not the server's |
| 3 | the 11 personal constants read from config |
| 4 | one sport table instead of six |
| 5 | tier ladder derived from the athlete's own history |
| 5a | weekly template generated, 2–7 ride days *(unplanned)* |
| 5b | outgrown check, surfaced to the coach *(unplanned)* |
| — | ladder versioning: a decision keeps the rungs it was graded against |
| 6 | archetype overrides, with non-configurable safety rules |
| 7 | a first run that says what to do next |
| 8 | which days you can train: `available_days`, `long_ride_day` *(unplanned; a prerequisite for the UI)* |
| 9 | how long a ride may be: `max_ride_min`, `day_max_ride_min`, derived inside and never clamped, with the coach told when it binds *(unplanned)* |

Schema at `user_version` 11. 978 tests.

**The first run has been walked on a clean clone, 2026-11-06**, end to end by someone other
than the code: clone, config, migrate, fetch, bootstrap, sync, seed FTP, serve. It worked,
and the derivation reproduced the live ladder exactly (T0 2.08h to T4 4.08h) from a separate
database — the first independent check that bootstrapping is reproducible rather than an
artefact of the machine it was written on.
Cloning again before the first push found three things the real checkout had been hiding:
`pytest` wrote `config/athlete.yaml` during *collection* (a parametrize argument reads the
config before any fixture runs), `serve.py --help` did the same, and `seed_profile.py --help`
created `data/fitness.db`. All fixed, and the README-commands test now runs from a throwaway
copy so it can see them.

It also found the one class of damage this project can do. **The calendar push is now off
until configured on**, in the example, in the dataclass default, and — the part that was
actually broken — in `sync_now.py`, which passed `push_enabled=not args.no_push` and so
overrode the setting on every ordinary run. A second copy of this project derives the same
plan slug and therefore the same `external_id`s, so the sandbox overwrote all twelve events
of the real horizon on its first sync. Nothing was lost, because both had derived the same
plan from the same history; that is luck, not a safeguard.

**Next: a UI, not hosting.** Decided 2026-11-06: the project has outgrown being a CLI, and the
things a person has to do by hand — the first run, deriving a ladder, setting FTP, turning the
push on, a check-in — are flows, which is where a real frontend pays and generated HTML does
not. The shape is a JS frontend (Vite, React, TypeScript) built to static files and served by the
same FastAPI process, over the API that already exists; the chart pages stay as they are and are
embedded rather than rewritten. Streamlit was considered and rejected: it would be a second
server beside the API, reruns its script on every interaction, and has no routing, which a
resumable chat and a multi-step setup both want.

The screens, taken from what TrainerRoad's plan builder does well and leaving out what it does
not (billing, opaque progression): a setup wizard driven by `firstrun.checks()` so the CLI and
UI cannot drift; a calendar as the home screen; a plan screen showing the tier *and why* from the
decision log, the ladder, the horizon with frozen sessions marked, and regenerate-as-a-new-
version behind a preview; the existing analytics embedded; the coach. Config stays the source of
truth — the UI validates and writes it through the loader, and must preserve its comments, which
are half the documentation, so `ruamel.yaml` rather than PyYAML.

**The Origin check and token are built** (`src/api/guard.py`, `tests/test_api_guard.py`). A page
you visit can make your browser send requests to `127.0.0.1:8800`, and DNS rebinding can let it
read the replies; some writes are a calendar push or a billed chat call. The rule is by method, in
one middleware, so a route added later is covered without anyone remembering: every request needs
a loopback `Host` and, if it sends one, this server's own `Origin`; every non-GET also needs the
per-process `X-Fitness-Token` from `GET /api/session`. No CORS header is ever set, which is what
keeps the token from other origins. A dev frontend on another port is therefore refused by design:
serve the UI from this app, or widen deliberately with `FITNESS_COACH_ALLOWED_HOSTS`. A small slice
of the authentication work, not the whole of it. Config writes go through `src/config_write.py` (ruamel.yaml: comments survive, the result is validated before an atomic replace).

**The first screens are built** (2026-11-06): `/app`, one HTML file with no build step, served by
the same process so it shares the guard's origin. *Setup* is `firstrun.checks()` as a checklist
(`GET /api/setup`, the same list `status.py` prints) and *Settings* edits availability, ride limits,
long-ride day, lifting days and the calendar push through `PUT /api/settings`, which refuses
anything outside a pinned whitelist (no server host or port, plan slug or coach model) and returns
the checklist again so a stale ladder shows on the save that caused it. This is vanilla JS rather
than the Vite/React/TypeScript above, deliberately for now: two screens do not earn a toolchain,
and the move costs one file. The *Ladder* screen (`/app#ladder`) re-derives under the current settings and shows what would
change against the stored ladder, rung by rung and field by field, before "Use this ladder" stores
it as a new version; a second button rebuilds the stored sessions and never pushes. The derivation
lives in `src/planning/ladder_ops.py`, shared with `bootstrap_plan.py`. The *Plan* screen is the
home (`/app`): the rung and why, this week against what would move you (the thresholds come from
`classify_week`, and a test pins them to it at the boundary), the next three weeks with locked and
on-calendar marked, the projected shape, the weeks the gate has graded, and the goals. It is one
read-only call, `GET /api/plan/overview`, built in `src/planning/overview.py`. The setup checklist now
does the steps that were commands: database, history (a background job with progress,
`src/history_fetch.py`, which takes the maintenance lock), FTP (range-checked, a dated entry) and
building the plan (never pushes). Walked end to end on a fresh clone in a browser. The API key stays
in `.env` on purpose. Goals are edited on the Settings screen and live in the `goals` table
(`src/goals.py`), the same rows the coach reads and writes; the config's `goals:` is a first-run
seed. That removed a drift where the plan screen and the coach counted down to different dates.
The *Coach* screen (`/app#coach`) streams the conversation over the existing SSE route: past
conversations resume, tool use is shown, and a tool that *changes* something (goals, life events,
how you feel) is marked with what it did so a change made in chat is never invisible. Everything
the model writes is rendered as text nodes, never HTML. `coach.enabled` and `coach.model` are now
actually read (`src/coach/settings.py`): they were declared and used by nothing. Asking it why the
plan says what it does found the coach telling the athlete that promotion needs "three times in a
row"; the gate needs one met week, so the snapshot now states the rule exactly from the gate's own
numbers (`gate_thresholds`). The weekly check-in and the block review now run from that screen, with a status
(`src/coach/due.py`, `GET /api/chat/due`): the weekly one is due once a week has closed since the
last check-in, overdue at two weeks or never done, and a progression event (the tier moved up or
down, or a missed week) is its own reason; it never prompts within two days of the last one. The
block review is due when a block of the *current* plan has closed with no review filed. A count
shows on the Coach tab on every page and a card on the Plan screen. Both runs sync first, are
recorded only when the model returned something (the log row is what the status reads, and an
empty review row would mark a block reviewed for ever), and share their briefing with the command
line (`checkin_inputs`, `cycle_review_inputs`), which now syncs before it builds the briefing
rather than after. What the coach remembers is now one mechanism (`src/coach/memory.py`). Conversations are stored
in one place whether they happen in the app or on the command line (`ask.py` writes to the same
tables; its JSON files and `transcripts` rows are retired, and `scripts/import_transcripts.py`
brought the old ones across). A note is written when a conversation ends, and an abandoned one is
caught up when the next conversation or check-in begins, so what you say in the app now reaches
the coach's memory (before, it did not at all). The note is made with no tools and its own short
prompt: the old command-line summary re-ran the whole coaching loop, with every tool available,
over the whole snapshot. The coach is shown the last four conversation notes and the last
check-in, and the Coach screen shows the athlete exactly that list, marked, with Edit and Forget
on conversation notes. Still to build: coach suggestions as buttons (raise the limit, add a day)
instead of text.

**Ride limits are built** (`max_ride_min`, `day_max_ride_min`), as the second half of what the UI's
setup wizard has to ask. The decision that matters is *derive inside, never clamp*: a rung is
graded against 90% of its minutes, so a ride shortened to fit leaves a rung the gate expects and
the calendar cannot deliver, and the athlete holds at one tier for ever with nothing saying why.
The ladder therefore climbs to what the limit allows and stops; when it has, the coach raises it
with the options and what each costs (`outgrown.py`); and a limit tightened after the ladder was
derived blocks the roll with a message instead of quietly changing the plan.

Measuring found three things. Climbing only the long ride under a 90-minute limit gave five-minute
rungs and a top of 3.58h against 4.5h of capacity, so the easy rides now take up the climb once
the long ride is at its limit. `Tier.target_min` counted one easy ride whatever the ride count,
so at five rides the gate expected 195 minutes against 315 scheduled — fixed, and held to the
generator's own output. And a limit at or below current riding used to fail as "T4 does not ask
for more than T3"; it now names the setting and the number.

Not built, on purpose: **fewer than five rungs.** A limit at or below what you already ride has
no room for a progression, so it is refused with the amount to raise rather than given a short
ladder. Variable rung counts touch the gate (`CAP_TIER` is a module constant that the plan's own
ladder does not override), so it waits for someone who needs it.

**What is left before this is safe to host:** authentication. `serve.py` holds the intervals.icu
and Anthropic keys, has no login, and `/api/chat/ask` spends money per call, so it binds
loopback and refuses to be useful anywhere else.

**Then, in order:** eFTP tracking, because four different FTP numbers disagreed and the one in
use was chosen by argument rather than measurement; CTL computed locally from `effective_tss`,
so one FTP correction fixes the whole history instead of only new rides; then execution
scoring, power curve, workout alternates, and verifying Garmin delivery.

**Known and deliberate:** intervals.icu does not recompute historical TSS when FTP changes, so
`activities.tss` is stale for anything before the correction. Everything this project reasons
with is recomputed by `_manual_tss`; `wellness.ctl` is still intervals.icu's and therefore
built on the old number. Not a risk to the gate, which reads `SUM(moving_time)` and never
touches TSS — a choice made for a different reason that turned out to insulate it from this.

---

## The coach's signal ledger: agreed plan, 2026-11-07

Not built. Agreed in conversation, written here so the build can be checked against it.

**Why.** The coach remembers through short prose notes, which are hard to query, restate numbers
that go stale, and fall out of a four-note window (a knee that ached in week 1 is gone by week
3). Replace them, as the main memory, with a **ledger of signals**: every thing the athlete says
about illness, stress, work, motivation, nutrition, how training feels, and so on, cheaply
catalogued against a timestamp, so the coach is briefed with a short watch list and can pull
longer ranges through a tool that is SQL, not a model call.

**Decisions taken**

- **Mentions only.** One append-only table of mentions; a "thread" (the same knee issue over
  three weeks) is the same kind and topic grouped within a few weeks, worked out when read.
  Revisit with a thread table only if reads prove slow or awkward.
- **`life_events` is folded in.** Its rows migrate into the ledger (they are signals with a date
  range) and `log_life_event` writes there. `subjective_feel` stays separate: it is a numeric
  time series, not events.
- **Extraction after the conversation**, one cheap call, not the coach logging as it goes.
- **Conversations lock when closed.** A conversation ends when the athlete ends it (or starts a
  new one, or quits the command line), or after it has been idle too long (60 minutes proposed).
  A closed one is read-only: it can be reopened to read, never continued, and asking in it is
  refused with "start a new one". A check-in is therefore a discrete event, not an ongoing
  thread. Extraction runs once, at close; there is no re-summarising a conversation that
  "continued", so the `summarised_at < updated_at` staleness rule goes. A failed extraction is
  retried on the next catch-up.
- **Signals are stored silently**, no per-conversation confirmation. They are visible and
  correctable afterwards on the memory screen. A dismissal is a soft delete with a reason (the one
  mutable column), so dismissals become known-wrong examples for the validation set.
- **Advisory only for now, with a seam so that it need not stay that way.** A signal can raise a
  check-in prompt and sit prominently in the briefing; it does not change a prescription by
  itself, because a mis-graded "burnout" must not reshape the plan. It is expected to influence
  progression later, so the hand-off is designed now (see "The plan-advisory seam" below) and is
  live from the first release: advisories are computed and *shown* as "what the signals would do,
  not applied", which is also how their quality gets judged before anyone lets them act.
- **No model-written "about you" blob.** The always-shown picture of the athlete is derived by
  code from long-running threads, plus the athlete's own `athlete.notes` from the config.

**The kinds** (a text column validated in code, so adding one is not a migration; a kind the
model invents becomes `other`, and the rate of `other` is how we know the list needs a change):
illness, injury_pain, poor_sleep, fatigue; workouts_too_hard, workouts_too_easy,
missed_sessions, feeling_strong; motivation_low, motivation_high, burnout; work_event,
family_caregiving, travel, schedule_change, stress; wants_more, wants_less, goal_change,
preference; nutrition, gear; other. A free-text `topic` sits beside the kind ("left knee",
"night shifts") so the list stays short.

**The row.** Observed-at (UTC) and the athlete's local date it refers to (which can be earlier
than the conversation: "I was ill last week"); kind; topic; severity 0-3; state (new, ongoing,
improving, resolved); a short quote; optional start and end dates for episodes; where it came
from (conversation and message, check-in note, activity note, feel log, migrated life event,
manual); the extractor version; and the dismissal columns.

**Severity.** 0 a passing mention; 1 worth knowing; 2 affecting training; 3 needs action now
(illness, injury, burnout language). The model grades against that rubric and **code adjusts
it**: the same kind and topic three times in two weeks goes up a level (capped at 3); an
unrepeated level 2 drops off the watch list after 21 days unless its state is ongoing; a level 3
stays until it is resolved, dismissed, or 28 days silent. Level 3 and recurring items sort first.

**The extractor.** One call per closed conversation (and per check-in note), Haiku, forced JSON.
It sees only the athlete's own words plus the currently open threads, so it can mark one
"ongoing" or "resolved" instead of duplicating it. **Every signal must carry a quote that appears
word for word in the athlete's text**; one that does not is discarded, the cheap guard against
invented signals. It also returns follow-ups the athlete committed to and a gist of at most 40
words, which replace the long prose note.

**What the coach is shown.** The watch list (open threads at severity 2 or more, or recurring,
with first seen, last seen and count); a one-line 28-day digest; the derived athlete picture; and
the conversation gists and follow-ups, age-stamped, with an instruction that when the data
disagrees with a note, the data wins. A `query_signals` tool (by kind, topic, severity and range,
as a summary or a timeline) for anything longer. Check-in text stops being memory except its
follow-ups. About 400 tokens in the briefing.

**Validation, which is the point of the backfill.** The real corpus is small: 4 ride notes, 6
feel notes, 1 life event, 2 weekly narratives and 5 conversations (3 real, 2 test), about 18
texts. Those, plus about 20 written cases for the kinds the real data lacks (burnout, nutrition,
illness, work event, too hard...), are labelled once and reviewed by the athlete in a single
pass, then frozen as `tests/fixtures/signals_gold.json`. `scripts/eval_signals.py` runs the
extractor over it and reports precision and recall by kind and lists every false positive. It is
on demand, not in the default suite, because it costs money and a model is not deterministic;
everything else (validation, severity maths, threads, queries, locking) is ordinary
deterministic tests with mutation checks. Bar to start trusting it: no kind at severity 2 or 3
with more than 10% false positives on the gold set. The backfill of the real texts into the
ledger is the first run of that evaluation. The `life_events` row migrates with no model call.

**Phases, each useful on its own**

1. **Locking.** *(built)* Closed conversations are read-only, idle auto-close after 60 minutes,
   an "End conversation" control, the command line locked at quit, and a closed one opens read-only
   from the list. Check-ins follow the same rules, so a reply to one is possible until it is ended.
   Each conversation is noted once, at close; the "continued after it was noted" case is gone.
2. **Ledger, no model.** *(built)* `src/signals.py` and migration 13: the `signals` table,
   validation, `record`, `dismiss`/`restore`, thread grouping, the severity maths, the watch list,
   summary, timeline and digest. `life_events` rows were carried in (the table is left in place,
   unwritten) and `log_life_event` writes the ledger; the coach's briefing and `query_history`
   read it. Nothing writes signals from conversations yet: that is phase 3.
3. **Extractor, gold set, evaluation, backfill.** *(built; gold set awaiting review)* Replaces the
   note writer on the same triggers (`src/coach/extract.py`, wired through `memory.summarise`):
   one forced-tool Haiku call per closed conversation, filing signals, follow-ups (on the
   conversation) and a 40-word gist. `scripts/eval_signals.py` scores it against the gold set
   (`src/coach/signal_eval.py`); `scripts/backfill_signals.py` reads the athlete's earlier feel
   notes, ride notes and conversations (preview by default). Exit: the bar above on the frozen
   gold set, which is `tests/fixtures/signals_gold.draft.json` until the athlete has reviewed it.
4. **Briefing and tool.** Watch list, digest, derived picture, `query_signals`, age-stamped
   gists, drop check-in-as-memory.
5. **Screens.** A timeline with filters and dismissal, a watch strip on the Plan screen, and
   severity as a reason for a check-in prompt. The plan-advisory seam goes in here, displayed and
   not applied.
6. **Later.** Let a first, easier-only advisory act on the plan (see the seam); correlate signals
   with HRV and load; feed `workouts_too_easy` into the ladder's outgrown check; ingest ride notes
   as they sync.

**The plan-advisory seam.** A pure function, `plan_advisories(watch_list, gate_state)`, turns
the watch list into typed advisories: `hold` the tier this week, `ease` the week, `add_recovery`,
`consider_harder`, each with a reason and the ids of the mentions behind it, and `applied: false`.
They appear on the Plan screen and in the coach's briefing as not applied. The gate
(`next_state`) gains an optional `advisories` argument that defaults to none; turning an action
on later is one config entry per action (`plan.signal_effects`, absent today), not a rewrite,
and a test pins that with nothing applied every decision is byte-identical to today's. Nothing is
declared in config until something reads it. Rules of the road when it is switched on: only
actions that make the plan easier or hold it (never one that raises the load) are eligible to
apply on a single signal; every applied advisory is written beside the decision it affected, so
the log still says why; and the athlete can decline one, which is itself recorded.

**Risks.** "Burnout" and "illness" are health-adjacent: the coach states them as observations
("you mentioned..."), never as diagnoses, and every signal is visible and deletable. With a few
conversations a week the ledger is thin for months, so the digest must read fine when it is
nearly empty. Recall is the hard part: offhand phrasing is what the gold set exists to test.

## Historical: state as of Session 16, 2026-09-02

*Superseded by the section above. Parts of this describe integrations since retired — the
TrainerRoad iCal feed, Google Calendar as a planned-workout source — and a coaching layer
described as CLI-only that now has an API and a dashboard behind it.*

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
- **LTHR in intervals.icu is stale at 177, and it silently corrupts every HR-derived figure
  (Session 16, athlete-side fix).** Across every hard session in the current block the athlete
  peaks at 159-173 and averages 142-146 while riding at 87-96% of FTP. The zone table puts the
  top of HR Z2 at 158, so his entire threshold workload lands inside "Z2 = easy" — an HR-based
  intensity split returned **90% easy** for a block containing nothing but sweet spot and
  threshold. Caught only because the athlete said the number looked wrong.
  Scope is wider than the intensity metric: `hr_load`/TRIMP is scored against these zones, and
  that is the *only* load signal for strength, yoga and every other non-power session — so
  cross-sport load is miscounted too. Nothing in this repo can fix it; LTHR has to be reset in
  intervals.icu. A hard session peaking at 173 suggests something nearer 160.
- ~~**14 commits sit unpushed on `main`**~~ — **pushed 2026-08-29.** 16 commits went to
  `github.com/iauger/Fitness-Coach`, the first push in 13+ sessions. `devlog.md` was removed from
  the 15 unpushed commits first and is now gitignored: the repo is **public**, and the devlog
  carried the athlete's email, intervals.icu athlete id, and 65 lines of HRV/RHR/sleep/weight
  figures. Rewriting was free only because nothing had been pushed — redacting in a later commit
  would have left the strings browsable in history. `f650011` was left untouched so the push was
  an ordinary fast-forward. A local `backup-before-devlog-removal` branch still holds the
  pre-rewrite history *including* the full devlog and must never be pushed.
- **`log_life_event` still depends on the model choosing to call it.** Accepted rather than
  fixed: unlike the check-in's feel scores, a life event genuinely only surfaces mid-conversation
  and has no structured point of capture to write from. Worth revisiting if the `life_events`
  table stays near-empty (1 row as of Session 16).
- ~~**Google Calendar OAuth token is dead and calendar sync is down (Session 15).**~~
  **RESOLVED by deleting the OAuth path entirely (Session 15).** The dead-token symptom
  (`RefreshError: invalid_grant`, caused by the consent screen sitting in Testing status, which
  expires refresh tokens every 7 days) prompted a look at the alternatives on the calendar's
  settings page. **The public iCal feed is strictly better than the API on every axis:** no
  auth, no Cloud project, no token, no expiry — and 319 events reaching 2027-08-27 versus the
  40 rows the API path had. Switched; `google_calendar.py` and `auth_google_calendar.py` deleted
  and all four google-auth/api packages dropped from requirements. Config is now one env var,
  `GCAL_ICAL_URL`.
  - **Caveat:** the feed is readable by anyone holding the URL — a property of the calendar's
    sharing setting, not of the integration. Google can regenerate the address to rotate it.
  - **Google is still a middleman** and refreshes the calendar it subscribes to on its own
    schedule (sometimes 8-24h behind TrainerRoad). That latency existed on the API path too, so
    it is not a regression. Pointing `GCAL_ICAL_URL` at TrainerRoad's own iCal export would cut
    Google out and be fresher; `tr_calendar.py` reads standard VEVENTs and would need no change.
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
  ~~**Open task, athlete-side not code-side:** get intervals.icu's own FTP *setting* (`icu_ftp`)
  corrected to 238.~~ **DONE by the athlete, 2026-08-28 (Session 15).** Verified against the API:
  `sportSettings[0].ftp = 238` and `indoor_ftp = 238`, matching the tracked `athlete_profile`
  value. The `eftp` model estimate is unchanged at 260 and is *expected* to differ — it's a
  critical-power fit, not a setting.
  **New consequence to decide on: the change is not retroactive.** Each activity bakes in the FTP
  it was analyzed under. The 2026-08-27 ride carries `icu_ftp = 238`; the 08-25 and 08-22 rides
  carry **297**. So intervals.icu's own `icu_training_load` understates TSS on everything analyzed
  before the fix (too-high FTP makes the same watts look easier), while `_manual_tss()` computes
  correctly against the tracked 238 throughout. Options: leave it (the divergence is bounded and
  historical), force a re-analysis of affected activities upstream, or prefer `_manual_tss()` over
  `icu_training_load` for the affected date range. Not urgent — but `_manual_tss()` cannot be
  retired yet, contrary to the note above.

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
     - **Note-entry test RUN and RESOLVED (Session 15) — but the answer was not `description`.**
       Two real notes were written in intervals.icu on the 2026-08-25 and 2026-08-27 rides.
       `description` came back **null on both**, on the list *and* the detail endpoint (identical
       183-key payloads — there is no richer detail response). So did `notes`, `note`, `comment`,
       `feel`, `perceived_exertion`, `tags`, and `attachments`. Activity-level `description` is
       **not** athlete-editable post-ride text; it appears to be an import-populated field only.
     - **Notes live in a chat thread, at `GET /activity/{id}/messages`.** intervals.icu models
       post-ride commentary as a conversation, not a field — which is also why it supports coach
       replies. Both notes were there intact, attributed and timestamped. Message shape:
       `{id, athlete_id, name, created, type: "TEXT", content, deleted, ...}`.
     - **`icu_chat_id` on the activity is an exact predicate for "has a note"** — non-null on
       precisely the 2 activities with threads, null on the other 21 since 2026-07-01 (and on
       2,432 of 2,434 across all history). So this costs a couple of requests per sync, not one
       per activity. This is what makes the native path cheap enough to be the default.
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
   - **Notes now reach the coach (Session 15) — the read path is DONE.** What shipped:
     `IntervalsClient.get_activity_messages()`; `athlete_note TEXT` on `activities` via
     `migrate_db()` *and* `init_db()`; `flatten_messages()` / `sync_activity_notes()` in `sync.py`;
     `store.update_activity_notes()`; wired into `incremental_sync()`; surfaced through
     `recent_activities()` as `note` and rendered untruncated under each RECENT ACTIVITIES line;
     `scripts/backfill_notes.py` for history. Verified: a full re-sync repopulates both notes
     rather than clobbering them.
     - **`athlete_note` is written by a targeted UPDATE, never by `upsert_activities()`.** Notes
       are fetched only for activities carrying an `icu_chat_id`, so folding the column into the
       main upsert's SET list would null it out on every *other* activity in the same window —
       a different failure mode from the Phase 0 `INSERT OR REPLACE` bug, same end result.
     - **The note scan uses its own 28-day lookback** (`NOTE_LOOKBACK_DAYS`), not `sync_window()`'s
       ~2 days. Notes get written whenever the athlete gets round to it, not when the ride uploads
       — both real notes were written the following morning, and the 8/25 one was already outside
       the activity window. Costs one extra list request; chat threads are still only fetched for
       the handful of activities that have one.
     - Note fetching is wrapped in its own try/except *inside* the activities block, so an API
       failure there can't cost us the activity rows already written.
   - **`prompt.py` now tells the coach how to use notes**, not just that they exist: engage with
     the athlete's reasoning rather than reflecting it back, answer questions he raises about the
     training itself with an actual view (agreeing or disagreeing), and treat RPE and note as
     independent signals that can legitimately disagree (RPE 5 with a note describing a hard final
     set means the average was moderate and the end was not).
   - **Weekday labels added to snapshot dates** (`report.py:_with_weekday`). Caught on the first
     live run: the coach called 2026-08-27 "Tuesday" and 2026-08-25 "Sunday" — both wrong. It was
     inferring weekdays from bare ISO dates, which matters precisely *because* athlete notes refer
     to sessions by weekday ("same basic workout as Tuesday"). Re-test named both correctly.
   - **A CLI tool for manual entry/testing is now OPTIONAL, not required** (`scripts/log_workout.py`
     -style, writing directly to our DB). The native path verified out, so this drops from
     "primary entry point" to a dev/testing convenience — build it if a need appears.
   - The dashboard/calendar "clickable activity card" UI idea from earlier discussion is a
     plausible eventual target but explicitly deferred — it's blocked on item 12D's write paths
     (static HTML has no write path back to the DB) and may be superseded entirely if the
     intervals.icu-native path above verifies out.

8. **4-week training-cycle review** (not calendar-monthly — a real TrainerRoad 3-week-build /
   1-week-rest mesocycle). Distinct from the weekly check-in: looks at performance across the
   *full* cycle and forward-looking thoughts heading into the next one, not just the current week.

   **Schema + plan position: DONE (Session 15). The review itself is still to build.**
   - ~~**New `training_plan_weeks` table**~~ — **built and seeded.** One row per week:
     `plan_name`, `plan_start_date`, `plan_end_date`, `week_start_date`, `week_end_date`,
     `week_type` (base/build/specialty/rest), `phase`, `phase_number`, `phase_week_number`,
     `plan_week_number`; `UNIQUE(plan_name, week_start_date)`. Denormalized plan dates on every
     row is fine at this scale.
     - **`phase` was added beyond the original design** so a rest week still records which block
       it closed out. `week_type='rest'` overwrites the phase name it sits in, so without a
       separate column a rest week is orphaned from its parent block and a cycle review can't
       say "the rest week that closed Base block 2".
   - **Seeded plan (2026-08-28): TrainerRoad 2026-27, 45 weeks, 2026-08-17 → 2027-06-27.**
     Base 12wk → Build 8wk → Specialty 8wk → Build 8wk → Specialty 8wk → Recovery 1wk. Rest weeks
     are **every 4th week of every phase** (confirmed by the athlete), giving 11 clean 4-week
     cycles that each land exactly on a phase boundary, plus the trailing 1-week recovery block.
   - **`scripts/seed_training_plan.py`** takes a declarative `PHASES` + `REST_WEEKS` spec and
     expands it to week rows. Validates that every phase starts on a Monday and that phases are
     contiguous — both would otherwise fail silently and misalign every downstream week number.
     Idempotent (`ON CONFLICT DO UPDATE`), and drops orphan weeks when a plan is re-cut.
   - **`src/analysis/training_plan.py`** — `current_week`, `cycles`, `current_cycle`,
     `just_completed_cycle`, `plan_summary`, `cycle_data_complete`.
     `just_completed_cycle()` is the review trigger: it returns a cycle exactly once, on the
     Monday after that cycle's rest week ends, so a caller fires a review without tracking state.
     Verified to fire exactly 12 times across the plan's 320 days.
   - **`cycle_data_complete()` guards every derived figure.** Before rest weeks were confirmed,
     the plan read as a single 45-week "cycle" with the next rest week 43 weeks away — plausible
     enough to mislead. Cycle fields now return `None` in that state and the snapshot says so
     explicitly; week and phase position stay valid regardless, since they don't depend on
     rest-week placement.
   - **Surfaced to the coach** as a TRAINING PLAN POSITION block plus `prompt.py` guidance on
     what cycle position *means* — rising fatigue in week 3 of a build is the plan working, the
     same reading in week 1 says the last rest week didn't do its job, flat CTL in a rest week is
     intended. Live-verified: the coach placed him in "week two of a four-week base block", named
     week 3 as where fatigue bites, and called the 2026-09-07 rest week unprompted.
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
   - ~~**New evidence (Session 14 audit), worth using as a cross-check:** `events` contains
     `category='NOTE', name='Recovery Week'` rows 4 weeks apart.~~ **DEAD IDEA — checked and
     rejected (Session 15).** Pulling `events` across the full plan span shows those rows belong
     to a *different, stale* plan: `plan_applied: 2026-05-26`, a `PLAN` event described as
     "Peak phase: race intensity, sharpening, taper" tagged `Peak`, with nothing at all past
     2026-09-21. intervals.icu knows nothing about the TrainerRoad plan, so those NOTEs would
     have validated hand-seeded rest weeks against the wrong plan entirely. Hand-seeding is the
     only path, exactly as originally designed.
   - **Still to build: the review itself.** Everything above is position and structure; nothing
     yet *reviews* a completed cycle. Remaining work: aggregate compliance / load / recovery /
     RPE / notes across the cycle `just_completed_cycle()` returns, and generate the review.
   - ~~**BLOCKING prerequisites**~~ — **both cleared by the iCal switch (Session 15).** The
     narrow ~2-day sync/match window and the dead OAuth token are gone together: calendar sync
     now runs unauthenticated over 60 days back / 120 forward on every run, so a workout whose
     activity synced late gets re-matched and a full mesocycle of compliance data always exists.
     `planned_workouts` went 40 → 319 rows. **Item 8's review is unblocked.** First cycle closes
     **2026-09-14**.

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

10. ~~**Context summarization for check-ins**~~ — **DONE (Session 16).**
    **What shipped:** `weekly_summaries` table; `src/analysis/weekly.py` (deterministic rollup +
    storage, no API dependency, reusing `cycle.py`'s load/recovery helpers);
    `src/coach/summarize.py` (the only place a model compresses rather than coaches);
    `scripts/weekly_summary.py` (backfill, regeneration, `--metrics-only` inspection with no API
    call); `checkin()` summarises the just-completed week as a byproduct, wrapped so a
    summarisation failure can't lose the check-in; and `report.py` tiers the context — raw
    per-session detail for 14 days, earlier weeks of the current cycle as stored summaries,
    anything older left out and still reachable via `query_history`.
    - **The model is called only when a week contains free text.** A week with no notes stores
      a null narrative at zero cost — two of the three backfilled weeks made no API call.
    - **Measured on real data:** 1005 chars of notes compressed to 462, keeping where fatigue
      appeared in the session, his doubt about the volume of sweet spot, and the
      three-sessions-a-week constraint, while mentioning no TSS, CTL, adherence or HRV figure.
      Rendered saving is 59% for a note-free week and 32% for a week with two notes — the saving
      grows with note density, which is the curve this item exists to absorb. At present volume
      it is a modest win; it matters once a note per session accumulates.
    - Raw activity detail is what gets the horizon because it is the only block that grows
      without bound. `DERIVED METRICS` is already compact and fixed-size, and coaching memory is
      bounded by its own character budget.

    *Original design notes below, kept for the reasoning.*

    **Context summarization for check-ins** — a cost-reduction mechanism, not just a quality one.
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
    - ~~**Storage: new `coaching_log.type` values**~~ — **superseded (Session 16).** The cycle
      summary is now a `cycle_reviews` row, not a `coaching_log` type. Structured storage does
      something a text blob can't: it makes cycle-over-cycle comparison a SQL query rather than
      something the model has to recall, which is the main thing a review offers over a
      check-in. Named columns for the figures compared across cycles, `metrics_json` for the
      full rollup. A future `weekly_summary` should follow the same shape rather than going
      back into `coaching_log`.
    - ~~**Two blockers in `report.py` to fix FIRST (Session 14 audit)**~~ — **both fixed,
      Session 16.** They were worse than the audit estimated: *every* one of the 9 stored
      `coaching_log` rows exceeded 300 chars (842-3023), so ~85% of each was being discarded
      mid-sentence on every injection. Replaced with the most recent entry of each type, in
      full, under a 6000-char budget that trims oldest-first on a paragraph boundary and marks
      that it did. Memory went from ~900 chars of fragments to 3222 chars of whole entries.
    - **The cost mechanic above still holds, but the quantitative half is already done**
      (Session 16). `src/analysis/derive.py` computes the whole stats rollup deterministically
      and is *not* persisted — see the "compute vs store" rule in the architecture notes below.
      What remains for item 10 is only the qualitative-narrative compression.
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
    - ~~FTP not updating correctly from intervals.icu is a known **separate** issue the user is
      troubleshooting directly~~ — **resolved 2026-08-28**, `icu_ftp` now reads 238. Note the
      non-retroactivity caveat in "Current state assessment" above before using pre-08-27
      `icu_training_load` as an ML feature.

### Next — make it a real app, not a static file

**Target shape, decided 2026-09-02: a local web app.** FastAPI serving a browser UI, running on
the athlete's machine against the existing SQLite. That *is* the desktop experience — a native
shell (Tauri, pywebview, Electron) is a later wrapper around the same server, not a rewrite.
Remote access (Tailscale, or self-hosting on a local server) is a downstream add-on that changes
nothing about the application itself. See item 16.

Sequencing rationale: the local server is the common substrate for every destination. Building it
first defers the auth-and-hosting decision until the app exists and its usage is known, rather
than designing around a guess. Nothing here forecloses hosting later.

**What already carries over — the migration is smaller than "CLI to app" suggests:**
- **`src/analysis/*` (~2,300 lines) is already a service layer.** Every function is pure: reads
  SQLite, returns dicts, no I/O, no printing, no interactivity. `derive.py`, `cycle.py`,
  `weekly.py`, `training_plan.py`, `load.py`, `compliance.py` need **zero** changes to sit behind
  HTTP endpoints. The only `print()` in the package is inside a `__main__` block.
- **The charts are already composable fragments.** `dashboard.py` emits
  `full_html=False, include_plotlyjs=False`, so each figure is a fragment rather than a page.
  The hard part of a chart migration is done.
- **The chat panel markup and CSS already exist** in `dashboard.py` as a disabled placeholder.
- `src/db`, `src/intervals`, `src/integrations`, `src/athlete` and the sync pipeline are
  unaffected. Four dependencies total — no framework debt to unwind.

12. **Local backend + web UI.** Replaces the static-file dashboard and the CLI entry points,
    while leaving the scripts working as a fallback. Sub-phases, in order:
    - ~~**12A — FastAPI skeleton, read-only.**~~ **DONE 2026-09-02.** `src/api/app.py` (19
      routes), `src/api/cache.py`, `scripts/serve.py`. Pages at `/` and `/calendar` with
      `.html` aliases redirecting so the generated file's header link still works, plus JSON for
      snapshot, fitness, derived metrics, activities, load, load-correction, plan, cycles,
      reviews and weekly summaries. FastAPI's `/docs` comes free.
      - **Verified by byte comparison, not by eye.** The served pages are byte-identical to what
        `generate_charts.py` writes once Windows text-mode newline translation is normalised —
        `write_text()` turning `
` into `
` was the entire 544-byte difference. For a phase
        whose claim is "nothing rendered changed", that is a stronger check than a screenshot.
      - **The cache needed real thought.** `build_dashboard()` takes ~3.8s. Cached against a stat
        fingerprint of the database, **keyed on `fitness.db-wal` as well as the main file** — the
        connection runs in WAL mode, so a write lands in the sidecar and can leave the main
        file's mtime untouched until a checkpoint. Keying on the main file alone would have
        served a stale dashboard after every sync. Measured 0.88s cold to 0.006s warm.
      - Binds `127.0.0.1` and warns on any other host: this process reads `.env`, so it holds the
        intervals and Anthropic keys and has no authentication.
    - ~~**12B — Real schema migrations, before any write path.**~~ **DONE 2026-09-02.**
      `src/db/migrations.py` + `scripts/migrate.py`. `PRAGMA user_version` is the authoritative
      pointer and `schema_migrations` holds history — no new dependency, which suits a project
      with four of them and no ORM. Alembic would have pulled in SQLAlchemy.
      - **It found a real bug, not just untidiness.** `init_db()` and `migrate_db()` had drifted:
        `training_plan_weeks`, `cycle_reviews` and `weekly_summaries` existed **only** in
        `migrate_db()`, so `scripts/fetch_history.py` — which calls `init_db()` alone — would
        leave a fresh database missing three tables. Both are now thin delegates over one ordered
        set of migrations and can no longer disagree.
      - `except Exception: pass` around every ALTER could not distinguish "already applied" from
        a syntax error or a missing table. `add_column()` checks `PRAGMA table_info` instead.
      - Migration 001 is an idempotent baseline of the whole schema, so it converges an empty
        database and the existing one on the same result — no fragile "does this database predate
        migrations" heuristic. Verified on a copy first: a database built purely from migrations
        has identical tables, column names, column order and indexes to the live one, the only
        delta being `schema_migrations` itself. Applied to the real ~19MB database after backup,
        with every row count unchanged.
    - ~~**12C — Streaming chat. The one genuinely hard piece.**~~ **DONE 2026-09-02.**
      `src/coach/stream.py`, `src/coach/chat_store.py`, `src/api/chat.py`, migration 002.
      - `stream_turn()` is the same loop as a generator of typed events (`start`, `text`,
        `tool_start`, `tool_end`, `turn_end`, `error`). `run_turn()` drains it and returns the
        final text, so `checkin()`, `cycle_review()` and `CoachSession` were untouched. Bounded
        at `MAX_ITERATIONS`; a failing tool reports the error back to the model rather than
        aborting the turn.
      - **`transcripts` did not serve for persistence**, contrary to the plan above: it stores one
        summary row written at the end, where a resumable conversation needs every turn as it
        happens. Migration 002 adds `chat_sessions` and `chat_messages`. Content is stored as
        JSON, not text — an assistant turn that used a tool is a list of content blocks that must
        round-trip exactly or the next API call is malformed.
      - **SSE over WebSocket**: traffic is one-directional once a question is asked, and SSE needs
        no protocol upgrade or extra dependency. The synchronous SDK runs on a worker thread
        feeding an asyncio queue. The system prompt is built **once per session, not per turn** —
        it carries the ~8,100-token snapshot, so rebuilding it each turn would re-query the
        analysis layer and defeat prompt caching.
      - **A bug the delegation exposed:** the old loop returned on `end_turn` *without* recording
        the assistant turn, so `CoachSession.ask()` appended it as plain text. `stream_turn`
        records every turn as blocks, so that append would have duplicated the answer in the
        history sent with the next question.
      - Verified live: first token at 1.41s and the turn complete at 2.07s, delivered over 0.65s
        rather than in one lump; `tool_start`/`tool_end` surfacing correctly across a second loop
        iteration; sessions persisting, resuming in context, and titled from the opening question.
      - `/chat-test` is a bare harness proving the pipe. The real interface is 12E.
    - **12D — Write paths, which are what a UI actually buys.** `checkin.py`'s `prompt_for_feel()`
      `input()` calls become a form. Then per-activity RPE and notes — item 7's deferred
      "clickable activity card", which was explicitly blocked on this because static HTML has no
      write path back to the DB.
    - **12E — Frontend. Needs the Playwright MCP reconnected first.** It went down mid-session
      on 2026-09-02; the install is healthy (package v0.0.80, Chromium present, Node 24.13.0)
      so it is a session-level drop, recoverable via `/mcp`. 12A and 12B did not need it —
      byte comparison was the better check — but 12E is where `CLAUDE.md`'s rule stops being
      a formality, since a chart that renders wrong at a different viewport is exactly the
      failure mode that survived two sessions of code review before.
      Charts stay Plotly; the server hands over JSON. For a single user,
      resist a React SPA: server-rendered templates plus htmx gives interactivity with no build
      step, and the date-filter JS already written in `dashboard.py` ports directly.
    - **12F — Packaging.** One command or a desktop shortcut, so running it is not
      `python scripts/*.py`. This is also the natural point to add a native shell if wanted.
    - **Prompt caching should be designed in here, not retrofitted.** Measured 2026-09-02: a
      coaching call is **~8,100 input tokens** (system prompt + snapshot) and ~900 output tokens.
      `CoachSession` resends full history every turn, so a long chat pays full price repeatedly.
      Cache reads cost ~0.1x and writes ~1.25x (5-minute default TTL, 1-hour available), which is
      close to a 90% input saving on turns 2+.

13. **Automated sync and scheduled jobs.** Currently `incremental_sync()` runs manually or at the
    start of a coach session, weekly summaries ride on someone running `checkin.py`, and the
    cycle-review trigger needs a human to notice. A server can own all three — APScheduler
    in-process is sufficient. **Note the constraint this creates:** scheduled jobs only run while
    the machine is awake, which is the main argument for item 16's hosting option later.

### Later — currently just aspirational goals text, not built

14. Multi-sport recommendation logic (original Phase 5) — today this is only descriptive
    sport-distribution stats, no actual "you should do X this week" reasoning.
15. Schema migration cleanup (pulled forward into 12B — it becomes load-bearing there), first-class
    tests, eventual hosting (original Phase 1 note: "local first, designed to be hostable later").
    **There are currently no tests.** Read-only CLI scripts inspected by eye are one thing; HTTP
    write endpoints are another. Tests become necessary at 12D, not before.

16. **Remote access — deferred, and deliberately not designed for yet.** Costed 2026-09-02.
    Two wants look similar here and are not the same thing:
    - **Phone access is nearly free.** Put **Tailscale** in front of the local server: the phone
      joins the tailnet and hits the desktop directly. No public exposure, no auth to build
      (device auth is the VPN's job), no data migration, no deploy pipeline, $0 on the personal
      tier. Cloudflare Tunnel is an equivalent alternative, with Cloudflare Access for email-OTP
      login, also $0. **Neither needs any change to item 12's app.**
    - **Always-on is what costs money.** Both options above require the desktop to be awake, which
      collides with item 13's scheduled sync and with a cycle review firing while you are away.
      Real hosting (Fly, Railway, Render — Render's free tier spins down and would break scheduled
      sync) is roughly **$5-7/month**, but the dollars are the small part: it mandates auth
      (without it the app is an open proxy to the `ANTHROPIC_API_KEY`), secrets management, a
      persistent volume for the ~18MB SQLite, and migrations that run on deploy. Call it 2-3x the
      backend work, maintained indefinitely.
    - **API cost is unchanged by any of this** and is not a factor in the decision. At ~8,100
      input and ~900 output tokens per interaction: Haiku 4.5 is **$0.013** a call (~$1.20/month
      at 3/day), Sonnet 5 $0.038, Opus 5 $0.064. Current usage is roughly ten calls a *month*.
    - Revisit when always-on is the actual requirement — which will be obvious from using the
      app, rather than guessable now.
---

## Data architecture: what gets computed, what gets stored (Session 16)

Three kinds of information were being stored the same way, and separating them is the rule that
now governs where anything new goes. Written down because the code shows *what* was done and
not *why*, and the wrong instinct here is expensive to undo.

**Facts — computed on read, never stored, never remembered.** CTL ramps, weekly planned-vs-actual
load, intensity distribution, recovery against baseline, RPE against prescribed IF, consistency.
All derivable from `activities` + `wellness` + `planned_workouts` in microseconds on a few
thousand SQLite rows. `src/analysis/derive.py` computes them and renders them into the snapshot.
Persisting any of it would be denormalisation that goes stale the moment a late activity syncs.

**State — structured rows, effective-dated, written deterministically at the point of capture.**
FTP history, goals, life events, plan weeks, per-session RPE and notes. The rule learned the hard
way: *if the data arrives structured, write it structurally — never hand it to the model as prose
and hope it calls a tool.* `checkin.py` collected three 1-10 scores and a closed-enum compliance
value, formatted them into a sentence, and produced 4 stored rows across 7 check-ins.

**Narrative — stored whole, retrieved deliberately, never truncated.** Coach reasoning, athlete
notes, cycle reviews.

**When to persist a number:** only when it must stay pinned to a narrative written at that
moment. `cycle_reviews` qualifies — its figures and its prose have to travel together, and
recomputing them later against changed data would misrepresent what the coach was looking at.
A weekly rollup does not qualify; it has no paired narrative, so it is computed.

**When a source is wrong, correct on read rather than storing a correction.** `analysis/load.py`
recomputes TSS for the rides intervals.icu analysed against a stale FTP, instead of writing a
corrected column. A stored correction would need explicit exclusion from `upsert_activities`'
SET list or the next sync destroys it — the same trap that would have eaten the athlete notes —
and it would create a second source of truth to keep consistent.

**Don't rebuild an upstream model to fix a self-retiring error.** CTL/ATL are computed
server-side by intervals.icu and inherit the same FTP error. Recomputing them locally would mean
maintaining a PMC that permanently disagrees with intervals.icu's own charts, to fix a gap that
decays with a 42-day half-life (2.2 CTL now, 0.28 in twelve weeks). The snapshot reports the gap
instead, and the block disappears on its own once no affected rides remain in the window.

**Prompt ordering is load-bearing, not cosmetic.** Three separate times a correct instruction was
ignored because it sat far from what it governed: the format rules before the data snapshot
(Session 14), and the LOAD CORRECTION block below `ANNUAL VOLUME` while the CTL figures it
qualified were far above (Session 16 — a live run quoted "11 percent of your fitness ceiling"
off an understated CTL without mentioning the caveat, and moving the block directly under
CTL/ATL/TSB fixed it on the re-run). Put a caveat adjacent to the number it qualifies.

**Never give the model a label where a number exists.** `RECOVERY SIGNALS` used to print
"trend: stable" — which was hiding a 3.3ms HRV drop against baseline with RHR up 1.6. The
label was doing the interpreting. Give values and deltas; let the coach judge them against the
thresholds already in the prompt.

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

---

# Refactor: one athlete's tool → something another person can run

*Added 2026-11-04, after steps 0–9 of the rolling-plan rebuild and the code review that
followed. This section is the plan for the next refactor and the questions still open on it.*

## Why now

Steps 0–9 are done and the loop closes: a session is prescribed, written to Zwift, ridden,
synced back, and scored. The code review that followed found the project is in better shape
for this than expected — the gate, the roller, the exporter and the serialisers already take
their inputs as arguments rather than reaching for globals, and every chart, the coaching
snapshot and the forecast already work against an empty database.

What remains personal is small and enumerable. A full AST sweep of every module-level constant
in `src/` and `scripts/` found **12 personal values and 113 tunable ones**, and the 12 sit in
seven files.

The aim is that this is the last structural refactor for a long time, so the plan below also
records how each foreseeable feature lands in the architecture — not to build them now, but to
check the shape can absorb them.

## The sweep: everything personal, as of 2026-11-04

| File | Constant | Value |
|---|---|---|
| `analysis/report.py` | `ATHLETE_PROFILE` | name, goals, equipment, "two young kids" |
| `planning/constants.py` | `PLAN_NAME` | `Sustainable rolling` |
| `planning/constants.py` | `GOAL_DATE` | `2027-10-01` (CX) |
| `planning/constants.py` | `GRAVEL_DATE` | `2027-06-18` |
| `planning/constants.py` | `PLAN_START_MONDAY` | `2026-11-02` |
| `scripts/plan_build.py` | `ZWIFT_ID` | `393440` |
| `scripts/seed_goals.py` | `GOALS` | five hardcoded goal rows |
| `scripts/seed_profile.py` | `DEFAULTS` | height, weight, FTP |
| `scripts/serve.py` | `DEFAULT_HOST` / `DEFAULT_PORT` | `127.0.0.1:8800` |
| `visualizations/dashboard.py` | page title ×2 | "Fitness Coach — Ian Auger" |
| `visualizations/calendar_view.py` | page title | "Training Calendar — Ian Auger" |
| `db/migrations.py` | `CUTOVER_MONDAY`, `*_PLAN_NAME`, `TR_RETIRED_ON` | **leave alone** |

The migration constants are deliberately excluded. A migration that reads a config value stops
replaying to the same database the moment that value changes; they are frozen history and must
stay frozen. A fresh install runs them against an empty database where they match nothing.

**Not personal but wrong anyway:** `SPORT_GROUPS` is duplicated in four files
(`analysis/activities.py`, `db/store.py`, `visualizations/calendar_view.py`,
`scripts/dedupe_activities.py`). One definition, imported.

## The 113 tunables are three different things

Lumping them together is the mistake to avoid. They divide by *who is allowed to change them*:

1. **Safety rules — not configurable.** `MAX_HARD_PER_WEEK = 2`; strength never trimmed; no
   lower-body work beside a long ride; threshold targets hedged below FTP; `FREEZE_DAYS`.
   These are the methodology. A config file that lets someone set six hard sessions a week has
   not made the tool flexible, it has removed the thing the tool is for. They stay in code,
   enforced in `blocks.pattern_for` and asserted in `tests/test_invariants.py`.

2. **Methodology defaults — overridable, validated.** The archetype libraries
   (`LIT_VARIANTS`, `SIT_VARIANTS`, `HIIT_VARIANTS`, `THRESHOLD_VARIANTS`,
   `STRENGTH_VARIANTS`), the `ROTATION`, `EASY_IF_CEILING` / `HARD_IF_FLOOR`. Ship ours as the
   default; let a config override a named variant; validate every override against the safety
   rules in (1) and reject with a message a stranger can act on.

3. **Mechanics — leave in code.** `CTL_DAYS = 42`, `OVERLAP_DAYS`, `LOCK_STALE_SECONDS`,
   `MAX_ITERATIONS`, cache budgets. Nobody wants to tune these and exposing them is surface
   area with no payoff.

## Decisions already taken

- **History comes from intervals.icu.** Assume a new user has an account and brings their
  history down on first run. This is what makes the tier ladder derivable rather than guessed.
- **One database, one athlete.** Clone the repo, run it locally. No multi-tenancy.
- **Archetypes: ours as default, override by name.**
- **The tier ladder is derived, not configured.** See below.

## The tier ladder: derive, don't configure

The ladder in `planning/progression.py` was not chosen, it was *measured* — T1 is roughly where
the athlete already was, and the ceiling is the 3.84h/wk actually held for twelve consecutive
weeks. Asking a new user to hand-write five tiers reproduces the failure this whole rebuild
removed: numbers that describe a past they did not live.

So config expresses **intent**, and a bootstrap derives the ladder from synced history:

```yaml
plan:
  sustainable_band_hours: [3.5, 4.0]   # where the gate should climb to
  long_ride_ceiling_min: 120           # extended on demand, never ramped
  start_from: measured                 # measured | floor | explicit
```

On first run, after the initial sync, `scripts/bootstrap_plan.py` walks the synced weeks with
the existing `analysis.activities.week_volume`, finds the athlete's own sustained volume, and
writes a ladder into the database. The floor is the minimum viable week; the ceiling is the
band; the rungs interpolate.

This removes the validation problem rather than solving it: there is no hand-written ladder to
validate. The generated one still gets checked against the existing internal-consistency rules
(`target_min` equals its components, `rides == 2 + hard`), which `tests/test_progression.py`
already asserts.

**`start_from: explicit`** stays available for someone who knows what they want, and *that*
path validates hard and refuses to load on failure.

## Proposed config shape

One file, `config/athlete.yaml`, with `config/athlete.example.yaml` committed and the real one
gitignored like `.env`. Loaded once into a frozen dataclass by a new leaf module
`src/config.py` — leaf because `planning/constants.py` already had to be a leaf for exactly
this reason (`analysis` cannot import `planning.blocks`, which imports `analysis.derive`).

```yaml
athlete:
  name: Ian Auger
  timezone: America/New_York          # currently implicit in date.today()
  history_start: 2017

goals:
  - {description: Cyclocross racing fall 2027, date: 2027-10-01, priority: primary}
  - {description: Gravel spring/summer 2027,   date: 2027-06-18, priority: opt_in}

plan:
  name: Sustainable rolling
  start_monday: 2026-11-02
  sustainable_band_hours: [3.5, 4.0]
  long_ride_ceiling_min: 120
  horizon_weeks: 3
  rotation: [consistency, sprints, threshold, vo2]

integrations:
  intervals_icu: {push_calendar: true}
  zwift: {athlete_id: "393440", enabled: true}

server:
  host: 127.0.0.1
  port: 8800

coach:
  model: claude-haiku-4-5
  enabled: true
```

Secrets stay in `.env`. The split is: `.env` is what must not be shared, `athlete.yaml` is what
someone *would* share when asking for help.

## Open questions

1. **Does the config reload, or is a restart required?** The maintenance loop and the server
   are long-lived. Simplest is load-once-at-import; the cost is that editing the file while the
   server runs does nothing, silently. Leaning: load once, expose the loaded values at
   `/api/config` so the running state is at least inspectable.

2. **What happens when `athlete.yaml` is missing?** Refuse to start, or run on defaults with
   the athlete's name blank? Refusing is clearer but makes the first run a wall. Leaning:
   generate it from the example on first run and say so.

3. **How much does the coach's system prompt come from config?** `coach/prompt.py` currently
   hardcodes the methodology, the voice, and this athlete's specific situation. The methodology
   should stay; the situation should not. Where exactly is the line?

4. **Does the bootstrap run automatically or explicitly?** Deriving a ladder from a sync that
   has not finished would produce a floor ladder and then never revisit it. Leaning: explicit
   `scripts/bootstrap_plan.py`, with the roller refusing to run until it has.

5. **What identifies a plan across a config change?** `PLAN_NAME` is the join key for five
   readers. If someone renames their plan in the config, every stored row orphans. Needs a
   stable internal id with the name as a label, or a documented rename path.

6. **Timezone.** `date.today()` is the athlete's day, correctly, but it is the *server's*
   today. Fine for a laptop in one timezone; wrong for a server left in UTC.

## Features: what to build, and what not to

### Network hosting with a real UI — the one that forces an architectural decision

Currently `scripts/serve.py` binds `127.0.0.1` deliberately: the process reads `.env`, so it
holds the intervals.icu and Anthropic keys, and it has **no authentication of any kind**. On a
routable interface it is an open proxy to both, and anyone on the network can spend the
Anthropic key.

So "available on the local network" is not a config change, it is a feature with a prerequisite:

- **Auth before exposure.** Single-user, so a long random token in `.env` checked by FastAPI
  middleware is proportionate. Sessions/OAuth are over-engineering for one person.
- **The chat endpoint is the exposure that matters.** `/api/chat/ask` spends money per call.
  It needs auth and a rate limit even on a trusted network.
- **Bind address stays explicit.** `--host 0.0.0.0` already warns; it should additionally
  refuse unless an auth token is configured.

The UI itself is the smaller half: the dashboard and calendar are already served, and
`/api/chat/ask` already streams SSE with a working test page at `/chat-test`. What is missing
is a real chat panel wired into the dashboard, which is roadmap work that predates this
refactor and is unaffected by it.

### ML for forecasting and fatigue — mostly no, and the reason is in our own data

You are right that the dataset is the limit, and it is worth writing down *how* limited so this
does not get revisited on vibes:

- **Fatigue early warning: one positive example.** Of eleven training dropouts in the reference
  history, exactly **one** had a fatigue signature. 2026-04-13 began with HRV 52 and the best
  sleep of the spring, then four weeks off. You cannot train an early-warning model on n=1, and
  the existing `wellness.recovery_flags()` already surfaces the signals as advisory text, which
  is the right altitude.
- **FTP projection: the measurement is cheaper than the model.** A ramp test is 25 minutes and
  gives a number; a model trained on one athlete's sparse history gives a guess with a
  confidence interval wide enough to contain the guess you would make anyway.
- **CTL/ATL projection is already deterministic** and does not want a model — it is the same
  EWMA intervals.icu uses, run forward over the prescribed horizon.

**The honest statistical play that is worth doing:** per-athlete control limits. Rolling
baselines of HRV and RHR with a simple deviation band, which `recovery_flags` half-does
already. That is statistics, not ML, it works on one athlete's data, and it is interpretable —
which matters when the output is advice.

### What other platforms have that is worth taking

Filtered hard through what this project is *for*. Most commercial features exist to drive
engagement or sell subscriptions, and several actively fight the adherence-gated philosophy.

**Worth building, in order:**

1. **eFTP tracking.** intervals.icu computes an estimated FTP from your power data
   continuously. We already sync the data and ignore the field. This directly fixes a live
   problem: the intervals.icu FTP is configured at 238 while eFTP reads 257, our own record
   says 260, and a Zwift ramp test said 271 — four numbers, and every `%FTP` target in every
   prescription is computed against one of them. Pull eFTP, track it as a profile metric
   alongside the manual one, and surface the disagreement rather than silently picking.

2. **Execution scoring, not just completion.** This is the biggest gap relative to the
   project's own goals. Compliance currently scores a session on duration and TSS — Tuesday's
   sprints came back "completed, 112%". It does not know whether eight maximal efforts actually
   happened. intervals.icu detects intervals within an activity; comparing detected intervals
   against `prescribed_sessions.params_json` would answer "did you execute the prescription",
   which is a different and more useful question than "did you ride for long enough". The gate
   should keep reading volume — that is deliberate and well-argued — but the *athlete* should
   see execution.

3. **Power curve (mean-maximal power) as the progress signal.** CTL measures volume, not
   fitness, and this project has nothing that answers "am I getting stronger". MMP over
   rolling windows does, needs no model, and is a chart we do not have.

4. **Workout alternates.** TrainerRoad's best feature: swap today's session for an equivalent
   at the same dose. This fits our architecture almost suspiciously well — the tier already
   defines the dose and the archetype defines the shape, so an alternate is "another variant at
   the same `target_min`". Probably a day's work.

5. **Outdoor / Garmin export.** We write `.zwo` for Zwift only. intervals.icu can push a
   structured workout to Garmin once `workout_doc` is populated, which it now is as of the
   export path. Caveat recorded during that work: the intervals.icu forum warned on 2026-10-09
   that Garmin API access may be restricted — verify before building.

**Deliberately not building:**

- **HRV-guided daily readiness** (Whoop, Xert). Decided against on evidence during the rebuild;
  see the dropout analysis above. Recorded here so a future contributor does not re-add it
  without new evidence.
- **Social, segments, leaderboards.** Not what this is for.
- **Nutrition and fuelling.** Real, and a different project.
- **Adaptive training that reschedules missed sessions.** This is what the tier gate replaces.
  A plan that moves missed work forward is the thing that produced a 32-week ramp nobody could
  follow.

## How each feature lands in the current architecture

The point of this section is to check the shape holds, not to schedule work.

| Feature | Where it goes | Does the architecture absorb it? |
|---|---|---|
| Config file | new `src/config.py` leaf; `planning/constants.py` reads it | Yes — constants.py exists for this reason |
| Tier bootstrap | new `scripts/bootstrap_plan.py` + a `tiers` table | Yes — `TIERS` is already plain data |
| Auth | FastAPI middleware in `api/app.py` | Yes — single entry point |
| Real chat UI | `visualizations/dashboard.py` panel + existing SSE | Yes — endpoint already streams |
| eFTP tracking | `intervals/client.py` field + `athlete/profile.py` metric | Yes — profile is already a dated history |
| Execution scoring | `analysis/compliance.py`, against `params_json` | Yes — params already stored per session |
| Power curve | new `analysis/power_curve.py` + a chart | Yes — same shape as every other chart |
| Workout alternates | `planning/archetypes.py` selection by `target_min` | Yes — dose and shape already separate |
| Garmin export | `planning/export.py`, same payload | Yes — `workout_doc` already populated |
| Control-limit flags | extend `wellness.recovery_flags()` | Yes |

Nothing on that list requires moving a boundary. The two that would have — multi-tenancy and
rescheduling missed work — are the two explicitly ruled out.

## Device delivery: what we learned, 2026-11-04

Investigated whether we need to build Garmin and Karoo push. **We do not** — intervals.icu
already owns that path, and we already produce the `workout_doc` it needs. What follows is
account configuration and verification, not code.

Read from the athlete object on the live account:

| Device | Reads our data | Pushes workouts | Field |
|---|---|---|---|
| Garmin | yes (activities + wellness) | **off** | `icu_garmin_upload_workouts = False` |
| Zwift | yes | **on** | `zwift_upload_workouts = True` |
| Karoo | not connected | — | no fields present |

**Garmin — deferred, no code needed.** The upload flag is off and `icu_garmin_training = False`
with `icu_garmin_last_upload = None`, which suggests the Garmin *Training API* scope was never
granted; flipping the flag may not be enough and a reconnect may be required. Two constraints
to design around when it is switched on:

- Garmin accepts roughly **7 days** of planned workouts, while our horizon is 3 weeks. The
  device window and the plan horizon are different things and should not be conflated.
- **Strength arrives as unstructured text.** Two of five weekly sessions are strength, so a
  third of the calendar lands without structure. Not a bug to fix, a limitation to state.
- A 2026 forum report says workouts **with text in the description** failed to sync via the
  API while empty ones succeeded. We put text in every description, and intervals.icu appends
  its own parsed step list to it. If Garmin sync misbehaves, the description is the first
  suspect — the same field that swallowed a workout on 2026-11-03.

**Zwift — open question, possible duplication.** `zwift_upload_workouts = True` means
intervals.icu is already pushing planned workouts to Zwift, and we also write `.zwo` files
directly into the Zwift folder. Those are two delivery paths to one destination. If Zwift is
showing each session twice, the file-writing is redundant and should be dropped rather than
reconciled. **Needs a human to look at Zwift and say.**

**Karoo — backlog, not for this athlete.** Wanted for a friend, no device here to test against.
Official intervals.icu sync exists (January 2025) but the connection is created *from the
Karoo*, not from intervals.icu's settings, so it cannot be set up or tested remotely. An
unresolved bug report (~75 days old) says planned workouts stopped reaching Hammerhead while
completed rides still uploaded. Revisit only with a device in hand.

`push_errors` exists on every event and is currently `null`. That is the hook for detecting
delivery failures programmatically, whenever device push is turned on.

---

## The refactor, chunked

Each step independently verifiable, smallest first, same discipline as the rolling-plan
rebuild. Steps 0–2 are mechanical; step 3 is where the design risk is.

### Decisions locked

- **YAML, via PyYAML.** Already installed transitively; it gets pinned explicitly rather than
  relied on by accident. `tomllib` would be zero-dependency and is the honest alternative, but
  the config carries nested lists of dicts (goals, rotation) and YAML reads better for those.
- **Config is loaded once, at import.** Editing the file while the server runs does nothing.
  Mitigated by exposing the loaded values at `/api/config` so the running state is inspectable
  rather than guessed at.
- **A missing config is generated from the example on first run**, with a message saying so.
  Refusing to start is clearer but makes the first run a wall for someone who just cloned.
- **Secrets stay in `.env`.** The split: `.env` is what must never be shared,
  `config/athlete.yaml` is what someone *would* paste when asking for help.

### Step 0 — the loader, with no consumers

`src/config.py` as a leaf module (no imports from this project), `config/athlete.example.yaml`
committed, `config/athlete.yaml` gitignored. A frozen dataclass per section, so a typo in the
file is an error at load rather than an `AttributeError` three modules away.

*Verify:* the example loads; an unknown key raises with the key named; a missing required
field raises with the field named; `python -c "import src.config"` costs nothing at import.

### Step 1 — the 12 personal constants read from it

`ATHLETE_PROFILE`, the four in `planning/constants.py`, `ZWIFT_ID`, the two seed scripts, the
server host/port, and the three page titles.

*Verify, and this is the whole point of the step:* generate the horizon before and after, and
diff `prescribed_sessions`. **Byte-identical, or the step is wrong.** The committed config must
reproduce today's plan exactly. 469 tests still green.

### Step 2 — deduplicate `SPORT_GROUPS`

One definition, imported by `analysis/activities.py`, `db/store.py`,
`visualizations/calendar_view.py`, `scripts/dedupe_activities.py`. Kept separate from step 1 so
that step's diff stays clean.

*Verify:* the four copies are identical today, so the merge is provably safe; tests green.

### Step 3 — derive the tier ladder from synced history

The design risk lives here. `scripts/bootstrap_plan.py` walks synced weeks with the existing
`analysis.activities.week_volume`, finds the athlete's own sustained volume, and writes a
ladder. Config states intent (`sustainable_band_hours`, `long_ride_ceiling_min`), not numbers.

*Verify the way the progression gate was verified:* replay against the frozen
`tests/fixtures/history_2025-09_2026-11.json` and compare the derived ladder to the one chosen
by hand. They will not match exactly — the question is whether the differences are defensible.
A derived T1 wildly above measured practice means the derivation is wrong, not the fixture.

Also: the roller must refuse to run before a bootstrap has happened, rather than silently
generating a floor ladder and never revisiting it.

### Step 4 — archetype overrides

Ours stay as the default library. A config may override a named variant. Every override is
validated against the safety rules that are **not** configurable: never above FTP, work:rest
floors, at most two hard sessions a week, strength never trimmed.

*Verify:* an override that breaks a safety rule is refused with a message naming the rule;
`tests/test_invariants.py` passes against a config that overrides every variant.

### Step 5 — first-run experience

`README` for a stranger, config generated on first run, bootstrap wired in, and the empty
database path (already covered by `tests/test_empty_database.py`) exercised end to end from a
fresh clone.

*Verify:* clone into a clean directory, follow the README, reach a generated plan. That is the
test — not a unit test, a walkthrough.

### Step 6 — auth, then network binding

Not a config change: `serve.py` binds loopback because the process holds both API keys and has
no authentication, and `/api/chat/ask` spends money per call. A long random token in `.env`,
checked by FastAPI middleware, is proportionate for one user. `--host 0.0.0.0` already warns;
it should refuse unless a token is configured.

*Verify:* every route 401s without the token; the chat endpoint rate-limits; binding non-loopback
without a token exits rather than warning.

### Then: features

In the order argued above — eFTP tracking first (four disagreeing FTP numbers today), then
execution scoring, power curve, workout alternates. Garmin verification slots in whenever the
account flag is flipped; Karoo waits for hardware.

### Still needing a human answer

1. **Is Zwift showing duplicates?** Decides whether `.zwo` file-writing survives at all.
2. **What identifies a plan across a config rename?** `PLAN_NAME` is the join key for five
   readers; renaming it in config orphans every stored row. Either a stable internal id with
   the name as a label, or a documented rename path. Leaning: internal id, because a config
   file invites editing and the current behaviour silently loses data.
3. **Timezone.** `date.today()` is correctly the athlete's day but is really the *server's*
   day. Fine on a laptop, wrong on a server left in UTC. Worth fixing in step 1 while the
   config is being built, or deferring until hosting is real.

---

## Resolved, 2026-11-04

### Zwift delivery: done, and it deleted code

Confirmed on the device: Zwift was showing every session twice. `zwift_upload_workouts` is
enabled on this intervals.icu account, so pushing an event there already delivers the workout.
Writing the same file into the Zwift folder was a second path to one destination.

Removed: `supersede`, `prune_week_dirs`, `week_dir`, `zwift_workouts_dir`, `plan_prefix`,
`display_name`, `ZWIFT_ROOTS`, and `plan_build`'s four `--zwo*` flags. `to_zwo` stays — it
produces the `file_contents` intervals.icu parses into a `workout_doc`, which is also what
Garmin will need.

Two days of work deleted: the folder layout, the chronological naming, the superseding logic.
All of it solved a problem that existed only because we were duplicating a delivery we were
already getting for free. **`ZWIFT_ID` goes with it — 11 personal constants now, not 12, and
one fewer integration to express in config.**

### Identity: give everything an id

Decision: lean in. The config file invites renaming, and a plan identified by its name loses
every stored row the moment someone edits it.

**The real gap is that a plan has no row.** `plan_name` is a string repeated across
`prescribed_sessions`, `training_plan_weeks` and `plan_progression`, joined on by five readers.
So:

```sql
CREATE TABLE plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,   -- stable, generated once, never edited
    name          TEXT NOT NULL,          -- the label, free to change
    started_on    TEXT NOT NULL,
    goal_date     TEXT,
    config_hash   TEXT,                   -- which config produced this plan
    created_at    TEXT NOT NULL,
    retired_at    TEXT                    -- a plan ends by being retired, not deleted
);
```

The three plan tables take `plan_id` as a foreign key. `plan_name` stays as a denormalised
column for one migration so nothing breaks mid-flight, then goes.

`config_hash` is the part worth arguing for: it answers "which settings produced this
prescription", which is the question that becomes unanswerable the moment config is editable.
Same reasoning as `tier_applied` on `plan_progression` — store the inputs the decision was made
under, because re-deriving them later gives you today's answer to yesterday's question.

**What already has sufficient identity and should be left alone:**

- `prescribed_sessions` — autoincrement `id` plus `external_id` (`<slug>-<date>-<role>`), which
  is the stable key the remote calendar matches on. Worth noting it is *date-derived*: move a
  session to another day and it becomes a different session, which is correct in this design
  but should be stated rather than discovered.
- `activities`, `events` — intervals.icu ids.
- `wellness` — date is the natural key.
- `goals`, `life_events`, `subjective_feel`, `chat_sessions` — all have ids.

**What does not need one:** archetype variants are keyed by name (`long_reps`, `short_reps`),
and that name is exactly what a config override targets. An opaque id would make overrides
unreadable.

**What a `tiers` table needs** (step 3): `plan_id`, `index`, the five dose columns, and
`derived_from` — the window of history the bootstrap used. Without that, a ladder cannot be
explained or regenerated.

### Timezone: UTC underneath, athlete timezone on top

Decision: UTC is the default and the storage format; an athlete timezone in config decides what
"today" means.

The split already exists in `src/clock.py` and is correct — `utc_now()` for audit timestamps,
`today_local()` for anything a human would call a date. What is missing is that `today_local()`
is `date.today()`, which is the *server's* day, not the athlete's. Fine on a laptop in one
timezone; wrong on a server left in UTC, where an evening check-in in the Americas files under
tomorrow and is then compared against activity dates intervals.icu reports in local time.

```yaml
athlete:
  timezone: America/New_York     # IANA; defaults to UTC when unset
```

`clock.today_local()` becomes `datetime.now(ZoneInfo(tz)).date()` using stdlib `zoneinfo`, no
dependency. `utc_now()` is unchanged.

**The work is not the clock module, it is the 58 call sites.** `date.today()` is called 58
times across 28 files, and every one that means "the athlete's day" has to route through
`clock.today_local()` instead. Some genuinely mean "now" for a log line and should stay.
Mechanical, but it needs reading each call rather than a blind replace — which is why it is its
own step rather than a line in step 1.

A guard makes it stick: a test that greps for `date.today()` outside `clock.py` and fails,
the same shape as the real-database guard in `tests/conftest.py`.

### Revised step order

Steps renumber because identity has to land before config can rename anything safely.

| Step | What | Why here |
|---|---|---|
| 0 | `src/config.py` loader, no consumers | **done** |
| 1 | `plans` table, `plan_id` foreign keys, `config_hash` | **done** |
| 2 | Timezone: `clock.today_local()` + the 58 call sites | **done** |
| 3 | The 11 personal constants read from config | **done** — `prescribed_sessions` byte-identical |
| 4 | Deduplicate `SPORT_GROUPS` | **done** |
| 5 | Tier ladder derived from history, `tiers` table | **done** — reproduces the hand-written ladder |
| 5a | Weekly template generated, 2–7 ride days | unplanned; three was the code's limit, not the athlete's |
| 5b | Outgrown check, surfaced to the coach | unplanned; closes the loop 5a opened |
| 6 | Archetype overrides, validated | needs the loader and the safety rules |
| 7 | First-run experience, README | **grew** — `bootstrap_plan.py` is now a required first run |
| 8 | Auth, then network binding | independent, but pointless before first-run works |

5a and 5b were not in the plan. Both came out of one question — *"are we hard coding 3 ride
days?"* — and neither is scope creep: 5a removed a limitation that would have made the project
unusable for a second athlete, and 5b closes the loop 5a opened, since configurable
availability is decorative if nothing ever notices the athlete has grown into it.

Step 7 grew as a result. The first run is now `fetch_history` → `seed_profile` →
**`bootstrap_plan`** → `serve`, and the roller refuses until that third step has happened. That
refusal is deliberate, but it means the clean-clone walkthrough has a required step that has
never been exercised from scratch.

### Backlog, added 2026-11-04

- **Shorter event names for the Zwift card.** Zwift prefixes delivered workouts with
  "Intervals icu: " and then truncates, so the prefix eats the identifying part:
  `Intervals icu: 6x 20s all-out,…`. Moving the event `name` to `session_name()`'s short form
  (`Sprints 45min (medium)`) and leaving the prescription in the description reads better in
  both Zwift and the intervals.icu calendar, where the date is already shown. Not a blocker.
  Unknown whether Zwift keys its cloud copies on the name — if it does, renaming may strand
  the old copies, so verify in the app rather than assuming.
- **Karoo (Hammerhead).** For a friend; no device here to test against. The connection is
  created from the Karoo rather than from intervals.icu, so it cannot be set up remotely, and
  there is an unresolved report of planned workouts no longer reaching Hammerhead.

### Measured, 2026-11-04

The 7-day device window is now observed rather than quoted. intervals.icu delivered exactly
our next three *rides* within seven days — Thu 60min, Sat 90min, Tue 6x20s — and correctly
excluded the three strength days, which are `WeightTraining` rather than rides. It files them
under its own `Intervals icu` folder in Zwift, which is the folder organisation we briefly
built by hand and then deleted.

### Step 3, done — what it cost and what it found

The eleven are gone: `ATHLETE_PROFILE`, `PLAN_NAME`, `PLAN_SLUG`, `GOAL_DATE`, `GRAVEL_DATE`,
`PLAN_START_MONDAY`, the two seed scripts' tables, the server host/port, and the two page
titles.

**The shape change that mattered.** `planning/constants.py` became *functions*, not constants
reassigned from config. A constant is read into every default argument it appears in —
`plan_name: str = PLAN_NAME` binds when the function is defined — and `PLAN_NAME` sat in eight
of those. Reassigning it from config would have left all eight holding whatever loaded first,
unreachable by any later `reset()`. Same mechanism as the `schema.DB_PATH` fixture that did
nothing and let the suite write to the real database. So every such default became `None`,
resolved in the body.

**Verification.** The horizon was generated from a `HEAD` worktree and from the new code into
two copies of the live database and the stored rows diffed: identical, twice — once with the
`plans` table emptied, once against the live state. The committed config reproduces today's
plan exactly.

**`GRAVEL_DATE` did not become a setting.** It was the second of two events the report counted
down to by name. The report now counts down to whatever dated goals are configured, soonest
first, with the priority beside each — which is the only version of that line that means
anything to a second athlete, and which says out loud that an `opt_in` goal is one the
rotation does not taper for.

**`plans.resolve`, and a bug reopened by this step.** Step 1 closed the rename-splits-a-plan
bug by hardcoding `PLAN_SLUG`. Reading the name from a file someone is invited to edit reopens
it, because the fix was a hardcoded answer. `resolve` takes the configured slug if there is
one, otherwise the slug of the plan already in the database, and derives from the name only
when there is no plan at all — so deriving is now reachable only when there is nothing to
orphan.

**Found by running it, not by tests:** `plan_build.py` printed `None` as the plan name, because
one of the mechanical replacements silently did not apply. And two fields of real content —
`equipment` and `notes` — were briefly lost, because the config file had no home for them; the
coach's "Life context" line had gone blank. That line is the one field in the file that changes
what the model says, and nothing would have failed.

11 mutants, all caught; two survived first — `expand_week`'s fallback was only ever exercised
under a config whose plan name matched the old constant, so the test asserted the mutant's own
answer. 525 tests green. Dashboard and calendar verified in a browser per `CLAUDE.md`.

### Step 4.5 — settle FTP before deriving anything from it, 2026-11-05

Small, and it goes first because step 5's output is only as good as this input. The ladder
itself is volume (minutes and ride-days), so FTP does not change the derivation — but every
`%FTP` target in every session the derived ladder prescribes is a percentage of this number,
and a wrong one makes the whole ladder unrideable while looking correct.

**Four numbers disagreed:** intervals.icu 238, eFTP 257, power record 260, ramp test 271.

**Resolved: 260.** Already what `athlete_profile` holds effective 2026-10-01, and the only
value corroborated twice independently.

The argument against 271, which was the tempting one ("if we don't trust the test, what's the
point"): a Zwift ramp test measures MAP and multiplies by a fixed 0.75. The measurement is
sound — best 1-minute ≈ 361W — and **the 0.75 is the assumption**, the part that varies most
between riders. The test also predates five weeks of 2–3h/week riding, and a ramp is the most
volume-insensitive protocol there is, so it reads high off a thin base.

What settled it was the measured data rather than the reasoning: **the highest normalised
power for any ride since 2026-09-15 is 215W**, over 64 minutes. Nothing in seven weeks of
riding corroborates 271.

And the error is asymmetric in a way that is specific to this design. At 271 a threshold rep
asks for ~244W against a best hour of 215 NP; those sessions do not get completed, the gate
reads measured volume, and a partial week steps the tier **down** — the ladder punishing the
athlete for a number they typed in, and landing squarely on the abandonment failure mode the
whole plan exists to avoid. At 260 the sessions come out slightly easy, get completed, and
eFTP climbs on evidence. **The system self-corrects upward and has no mechanism to correct
downward.**

Two fixes to land before step 5:

1. **Update intervals.icu's FTP field from 238 to 260.** Its own TSS figures are computed
   against it, so every synced `tss` since 2026-10-01 is currently inflated. Ours are
   recomputed against the tracked history (`_manual_tss`), which is why this has not shown up
   as a visible wrongness — but the gate is about to read numbers derived from it.
2. **`profile.ftp: 238.0` in `config/athlete.yaml`** reads as a current value and is not one.
   It only ever seeds an empty `athlete_profile`, and this one is not empty — so it is inert
   and misleading at the same time. Either set it to 260 or drop the key.

Then **eFTP tracking** moves to the front of the feature queue, which is what makes this
decision provisional rather than permanent: if 271 is real, the first threshold sessions at
0.90 × 260 = 234W will feel easy and eFTP will walk up within weeks. FTP should move on
demonstrated power, not on a number anyone decided — including this one.

#### Correction, same day: intervals.icu does not backfill TSS

FTP was corrected to 260 on intervals.icu. **Re-syncing changed nothing.** A backfill over
2026-09-01 → 2026-11-05 re-pulled all 19 activities and not one `tss` value moved: intervals
applies a new FTP going forward and leaves history as recorded.

Arithmetic on the 2026-10-27 ride (64min, NP 215) puts it beyond doubt:

| | |
|---|---|
| stored `activities.tss` from intervals | **87.0** |
| TSS at FTP 238 | **87.0** |
| TSS at FTP 260 | 72.9 |
| our `effective_tss`, recomputed | **73.1** (flagged as recomputed) |

So the plan above was wrong in its remedy, right in its diagnosis. The inflation is baked into
the stored column and no sync will clear it. What saves us is `_manual_tss`: every TSS this
project reasons with is recomputed against the tracked FTP history, and it returns 73.1 where
intervals says 87.

**The part still wrong, and it is not cosmetic.** `wellness.ctl` / `.atl` are intervals.icu's
own figures, computed from *their* TSS — so the CTL on the dashboard, in `current_fitness` and
in every PMC panel is built on FTP 238 for everything since 2026-10-01, while the TSS beside
it is built on 260. Two numbers on one chart from two different FTPs.

Not urgent, and specifically **not a risk to step 5**: the progression gate reads
`SUM(moving_time)` per week and never touches TSS or CTL. That decision was made to avoid
inheriting compliance scoring's failure modes, and it turns out to insulate the gate from this
one too.

Added to the backlog, ahead of eFTP tracking since they are the same piece of work:
**compute CTL/ATL locally from `effective_tss` instead of reading intervals.icu's.** Then one
FTP correction fixes the whole history at once, which is the behaviour that was wanted here.

### Step 5 — the tier ladder derived from history, done 2026-11-05

The design-risk step, and it came out better than expected.

**The result.** Derived against the reference year, the ladder independently reproduces the
hand-written one: identical long rides (75, 75, 90, 105, 120), identical ride days, identical
hard-session placement. The single difference is a uniform +5min easy ride — the athlete's
median session is 64min, so the derivation rounds to 65 where a person rounded to 60. The
derivation is arguably the more correct of the two.

**What is derived, what is intent, what is fixed.**

| | |
|---|---|
| derived from history | long ride, easy ride, ride days, where the floor sits |
| config, as intent | `sustainable_band_hours`, `long_ride_ceiling_min` |
| fixed by design | five rungs, one hard session above the floor, none at the floor, strength never trimmed |

Config says where to climb to, never what a rung contains. When the history cannot reach the
configured band the bootstrap **says so and builds the shorter ladder** — bending a ladder up
to meet an aspiration is how the old plan reached 8.4h/week.

**Three defects found by deriving rather than by reasoning.**

1. **A dead rung.** `round()` is banker's rounding, so at a 15-minute step the second and third
   rungs landed on 86.25 and 97.5 → 90 and, because 6.5 goes to 6, also 90. Two identical
   rungs: the gate advances a tier, prescribes exactly the same week, and costs a week before
   the next real step. No error, plausible-looking ladder.
2. **The floor and the first rung should share a long ride.** Forcing a strict increase
   everywhere made T0→T1 a 68% jump and compressed everything above it. The hand ladder has
   both at 75min, and the reason is that what separates them is the hard session. The strict
   rule was invented during implementation rather than read off the design.
3. **Ride days collided with the rotation's ride slots.** Every block declared three;
   `pattern_for` only ever reduces, and `Block` raised at construction if a tier asked for
   more, so a five-day-a-week athlete derived a ladder that crashed the generator at import.
   Fixed here with a clamp — **which was the wrong fix, and was replaced within the hour by
   generating the weekly template instead.** See the follow-up below: a clamp that stops the
   derivation asking for what the athlete actually does is not a constraint, it is the code's
   limitation wearing a constraint's clothes.

**The refusal.** `tiers_store.require` raises when a plan has no ladder, and `store_horizon`
calls it before building anything. Not `load(...) or TIERS` — that one-liner generates a
complete, believable plan from a stranger's measured practice and reports nothing, and because
it never fails there is no later moment at which anyone finds out. Verified live: the sync
refused with the bootstrap command in the message, and still completed its activity and
wellness sections.

**A consistency bug introduced and fixed inside the step.** The write path took the stored
ladder while the read paths still used the module constant, so `/api/plan/state` reported 3.42h
against a calendar showing 3.5h. Exactly the shape of the CTL/TSS split noted above, and one of
those in a project is enough. `outline`, `report` and the API now all take the plan's ladder,
with a test asserting that what is reported is what was prescribed.

**Live.** Bootstrapped plan 2 from 78 weeks; the only change to the horizon was the easy ride
going 60 → 65min, and 13 calendar events were re-pushed. `derived_from` holds the observed
statistics, so the ladder can be explained later rather than merely obeyed.

17 mutants, all caught — four survived first, including the two that mattered most: the roller
gating on the ladder and then generating without it, and `expand_week` ignoring the ladder it
was handed. Both escaped because the stored and hand-written ladders agree at the low tiers a
fresh database starts on. The test now uses a ladder that overlaps the module one nowhere.

**Also corrected:** the README's tier table had been copied from an early design draft and was
wrong for T1, T2 and T4. It is now generated from what the derivation actually produces.

#### Step 5 follow-up: three ride days was the code's limit, not the athlete's

Raised immediately after step 5 landed, and correctly: *"are we hard coding 3 ride days as the
only configuration?"*

We were — but not in the ladder. The weekly template was two literal lists:

```python
_EASY_WEEK     = [(1,"lit"),  (2,"strength"), (3,"lit"), (5,"long"), (6,"strength")]
_EMPHASIS_WEEK = [(1,"hard"), (2,"strength"), (3,"lit"), (5,"long"), (6,"strength")]
```

Three ride slots, Monday and Friday rest, baked in for everyone. Because `pattern_for` only
ever *reduces* a pattern, three was a hard ceiling — and `max_pattern_rides()`, added hours
earlier in step 5, taught the derivation to respect a limit that was really just the shape of
those two lines. A clamp dressed as a constraint.

**`blocks.build_pattern(ride_days, strength_days)` generates the template.** For (3, 2) it
reproduces both lists exactly — asserted, same discipline as the config refactor's
byte-identical check, because this template decides every athlete's week. Two to seven days
generate, each verified end to end.

Strength days are *scored* rather than placed: rest days first, then spacing between lifts,
then the day after a ride. For three ride days that independently picks Wednesday and Sunday,
which is what the hand-written template said.

**Decision taken: config caps, history sets.** `plan.max_ride_days` is availability, not a
target — the ladder's ride-day count is read from what the athlete actually rides and only
stops climbing when it meets the ceiling. Someone who sets 7 and rides 3 gets a three-day week,
and is told nothing. Someone who rides 5 with a ceiling of 3 is told the plan is not claiming
the other two. Consistent with `sustainable_band_hours`: config states intent, history states
fact.

**One invariant had to loosen.** `Block` rejected two sessions on one weekday. At six or seven
riding days there is no empty day left for a lift, and a ride plus a lift on one day is
ordinary practice — `prescribed_sessions` is keyed on `(plan, date, role)`, so both store and
push. What stays forbidden is *two rides* on one day, which is a generator error every time.

**What did not change:** this athlete's plan. History says three ride days, the ceiling is
seven, so the ladder, the weekday layout and the pushed calendar are all identical. Verified by
running it, not by reasoning about it.

The safety rules survive every width, which is the only reason this is safe to allow: no
lower-body lift beside the long ride (`_strength_for_week` already computed adjacency from
whatever pattern it was handed, so it needed no change), at most one hard session, two lifts
kept at every tier, no day given two rides. Parametrised over 2–7 ride days end to end.

#### The outgrown check, 2026-11-05

Asked for right after the ride-day work, and it closes the loop that work exposed: *"wire it
into the coaching aspect so that in a weekly or monthly check-in, the coach will prompt the
user to re-bootstrap if they are outgrowing their ride days."*

**The loop.** The ladder is derived once and then fixed — correctly, since the gate's recorded
decisions were graded against the rungs in force when each week closed. But that is
self-reinforcing: the plan asks for three days, three days get ridden, a re-derivation finds
three days. `plan.max_ride_days: 7` could be set and never once be reached by the system's own
operation. Someone could sit at the cap for a year riding five days a week and keep being
handed three.

The escape hatch already worked and nobody was using it: `week_volume` counts every ride
whether or not it matched a prescription, so a fourth day ridden off-plan *is* visible. What
was missing is that nothing looked.

**Three signals** (`src/planning/outgrown.py`, pure functions, no database):

| signal | what it means |
|---|---|
| `ride_days` | riding more days than the cap asks for — the headline, and the one a re-derivation cannot discover on its own |
| `volume` | riding more than the top rung's target, by a 10% margin |
| `held_cap` | four straight met weeks at the cap — **context, never a recommendation** |

`held_cap` alone never suggests re-deriving. Holding the top rung is a good steady state, and
the design already says going higher is a decision rather than a consequence of a good streak.
It is reported so the coach can ask, not so the plan can push.

**It only ever suggests.** Re-deriving changes what the tiers already recorded in
`plan_progression` meant, so it stays the athlete's call. The output is a sentence the coach
raises at a check-in, carried in `rolling_plan.outgrown` and rendered under `LADDER CHECK` in
the context text the model reads.

**Three ways it declines to nag**, each its own test: a ladder younger than eight weeks is left
alone; fewer than three ridden weeks in the window is too thin to judge (returning from a gap
and outgrowing a ladder are described by the same scarcity of data); and every statistic is a
median, so one big week cannot fire it.

**When re-deriving will not help**, it says so instead. `bootstrap` clamps the derived day count
to `plan.max_ride_days`, so if the ceiling is already what the ladder uses, "re-bootstrap" sends
the athlete round a loop that changes nothing — the config is the binding constraint and gets
named. If volume also fired, both are said, because the rungs can lengthen even though the week
cannot widen.

12 mutants, all caught. Two survived first, and both were tests that could not fail:
the returning-from-a-gap test used a week modest enough that no signal would have fired either
way, so the guard it existed to protect could be deleted with the suite green; and nothing ran
the check end to end, so `_rolling_plan` could stop calling it entirely — the detector correct,
fully covered, and never executed.

### Backlog, added 2026-11-05

- ~~**Version the tier ladder, and record which one graded each week.**~~ **Done
  2026-11-05, migration 10.** `plan_progression` rows
  carry `tier_applied`, which was stored precisely so a decision could be read back against
  the inputs it was made under. That stops being true the moment a ladder is re-derived:
  `tier_applied = 2` means one week of training before the re-bootstrap and a different one
  after, with nothing in the row to say which. `tiers_store.save` warns about this and makes
  `--replace` explicit, which is a speed bump rather than a fix.

  **This is now more pressing than it was**, because the outgrown check actively recommends
  re-deriving. We have built something that tells the athlete to do a thing with a known,
  unresolved consequence. The shape of the fix is the same principle one level up: a
  `ladder_version` on `tiers` (plan_id, version, …) and a `ladder_version` column on
  `plan_progression`, so a decision points at the rungs that produced it. Small, additive, and
  it makes re-deriving a safe operation rather than a warned-about one.

- **Exercise the clean-clone first run.** Step 7's own verification — clone into an empty
  directory, follow the README, reach a generated plan — now has `bootstrap_plan.py` in the
  middle of it, and the refusal path that protects it has only ever been seen on a database
  that already had history. The interesting case is the opposite: a fresh clone with almost no
  synced riding, where `BootstrapError` fires and the message has to be good enough to act on.

#### Ladder versions, done 2026-11-05 (migration 10)

Taken off the backlog before step 6, because the outgrown check had started recommending
re-derivation and that made a theoretical gap into advice we were actively giving.

**The gap.** `plan_progression.tier_applied` records which rung a week was asked to meet, and
exists so a decision can be read back against the inputs it was made under — the same
reasoning as `config_hash` on `plans`. Re-deriving a ladder broke that silently:
`tier_applied = 2` meant one week of training before and a different one after, with nothing
in the row to say which.

**The fix, additive.** `tiers` gains `version`, keyed `UNIQUE(plan_id, version, tier_index)`,
and stops being overwritten — `save` appends a new version and the old rungs stay, so a past
decision can still be resolved. `plan_progression` gains `ladder_version`, written by
`record_week` from whichever version was in force when the week was graded.

**NULL means something.** Rows decided before a plan had a derived ladder were graded against
the built-in rungs, and `ladder_for` returns those. Backfilling `1` onto them would claim they
were graded by a ladder that did not exist when they closed — the exact dishonesty the column
is here to prevent. That mutant was the only one of ten to survive the first pass: the
migration could have written `1` into every old row and nothing failed.

**What `--replace` means now.** Not "overwrite" — "derive a new version". It is still a flag,
but for a different reason: re-deriving is no longer retroactively destructive, it is simply a
real change to everything prescribed from the next roll onward.

Verified live: re-derived, got version 2, confirmed v1 still loadable and byte-identical (same
history, same config). The redundant version was then removed, since nothing referenced it and
a version with no reason to exist is noise in the athlete's own data.

### Step 6 — archetype overrides, done 2026-11-05

The first step where editing a text file changes what a session asks someone to ride. Most of
that is theirs; a few things are not, and the refusal turned out to be the more interesting
half.

**The shape.** `archetypes:` in config, merged per variant and per key — naming
`threshold.long_reps.pct` changes the target and leaves the reps, bout length and rest alone.
A name the library does not have *creates* a variant, which is what makes the library
extensible rather than merely adjustable. `archetypes.variants()` does the merge at use, not
at import, for the same reason `plan_name()` is a function.

**The rules that are not configurable** live in `src/planning/safety.py`: threshold never
crosses FTP (including the micro alternation), bouts are 10min+ accumulating 40min+ at ≥8:1
work:rest, HIIT bouts are 4min+ and sub-maximal, sprints are 10–40s, the easy ride stays easy.
Each is there because breaking it produces a *plausible* session that is wrong — a threshold
target of 1.02 looks almost identical to 0.98 and is a different session.

Validated **at config load**, merged over the defaults first, with every broken rule reported
at once. Judged alone, an override naming only `rest_min` would have no `work_min` to form a
ratio with and would pass.

**`safety.py` is a leaf that imports nothing**, which is what lets `config.py` use it without
closing the import cycle the leaf rule exists to prevent. The existing "config imports nothing"
test now permits exactly two names and asserts `safety` itself imports none — plus a third
test that neither import is at module level, so `import src.config` still costs nothing.

**Our own library is validated against these rules in the suite.** A rule the defaults break is
a wrong rule, not a library to fix — and without that test the rules could be written to any
strictness and only ever be felt by a stranger with a config file.

The roadmap's bar — invariants holding against a config overriding *every* variant — passes:
the hard-session ceiling, two lifts a week, no lower body beside a long ride, no doubled slot.
None of those come from the library, which is the point: the library decides what a hard
session *is*, never how many there are or where they land.

**A real bug, found by testing something else.** A minimal config written for these tests had
no `goal_date`, and `store_horizon` died on `IntegrityError` —
`training_plan_weeks.plan_end_date` was NOT NULL from when the date was a module constant that
always existed. Plenty of people train without a dated event. Migration 11 makes it nullable;
whoever hit this would have been someone running the project for the first time.

**The read/write split appeared a third time.** `plan_build.py` printed 3.42h and a 60min easy
ride while the calendar had 3.5h and 65min, because it read `progression.TIERS` directly. Fixed
in the API and the report during step 5; this script was not looked at. There is now an
AST-based guard: `TIERS[...]` as a direct subscript fails the suite outside the four modules
where the module ladder is the subject rather than a shortcut. Verified the guard fires.

17 mutants, all caught. One survived first — removing the unknown-archetype guard in
`check_all`, which is reachable only when the misspelled block has *no* variants, since
`violations` catches it otherwise.

### Step 7 — the first run, done 2026-11-05

Built the way the roadmap said to verify it: clone into an empty directory, follow the README,
write down where it dead-ends. **Three of the first five commands ended in a raw traceback.**

| command | what a newcomer saw |
|---|---|
| `fetch_history.py` | `KeyError: 'INTERVALS_API_KEY'` |
| `bootstrap_plan.py` | `OperationalError: no such table: activities` |
| `fetch_history.py`, bad key | `401 Client Error: Unauthorized for url: ...`, four times in one line |

Each is a complete diagnosis for someone who already knows this project and useless to anyone
else — which is the definition of the problem this step exists to fix.

**`scripts/status.py` is the answer, and the first command in the README.** It reports where a
setup has got to and prints the single next thing to run. The whole first run became: run it,
do what it says, repeat. It is read-only — creates no files, migrates nothing, calls no API —
because a status command that changed things is the opposite of what someone reaches for when
they are lost.

Two design points worth keeping:

- **It stops at the first blocker.** Without a key there is no sync, so no history, no ladder
  and no plan. Reporting all four buries the only one that can be acted on under three of its
  own consequences.
- **A missing FTP warns without blocking.** Every percentage in every prescription resolves
  against it, so the sessions are shapes with no watts — but the plan does generate, and
  halting there would hide the ladder and plan checks behind it.

**Fixed along the way:** a missing API key now says where to get one; a rejected key names
*which* of the two credentials to check and what the athlete id currently is;
`bootstrap_plan.py` migrates before it reads; `fetch_history.py` prints the credentials
message rather than a stack trace; `serve.py` prints outstanding items before the URL, so a
half-set-up install does not open on empty charts with no explanation. And `plan_build.py`
stopped telling people to wait for "the roller (step 4)", which meant nothing outside this
file.

**The derivation was validated on someone else's data for the first time.** A synthetic
athlete riding 3×60min and a 90min weekend ride derived a ladder with a 90min long ride and
T1 at 3.58h — genuinely different rungs from this athlete's, which is the thing step 5 could
not demonstrate using only one person's history.

14 mutants, 13 caught. The survivor is `create_if_missing=False` in the config check, which is
unreachable behind an existence test — kept anyway, and now commented as such, because the
flag is right independently of the guard and `clock.today_local()` generating a config as a
side effect is a trap this project has already fallen into once.

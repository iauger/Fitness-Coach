# Fitness Coach

A cycling training plan that follows what you actually ride, instead of what week it is.

It connects to [intervals.icu](https://intervals.icu), reads your rides and recovery data, plans
your next three weeks of workouts, and puts them on your intervals.icu calendar, which sends them
on to Zwift and Garmin. At the end of each week it looks at what you did and decides whether to
ask for more, hold steady, or ease back. A coach you can talk to, powered by Claude, sees the same
data and can answer questions about your training.

It runs on your own computer and keeps everything in a local database. There is no account to
create and nothing is hosted: you need an intervals.icu account (free) and, for the coach, an
Anthropic API key.

## What it does

- **Plans.** Each week it builds the next three weeks of sessions (easy rides, a long ride, usually
  one hard session, and strength work) sized to where you are now.
- **Adapts.** Complete a week and the plan steps up. Have a partial week and it holds. Miss time
  and it eases you back down and gives you a gentle first week back, rather than asking you to
  catch up.
- **Delivers.** Sessions appear on your intervals.icu calendar and reach your head unit or Zwift
  with no manual export. Nothing is written to your calendar until you switch that on.
- **Shows.** A local app has your plan, your progress through the levels, a calendar, and a
  dashboard of fitness, load and recovery.
- **Coaches.** You can ask the coach about your week, do a weekly check-in, and tell it how you
  are doing. It remembers what you tell it (illness, stress, a sore knee) and you can see and
  correct everything it has noted.

## Why it exists

Most training plans are a calendar. Week 14 asks for week 14's volume whether or not weeks 1 to 13
happened. That works until you miss a fortnight: from then on the plan describes a life you did
not live, and the only way back is to abandon it and start over. Plenty of people do exactly that.

This app is built around that moment. There is no end date and no fixed schedule. The plan only
looks three weeks ahead, and you move forward by completing weeks, not by the calendar advancing.
Miss a week and you lose one step, not the whole plan. Strength work is never cut, at any level,
because it is often the habit that survives a break.

## How the plan works

### Five levels, built from your own riding

Progress is a ladder of five levels, T0 to T4. The ladder is not shipped with the project: it is
worked out from your own history. Run `scripts/bootstrap_plan.py` once and it looks at the hours
you have really been riding and the longest ride you really do, then builds five steps from there
up toward the weekly hours you want to reach. Until that has been done the app will not generate
a plan, because a plan built on someone else's riding would look reasonable and be wrong.

As an example, a rider coming back from a long break who was doing about 2.8 hours a week with a
70-minute longest ride gets:

| level | rides | long ride | hard sessions | easy ride | ~hours a week |
|---|---|---|---|---|---|
| T0 | 2 | 75 min | 0 | 50 min | 2.1 |
| T1 | 3 | 75 min | 1 | 65 min | 3.3 |
| T2 | 3 | 90 min | 1 | 65 min | 3.5 |
| T3 | 3 | 105 min | 1 | 65 min | 3.8 |
| T4 | 3 | 120 min | 1 | 65 min | 4.1 |

Yours will look different. The top level aims for `sustainable_band_hours` from your settings, but
never beyond what `long_ride_ceiling_min` allows. If your history cannot reach the weekly hours
you asked for, the bootstrap tells you so instead of quietly stretching the ladder to fit.

### Moving between levels

At the end of each week the app compares the time you rode with what that week asked for:

- **Rode at least 90% of the planned time, on at least the planned number of days:** up one level.
- **Rode some, but less:** stay where you are.
- **Missed time:** down one level for each week missed, and the first week back is a lighter
  "re-entry" week, not the middle of a block you were never part of.

The top level is a ceiling. Going beyond it is a decision you make, never something a good run
of weeks does for you.

Three rules keep this trustworthy:

- **It counts time on the bike, not how closely you matched a workout.** Matching rides to
  prescribed sessions fails for reasons that have nothing to do with training: an outdoor ride
  that looks nothing like the plan, a heart-rate-only file, a strength session with no power data.
  Counting minutes ridden has one failure mode, a ride that did not sync.
- **A week is graded once.** The result is saved with the numbers it was based on, so you can
  always see why you are at the level you are.
- **Today and tomorrow never change.** When the plan updates, it only rewrites sessions two or
  more days away. An evening sync cannot swap tomorrow morning's ride, and past weeks stay as a
  record of what was asked.

### Fitting it to your week

Three settings, each separate, shape how a week is laid out:

**How many days you ride.** This comes from what you actually ride, capped by `max_ride_days`
in your settings. Setting 7 when you really ride 3 gives you a three-day week. Anything from two
to seven days works (seven is allowed and is a bad idea for most people), with the long ride on
Saturday and strength sessions on rest days where there are any:

| days | the week |
|---|---|
| 3 | Tue hard · Wed lift · Thu easy · Sat long · Sun lift |
| 5 | Mon lift · Tue hard · Wed easy · Thu easy · Fri lift · Sat long · Sun easy |
| 7 | every day rides; two of them also lift |

**Which days.** By default the long ride is Saturday and the hard session Tuesday. If your week is
different, say so:

```yaml
plan:
  available_days: [mon, wed, fri, sun]     # days you can train; unset means any
  long_ride_day: sun                       # optional; otherwise Saturday, or nearest the weekend
```

Everything, strength included, is placed on those days and nowhere else. The hard session goes as
far from the long ride as the week allows, and strength stays away from both. With the days above
that gives a long ride on Sunday, hard on Wednesday, easy on Friday, and strength on Monday and
Friday. The settings are checked when they load (a three-day week with two strength sessions is
refused, with the reason), and `status.py` re-checks them against your saved ladder, since editing
your days later could otherwise break the next update without anyone noticing.

**How long a ride can be.** "Three days a week, ninety minutes at most" is two numbers:

```yaml
plan:
  max_ride_days: 3
  max_ride_min: 90                  # the longest any single ride may be
  day_max_ride_min: {sat: 150}      # optional per-day overrides
```

The ladder is built to fit inside your limit rather than having rides trimmed afterwards. The
difference matters: if a 105-minute ride were quietly cut to 90 to fit, the level would still
expect 105. You would do everything you were asked and stay on the same level for ever, with
nothing telling you why. Instead the ladder climbs as far as your limit allows (the long ride
first, then the easy rides) and stops, and the app says so. At the top level it notes that your
limit is what is holding you there. After a month of meeting it, the coach raises it at a check-in
with the options and what each costs, for example Saturday from 90 to 105 minutes (about a quarter
of an hour more a week) or a fourth ride day (about an hour and a half). If your limit leaves no
room to climb at all, the bootstrap refuses and names the number ("raise `plan.max_ride_min` to at
least 80"). A limit you tighten later that your saved ladder no longer fits is flagged by
`status.py` before the next update can fail.

Hard sessions come in fixed shapes of 40 to 78 minutes. One that does not fit its day is swapped
for a variant that does, and if none fit, that day becomes an easy ride. Threshold work needs
about 60 minutes and VO2 work about 52; sprint sessions fit in 45. `status.py` tells you which
blocks are affected, so you do not find out from a calendar with no intervals on it.

## How the coach remembers

The coach has no memory of its own. Each time you talk to it, it starts fresh and is handed a
briefing: your training data, plus a few short notes about earlier conversations. Everything it
"remembers" is what is written in that briefing.

- **Every conversation is stored in full**, in the app or on the command line, so you can reopen
  it. The coach does not read these; they are too long to hand over each time. A conversation
  ends when you end it or start a new one, or after an hour of silence, and a finished one is
  read-only: to keep talking you start a new one, so a check-in stays one event.
- **When a conversation ends, it is read once** by a separate, small request that sees only your
  words and cannot change any of your records. It files what you said about how you are (illness,
  sleep, stress, work, motivation, how sessions felt...) as dated, graded **signals**, and writes
  a gist of up to 40 words. Every signal has to quote you word for word or it is thrown away.
  A conversation that ended without being read (you closed the tab) is read the next time a
  conversation or check-in begins. Signals are stored silently; nothing in them changes your plan.
- **The coach is shown a short block built from those signals**: what is open and serious (the
  "watch list"), a one-line summary of the last four weeks, and themes that keep coming back,
  worked out by code rather than written by a model. It also gets the gists and follow-ups of
  your last four conversations, each with its age. The last weekly check-in is no longer handed
  back to it. Anything older it can look up with a tool (`query_signals`), and where what you
  said disagrees with your data, it is told the data wins.
- **You can see and correct it.** The Signals screen lists everything filed from your words, with
  the quote it came from. "That is wrong" sets one aside (kept, with your reason) and the coach
  stops being told it at once. The Coach screen's "What the coach remembers" lists the
  conversation gists and lets you edit or forget one. The Plan screen shows what is open, and what
  it would do to your plan if it were allowed to. It is not: nothing you say changes the plan yet.
  A serious signal can make a check-in due.
- **The coach can offer a change but never make one.** If you say the sessions feel too easy and
  the number of rides a week is what is holding you back, it can put a button in the conversation
  ("rides a week 3 → 4, because..."). Nothing happens until you press Apply, which saves that one
  setting; your ladder is not rebuilt until you review it on the Ladder screen.

Hard facts (an injury, a goal, a change at work) belong in the structured records, not in a note:
the notes are a short rolling window, and the oldest fall out of it.

## Where the design came from

It was first built for one rider returning from a long break, using nine years of their own
training data, and the shape came from what that data showed rather than from a training
methodology. Over fourteen months, 21 of 62 weeks had no ride at all, yet only one of the eleven
breaks was preceded by signs of overreaching: the rest began from normal heart-rate variability
and good sleep. People were not burning out. They were falling out of the routine and finding no
way back in.

That is why there is no ramp to fall behind, why a missed week costs one step instead of the
plan, and why strength work is never trimmed: in 8 of those 21 empty weeks the lifting carried on
after the cycling stopped.

Your data will say something different, which is why the ladder is worked out from yours instead
of shipped. As a check on that process, run against the original rider's history it reproduced
the ladder a person had chosen by hand from the same data: the same long rides, ride days and
hard-session placement, differing only by a 64-minute median ride rounded up to 65 where the
person rounded down to 60.

## Configuration

Everything personal lives in one file. Copy the example and edit it:

```bash
cp config/athlete.example.yaml config/athlete.yaml
```

```yaml
athlete:
  name: Your Name
  timezone: America/New_York     # IANA; decides what "today" means. UTC underneath.
  notes: "Two young kids, shift work."   # read by the coach, by nothing else

goals:
  - description: Cyclocross racing fall 2027
    date: 2027-10-01
    priority: primary            # primary | secondary | opt_in

plan:
  name: Sustainable rolling
  start_monday: 2026-11-02       # must be a Monday
  sustainable_band_hours: [3.5, 4.0]
  horizon_weeks: 3
```

`config/athlete.yaml` is gitignored. It is also the file you *would* paste when asking for
help — secrets stay in `.env`, and that is the whole split. The loader is strict: an unknown
key is an error naming the key and listing the alternatives, rather than a setting you believe
you changed.

The file is generated from the example on first run if it is missing.

## Running it

Requires Python 3.12+ and an intervals.icu account (free). Garmin, Zwift and Strava reach this
project *through* intervals.icu — none of them are integrated directly.

```bash
python -m venv .venv
.venv/Scripts/activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/status.py
```

Run everything below with that environment active (or call `.venv/Scripts/python.exe` directly).
A system Python that happens to have some of the packages will run some scripts and fail others
with `ModuleNotFoundError`, which looks like a missing dependency and is not one.

**`status.py` is the first command and the one to come back to.** It reports where your setup
has got to and prints the single next thing to run — it creates nothing, changes nothing and
calls no API, so it is also what to reach for when something stops working. The whole first
run is: run it, do what it says, repeat.

```
  ok  secrets       intervals.icu key present
  ok  config        Your Name, Europe/London
  ok  database      22 tables
  ok  history       412 activities, 2019-03-02 to 2026-11-04
  --  ftp           no FTP on record — prescriptions will have no watts
  --  calendar      push off — sessions stay on this machine
  ->  tier ladder   not derived yet

  next: python scripts/bootstrap_plan.py            # look at it
    python scripts/bootstrap_plan.py --write    # keep it
```

In order, that walks you through:

| | |
|---|---|
| `cp .env.example .env` | your intervals.icu key, from **Settings > Developer** |
| `cp config/athlete.example.yaml config/athlete.yaml` | then edit it — name, timezone, goals, plan start |
| `python scripts/migrate.py --apply` | create the database. Whenever a migration is pending on a database that has anything in it, `data/backups/` gets a copy first (the newest five are kept) |
| `python scripts/fetch_history.py` | backfill your riding; this is what the ladder is derived from |
| `python scripts/seed_profile.py --ftp 250` | your FTP — every prescription is a percentage of it |
| `python scripts/bootstrap_plan.py` | look at the derived ladder, then `--write` to keep it |
| `python scripts/sync_now.py` | generate the first horizon |
| `python scripts/serve.py --open` | the app: plan, setup, settings, ladder, with the dashboard and calendar one click away |

**Nothing reaches your intervals.icu calendar until you ask it to.** The push is the only
thing here that writes anywhere but your own machine, and it writes to a calendar that may
already hold a plan you are part-way through, so it is off until you turn it on:

```yaml
integrations:
  intervals_icu:
    push_calendar: true
```

Generate a horizon first, read it with `python scripts/plan_build.py`, and turn the push on
when you want those sessions on your devices. `status.py` says which state you are in.

Leave it off in any **second copy** of this project pointed at the same account. Every event
id is derived from the plan slug, so two copies produce identical ids and the second
overwrites the first one's calendar rather than adding to it — which is how this default was
arrived at, after a sandbox clone replaced all twelve events of a real horizon on its first
sync.

The push also **cleans up after itself**: when a session leaves your plan (you lower the rides a
week, the gate steps you down a rung), the calendar event it created is removed on the next pass,
including ones left behind by earlier versions. It only ever touches events whose id has this
plan's exact shape (`<plan-slug>-<date>-<role>`), never one you added yourself. That is one more
reason a second copy must keep the push off: it would remove the first copy's events its own plan
does not contain.

Two more worth knowing about in advance. **`fetch_history.py` is not optional** — the
tier ladder is derived from your own riding, and the generator refuses to build anything until
it has been, rather than quietly falling back to someone else's rungs. And **`bootstrap_plan.py`
is dry by default**: it prints the ladder it would derive and writes nothing until you pass
`--write`, because that ladder decides every session for months.

`serve.py` runs a background loop that syncs, rolls the plan forward and pushes the horizon to
your calendar. To do one pass without the server:

```bash
python scripts/sync_now.py         # sync, roll, push if configured to
python scripts/plan_build.py       # preview the horizon without writing it
python scripts/checkin.py          # weekly check-in with the coach
python scripts/cycle_review.py     # 4-week block review
python scripts/ask.py              # conversational session
```

### Keeping it fresh

The six-week gap this project was rebuilt to fix happened because syncing was something a person
had to remember. Two things now do it:

- **`serve.py`** runs the maintenance loop (pull, roll, reconcile, push) every hour while it is up
  (`SYNC_INTERVAL_MINUTES` in `.env` changes that; `FITNESS_COACH_NO_SCHEDULER=1` turns it off).
- **A Windows Scheduled Task** runs `sync_now.py` whether or not the server is up:
  `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1` (`-IntervalMinutes 120`,
  `-Uninstall`). It runs only while you are logged in and never wakes the machine. If both fire
  together, a file lock makes the second one record a skip instead of colliding. On macOS or Linux,
  point cron at `python scripts/sync_now.py`.

The Anthropic key is used by the coach only (`src/coach/`): each message you send is a billed
request to the model in `COACH_MODEL` (default `claude-haiku-5-5`), and ending a conversation
costs one more small request to file what you said. Syncing, planning, the gate and the
dashboard make no model calls at all, and `scripts/logs.py --event coach.turn` shows the tokens
each turn used.

### When something goes wrong

The app keeps a log of what it did and what failed in `data/logs/app.jsonl` (five rotating files
of 1 MB). It records events, never words: counts, durations, exception types and where they came
from, never an activity name, a note, a goal, a question or a coach reply, and never a key.

```bash
python scripts/logs.py --level warning --since 24h     # what went wrong lately
python scripts/logs.py --counts --since 7d             # how often each thing happened
python scripts/support_bundle.py                       # one zip to send to whoever is helping
```

`status.py` also adds a `problems` line when the last day has any warnings or errors. The support
bundle holds the status report, the log, installed versions, the *shape* of the database (table
names and row counts) and the settings that are not free text. It holds none of your activities,
notes, conversations or goals, and your name is removed wherever it appeared. Nothing is sent
anywhere: it is a file under `data/bundles/`, and sending it is up to you after you have opened it
and looked.

**To ask someone for help**, in this order:

1. `python scripts/status.py` — it often names the fix itself.
2. `python scripts/support_bundle.py` — writes `data/bundles/support-<time>.zip` and prints what
   is inside it. Nothing is sent anywhere.
3. Open the zip and read it. It holds the status report, the log, installed versions, the shape of
   the database (table names and row counts) and the settings that are not free text. It should
   hold none of your activities, notes, conversations, goals or keys, and your name is removed
   wherever it appeared; if you spot anything that is yours to keep, do not send it and say so.
4. Send the zip to whoever is helping.

Each coach turn is one log line too (tokens in and out, which tools ran, whether a suggestion
button was offered, how long it took), which is the usage data without any of the conversation.

### Every command

All are run from the project folder with the virtual environment active. `--help` works on each.

| command | what it does |
|---|---|
| **Setup and health** | |
| `python scripts/status.py` | where setup has got to, the one next command, and a `problems` line; read-only |
| `python scripts/migrate.py` / `--apply` | show / apply pending database migrations (a backup is taken first) |
| `python scripts/fetch_history.py` | backfill your full history from intervals.icu |
| `python scripts/seed_profile.py --ftp 250` | record your FTP (and height or weight) |
| `python scripts/seed_goals.py` | fill an empty goals table from the `goals:` in your config (`--reset` re-seeds); the Settings screen edits them afterwards |
| `python scripts/bootstrap_plan.py` / `--write` | preview / keep the tier ladder derived from your riding |
| **Every day** | |
| `python scripts/serve.py --open` | the app at `http://127.0.0.1:8800`; also runs the hourly sync |
| `python scripts/sync_now.py` | one cycle: sync, roll the plan, push if configured (`--no-push`, `--push`, `--verbose`) |
| `python scripts/sync.py` | the sync alone, no calendar push (`--dry-run` shows the window) |
| `python scripts/plan_build.py` | preview the horizon without writing it (`--weeks 3`) |
| `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1` | Windows scheduled sync (`-IntervalMinutes`, `-Uninstall`) |
| **The coach** | |
| `python scripts/checkin.py` | the weekly check-in |
| `python scripts/cycle_review.py` | the four-week block review |
| `python scripts/ask.py` | a free conversation |
| `python scripts/weekly_summary.py` | regenerate a week's summary |
| **Charts** | |
| `python scripts/generate_charts.py` | write the dashboard and calendar pages to `data/charts/` |
| **When something is wrong** | |
| `python scripts/logs.py` | read the application log (`--level warning --since 24h`, `--counts`, `--event coach.turn`) |
| `python scripts/support_bundle.py` | the zip to send when asking for help |
| **Occasional** | |
| `python scripts/import_strava_archive.py --archive export.zip` | add activities from a Strava export |
| `python scripts/dedupe_activities.py` | find and remove the same ride recorded twice |
| `python scripts/backfill_notes.py` | pull older activity notes from intervals.icu |
| `python scripts/backfill_signals.py` / `--apply` | preview / file signals from your earlier writing (calls the model) |
| `python scripts/import_transcripts.py` | bring old command-line transcripts into the app |
| `python scripts/eval_signals.py` | score the signal extractor against the gold set (calls the model) |
| `python scripts/code_graph.py` | regenerate the code map (developers) |

> **`serve.py` binds to `127.0.0.1` and must stay there.** The process reads `.env`, so it
> holds your API keys, it has no authentication, and the chat endpoint spends money per call.
> Remote access is a VPN's job until auth exists.

## How the pieces fit

```
intervals.icu ──sync──> SQLite ──> progression gate ──> rolling horizon
                          │        grades closed weeks,  3 weeks of dated
                          │        one row each, forever  sessions
                          │                                    │
                          ├──> analysis: PMC, load, wellness   │
                          │         │                          │
                          │         └──> Claude coaching layer │
                          │                                    │
                          └──> dashboard + calendar (local) <───┤
                                                                │
                            intervals.icu calendar <──push──────┘
                                       │
                                  Zwift, Garmin
```

| | |
|---|---|
| `src/config.py` | the athlete's file, parsed into frozen dataclasses and validated at the door |
| `src/sync.py` | incremental pull from intervals.icu, then score, then roll |
| `src/maintenance.py` | the unattended cycle: sync, roll, reconcile, push; takes the cross-process lock |
| `src/planning/bootstrap.py` | derives the tier ladder from history; pure functions |
| `src/planning/progression.py` | the gate, as pure functions — no database, no I/O |
| `src/planning/progression_store.py` | the append-only decision log |
| `src/planning/roller.py` | builds and stores the horizon; owns the freeze rule |
| `src/planning/export.py` | push and reconcile against the intervals.icu calendar |
| `src/analysis/` | PMC, weekly load, compliance, wellness, cycle reviews |
| `src/coach/` | the Claude layer: prompt, tools, sessions, memory, the suggestion cards |
| `src/signals.py` | the ledger of what you have said about how you are; the one place that decides how long it matters |
| `src/api/` | the FastAPI app, its guard (Host, Origin, token) and the routes behind each screen |
| `src/web/index.html` | the app's screens, one file of plain JavaScript, no build step |
| `src/visualizations/` | the Plotly dashboard and the calendar, written to `data/charts/` |
| `src/db/` | the schema as numbered, append-only migrations, and the write helpers |
| `src/firstrun.py` | what `status.py` and the Setup screen report; the two cannot disagree |
| `src/applog.py`, `src/support.py` | the event log and the support bundle |

That is the shape, not the inventory. [docs/CODEMAP.md](docs/CODEMAP.md) is generated from the
source and lists every module with what it imports, what uses it, the tables it touches and the
tests that cover it.

## Working on the code

- **Start with [docs/CODEMAP.md](docs/CODEMAP.md)** rather than searching the tree. For one thing
  and everything connected to it, `python scripts/code_graph.py show <name>` answers in a few lines
  (a module, a function, `table:signals`, `route:POST /api/chat/ask`, `event:sync.done`). The same
  graph as data, every node and edge, is `docs/codegraph/graph.json`. Calls are drawn only where a
  name resolves statically, so "no callers" can mean dispatch through a table; imports, tables,
  routes and events are exact.
- **Regenerate it when the structure changes:** `python scripts/code_graph.py`. A test fails if
  the committed map has drifted from the code, and its message says the command.
- **[CLAUDE.md](CLAUDE.md) holds the rules that must not be broken**, each with the failure that
  caused it. Read the section before changing the files it names. It is written for coding agents
  and is just as useful to a person.
- [ROADMAP.md](ROADMAP.md) is the reasoning and the backlog. `devlog.md`, the session-by-session
  record, is kept on the author's machine only.

## Status

Working and in daily use by its author: the sync, the gate, the rolling horizon, calendar push
with device delivery confirmed on Zwift, the dashboard and calendar, and the coaching layer.

The refactor from *one person's plan* into *something someone else could run* is done:
configuration in one file, a plan identity that survives being renamed, the athlete's timezone,
a tier ladder derived from your own history and versioned so past decisions keep the rungs they
were graded against, an overridable workout library with non-negotiable safety rules, and a
first run that tells you what to do next.

One thing stands between this and anything networked:

- **Authentication.** `serve.py` holds your API keys and has no login, so it binds loopback and
  refuses to be useful anywhere else. Until that exists, remote access is a VPN's job.

Then features: FTP that tracks your own estimates rather than a number someone typed, CTL
computed locally so one FTP correction fixes the whole history, power curves, workout
alternates, and Garmin delivery verification.

See [ROADMAP.md](ROADMAP.md) for the reasoning behind each, and the backlog.

## Testing

```bash
pip install -r requirements-dev.txt     # the app's requirements plus pytest
pytest
```

Over 1,600 tests, about a minute and a half. The convention worth knowing: new behaviour is checked by **mutation testing**, not
only by a green suite. Breaking the code deliberately and confirming a test fails has
repeatedly caught tests here that passed for the wrong reason — one asserting a constant's own
value back at itself, a filter that worked only because of insert order. Several docstrings in
`tests/` record exactly which mutant a test exists to catch.

Several guards are greps or AST checks rather than behaviour, each added after the same defect
appeared twice: `date.today()` outside `src/clock.py` fails the suite, because it is the
server's day and not the athlete's; so does a second definition of the sport table; so does
reading the built-in tier ladder where a plan has its own, which had the dashboard reporting
3.42h beside a calendar showing 3.5h; so does building the weekly template anywhere but
`plan_rotation`, which would ignore the days the athlete said they can train. Tests cannot open
the real database, and read the committed example config rather than yours.

## Stack

Python · SQLite · FastAPI · Plotly · intervals.icu API · Claude API

## License

[MIT](LICENSE).

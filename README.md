# Fitness Coach

A training plan that is gated on what you actually rode, not on what week it is.

It sits on top of [intervals.icu](https://intervals.icu): it reads your synced activities and
wellness data, decides what the next three weeks should look like, writes those sessions to
your intervals.icu calendar — which delivers them to Zwift and Garmin — and scores what
happens. A Claude-backed coaching layer reasons over the same data.

Everything runs locally against a SQLite file. There is no hosted service.

## The idea

Most structured plans are a dated ramp: week 14 asks for week 14's volume whether or not
weeks 1–13 happened. That works until you miss a fortnight, at which point the plan describes
a past you did not live and there is no way back into it except to start over.

This one has no dates beyond a three-week horizon and no end. Progression is a **tier ladder**,
and you move up a rung by completing a week, not by the calendar advancing.

**The ladder is derived from your own synced riding**, not shipped with the project. One run of
`scripts/bootstrap_plan.py` reads your history, finds the volume you have actually held and the
long ride you actually do, and places five rungs between there and the band you say you want to
reach. Until that has happened the generator refuses to build anything — a plan built on a
stranger's measured practice would be plausible, wrong, and silent about it.

For one real athlete — returning from a six-month gap, riding about 2.8h a week with a
70-minute longest ride — it derives:

| tier | rides | long ride | hard | easy | ~hours |
|---|---|---|---|---|---|
| T0 | 2 | 75min | 0 | 50min | 2.1 |
| T1 | 3 | 75min | 1 | 65min | 3.3 |
| T2 | 3 | 90min | 1 | 65min | 3.5 |
| T3 | 3 | 105min | 1 | 65min | 3.8 |
| T4 | 3 | 120min | 1 | 65min | 4.1 |

T0 and T1 share a long ride on purpose: what separates them is the hard session. Yours will
look different, and the top rung reaches for `sustainable_band_hours` from your config without
exceeding what `long_ride_ceiling_min` allows — if your history cannot reach the band you asked
for, the bootstrap says so rather than quietly bending the ladder upward to meet it.

**The shape of the week is derived too**, not just its size. How many days you ride comes from
what you actually ride, capped by `max_ride_days` — config states availability, history states
fact, and someone who sets 7 but rides 3 gets a three-day week. Anything from two to seven days
generates, with the long ride anchoring Saturday and lifts placed on rest days where there are
any:

| days | the week |
|---|---|
| 3 | Tue hard · Wed lift · Thu easy · Sat long · Sun lift |
| 5 | Mon lift · Tue hard · Wed easy · Thu easy · Fri lift · Sat long · Sun easy |
| 7 | every day rides; two of them also lift |

Seven is permitted and is a bad idea for most people. That judgement belongs to the athlete, not
to the code.

**Which days** is a separate setting from how many. Left alone, the long ride is Saturday and the
hard session Tuesday, because that is when the athlete this plan was first built for rides. For
anyone else:

```yaml
plan:
  available_days: [mon, wed, fri, sun]     # days you can train; unset means any
  long_ride_day: sun                       # optional; otherwise Saturday, or nearest the weekend
```

The week is then placed inside those days and nothing, lifts included, lands outside them. The
hard session goes as far from the long ride as the week allows on both sides, and lifts keep
away from both. With the days above that is Sunday long, Wednesday hard, Friday easy, lifts on
Monday and Friday. The rules are checked when the file loads — a three-day week with two lifts
is refused there, naming the arithmetic — and `status.py` re-checks them against your stored
ladder, because you can edit the days after the ladder was derived and the next roll would
otherwise fail without anyone watching.

**How long a ride may be** is a third, separate setting. "Three days a week, ninety minutes at
most" is two numbers:

```yaml
plan:
  max_ride_days: 3
  max_ride_min: 90                  # the longest any single ride may be
  day_max_ride_min: {sat: 150}      # optional per-day overrides
```

The plan is built *inside* that limit rather than clamped to it afterwards, and that distinction
is the design. A rung is graded against 90% of its minutes, so a ride quietly shortened to fit
would leave a rung the gate expects and the calendar cannot deliver: you would do everything
asked and hold at the same tier for ever, with nothing saying why. Instead the ladder climbs to
what your limit allows — the long ride first, then the easy rides, one thing changing per rung —
and stops. For someone riding 75-minute long rides and 65-minute easy ones, a 90-minute limit
climbs 3.25h to 4.0h in real steps.

When the plan reaches the limit, it tells you. Once you are at the top rung it notes that the
limit is what is holding the plan there. After a month of meeting it, the coach raises it at a
check-in with the options and what each costs: raise Saturday from 90 to 105 (about a quarter
of an hour more a week), or add a fourth day (about an hour and a half). If your limit leaves no
room to climb at all, bootstrap refuses and names the number: "raise `plan.max_ride_min` to at
least 80". The same goes for a limit you tighten later that the stored ladder no longer fits:
`status.py` flags it before the next roll can fail.

Hard sessions come in fixed shapes of 40–78 minutes, so one that cannot fit its day is chosen
from the variants that do, and if none do, that block's hard slot becomes an easy ride. Threshold
needs about 60 minutes and VO2 about 52; the sprint sessions fit in 45. `status.py` says which
blocks are affected rather than leaving you to find out from a calendar with no intervals on it.

A week that hits ≥90% of its tier's volume *and* its ride-day count advances you one rung. A
partial week holds. A gap steps you down one rung per week and produces a **re-entry week**
rather than dropping you into week 4 of a block you were never in. T4 is a ceiling, not a
waypoint — going beyond it is an explicit decision, never the automatic consequence of a good
streak.

Three properties make it behave:

- **The gate reads measured volume, never compliance scoring.** Adherence matching fails for
  reasons that have nothing to do with training — an outdoor ride that does not resemble the
  prescription, a heart-rate-only file, strength work that carries no planned TSS. A gate built
  on `SUM(moving_time)` per Monday-week inherits exactly one failure mode: a ride that did not
  sync.
- **Decisions are append-only.** When a week closes it is graded once and the row is written
  once. Re-deriving a past decision later answers today's question, not the one that was asked,
  so the inputs each decision was made under are stored beside it.
- **Today and tomorrow are frozen.** A regeneration only ever rewrites sessions dated two or
  more days out, so an evening sync cannot change tomorrow morning's ride, and past
  prescriptions survive as the record of what was asked.

### How the coach remembers

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
- **You can see and correct it.** The Coach screen's "What the coach remembers" lists exactly what
  the coach is given, and lets you edit or forget a conversation note.

Hard facts (an injury, a goal, a change at work) belong in the structured records, not in a note:
the notes are a short rolling window, and the oldest fall out of it.

### Why it is built this way

It was built for one athlete, from nine years of their own data, and the shape came out of
what that data said rather than out of a methodology. Over fourteen months, **21 of 62 weeks
had no ride at all** — but only one of eleven dropouts had any fatigue signature beforehand.
The rest began from normal or good HRV and sleep.

So the failure mode being designed against is *failure to re-enter after an interruption*, not
overreaching. That is why there is no ramp to fall behind, why a missed week costs one rung
instead of the plan, and why strength work is never trimmed at any tier: in 8 of those 21
zero-ride weeks, lifting continued after cycling stopped.

Your own data will say something different, which is why the ladder is derived rather than
shipped. As a check on that derivation: run against this athlete's own history, it independently
reproduces the ladder a human chose from the same data — identical long rides, ride days and
hard-session placement — differing only by rounding a 64-minute median session up to 65 where
the human rounded down to 60.

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
pip install -r requirements.txt
python scripts/status.py
```

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
| `python scripts/migrate.py --apply` | create the database |
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
| `src/planning/bootstrap.py` | derives the tier ladder from history; pure functions |
| `src/planning/progression.py` | the gate, as pure functions — no database, no I/O |
| `src/planning/progression_store.py` | the append-only decision log |
| `src/planning/roller.py` | builds and stores the horizon; owns the freeze rule |
| `src/planning/export.py` | push and reconcile against the intervals.icu calendar |
| `src/analysis/` | PMC, weekly load, compliance, wellness, cycle reviews |
| `src/coach/` | the Claude layer: prompt, tools, sessions |

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
pytest
```

978 tests. The convention worth knowing: new behaviour is checked by **mutation testing**, not
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

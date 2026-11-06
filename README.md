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
cp .env.example .env               # intervals.icu + Anthropic keys
cp config/athlete.example.yaml config/athlete.yaml

python scripts/fetch_history.py    # one-time backfill
python scripts/seed_profile.py     # height / weight / FTP baseline
python scripts/bootstrap_plan.py   # derive your tier ladder (add --write to keep it)
python scripts/serve.py --open     # dashboard, calendar, API
```

`serve.py` runs a background loop that syncs, rolls the plan forward and pushes the horizon to
your calendar. To do one pass without the server:

```bash
python scripts/sync_now.py         # sync, roll, push
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

Mid-refactor from *one person's plan* into *something someone else could run*. Steps 0–5 are
done — the config loader, a plan identity that survives being renamed, athlete timezone, the
personal constants moved into config, the sport table deduplicated, and the tier ladder derived
from your own history. Still ahead:

- Archetype overrides, validated against the safety rules that are *not* configurable
- A first-run path that works from a clean clone
- **Authentication, before the server can bind anywhere but loopback**

See [ROADMAP.md](ROADMAP.md) for the reasoning behind each, and the feature backlog.

## Testing

```bash
pytest
```

558 tests. The convention worth knowing: new behaviour is checked by **mutation testing**, not
only by a green suite. Breaking the code deliberately and confirming a test fails has
repeatedly caught tests here that passed for the wrong reason — one asserting a constant's own
value back at itself, a filter that worked only because of insert order. Several docstrings in
`tests/` record exactly which mutant a test exists to catch.

Two guards are greps rather than behaviour: `date.today()` anywhere outside `src/clock.py`
fails the suite, because it is the server's day and not the athlete's; so does a second
definition of the sport table. Tests cannot open the real database, and read the committed
example config rather than yours.

## Stack

Python · SQLite · FastAPI · Plotly · intervals.icu API · Claude API

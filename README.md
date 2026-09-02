# Fitness Coach

An AI-powered personal coaching layer built on top of intervals.icu. Reads your real training and wellness data, then provides the diagnostic and prescriptive guidance a coach would give — not more charts, but actual reasoning about your fitness.

## What it does

- Pulls activity, wellness, and fitness data from intervals.icu
- Analyzes training load, recovery, and trends over time
- Reads your session RPE and the notes you write on rides in intervals.icu, so the coach reasons
  from how a workout actually felt and not just what the numbers say
- Uses Claude to generate coach-style check-ins, diagnostics, and recommendations
- Supports multi-sport training (cycling, running, yoga, weights, swimming)
- Provides periodization guidance toward specific goals and events
- Tracks where you are in a TrainerRoad plan and reviews each 4-week block when it closes

## Goals (personal)

- Return to consistent training after a 6-month gap
- Gravel races spring/summer 2027
- Cyclocross racing fall 2027
- Base fitness: 120-mile ride not an overreach

## Running it

Everything is local. The database is SQLite in `data/`, and nothing is hosted.

```bash
pip install -r requirements.txt
cp .env.example .env          # add your intervals.icu and Anthropic keys

python scripts/fetch_history.py    # one-time backfill
python scripts/sync.py             # incremental sync
python scripts/serve.py --open     # the app
```

`serve.py` binds to `127.0.0.1` only. It reads `.env`, so it holds your API keys and has no
authentication — do not expose it on a routable interface. Remote access is a VPN's job.

The CLI entry points still work and remain the fallback:

```bash
python scripts/checkin.py          # weekly check-in
python scripts/ask.py              # conversational session
python scripts/cycle_review.py     # 4-week block review
python scripts/migrate.py          # schema status
```

## Stack

- Python, SQLite, FastAPI
- intervals.icu API (activities, wellness, session notes)
- TrainerRoad calendar via its public iCal feed
- Claude API (Anthropic)

## Status

In development. The data pipeline, analysis layer and coaching logic are working; the app is
mid-migration from CLI scripts to a local web app (phases 12A-12C done, 12D-12F remaining).

See [ROADMAP.md](ROADMAP.md) for the north star, current state, and what's being built next.

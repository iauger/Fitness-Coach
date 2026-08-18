# Fitness Coach — Dev Log

---

## 2026-08-17 — Session 1: Project Setup & Vision

### Context
Building a personal AI coaching layer on top of intervals.icu. The user is a committed cyclist since 2017 who's had a 6-month training gap (grad school finish, new job, two kids). Primary goals:
- Return to consistent training rhythm
- Introduce multi-sport variety (yoga, weights, swimming, running) to prevent burnout
- Target: gravel races spring/summer 2027
- Primary target: cyclocross racing fall 2027
- Baseline capability goal: 120-mile ride not being an overreach

### Data assets
- intervals.icu with full history since 2017 (activities, wellness, fitness curves)
- Garmin Epix Pro Gen 2 (current) + prior Garmin watches ~4 years — HRV, steps, sleep all likely in intervals already
- Data pull strategy: use intervals.icu API as the single source of truth (already aggregates Garmin sync)

### Architecture decisions
- **Local first**, with clean separation so it can be hosted later
- **Python** — intervals API client, data processing, Claude SDK
- **SQLite** — local storage for activity/wellness history (avoids repeated API calls)
- **Claude API** — coaching reasoning layer (claude-sonnet-4-6)
- **CLI first**, UI deferred to a later phase
- Pull as much historical data as possible from intervals.icu — the 2017+ history is a major asset for understanding baseline fitness

### Phases
1. intervals.icu API client — pull activities, wellness, fitness curves
2. Local DB + data pipeline — store and process historical data
3. Analysis layer — fitness trends, load patterns, recovery signals
4. Claude coaching layer — weekly check-ins, diagnostics, recommendations
5. Multi-sport recommendation logic
6. UI (web dashboard + chat)

### Setup completed
- Git repo initialized
- Connected to GitHub: https://github.com/iauger/Fitness-Coach (private)
- Project structure: `src/` for code, `devlog.md` for session notes

### Next session
Start Phase 1: explore intervals.icu API, build the Python client, pull first data batch.

# Fitness Coach — Project Instructions

## Visual verification is mandatory after any visual-element change

Any change to a chart, HTML/CSS layout, or anything else user-visible in `data/charts/*.html`
(dashboard, calendar) must be verified with the Playwright MCP tools before being called done —
not just regenerated and assumed correct from reading the generating code.

**Why:** Session 11's PMC 4-panel rebuild shipped with three silent rendering bugs (embedded-view
clipping, invisible zone bands, an undersized expand-modal) that passed two full sessions of
code-level review because nothing had actually opened it in a browser. All three were only found
in Session 13 once Playwright MCP was working. Code that looks correct and JSON that looks correct
are not proof a browser renders it correctly — Plotly in particular has several silent-failure
modes (`add_hrect`'s `exclude_empty_subplots` default, `display:none` containers returning 0
width, stale baked-in `layout.width`/`layout.height` from a prior autosize) that produce no error
and no console warning.

**How to apply:**
1. Regenerate the affected HTML (`python scripts/generate_charts.py`).
2. Serve it over `http://localhost` — Playwright's browser blocks `file://` navigation, so charts
   must be served (`python -m http.server <port>` from `data/charts/`, or equivalent).
3. Navigate to the page with `browser_navigate`, then check console messages
   (`browser_console_messages`) for errors/warnings.
4. Screenshot the specific changed element (`browser_take_screenshot` with a `target` selector,
   not just a full-page shot) and visually confirm the change looks as intended.
5. For anything involving hidden/computed state (opacity, sizing, shape counts, layout width) —
   don't just eyeball a screenshot, inspect the actual rendered DOM/Plotly state with
   `browser_evaluate` (e.g. `el.layout.shapes`, `el._fullLayout.width`). A screenshot can look
   plausible while the underlying bug is still present at a different data range or viewport size.
6. If the change involves an interactive element (modal, button, hover), actually trigger it via
   `browser_click`/`browser_evaluate` and re-screenshot — don't assume static-HTML correctness
   implies interactive correctness.

Skip this only for non-visual changes (data pipeline, analysis logic, docs).

## Always run Python through the project venv

Use `.venv/Scripts/python.exe` (or activate the venv first). **Bare `python` in a shell here
resolves to the system interpreter at `C:\Python314\python.exe`, which does not have this
project's dependencies.**

**Why:** the system interpreter happens to have `pytest`, `fastapi`, `plotly` and `requests`
installed, so tests and most scripts run under it and appear to pass — but it has no
`anthropic`, so anything touching `src/coach/` or `src/api/app.py` fails with
`ModuleNotFoundError`. That reads exactly like a missing dependency and isn't one. On
2026-11-02 this produced a false diagnosis that the API server and coach were broken and
needed `pip install -r requirements.txt`; the venv was complete the whole time.

Verify before concluding anything about a missing package:
`./.venv/Scripts/python.exe -c "import sys; print(sys.executable)"`

## Chart conventions are already fixed

This project's chart design is settled in `src/visualizations/` — colour palette, layout,
and the PMC 4-panel structure. Do not load the `dataviz` skill for routine chart
regeneration or edits that follow existing patterns; follow the modules instead. Load it
only when designing a genuinely new chart type or reconsidering the palette.

## Start with `python scripts/status.py`

It reports where the setup has got to and prints the single next command. Read-only — creates
no files, migrates nothing, calls no API. It is the first thing to run when anything behaves
oddly, before reading code.

## New behaviour is verified by breaking it, not by a green suite

Write the change, then deliberately break it and confirm a test fails. This has repeatedly
caught tests here that passed for the wrong reason:

- a test asserting a constant's own value back at itself
- a filter that worked only because of insert order
- `len(rides) <= ride_days`, which passes trivially whenever the generator is *narrower* than
  the ladder — so the roller could build at a fixed 3 rides and every test stayed green
- a guard test whose fixture was mild enough that the guard it protected could be deleted

Several docstrings in `tests/` name the exact mutant a test exists to catch. Keep that habit:
it is the only thing that tells a later reader why an oddly specific assertion is that way.

## Do not delete the grep/AST guard tests

Each was added after the same defect appeared **twice**, and none of them can be replaced by a
behavioural test because both behaviours look plausible in isolation:

| guard | what it prevents |
|---|---|
| no `date.today()` outside `src/clock.py` | the server's day silently replacing the athlete's |
| one definition of `SPORT_GROUPS` | an unrecognised activity type falling to `"other"` in three places independently |
| no `TIERS[...]` outside the four modules that own it | a reader reporting 3.42h beside a calendar showing 3.5h |
| no `rotation_for(` outside `blocks.py` — callers use `plan_rotation` | a preview or roller building a week that ignores the athlete's available days |

## Read the plan's own ladder, never the module constant

`progression.TIERS` is the built-in ladder and a fallback. The plan's real rungs live in the
`tiers` table and come from `tiers_store.load`. Any reader that subscripts `TIERS` directly
will disagree with what was actually prescribed — this was fixed in the API, then the coaching
report, then found a third time in `plan_build.py`.

## Nothing generates config as a side effect

`config.load(..., create_if_missing=True)` belongs to a deliberate first run and nowhere else.
`clock.today_local()` once created `config/athlete.yaml` merely by being asked the date — and
wrote UTC into it, moving the athlete a day forward.

## Migrations are append-only and never read the clock

Add a numbered step; never edit an applied one. A migration that depends on `date.today()`
produces a different database when replayed, so dates are frozen constants in the migration
itself even where a code constant with the same value exists.

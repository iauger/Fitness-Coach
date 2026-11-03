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

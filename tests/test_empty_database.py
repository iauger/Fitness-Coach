"""
Day one: a migrated database with nothing in it.

Every one of these paths was written against nine years of the author's data, which is the
least representative input they will ever get. Someone cloning this repo has an empty database
until their first sync finishes, and a crash there is the first thing they would see.

These are also the tests that stop the config refactor from regressing: as personal constants
move out into a config file, "works with no data" is the property most likely to break
quietly, because the developer running the suite always has data.
"""

import contextlib
import sqlite3

import pytest

from src.db.migrations import migrate


@pytest.fixture
def empty(tmp_path, monkeypatch):
    """A migrated, empty database, wired into every module that opens one."""
    path = tmp_path / "empty.db"
    migrate(path)

    import src.db.schema as schema

    real = schema.get_connection

    def get_conn(db_path=path, **kw):
        return real(path, **kw)

    @contextlib.contextmanager
    def ctx(db_path=path, **kw):
        conn = real(path, **kw)
        try:
            yield conn
        finally:
            conn.close()

    # Modules bind these at import time, so patching src.db.schema alone reaches nothing.
    import src.analysis.activities, src.analysis.compliance, src.analysis.cycle
    import src.analysis.derive, src.analysis.fitness, src.analysis.forecast
    import src.analysis.load, src.analysis.training_plan, src.analysis.weekly
    import src.analysis.wellness, src.athlete.profile
    import src.visualizations.calendar_view, src.visualizations.dashboard
    import src.visualizations.distribution, src.visualizations.load
    import src.visualizations.pmc, src.visualizations.recovery, src.visualizations.volume

    import sys

    for name, module in list(sys.modules.items()):
        if not name.startswith("src."):
            continue
        if hasattr(module, "get_connection"):
            monkeypatch.setattr(module, "get_connection", get_conn, raising=False)
        if hasattr(module, "read_connection"):
            monkeypatch.setattr(module, "read_connection", ctx, raising=False)
        if hasattr(module, "write_connection"):
            monkeypatch.setattr(module, "write_connection", ctx, raising=False)
    return path


# ── charts ────────────────────────────────────────────────────────────────────

def test_the_calendar_renders_with_no_data(empty):
    from src.visualizations.calendar_view import build_calendar_html

    html = build_calendar_html()
    assert "<html" in html.lower()


def test_the_dashboard_renders_with_no_data(empty):
    from src.visualizations.dashboard import build_dashboard

    assert len(build_dashboard()) > 1000


def test_both_pages_title_themselves_from_the_configuration(empty, tmp_path, monkeypatch):
    """
    The name in the title bar used to be a string literal in two modules. It is now the
    athlete's, and it is **escaped**, because it arrives from a file and lands inside markup —
    an apostrophe alone would break the page for a plausible name.
    """
    import src.config
    from src.visualizations.calendar_view import build_calendar_html
    from src.visualizations.dashboard import build_dashboard

    path = tmp_path / "c.yaml"
    path.write_text(
        'athlete:\n  name: "A & B <script>"\n'
        'plan:\n  name: P\n  start_monday: 2026-11-02\n', encoding="utf-8")
    monkeypatch.setattr(src.config, "CONFIG_PATH", path)
    src.config.reset()

    for html in (build_calendar_html(), build_dashboard()):
        assert "A &amp; B &lt;script&gt;" in html
        # The dashboard has <script> blocks of its own, so the check is that the *name* never
        # reaches the page unescaped — not that the page contains no script tag.
        assert "A & B <script>" not in html


@pytest.mark.parametrize("module_name", ["pmc", "recovery", "volume", "distribution", "load"])
def test_every_individual_chart_builds_with_no_data(empty, module_name):
    """
    These five divide by counts, average over windows and index into series.

    An empty table is where that arithmetic goes wrong — a zero denominator or an index into
    nothing — and none of it is reachable from the author's own database.
    """
    import importlib

    module = importlib.import_module(f"src.visualizations.{module_name}")
    figure = module.build()
    assert figure is not None


# ── analysis ──────────────────────────────────────────────────────────────────

def test_the_coaching_snapshot_builds_with_no_data(empty):
    """The coach's entire view of the athlete. It must degrade, not raise."""
    from src.analysis.report import coaching_context_text

    assert len(coaching_context_text()) > 0


def test_the_forecast_returns_nothing_rather_than_guessing(empty):
    from src.analysis.forecast import project_fitness

    assert project_fitness() == []


def test_weekly_volume_reports_an_empty_week_rather_than_omitting_it(empty):
    """
    The distinction the whole progression gate rests on.

    A week with no activity produces no rows to group by, so anything reading the data rather
    than the calendar silently loses it — and those are exactly the weeks the re-entry rule
    exists to detect.
    """
    from datetime import date

    from src.analysis.activities import week_volume
    import src.db.schema as schema

    conn = schema.get_connection()
    try:
        week = week_volume(conn, date(2026, 11, 2))
    finally:
        conn.close()

    assert week["ride_min"] == 0
    assert week["ride_days"] == 0
    assert week["any_activity"] is False

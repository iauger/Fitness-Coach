"""
The first-run ladder: where a setup has got to, and the one command that moves it forward.

Written after walking a fresh clone through the documented first run and recording where it
dead-ended. Three of the first five commands ended in a raw traceback — `KeyError:
'INTERVALS_API_KEY'`, `OperationalError: no such table: activities` — each a complete
diagnosis for someone who already knows this project and useless to anyone else.

The tests here are mostly about *order* and *not stopping on the wrong thing*. A status
command that reports six failures to someone whose only real problem is a missing API key has
buried the one thing they can act on under five of its consequences.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import src.config as C
from src import firstrun
from src.db.migrations import migrate

EXAMPLE_NAME = "Your Name"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone-shaped directory: an example config and an example .env, nothing filled in."""
    (tmp_path / "config").mkdir()
    (tmp_path / ".env.example").write_text("INTERVALS_API_KEY=\n", encoding="utf-8")
    example = tmp_path / "config" / "athlete.example.yaml"
    example.write_text(
        f"athlete:\n  name: {EXAMPLE_NAME}\nplan:\n  name: P\n  start_monday: 2026-11-02\n",
        encoding="utf-8")

    monkeypatch.setattr(firstrun, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(C, "EXAMPLE_PATH", example)
    monkeypatch.setattr(C, "CONFIG_PATH", tmp_path / "config" / "athlete.yaml")
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    C.reset()
    yield tmp_path
    C.reset()


def named(found: list) -> dict:
    return {c.name: c for c in found}


def first_blocked(found: list):
    return next((c for c in found if not c.ok and c.blocking), None)


def write_env(repo, key="abc123"):
    (repo / ".env").write_text(f"INTERVALS_API_KEY={key}\n", encoding="utf-8")


def write_config(repo, name="Real Person"):
    C.CONFIG_PATH.write_text(
        f"athlete:\n  name: {name}\nplan:\n  name: P\n  start_monday: 2026-11-02\n",
        encoding="utf-8")
    C.reset()


def seed_db(path: Path, rides: int = 60, ftp: float | None = None) -> None:
    migrate(path)
    conn = sqlite3.connect(path)
    start = date(2026, 11, 5) - timedelta(weeks=20)
    for i in range(rides):
        day = start + timedelta(days=i * 2)
        conn.execute(
            "INSERT INTO activities (id, date, name, type, moving_time) VALUES (?,?,?,?,?)",
            (f"a{i}", day.isoformat(), "ride", "Ride", 3600))
    if ftp:
        conn.execute(
            "INSERT INTO athlete_profile (metric, value, effective_date, created_at) "
            "VALUES ('ftp', ?, '2026-01-01', '2026-01-01')", (ftp,))
    conn.commit()
    conn.close()


# ── the order things are reported in ─────────────────────────────────────────

def test_a_bare_clone_asks_for_the_env_file_first(repo):
    found = firstrun.checks(repo / "none.db")
    assert first_blocked(found).name == "secrets"
    assert ".env" in first_blocked(found).fix


def test_a_missing_key_is_not_buried_under_its_own_consequences(repo):
    """
    Without the key there is no sync, so no history, no ladder and no plan. Reporting all four
    would hide the only one the person can do anything about.
    """
    found = firstrun.checks(repo / "none.db")
    assert {c.name for c in found} == {"secrets", "config"}
    assert "history" not in named(found)


def test_an_empty_key_in_the_file_is_distinguished_from_a_missing_file(repo):
    write_env(repo, key="")
    detail = named(firstrun.checks(repo / "none.db"))["secrets"].detail
    assert "empty" in detail


def test_an_exported_key_counts_even_with_no_env_file(repo, monkeypatch):
    """Someone running under a secrets manager has no .env at all."""
    monkeypatch.setenv("INTERVALS_API_KEY", "from-the-environment")
    assert named(firstrun.checks(repo / "none.db"))["secrets"].ok


def test_the_unedited_example_config_is_caught(repo):
    """
    Generated on first run from the example, so it exists and parses — and describes someone
    else. The plan it produces would be valid and wrong.
    """
    write_env(repo)
    write_config(repo, name=EXAMPLE_NAME)
    blocked = first_blocked(firstrun.checks(repo / "none.db"))
    assert blocked.name == "config"
    assert "example" in blocked.detail


def test_a_config_that_does_not_parse_reports_the_reason(repo):
    write_env(repo)
    C.CONFIG_PATH.write_text("athlete:\n  name: X\n  nonsense: 1\nplan:\n  name: P\n"
                             "  start_monday: 2026-11-02\n", encoding="utf-8")
    C.reset()
    blocked = first_blocked(firstrun.checks(repo / "none.db"))
    assert blocked.name == "config"
    assert "nonsense" in blocked.detail


def test_a_missing_database_asks_for_the_migration(repo):
    write_env(repo)
    write_config(repo)
    blocked = first_blocked(firstrun.checks(repo / "none.db"))
    assert blocked.name == "database"
    assert "migrate" in blocked.fix


def test_an_empty_database_asks_for_a_sync(repo):
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    migrate(path)
    blocked = first_blocked(firstrun.checks(path))
    assert blocked.name == "history"
    assert "fetch_history" in blocked.fix


def test_synced_history_with_no_ladder_asks_for_the_bootstrap(repo):
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    seed_db(path, ftp=250)
    blocked = first_blocked(firstrun.checks(path))
    assert blocked.name == "tier ladder"
    assert "bootstrap_plan" in blocked.fix


# ── the one warning that is not a stop ───────────────────────────────────────

def test_a_missing_ftp_warns_without_blocking(repo):
    """
    Every percentage in every prescription resolves against FTP, so without one the sessions
    are shapes with no watts. The plan still generates, which is why this reports and does not
    stop — but it is the warning most worth reading, so the ladder must not halt on it and
    hide what comes after.
    """
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    seed_db(path, ftp=None)
    found = firstrun.checks(path)
    ftp = named(found)["ftp"]
    assert not ftp.ok and not ftp.blocking
    assert "tier ladder" in named(found), "the ladder halted on a non-blocking warning"


def test_ready_ignores_warnings_but_not_blockers(repo):
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    seed_db(path, ftp=None)
    assert not firstrun.ready(path), "no ladder yet, so not ready"


# ── the report someone actually reads ────────────────────────────────────────

def test_the_report_ends_with_exactly_one_next_command(repo):
    write_env(repo)
    write_config(repo)
    text = firstrun.report(repo / "none.db")
    assert text.count("next:") == 1
    assert "migrate.py --apply" in text


def test_the_report_names_the_warning_when_nothing_is_blocking(repo):
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    seed_db(path, ftp=None)

    from src.planning import plans, tiers_store
    from src.planning.bootstrap import derive
    from src.planning.constants import plan_name

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    plan = plans.ensure(conn, plans.slug_for(plan_name()), plan_name(),
                        started_on="2026-11-02")
    weeks = [{"week_start": "2026-11-02", "ride_min": 180.0, "ride_days": 3,
              "longest_ride_min": 60.0, "strength_sessions": 2, "training_days": 3,
              "any_activity": True} for _ in range(16)]
    tiers_store.save(conn, plan.id, derive(weeks, (3.5, 4.0), 120, 7))
    conn.execute(
        "INSERT INTO prescribed_sessions (plan_name, plan_id, date, weekday, block_index, "
        "block_name, week_in_block, is_recovery_week, role, archetype, variant, prescription, "
        "params_json, created_at) VALUES (?, ?, '2026-11-03', 1, 0, 'B', 1, 0, 'lit', 'lit', "
        "'steady', 'x', '{}', '2026-11-02')", (plan_name(), plan.id))
    conn.commit()
    conn.close()

    text = firstrun.report(path)
    assert "next:" not in text
    assert "seed_profile" in text


def test_the_report_is_read_only(repo):
    """
    It creates no files, migrates no schema and calls no API. A status command that changed
    things is the opposite of what someone reaches for when they are lost — and `config.load`
    generating `athlete.yaml` as a side effect is a trap this project has already fallen into
    once, in `clock.today_local()`.
    """
    write_env(repo)
    before = sorted(p.name for p in repo.rglob("*"))
    firstrun.report(repo / "none.db")
    assert sorted(p.name for p in repo.rglob("*")) == before
    assert not C.CONFIG_PATH.exists()


def test_serving_an_unfinished_setup_says_what_is_outstanding(repo, monkeypatch, capsys):
    """
    Serving before setup is finished is allowed — the dashboard and calendar render with no
    data, and seeing them is part of deciding whether to bother. Doing it *silently* means the
    first thing someone meets is a set of empty charts with nothing to say what is missing.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("serve_mod", "scripts/serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)

    write_env(repo)
    write_config(repo)
    monkeypatch.setattr(serve, "migrate_db", lambda *a, **k: None)
    monkeypatch.setattr(serve, "ready", lambda: False)
    monkeypatch.setattr(serve, "report", lambda: "  ->  history   nothing synced yet")
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_app", lambda: object())
    monkeypatch.setattr("sys.argv", ["serve.py"])

    serve.main()
    out = capsys.readouterr().out
    assert "nothing synced yet" in out
    assert out.index("nothing synced yet") < out.index("http://"), (
        "the outstanding thing should be named before the URL, not after it")


def test_serving_a_finished_setup_says_nothing_extra(repo, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("serve_mod2", "scripts/serve.py")
    serve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(serve)

    monkeypatch.setattr(serve, "migrate_db", lambda *a, **k: None)
    monkeypatch.setattr(serve, "ready", lambda: True)
    monkeypatch.setattr(serve, "report", lambda: "SHOULD NOT APPEAR")
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: None)
    monkeypatch.setattr(serve, "_app", lambda: object())
    monkeypatch.setattr("sys.argv", ["serve.py"])

    serve.main()
    assert "SHOULD NOT APPEAR" not in capsys.readouterr().out


def test_the_ladder_stops_at_the_first_blocker_inside_the_database_too(repo):
    """
    The early return before the database covers `secrets` and `config`; this covers the loop
    after it. With nothing synced there is no ladder and no plan *because* of that, and
    listing all three buries the only one that can be acted on.
    """
    write_env(repo)
    write_config(repo)
    path = repo / "f.db"
    migrate(path)
    names = [c.name for c in firstrun.checks(path)]
    assert names == ["secrets", "config", "database", "history"], names


def test_a_half_migrated_database_asks_for_the_migration(repo):
    """
    Not the same as a missing one. Someone who cloned before a migration existed has a
    database that opens fine and is missing the table the next step reads.
    """
    write_env(repo)
    write_config(repo)
    path = repo / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE activities (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    blocked = first_blocked(firstrun.checks(path))
    assert blocked.name == "database"
    assert "behind" in blocked.detail
    assert "migrate" in blocked.fix


def test_a_bare_clone_still_prints_only_one_next_command(repo):
    """
    `.env` and `config` are independent, so both are reported — but only one command can be
    the next one, or the person has to decide which to do first about something that has no
    order.
    """
    text = firstrun.report(repo / "none.db")
    assert text.count("next:") == 1


def test_bootstrap_migrates_before_it_reads(monkeypatch, tmp_path):
    """
    `bootstrap_plan.py` opened the database to read weekly volume without ensuring a schema,
    so on a fresh clone the second command in the README died with
    `OperationalError: no such table: activities`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("bp", "scripts/bootstrap_plan.py")
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)

    order = []
    monkeypatch.setattr(bp, "migrate_db", lambda *a, **k: order.append("migrate"))

    def read(*a, **k):
        order.append("read")
        raise RuntimeError("stop here")

    monkeypatch.setattr(bp, "read_connection", read)
    monkeypatch.setattr("sys.argv", ["bootstrap_plan.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        bp.main()
    assert order == ["migrate", "read"], order

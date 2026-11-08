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
from conftest import seed_ladder

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
    # The coach check reads this too, and `src.coach.session` loads the real .env into the
    # process the moment any test imports it, so without this the result depended on test order.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Neither where the computer is nor what day it is may decide a result: the example says UTC
    # and a start date a few days before this fixed "today", and the checks that read both are
    # tested on their own below.
    monkeypatch.setattr(firstrun, "_machine_utc_offset_minutes", lambda: 0)
    monkeypatch.setattr(firstrun, "_today", lambda: date(2026, 11, 4))
    C.reset()
    yield tmp_path
    C.reset()


def named(found: list) -> dict:
    return {c.name: c for c in found}


def first_blocked(found: list):
    return next((c for c in found if not c.ok and c.blocking), None)


def write_env(repo, key="abc123", anthropic="sk-test"):
    lines = f"INTERVALS_API_KEY={key}\n" + (f"ANTHROPIC_API_KEY={anthropic}\n" if anthropic else "")
    (repo / ".env").write_text(lines, encoding="utf-8")


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


# ── the README is part of the first run ──────────────────────────────────────

def test_every_command_the_readme_tells_you_to_run_exists_and_parses(tmp_path):
    """
    The documented first run *is* the feature here, so a stale command in it is a bug in the
    same way a stale code path is. This fails on a renamed script, a removed flag, or a
    typo — none of which any other test can see, and all of which land on whoever is following
    the instructions for the first time.

    `--help` rather than execution: it proves the entry point imports, argparse builds, and
    every flag the README names is real, without touching a database or the network.

    Run from a throwaway copy of the project, which is what makes "without touching a database"
    checkable: `serve.py --help` once generated config/athlete.yaml and `seed_profile.py --help`
    once created data/fitness.db, on a fresh clone, and both went unnoticed because the real
    checkout already had both files.
    """
    import re
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    work = tmp_path / "clone"
    shutil.copytree(repo, work, ignore=shutil.ignore_patterns(
        ".git", ".venv", "data", "__pycache__", "tests", ".playwright-mcp", "athlete.yaml*"))

    readme = (work / "README.md").read_text(encoding="utf-8")
    commands = sorted(set(re.findall(r"`(python scripts/[^`]+)`", readme)))
    assert len(commands) >= 6, f"only found {len(commands)} commands; has the README moved?"

    problems = []
    for command in commands:
        parts = command.split()
        script = work / parts[1]
        if not script.exists():
            problems.append(f"{command}: {script} does not exist")
            continue
        result = subprocess.run([sys.executable, str(script), "--help"],
                                capture_output=True, text=True, cwd=work)
        if result.returncode != 0:
            problems.append(f"{command}: --help exited {result.returncode}")
            continue
        for flag in (p for p in parts[2:] if p.startswith("--")):
            if flag not in result.stdout:
                problems.append(f"{command}: {script.name} has no {flag}")
    assert not problems, "\n  ".join([""] + problems)
    assert not (work / "data").exists(), "a --help created a database"
    assert not (work / "config" / "athlete.yaml").exists(), "a --help created a config"


def test_the_readme_opens_with_the_status_command():
    """
    The whole first run is 'run status, do what it says, repeat'. If that stops being the
    first thing someone is told to run, the rest of this step stops working.
    """
    from pathlib import Path

    readme = Path("README.md").read_text(encoding="utf-8")
    running = readme[readme.index("## Running it"):]
    assert "scripts/status.py" in running[:800], "status.py is not near the top of Running it"


# ── the next command has to be runnable, not just readable ───────────────────

def test_the_next_command_names_an_interpreter_that_exists(repo):
    """
    Every `fix` string used to begin with the literal word `python`, which is the one thing
    that does not work on the machine this was written on: a bare `python` there is a system
    interpreter with none of the dependencies, and under an unactivated venv it is nothing at
    all. During the first real walkthrough two of these lines were pasted and failed — once
    silently, because Windows handed the `.py` file to its file association instead.
    """
    found = firstrun.checks(repo / "none.db")
    fixes = [c.fix for c in found if c.fix]
    assert fixes
    for fix in fixes:
        for line in fix.splitlines():
            assert not line.strip().startswith("python "), f"not runnable here: {line}"


def test_the_in_repo_venv_is_preferred_to_whatever_is_running(repo, monkeypatch):
    """
    Short, no spaces to quote, identical across PowerShell, cmd and bash — and right even when
    status.py itself was launched with the wrong interpreter, which is exactly when someone is
    reading this output.
    """
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        venv = repo / ".venv"
        for leaf in (repo / ".venv" / "Scripts", repo / ".venv" / "bin"):
            leaf.mkdir(parents=True, exist_ok=True)
        (venv.joinpath(*parts)).write_text("", encoding="utf-8")
    assert firstrun.interpreter() == ".venv/Scripts/python.exe"


def test_a_path_with_spaces_is_made_runnable_rather_than_pasted_bare(repo, monkeypatch):
    r"""`C:\Program Files\py\python.exe scripts/x.py` is four arguments, not a command."""
    monkeypatch.setattr(firstrun.sys, "executable", r"C:\Program Files\py\python.exe")
    monkeypatch.setattr(firstrun.os, "name", "nt")
    out = firstrun.interpreter()
    assert out.startswith("& \""), out
    assert out.endswith("\"")


# ── the calendar push is reported, in both directions ────────────────────────

def _ready_repo(repo, push: str) -> Path:
    """A fully set-up clone whose only open question is whether it pushes."""
    write_env(repo)
    C.CONFIG_PATH.write_text(
        "athlete:\n  name: Real Person\n"
        "plan:\n  name: P\n  start_monday: 2026-11-02\n"
        f"integrations:\n  intervals_icu:\n    push_calendar: {push}\n", encoding="utf-8")
    C.reset()
    db = repo / "f.db"
    seed_db(db, ftp=250)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    with conn:
        seed_ladder(conn, "p", "P")
    conn.execute(
        "INSERT INTO prescribed_sessions (plan_name, plan_id, date, weekday, block_index, "
        "block_name, week_in_block, is_recovery_week, role, archetype, variant, "
        "prescription, minutes, tss, intensity_factor, params_json, created_at) "
        "VALUES ('P', 1, '2026-11-09', 0, 0, 'b', 1, 0, 'long', 'lit', 'steady', "
        "'90min', 90, 58, 0.62, '{}', '2026-11-02')")
    conn.commit()
    conn.close()
    return db


def test_a_plan_that_never_reaches_the_calendar_says_so(repo):
    """
    A plan that generates perfectly and silently never appears on a watch is, from the
    saddle, indistinguishable from one that is broken. Off is the default, so this is the
    state most people are in, and it has to be visible rather than inferred.
    """
    db = _ready_repo(repo, "false")
    calendar = named(firstrun.checks(db))["calendar"]
    assert not calendar.ok
    assert not calendar.blocking, "a local-only setup is a legitimate setup, not a failure"
    assert "push_calendar" in calendar.fix


def test_a_configured_push_is_reported_as_working(repo):
    db = _ready_repo(repo, "true")
    assert named(firstrun.checks(db))["calendar"].ok


def test_every_warning_is_shown_not_only_the_first(repo):
    """
    Mutant this catches: `warnings[0]` in `report`. A missing FTP and a calendar left off are
    unrelated, so showing one hides the other until the first is dealt with — and then the
    second appears as if it had just happened.
    """
    write_env(repo)
    C.CONFIG_PATH.write_text(
        "athlete:\n  name: Real Person\n"
        "plan:\n  name: P\n  start_monday: 2026-11-02\n"
        "integrations:\n  intervals_icu:\n    push_calendar: false\n", encoding="utf-8")
    C.reset()
    db = _ready_repo(repo, "false")
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM athlete_profile WHERE metric = 'ftp'")
    conn.commit()
    conn.close()

    found = firstrun.checks(db)
    warnings = [c for c in found if not c.ok and not c.blocking]
    assert {c.name for c in warnings} == {"ftp", "calendar"}

    text = firstrun.report(db)
    assert "2 things worth doing" in text
    assert "seed_profile" in text and "push_calendar" in text


# ── the week has to fit the days, even after the config changes ──────────────

def _config_with(extra: str) -> None:
    C.CONFIG_PATH.write_text(
        "athlete:\n  name: Real Person\n"
        f"plan:\n  name: P\n  start_monday: 2026-11-02\n{extra}", encoding="utf-8")
    C.reset()


def test_a_config_edit_that_leaves_the_ladder_without_enough_days_blocks_and_says_so(repo):
    """
    The ladder was derived for three rides a week; the config is then edited down to two
    available days. Neither is wrong alone, but the next roll cannot place the week — and the
    roll is unattended and hourly, so without this the first sign is a calendar that quietly
    stopped updating. Mutant this catches: `_week` dropped from the steps.
    """
    db = _ready_repo(repo, "false")
    _config_with("  strength_days: 0\n  available_days: [sat, sun]\n")

    week = named(firstrun.checks(db))["week"]
    assert not week.ok and week.blocking
    assert "3 ride days but only 2 are available" in week.detail
    assert "plan.available_days" in week.fix and "--replace" in week.fix
    assert first_blocked(firstrun.checks(db)).name == "week"


def test_a_week_that_fits_the_days_is_reported_as_fitting(repo):
    db = _ready_repo(repo, "false")
    _config_with("  available_days: [mon, wed, fri, sun]\n")
    assert named(firstrun.checks(db))["week"].ok


def test_nothing_is_said_about_the_week_when_nothing_is_configured(repo):
    """An `ok` line that every unconfigured user sees is noise in the one output people read to
    find what is wrong."""
    db = _ready_repo(repo, "false")
    assert "week" not in named(firstrun.checks(db))


def test_the_limits_note_reaches_the_status_report(repo):
    """
    `_limits` was tested directly and never through `checks()`, so deleting it from the steps
    went unnoticed — and the steps are what the athlete actually sees. Mutant this catches: it
    being dropped from the list.
    """
    db = _ready_repo(repo, "false")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE tiers SET lit_min = 45, long_min = 45")
    conn.commit()
    conn.close()
    _config_with("  max_ride_min: 50\n")

    found = named(firstrun.checks(db))
    assert found["week"].ok
    limits = found["limits"]
    assert not limits.ok and not limits.blocking
    assert "Threshold" in limits.detail
    assert "Threshold" in firstrun.report(db)


def test_a_missing_coach_key_is_a_warning_not_a_stop(repo):
    """The coach is optional: everything else works without a key, so it must never block."""
    db = _ready_repo(repo, "false")
    write_env(repo, anthropic=None)         # after the helper, which writes a full .env
    coach = named(firstrun.checks(db))["coach"]
    assert coach.ok is False and coach.blocking is False
    assert firstrun.ready(db)


# -- an example left as shipped ---------------------------------------------------------------

def test_a_timezone_left_at_utc_on_a_computer_that_is_not_is_refused(repo, monkeypatch):
    """
    Mutant: the check deleted. A US athlete on the example's UTC has Sunday end at about 7pm, and
    a week is graded once and for good, so a later ride is missed by a decision nothing revisits.
    """
    write_env(repo)
    write_config(repo)
    monkeypatch.setattr(firstrun, "_machine_utc_offset_minutes", lambda: -300)
    blocked = first_blocked(firstrun.checks(repo / "none.db"))
    assert blocked.name == "config" and "timezone is still UTC" in blocked.detail
    assert "America/Chicago" in blocked.fix


def test_utc_is_fine_on_a_computer_that_is_in_utc(repo):
    write_env(repo)
    write_config(repo)
    assert named(firstrun.checks(repo / "none.db"))["config"].ok


def test_a_real_timezone_is_never_questioned(repo, monkeypatch):
    write_env(repo)
    C.CONFIG_PATH.write_text("athlete:\n  name: Real Person\n  timezone: America/Chicago\n"
                             "plan:\n  name: P\n  start_monday: 2026-11-02\n", encoding="utf-8")
    C.reset()
    monkeypatch.setattr(firstrun, "_machine_utc_offset_minutes", lambda: -300)
    assert named(firstrun.checks(repo / "none.db"))["config"].ok


def _db_with_history(repo):
    path = repo / "t.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO activities (id, date, type, moving_time) VALUES ('a', '2026-10-30', 'Ride', 3600)")
    conn.commit()
    conn.close()
    return path


def test_a_plan_start_long_ago_is_called_out_until_a_week_has_been_decided(repo, monkeypatch):
    """
    Mutant: the 14-day grace, or the "only before the first decision" rule, removed. The gate
    grades every week since the start at once, so an example date left in place on a later day
    begins the record with a heap of weeks nobody followed.
    """
    write_env(repo)
    write_config(repo)
    path = _db_with_history(repo)
    monkeypatch.setattr(firstrun, "_today", lambda: date(2026, 11, 16))          # 14 days: fine
    assert "plan start" not in named(firstrun.checks(path))
    monkeypatch.setattr(firstrun, "_today", lambda: date(2026, 11, 17))          # 15 days: said
    found = named(firstrun.checks(path))["plan start"]
    assert not found.ok and not found.blocking and "15 days ago" in found.detail
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO plan_progression (plan_name, week_start, ride_minutes, ride_days, "
                 "strength_logged, outcome, tier_applied, verdict, tier_next, week_type_next, rotation, "
                 "block_index, week_in_block, reason, created_at) "
                 "VALUES ('P', '2026-11-02', 200, 3, 0, 'met', 2, 'advance', 3, 'normal', 0, 0, 1, 'x', 'x')")
    conn.commit()
    conn.close()
    assert "plan start" not in named(firstrun.checks(path))

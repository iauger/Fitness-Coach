"""
Where a setup has got to, and what to do next.

Built after walking a fresh clone through the documented first run and writing down where it
dead-ended. Three of the first five commands ended in a raw traceback — `KeyError:
'INTERVALS_API_KEY'`, `OperationalError: no such table: activities` — which tell someone who
already knows this project exactly what is wrong and tell a newcomer nothing at all.

So the states a setup can be in are enumerated here, in the order they have to be satisfied,
and each one knows the command that moves it forward. The checks are ordered because the
dependencies are real: there is no point reporting a missing tier ladder to someone who has
not synced any riding to derive it from.

**Read-only and side-effect free.** Nothing here creates a file, migrates a schema or calls an
API — a status command that changed things would be the opposite of what someone reaches for
when they are lost. In particular it does not generate `config/athlete.yaml`, which is exactly
the trap `clock.today_local()` fell into.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


def interpreter() -> str:
    """
    How to invoke Python, as text someone can paste into a shell.

    The literal word `python` is wrong here more often than it is right. On this project's own
    machine it resolves to a system interpreter carrying none of the dependencies, and with a
    venv that has not been activated it resolves to nothing useful at all. A printed command
    that does not run is worse than no command, because the person following it cannot tell a
    stale instruction from their own mistake — this cost two attempts during the first real
    walkthrough, on the two lines that are hardest to recover from being wrong about.

    The in-repo venv is preferred over `sys.executable` deliberately. It is short, it contains
    no spaces to quote, it is identical in PowerShell, cmd and bash, and it is still the right
    answer when `status.py` itself was launched with the wrong interpreter — which is precisely
    the situation where this output is being read.
    """
    for venv in (REPO_ROOT / ".venv" / "Scripts" / "python.exe",
                 REPO_ROOT / ".venv" / "bin" / "python"):
        if venv.exists():
            return venv.relative_to(REPO_ROOT).as_posix()

    current = str(Path(sys.executable))
    if " " not in current:
        return current
    # Quoting alone is not enough in PowerShell, which needs the call operator to run a
    # quoted path at all.
    return f'& "{current}"' if os.name == "nt" else f'"{current}"'


@dataclass(frozen=True)
class Check:
    """One prerequisite: whether it is satisfied, and the command that satisfies it."""
    name: str
    ok: bool
    detail: str
    fix: str = ""
    blocking: bool = True       # False for things worth saying but not worth stopping for


def _env_file() -> Check:
    """
    `.env` is read rather than loaded.

    `dotenv_values` parses without touching `os.environ`, which keeps this module read-only.
    `load_dotenv` would work and is what every entry point calls, but a status command that
    quietly alters the process environment is the kind of side effect that makes a diagnosis
    depend on what you ran first.

    Both sources count: an exported variable is as good as a file, and someone running under
    a secrets manager has no `.env` at all.
    """
    from dotenv import dotenv_values

    path = REPO_ROOT / ".env"
    from_file = dotenv_values(path) if path.exists() else {}
    key = os.environ.get("INTERVALS_API_KEY") or (from_file.get("INTERVALS_API_KEY") or "")

    if key.strip():
        return Check("secrets", True, "intervals.icu key present")
    if not path.exists():
        return Check("secrets", False, "no .env file",
                     "cp .env.example .env    # then add your intervals.icu API key")
    return Check("secrets", False, "INTERVALS_API_KEY is empty in .env",
                 "intervals.icu > Settings > Developer, then paste it into .env")


def _config_file() -> Check:
    from src.config import CONFIG_PATH, ConfigError, EXAMPLE_PATH, load

    if not CONFIG_PATH.exists():
        return Check("config", False, f"no {CONFIG_PATH.name}",
                     f"cp {EXAMPLE_PATH.relative_to(REPO_ROOT)} "
                     f"{CONFIG_PATH.relative_to(REPO_ROOT)}    # then edit it")
    try:
        # `create_if_missing=False` is belt-and-braces: the existence check above already
        # means this is never reached with the file absent, so a mutation test will not catch
        # it being flipped. It stays because the flag is right independently of the guard, and
        # `clock.today_local()` generating a config as a side effect of asking the time is a
        # trap this project has already fallen into once.
        cfg = load(CONFIG_PATH, create_if_missing=False)
    except ConfigError as exc:
        return Check("config", False, str(exc).splitlines()[0][:160],
                     f"Fix {CONFIG_PATH.name} — the message above names the setting")
    if cfg.athlete.name.strip().lower() in ("", "your name"):
        return Check("config", False, "still the unedited example (athlete.name)",
                     f"Edit {CONFIG_PATH.name}: your name, timezone, goals and plan start")
    return Check("config", True, f"{cfg.athlete.name}, {cfg.athlete.timezone}")


def _database(db_path: Path) -> Check:
    if not db_path.exists():
        return Check("database", False, "no database yet",
                     f"{interpreter()} scripts/migrate.py --apply")
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    missing = {"activities", "prescribed_sessions", "tiers"} - tables
    if missing:
        return Check("database", False, f"schema is behind ({', '.join(sorted(missing))})",
                     f"{interpreter()} scripts/migrate.py --apply")
    return Check("database", True, f"{len(tables)} tables")


def _history(conn) -> Check:
    rides = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    if not rides:
        return Check("history", False, "nothing synced yet",
                     f"{interpreter()} scripts/fetch_history.py")
    span = conn.execute("SELECT MIN(date), MAX(date) FROM activities").fetchone()
    return Check("history", True, f"{rides} activities, {span[0][:10]} to {span[1][:10]}")


def _ftp(conn) -> Check:
    """
    Not blocking, but the single most consequential missing number.

    Every percentage in every prescription resolves against it, so without one the sessions
    are shapes with no watts. The plan still generates, which is why this is a warning rather
    than a stop — but it is the warning most worth reading.
    """
    row = conn.execute(
        "SELECT value FROM athlete_profile WHERE metric = 'ftp' "
        "ORDER BY effective_date DESC LIMIT 1").fetchone()
    if row is None:
        return Check("ftp", False, "no FTP on record — prescriptions will have no watts",
                     f"{interpreter()} scripts/seed_profile.py --ftp 250    # your number",
                     blocking=False)
    return Check("ftp", True, f"{row[0]:.0f}W")


def _ladder(conn) -> Check:
    from src.planning import plans, tiers_store
    from src.planning.constants import plan_name

    try:
        plan = plans.by_name(conn, plan_name())
    except Exception:
        plan = None
    if plan is None or tiers_store.load(conn, plan.id) is None:
        return Check("tier ladder", False, "not derived yet",
                     f"{interpreter()} scripts/bootstrap_plan.py            # look at it\n"
                     f"    {interpreter()} scripts/bootstrap_plan.py --write    # keep it")
    version = tiers_store.current_version(conn, plan.id)
    ladder = tiers_store.load(conn, plan.id)
    return Check("tier ladder", True,
                 f"version {version}, T0 {ladder[min(ladder)].target_hours}h "
                 f"to T{max(ladder)} {ladder[max(ladder)].target_hours}h")


def _week(conn) -> Check | None:
    """
    Whether the stored ladder's week can be placed inside the days the configuration allows.

    Checked separately from the ladder because the two can disagree *after* a successful
    bootstrap: the ladder says three rides a week, and someone later edits `available_days`
    down to two. Nothing about the ladder is wrong and nothing about the config is invalid on
    its own, but the next roll cannot build a week — and the roll runs unattended, hourly, so
    the first anyone hears of it would be a calendar that stopped updating.
    """
    from src.planning import plans, tiers_store
    from src.planning.blocks import plan_rotation
    from src.planning.constants import available_days, long_ride_day, plan_name

    # Nothing to report when nothing is configured: with every day available and no pinned
    # long ride the week always builds, and a line that is `ok` for everyone who has not
    # touched these settings is noise in the one output people read to find what is wrong.
    if available_days() is None and long_ride_day() is None:
        return None

    plan = plans.by_name(conn, plan_name())
    ladder = tiers_store.load(conn, plan.id)
    try:
        plan_rotation(max(t.rides for t in ladder.values()))
    except ValueError as exc:
        return Check("week", False, str(exc)[:160],
                     "adjust plan.available_days, plan.long_ride_day or plan.strength_days in "
                     "config/athlete.yaml, or re-derive the ladder for the days you have:\n"
                     f"    {interpreter()} scripts/bootstrap_plan.py --write --replace")
    return Check("week", True, "can be placed inside the available days")


def _plan(conn) -> Check:
    from src.planning.constants import plan_name

    rows = conn.execute(
        "SELECT COUNT(*) FROM prescribed_sessions WHERE plan_name = ?",
        (plan_name(),)).fetchone()[0]
    if not rows:
        return Check("plan", False, "no sessions generated yet",
                     f"{interpreter()} scripts/sync_now.py")
    return Check("plan", True, f"{rows} sessions prescribed")


def _calendar(conn) -> Check:
    """
    Whether the horizon actually reaches intervals.icu, and so the athlete's devices.

    Non-blocking in both directions, because both answers are legitimate: the push is off by
    default on purpose, and plenty of people will want to read the plan locally and never
    turn it on. What is not legitimate is not knowing which — a plan that generates perfectly
    and silently never appears on a watch is indistinguishable, from the saddle, from one
    that is broken.

    Reported after `_plan` rather than beside the other settings because it is only
    meaningful once there are sessions that could be pushed.
    """
    from src.planning.constants import push_calendar

    if not push_calendar():
        return Check("calendar", False, "push off — sessions stay on this machine",
                     "set integrations.intervals_icu.push_calendar: true in "
                     "config/athlete.yaml    # then sync_now.py",
                     blocking=False)
    return Check("calendar", True, "pushing the horizon to intervals.icu")


def checks(db_path: Path | None = None) -> list[Check]:
    """
    Every prerequisite, in dependency order, stopping at the first unsatisfied one.

    Stopping matters. Reporting six failures to someone whose only real problem is a missing
    API key buries the one thing they can act on, and five of the six are consequences of it.
    """
    from src.db.schema import DB_PATH

    db_path = db_path or DB_PATH
    out = [_env_file(), _config_file()]
    if not out[-1].ok or not out[0].ok:
        return out

    database = _database(db_path)
    out.append(database)
    if not database.ok:
        return out

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for step in (_history, _ftp, _ladder, _week, _plan, _calendar):
            check = step(conn)
            if check is None:
                continue
            out.append(check)
            if not check.ok and check.blocking:
                return out
    finally:
        conn.close()
    return out


def ready(db_path: Path | None = None) -> bool:
    return all(c.ok for c in checks(db_path) if c.blocking)


def report(db_path: Path | None = None) -> str:
    """The checks as a block of text, ending with the one command to run next."""
    found = checks(db_path)
    width = max(len(c.name) for c in found)
    lines = []
    for c in found:
        mark = "ok  " if c.ok else ("--  " if not c.blocking else "->  ")
        lines.append(f"  {mark}{c.name.ljust(width)}   {c.detail}")

    blocked = [c for c in found if not c.ok and c.blocking]
    warnings = [c for c in found if not c.ok and not c.blocking]
    if blocked:
        lines += ["", f"  next: {blocked[0].fix}"]
    elif warnings:
        # All of them, not just the first. Warnings are independent — an FTP that was never
        # recorded and a calendar push left off are unrelated, and showing one hides the
        # other for as long as it takes to fix the first. Blocking checks are different and
        # stay one at a time: those really are consequences of each other.
        head = ("  ready, with one thing worth doing:" if len(warnings) == 1
                else f"  ready, with {len(warnings)} things worth doing:")
        lines += ["", head] + [f"    {w.fix}" for w in warnings]
    else:
        lines += ["", f"  ready. {interpreter()} scripts/serve.py --open"]
    return "\n".join(lines)

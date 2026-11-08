"""
A bundle someone can send when something is wrong: one zip, nothing in it that is theirs to keep.

It holds what a person helping would ask for first: where the setup has got to (`status.py`), what
the application log says, what versions are installed, and the *shape* of the database, which is
table names, row counts and sync outcomes. It does not hold a single activity, note, conversation,
goal or coach reply, and the free-text settings (name, notes, equipment) appear only as a length.

Everything text goes through `applog.scrub` with the athlete's own name added, so a name that ended
up in an exception message is removed too. Nothing is sent anywhere: this writes a file under
`data/bundles/` and says where, and sending it is a decision the person makes after looking inside.
"""

import io
import platform
import re
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from src import applog
from src.db.schema import DB_PATH

REPO_ROOT = Path(__file__).parent.parent
TEXT_SETTINGS = frozenset({"athlete.name", "athlete.notes", "athlete.equipment"})


def _own_words() -> tuple[str, ...]:
    """The athlete's name and its parts: the strings that must not survive in any file."""
    try:
        from src.config import CONFIG_PATH, load
        name = load(CONFIG_PATH, create_if_missing=False).athlete.name.strip()
    except Exception:     # not logged: a config that will not load is itself reported in status.txt
        return ()
    parts = [w for w in re.split(r"\W+", name) if len(w) >= 5]
    return tuple(dict.fromkeys([name, *parts])) if name else ()


def _versions() -> str:
    names = []
    for raw in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"([A-Za-z0-9_.\-]+)", raw.strip()) if raw.strip() and not raw.startswith("#") else None
        if m:
            names.append(m.group(1))
    lines = [f"python {sys.version.split()[0]}", f"platform {platform.platform()}"]
    for name in names:
        try:
            lines.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            lines.append(f"{name} NOT INSTALLED")
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if commit:
            lines.append(f"commit {commit}")
    except Exception:     # not logged: git is optional, and a ZIP download has none
        pass
    return "\n".join(lines)


def _settings() -> str:
    try:
        from src.api import setup
        values = setup.current()
    except Exception as exc:
        applog.warning("support.settings_unreadable", exc)
        return f"the configuration could not be loaded: {applog.scrub(str(exc))[:300]}"
    rows = []
    for key, value in values.items():
        shown = f"<{len(str(value or ''))} characters, not included>" if key in TEXT_SETTINGS else value
        rows.append(f"{key}: {shown}")
    return "\n".join(rows)


def _database(db_path: Path) -> str:
    if not db_path.exists():
        return "no database yet"
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        out = [f"schema version {conn.execute('PRAGMA user_version').fetchone()[0]}"]
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            count = conn.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
            out.append(f"{table}: {count} rows")
        if "activities" in tables:
            lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM activities").fetchone()
            out.append(f"activities span {lo} to {hi}")
        if "sync_log" in tables:
            out.append("last syncs (kind, end date, outcome):")
            for kind, end, status in conn.execute(
                    "SELECT data_type, end_date, status FROM sync_log ORDER BY id DESC LIMIT 8"):
                out.append(f"  {kind} {end} {status}")
        return "\n".join(out)
    finally:
        conn.close()


README = """This is a support bundle from Fitness Coach.

It contains: where the setup has got to, the application log, installed versions, the shape of the
database (table names and row counts) and the settings that are not free text.

It does not contain: activities, notes, conversations, goals, coach replies, API keys, or your name,
which is removed wherever it appeared. Nothing was sent anywhere; this file only exists on your
computer until you send it. Open the zip and read it first, so you know what you are sharing.
"""


def build(out_dir: Path | None = None, db_path: Path | None = None) -> Path:
    """Write the bundle and return its path."""
    from src.firstrun import report

    db_path = db_path or DB_PATH
    out_dir = out_dir or db_path.parent / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = _own_words()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"support-{stamp}.zip"

    def clean(text: str) -> str:
        return applog.scrub(text, extra)

    def section(name: str, make) -> str:
        """One part of the bundle. A part that cannot be collected says so; it never costs the rest.

        A support tool that fails when something is broken is useless at the one moment it is wanted,
        and a broken setup is exactly where `status` or the database or the config cannot be read."""
        try:
            return clean(make())
        except Exception as exc:
            applog.warning("support.section_failed", exc, section=name)
            return f"could not be collected: {type(exc).__name__}: {clean(str(exc))[:300]}"

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("README.txt", README)
        bundle.writestr("status.txt", section("status", lambda: report(db_path)))
        bundle.writestr("environment.txt", section("environment", _versions))
        bundle.writestr("settings.txt", section("settings", _settings))
        bundle.writestr("database.txt", section("database", lambda: _database(db_path)))
        for log in applog.files():
            bundle.writestr(f"logs/{log.name}",
                            section(f"log {log.name}", lambda log=log: log.read_text(encoding="utf-8", errors="replace")))
    return path


def contents(path: Path) -> dict[str, str]:
    """What is inside a bundle, for a test or for someone checking before they send it."""
    with zipfile.ZipFile(path) as bundle:
        return {n: bundle.read(n).decode("utf-8", "replace") for n in bundle.namelist()}

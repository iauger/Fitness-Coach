"""
Shared test setup — principally, a guard that stops the suite touching the real database.

This exists because the suite already wrote to it once. A fixture meant to redirect the
coaching tools at a temp database patched `schema.DB_PATH`, which does nothing: every helper
takes `db_path: Path = DB_PATH` and that default is bound when the function is *defined*, not
when it is called. The tests failed for unrelated-looking reasons, and in the course of
failing they inserted two goals, a subjective-feel row, and a life event recording an illness
that never happened — into the live database the coach reads. The fake flu was the dangerous
one: nothing downstream would have questioned it.

The guard is at the lowest level that catches every route in, because the higher-level ones
are exactly what failed. `sqlite3.connect` is where any path ends up, whatever helper or
default argument led there.

A test that genuinely needs the real database can ask for `allow_real_database`, which is
deliberately clumsy to type.

The same reasoning now covers the configuration file. Once `plan_name()` and friends read
`config/athlete.yaml`, a suite pointed at the real one asserts against *this* athlete's
settings and passes or fails depending on whose checkout it runs in; worse, `config.load`
generates the file from the example when it is absent, so a test run on a fresh clone would
create it as a side effect. Every test reads the committed example instead.
"""

import sqlite3

import pytest

import src.config
from src.db.schema import DB_PATH

_REAL = DB_PATH.resolve()
_real_connect = sqlite3.connect


class RealDatabaseTouched(RuntimeError):
    """A test tried to open data/fitness.db."""


@pytest.fixture
def allow_real_database(monkeypatch):
    """Opt out of the guard. Nothing uses this yet; it is here so the guard is not a wall."""
    monkeypatch.setattr(sqlite3, "connect", _real_connect)
    return _REAL


@pytest.fixture(autouse=True)
def _no_real_database(request, monkeypatch):
    if "allow_real_database" in request.fixturenames:
        return

    def guarded(database, *args, **kwargs):
        try:
            same = str(database) != ":memory:" and _REAL.samefile(str(database))
        except (OSError, ValueError, TypeError):
            # The path does not exist yet, which means it is not the live database.
            same = False
        if same:
            raise RealDatabaseTouched(
                f"{request.node.nodeid} opened the real database at {_REAL}.\n"
                f"Patching schema.DB_PATH does not redirect anything: db_path defaults are "
                f"bound at definition time. Pass the path explicitly, or patch "
                f"read_connection/write_connection in the module under test."
            )
        return _real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)


@pytest.fixture(autouse=True)
def _example_config(monkeypatch):
    """
    Point every test at `config/athlete.example.yaml`, and clear the memoized load.

    `CONFIG_PATH` is read inside `load()` rather than bound as a default, so patching the
    module global is enough — the lesson of the `DB_PATH` fixture above, applied the right way
    round this time. The cache is reset on both sides of the test so a test that loads config
    cannot leak its answer into the next one.
    """
    monkeypatch.setattr(src.config, "CONFIG_PATH", src.config.EXAMPLE_PATH)
    src.config.reset()
    yield
    src.config.reset()

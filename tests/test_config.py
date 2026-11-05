"""
Tests for the configuration loader.

The loader's job is not really to read YAML — PyYAML does that. Its job is to make a mistake in
the file produce **one error, at load, naming the thing that is wrong**, rather than a
`KeyError` four modules away at whatever time of day that code path first runs.

So most of these assert on the message. A loader that raises `KeyError('horzion_weeks')` has
technically failed correctly and has helped nobody.

The other half is about silence. A key the loader *ignores* is a setting the athlete believes
they changed — that is worse than a crash, because the plan then quietly runs on something
other than what the file says.
"""

from datetime import date

import pytest

from src import config as C

MINIMAL = """
athlete:
  name: Someone
plan:
  name: Test plan
  start_monday: 2026-11-02
"""


def cfg(text: str) -> C.Config:
    return C.parse(text)


# ── the happy path ────────────────────────────────────────────────────────────

def test_the_committed_example_loads():
    """It is what a new clone gets handed, so it has to be a working configuration."""
    loaded = C.load(C.EXAMPLE_PATH, create_if_missing=False)
    assert loaded.athlete.name
    assert loaded.plan.start_monday.weekday() == 0


def test_a_minimal_file_works_and_the_rest_defaults():
    c = cfg(MINIMAL)
    assert c.athlete.timezone == "UTC"
    assert c.plan.horizon_weeks == 3
    assert c.server.port == 8800
    assert c.coach.enabled is True
    assert c.goals == ()


def test_dates_parse_whether_quoted_or_not():
    """YAML turns an unquoted 2026-11-02 into a date and a quoted one into a string."""
    unquoted = cfg(MINIMAL)
    quoted = cfg(MINIMAL.replace("2026-11-02", '"2026-11-02"'))
    assert unquoted.plan.start_monday == quoted.plan.start_monday == date(2026, 11, 2)


def test_the_config_is_frozen():
    """Nothing should be able to edit the loaded settings and have that stick."""
    c = cfg(MINIMAL)
    with pytest.raises(Exception):
        c.plan.horizon_weeks = 9


def test_goals_parse_into_objects():
    c = cfg(MINIMAL + """
goals:
  - description: Ride further
    date: 2027-06-18
    priority: opt_in
""")
    assert c.goals[0].description == "Ride further"
    assert c.goals[0].priority == "opt_in"
    assert c.goals[0].date == date(2027, 6, 18)


# ── a typo must be loud, and must name itself ─────────────────────────────────

def test_an_unknown_setting_is_refused_and_named():
    """
    The failure this module exists to prevent.

    Ignoring an unknown key means the athlete edits a setting, sees no error, and gets a plan
    built on something else entirely.
    """
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL.replace("  horizon", "  nope") + "\n  horzion_weeks: 5\n")
    assert "horzion_weeks" in str(exc.value)


def test_an_unknown_setting_lists_the_ones_that_exist():
    """A misspelling is the commonest mistake; naming it alone leaves them guessing which."""
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL + "\n  horzion_weeks: 5\n")
    message = str(exc.value)
    assert "horzion_weeks" in message
    assert "horizon_weeks" in message


def test_an_unknown_top_level_section_is_refused():
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL + "\nnutrition:\n  carbs: lots\n")
    assert "nutrition" in str(exc.value)


def test_a_missing_required_setting_names_its_full_path():
    """
    The qualified path, not just the field name.

    Without the explicit check the dataclass still raises — "plan: Plan.__init__() missing 1
    required positional argument: 'start_monday'" — which names the field but reads like an
    internal error and loses the dotted path. A mutation test caught this: asserting only
    "start_monday" passed with the check removed, because that substring is in both messages.
    """
    with pytest.raises(C.ConfigError) as exc:
        cfg("athlete:\n  name: Someone\nplan:\n  name: Test plan\n")
    assert "plan.start_monday" in str(exc.value)


def test_a_missing_section_names_it():
    with pytest.raises(C.ConfigError) as exc:
        cfg("athlete:\n  name: Someone\n")
    assert "plan" in str(exc.value)


@pytest.mark.parametrize("bad, expect", [
    ("start_monday: not-a-date", "not a date"),
    ("horizon_weeks: three", "int"),
    ("start_monday: 2026-11-02\n  sustainable_band_hours: 3.5", "list"),
])
def test_a_wrong_type_says_what_was_expected(bad, expect):
    text = "athlete:\n  name: S\nplan:\n  name: T\n  start_monday: 2026-11-02\n  " + bad + "\n"
    with pytest.raises(C.ConfigError) as exc:
        cfg(text)
    assert expect in str(exc.value).lower()


def test_a_boolean_is_not_accepted_as_a_number():
    """`horizon_weeks: true` is 1 to Python, and bool is an int subclass, so this needs
    rejecting explicitly or the plan silently gets a one-week horizon."""
    with pytest.raises(C.ConfigError):
        cfg(MINIMAL + "\n  horizon_weeks: true\n")


# ── checks a type cannot make ─────────────────────────────────────────────────

def test_a_start_date_that_is_not_a_monday_is_refused():
    """
    Every week boundary in this project is Monday-to-Sunday. An off-by-one here does not
    crash, it silently moves every decision the gate ever makes.
    """
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL.replace("2026-11-02", "2026-11-03"))
    message = str(exc.value)
    assert "Monday" in message and "Tuesday" in message


def test_an_unknown_timezone_is_refused_with_examples():
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL.replace("name: Someone", "name: Someone\n  timezone: Mars/Olympus"))
    assert "Mars/Olympus" in str(exc.value)
    assert "America/New_York" in str(exc.value)


def test_a_real_timezone_is_accepted_and_usable():
    c = cfg(MINIMAL.replace("name: Someone", "name: Someone\n  timezone: America/New_York"))
    assert c.athlete.zone().key == "America/New_York"


@pytest.mark.parametrize("band", ["[0, 4]", "[4.0, 3.5]", "[-1, 2]"])
def test_a_nonsense_band_is_refused(band):
    with pytest.raises(C.ConfigError):
        cfg(MINIMAL + f"\n  sustainable_band_hours: {band}\n")


def test_a_horizon_of_zero_weeks_is_refused():
    """It would leave no dated sessions at all, which is not a configuration, it is an outage."""
    with pytest.raises(C.ConfigError):
        cfg(MINIMAL + "\n  horizon_weeks: 0\n")


def test_an_unusable_port_is_refused():
    with pytest.raises(C.ConfigError):
        cfg(MINIMAL + "\nserver:\n  port: 99999\n")


def test_an_unknown_goal_priority_is_refused_and_lists_the_valid_ones():
    with pytest.raises(C.ConfigError) as exc:
        cfg(MINIMAL + "\ngoals:\n  - description: x\n    priority: urgent\n")
    assert "urgent" in str(exc.value) and "primary" in str(exc.value)


# ── files ─────────────────────────────────────────────────────────────────────

def test_an_empty_file_says_so():
    with pytest.raises(C.ConfigError, match="empty"):
        cfg("")


def test_malformed_yaml_says_so_rather_than_raising_a_yaml_error():
    with pytest.raises(C.ConfigError, match="parse"):
        cfg("athlete:\n  name: [unclosed\n")


def test_a_missing_file_is_created_from_the_example(tmp_path, capsys):
    """
    Refusing is clearer to someone who knows the project and a wall to someone who just
    cloned it. The generated file is the committed example, which works.
    """
    target = tmp_path / "nested" / "athlete.yaml"
    loaded = C.load(target)

    assert target.exists()
    assert loaded.athlete.name == C.load(C.EXAMPLE_PATH, create_if_missing=False).athlete.name
    assert "created" in capsys.readouterr().out


def test_a_missing_file_can_be_made_fatal_instead(tmp_path):
    with pytest.raises(C.ConfigError, match="no configuration"):
        C.load(tmp_path / "absent.yaml", create_if_missing=False)


def test_an_error_in_a_file_names_the_file(tmp_path):
    """With several configs in play, the message is only actionable if it says which one."""
    bad = tmp_path / "broken.yaml"
    bad.write_text("athlete:\n  name: S\n", encoding="utf-8")
    with pytest.raises(C.ConfigError) as exc:
        C.load(bad, create_if_missing=False)
    assert "broken.yaml" in str(exc.value)


# ── the module stays a leaf, and import stays free ────────────────────────────

def test_config_is_read_once_per_process(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CONFIG_PATH", C.EXAMPLE_PATH)
    C.reset()
    first = C.config()
    assert C.config() is first
    C.reset()
    assert C.config() is not first


def test_importing_config_reads_no_file(monkeypatch):
    """
    An import-time load makes the module untestable against anything but the real file, and
    makes every importer pay for file I/O whether or not it reads a setting.
    """
    import importlib

    monkeypatch.setattr(C.Path, "read_text",
                        lambda *a, **k: pytest.fail("config read a file at import"))
    importlib.reload(C)


def test_config_imports_nothing_from_this_project():
    """
    It has to be a leaf. `planning/constants.py` already had to become one because
    `planning/blocks.py` imports `analysis/derive.py`; config is needed on both sides of that.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("src/config.py").read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    assert not [m for m in imported if m.startswith("src")], imported


# ── the athlete's day, not the server's ───────────────────────────────────────

def test_today_local_follows_the_configured_timezone(monkeypatch, tmp_path):
    """
    The point of the whole timezone step.

    `date.today()` is the day where the *process* runs. On a laptop that is the athlete's day
    by luck; on a server left in UTC an evening session in the Americas files under tomorrow,
    and is then compared against activity dates intervals.icu reports in local time.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src import clock

    cfg = tmp_path / "a.yaml"
    cfg.write_text(
        "athlete:\n  name: S\n  timezone: Pacific/Kiritimati\n"
        "plan:\n  name: T\n  start_monday: 2026-11-02\n", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfg)

    assert clock.athlete_timezone().key == "Pacific/Kiritimati"
    assert clock.today_local() == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()


def test_an_unreadable_config_falls_back_to_utc_rather_than_failing(monkeypatch, tmp_path):
    """
    This module is imported by the migration runner. A broken configuration must not be able
    to stop the schema upgrading or a timestamp being written — config problems get reported
    where config is actually read, not by taking down everything that needs the time.
    """
    from datetime import datetime, timezone

    from src import clock

    broken = tmp_path / "broken.yaml"
    broken.write_text("athlete:\n  name: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", broken)

    assert clock.athlete_timezone().key == "UTC"
    assert clock.today_local() == datetime.now(timezone.utc).date()


def test_asking_the_time_never_creates_a_config_file(monkeypatch, tmp_path):
    """
    It did. `today_local()` went through `load()`, which generates a starter file when none
    exists, so importing anything that touched the clock silently wrote config/athlete.yaml.
    Generating one belongs to a deliberate first run, not to a timestamp.
    """
    from src import clock

    absent = tmp_path / "nothing" / "athlete.yaml"
    monkeypatch.setattr(C, "CONFIG_PATH", absent)

    clock.today_local()

    assert not absent.exists()


def test_nothing_outside_clock_calls_date_today():
    """
    A grep, deliberately.

    57 call sites were routed through `clock.today_local()`; one new `date.today()` anywhere
    else silently reintroduces the server's timezone for whatever it touches, and no
    behavioural test would catch it on a laptop that happens to be in the right zone.
    """
    import pathlib

    offenders = []
    for root in ("src", "scripts"):
        for f in sorted(pathlib.Path(root).rglob("*.py")):
            if "__pycache__" in str(f) or f.name == "clock.py":
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
                if "date.today()" in line and "noqa" not in line:
                    offenders.append(f"{f}:{n}")
    assert not offenders, (
        "use clock.today_local() instead of date.today() — the athlete's day, not the "
        f"server's:\n  " + "\n  ".join(offenders))


# ── the settings step 3 added ─────────────────────────────────────────────────

def test_a_slug_that_could_not_survive_an_external_id_is_refused():
    """
    The slug is concatenated into every `external_id` pushed to the intervals.icu calendar.
    A space or a capital in one produces ids that do not match the events already there, and
    the push then creates duplicates beside them rather than updating.
    """
    for bad in ("My Plan", "My_Plan", "-leading", "trailing-", "", "Plan"):
        with pytest.raises(C.ConfigError, match="plan.slug"):
            cfg(MINIMAL + f'  slug: "{bad}"\n')


def test_a_generated_slug_is_accepted_by_the_validator():
    """These two derivations must agree, or a valid plan would be unconfigurable."""
    from src.planning.plans import slug_for

    for name in ("Sustainable rolling", "TrainerRoad 2026-27", "A/B test plan!"):
        c = cfg(MINIMAL + f'  slug: "{slug_for(name)}"\n')
        assert c.plan.slug == slug_for(name)


def test_the_slug_is_optional_and_defaults_to_being_resolved_later():
    assert cfg(MINIMAL).plan.slug is None


def test_a_profile_measurement_that_is_not_a_measurement_is_refused():
    for bad in ("0", "-5"):
        with pytest.raises(C.ConfigError, match="profile.ftp"):
            cfg(MINIMAL + f"\nprofile:\n  ftp: {bad}\n")


def test_profile_values_are_all_optional():
    """
    A missing one is seeded as nothing rather than as a guess. A guessed FTP would seed a
    wrong power history, and every zone in every prescription is a percentage of it.
    """
    c = cfg(MINIMAL + "\nprofile:\n  ftp: 250\n")
    assert c.profile.ftp == 250.0
    assert c.profile.height_in is None and c.profile.weight_lbs is None


def test_the_committed_example_fills_every_optional_section():
    """
    The example is what a first run copies, so a section missing from it is a section a new
    user never discovers. Every field is listed there even where its value is blank.
    """
    import yaml

    raw = yaml.safe_load(C.EXAMPLE_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"athlete", "goals", "plan", "profile", "integrations",
                        "server", "coach"}
    assert set(raw["athlete"]) >= {"name", "timezone", "history_start", "primary_sport",
                                   "equipment", "notes", "tools"}
    assert set(raw["profile"]) == {"height_in", "weight_lbs", "ftp"}


def test_the_sport_table_is_defined_exactly_once():
    """
    A grep, like the `date.today()` guard above and for the same reason.

    `SPORT_GROUPS` existed in four copies, plus two hand-maintained derivations. They agreed
    when they were merged, but nothing made them agree, and the way they would have diverged
    is quiet: an unrecognised activity type falls to "other", so a ride would silently vanish
    from a distribution chart, be skipped by the duplicate check and be left uncoloured on the
    calendar — three separate wrongnesses, no error.
    """
    import pathlib

    definitions = []
    for root in ("src", "scripts"):
        for f in sorted(pathlib.Path(root).rglob("*.py")):
            if "__pycache__" in str(f):
                continue
            for n, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
                if line.strip().startswith(("SPORT_GROUPS = {", "_SPORT_GROUPS = {")):
                    definitions.append(f"{f}:{n}")
    assert len(definitions) == 1, (
        f"the sport table is defined in {len(definitions)} places: {definitions}")
    assert definitions[0].replace(chr(92), "/").startswith("src/sports.py:")

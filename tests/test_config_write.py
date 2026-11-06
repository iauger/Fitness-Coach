"""
Saving a setting keeps the file's comments and refuses what the loader would refuse.

Mutants these exist to catch: writing with PyYAML (comments gone); writing before validating
(a bad save corrupts the file); validating the change rather than the result; a non-atomic
write; and the default path being the committed example.
"""

import shutil

import pytest

from src import config as cfg
from src.config_write import ConfigWriteError, apply, update


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "athlete.yaml"
    shutil.copy(cfg.EXAMPLE_PATH, p)
    return p


def test_a_saved_setting_is_read_back(path):
    new = update({"plan.max_ride_days": 3, "plan.max_ride_min": 90}, path)
    assert (new.plan.max_ride_days, new.plan.max_ride_min) == (3, 90)
    assert cfg.load(path).plan.max_ride_min == 90


def test_comments_survive_a_save(path):
    before = path.read_text(encoding="utf-8")
    update({"plan.max_ride_days": 3}, path)
    after = path.read_text(encoding="utf-8")
    comments = lambda t: [l.strip() for l in t.splitlines() if l.strip().startswith("#")]
    assert comments(after) == comments(before)           # none lost, none reordered


def test_only_the_changed_line_differs(path):
    before = path.read_text(encoding="utf-8").splitlines()
    update({"plan.strength_days": 3}, path)
    after = path.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after) and len(changed) == 1
    assert "strength_days: 3" in changed[0][1]


def test_lists_and_maps_stay_on_one_line(path):
    update({"plan.available_days": ["tue", "thu", "sat", "sun"],
            "plan.day_max_ride_min": {"sat": 150}, "plan.max_ride_min": 90}, path)
    text = path.read_text(encoding="utf-8")
    assert "available_days: [tue, thu, sat, sun]" in text
    assert "day_max_ride_min: {sat: 150}" in text
    assert cfg.load(path).plan.available_days is not None


def test_dates_are_written_bare_not_quoted(path):
    update({"plan.start_monday": "2026-11-09"}, path)
    assert "start_monday: 2026-11-09" in path.read_text(encoding="utf-8")
    assert str(cfg.load(path).plan.start_monday) == "2026-11-09"


def test_none_removes_the_key(path):
    update({"plan.max_ride_min": 90}, path)
    update({"plan.max_ride_min": None}, path)
    assert cfg.load(path).plan.max_ride_min is None
    live = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
    assert not any("max_ride_min" in l for l in live)       # the commented example stays


def test_removing_what_is_not_there_is_not_an_error(path):
    update({"plan.max_ride_min": None}, path)


def test_an_invalid_result_is_refused_and_the_file_is_untouched(path):
    """Mutant: write first, validate after. A start that is not a Monday must never land."""
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError, match="Monday"):
        update({"plan.start_monday": "2026-11-03"}, path)
    assert path.read_bytes() == before
    assert not path.with_name("athlete.yaml.tmp").exists()


def test_a_cross_field_rule_is_checked_on_the_whole_result(path):
    """Mutant: validating the change alone. Two days with two lifts is only wrong together."""
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError):
        update({"plan.available_days": ["sat", "sun"]}, path)
    assert path.read_bytes() == before


def test_an_unknown_setting_is_refused(path):
    before = path.read_bytes()
    with pytest.raises(ConfigWriteError):
        update({"plan.max_ride_minutes": 90}, path)
    assert path.read_bytes() == before


def test_the_previous_file_is_kept_as_a_backup(path):
    before = path.read_text(encoding="utf-8")
    update({"plan.max_ride_days": 3}, path)
    assert path.with_name("athlete.yaml.bak").read_text(encoding="utf-8") == before


def test_a_save_that_changes_nothing_writes_nothing(path):
    update({"plan.max_ride_days": 3}, path)
    path.with_name("athlete.yaml.bak").unlink()
    update({"plan.max_ride_days": 3}, path)
    assert not path.with_name("athlete.yaml.bak").exists()


def test_the_memoized_config_is_dropped_after_a_save(path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_PATH", path)
    cfg.reset()
    assert cfg.config().plan.max_ride_min is None
    update({"plan.max_ride_min": 90})
    assert cfg.config().plan.max_ride_min == 90


def test_the_committed_example_cannot_be_written(path, monkeypatch):
    """Under test CONFIG_PATH *is* the example; a forgotten path must not rewrite it.

    Aimed at a copy: with the guard removed this test once rewrote the real example.
    """
    monkeypatch.setattr(cfg, "EXAMPLE_PATH", path)
    monkeypatch.setattr(cfg, "CONFIG_PATH", path)
    with pytest.raises(ConfigWriteError, match="example"):
        update({"plan.max_ride_days": 3})


def test_a_missing_file_is_not_created(tmp_path):
    with pytest.raises(ConfigWriteError, match="no configuration"):
        update({"plan.max_ride_days": 3}, tmp_path / "nope.yaml")


def test_apply_is_pure():
    text = cfg.EXAMPLE_PATH.read_text(encoding="utf-8")
    assert apply(text, {"plan.max_ride_days": 3}) == apply(text, {"plan.max_ride_days": 3})


def test_a_failure_while_replacing_leaves_the_original_whole(path, monkeypatch):
    """Mutant: write straight over the file. The swap is the only step that can fail halfway."""
    from src import config_write
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk went away")
    monkeypatch.setattr(config_write.os, "replace", boom)
    with pytest.raises(OSError):
        update({"plan.max_ride_days": 3}, path)
    assert path.read_bytes() == before


CR, LF = chr(13).encode(), chr(10).encode()


def test_a_windows_file_keeps_its_line_endings(path):
    """Mutant: normalise to LF on write. The real config is CRLF, so a first save would turn
    every line of it into a diff."""
    crlf = path.read_bytes().replace(CR + LF, LF).replace(LF, CR + LF)
    path.write_bytes(crlf)
    update({"plan.max_ride_days": 3}, path)
    out = path.read_bytes()
    assert out.count(LF) == out.count(CR + LF) > 0            # no bare LF anywhere
    assert path.with_name("athlete.yaml.bak").read_bytes() == crlf   # the backup is the original


def test_a_unix_file_stays_unix(path):
    path.write_bytes(path.read_bytes().replace(CR + LF, LF))
    update({"plan.max_ride_days": 3}, path)
    assert CR not in path.read_bytes()


def test_a_new_key_does_not_swallow_the_gap_before_the_next_section():
    """Mutant: leave ruamel's placement. The blank line lands between the old last key and the
    new one, and the next section runs straight into this one."""
    text = """plan:
  a: 1

other:
  b: 2
"""
    assert apply(text, {"plan.c": 3}) == """plan:
  a: 1
  c: 3

other:
  b: 2
"""

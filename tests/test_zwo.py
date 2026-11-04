"""
Tests for the Zwift `.zwo` serializer.

The one that matters is `test_structure_survives_serialisation`: the archetypes were rewritten
so that a 30/15 bout emits real alternating 30s and 15s steps instead of describing itself in a
note string, and flattening them here would undo that and produce an unridable file.
"""

import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest

from src.planning import archetypes as A
from src.planning.zwo import (
    AUTHOR, AUTHOR_TAG, display_name, plan_dir, prune_week_dirs, safe_filename,
    session_name, supersede, to_zwo, week_dir, zwift_workouts_dir,
)


def parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def test_output_is_well_formed_and_shaped_as_zwift_expects():
    root = parse(to_zwo(A.lit(60, "steady"), "Endurance 60min", "a description"))
    assert root.tag == "workout_file"
    assert root.findtext("sportType") == "bike"
    assert root.findtext("name") == "Endurance 60min"
    assert root.findtext("description") == "a description"
    assert root.find("workout") is not None


@pytest.mark.parametrize("session", A.catalogue(),
                         ids=lambda s: f"{s.archetype}-{s.variant}")
def test_every_archetype_serialises_with_its_duration_intact(session):
    if session.archetype == "strength":
        return                                  # not a ride; never written to Zwift
    steps = list(parse(to_zwo(session, "x")).find("workout"))
    assert steps, f"{session.variant} produced no steps"
    total = sum(int(s.get("Duration")) for s in steps)
    assert total == session.seconds, f"{session.variant}: {total}s vs {session.seconds}s"


def test_structure_survives_serialisation():
    """
    A 30/15 set must come out as alternating 30s and 15s blocks. `thirty_fifteen` used to emit a
    single 9-minute block at 112% FTP with the pattern living only in a note string — which is
    not ridable, and would have gone into a .zwo verbatim.
    """
    steps = list(parse(to_zwo(A.hiit("thirty_fifteen"), "x")).find("workout"))
    work = [s for s in steps if float(s.get("Power")) > 1.0]
    assert len(work) > 1, "the 30/15 pattern collapsed into one block"
    assert {int(s.get("Duration")) for s in work} == {30}


def test_power_is_a_fraction_of_ftp_not_watts():
    """Zwift resolves Power against its own configured FTP, so nothing may be baked in."""
    for s in list(parse(to_zwo(A.threshold("long_reps"), "x")).find("workout")):
        assert 0.0 < float(s.get("Power")) < 3.0


def test_cadence_is_emitted_only_where_it_is_the_point():
    drills = list(parse(to_zwo(A.lit(60, "cadence_drills"), "x")).find("workout"))
    assert all(s.get("Cadence") for s in drills)
    assert len({s.get("Cadence") for s in drills}) > 1, "the drill does not alternate"

    steady = list(parse(to_zwo(A.lit(60, "steady"), "x")).find("workout"))
    assert not any(s.get("Cadence") for s in steady), "invented a cadence target"


def test_step_labels_become_on_screen_messages():
    steps = list(parse(to_zwo(A.sit("medium"), "x")).find("workout"))
    messages = [t.get("message") for s in steps for t in s.findall("textevent")]
    assert any("all-out" in m for m in messages)


def test_xml_special_characters_are_escaped():
    xml = to_zwo(A.lit(30, "steady"), 'Ride & "go" <fast>', "a & b")
    root = parse(xml)                           # would raise if unescaped
    assert root.findtext("name") == 'Ride & "go" <fast>'


def test_filenames_are_safe_on_windows_and_safe_to_sync():
    """
    A whitelist, not a blacklist. Zwift uploads imported workouts to its cloud so they reach
    other devices, and names with special characters are reported to stop that sync — and
    threshold prescriptions are full of `@`, `%` and `/`, exactly the characters a
    Windows-illegal blacklist would let through.
    """
    assert safe_filename("3x15min @ 90% / 1.5min") == "3x15min_90_1.5min.zwo"
    assert safe_filename('a<b>c:d"e\\f|g?h*i') == "abcdefghi.zwo"
    assert safe_filename("VO2 63min (thirty fifteen)") == "VO2_63min_thirty_fifteen.zwo"
    assert safe_filename("   ") == "workout.zwo"
    assert safe_filename("...") == "workout.zwo"
    assert safe_filename("ok").endswith(".zwo")


@pytest.mark.parametrize("session", [s for s in A.catalogue() if s.archetype != "strength"],
                         ids=lambda s: f"{s.archetype}-{s.variant}")
def test_real_session_names_produce_sync_safe_filenames(session):
    name = safe_filename(f"2026-11-03_{session_name(session)}")
    assert re.fullmatch(r"[A-Za-z0-9._-]+\.zwo", name), name


def test_a_session_is_comfortably_under_zwifts_64kb_sync_limit():
    """Over 64 KB a .zwo stays on the machine it was imported on and never reaches other
    devices. The longest session in the library is nowhere near, but assert it rather than
    assume it — the 30/15 set emits one block per 30 or 15 seconds."""
    biggest = max(len(to_zwo(s, "x").encode("utf-8"))
                  for s in A.catalogue() if s.archetype != "strength")
    assert biggest < 64 * 1024, f"largest session serialises to {biggest} bytes"


def test_session_names_say_what_the_session_is():
    assert session_name(A.lit(90, "steady")) == "Endurance 90min"
    assert session_name(A.hiit("thirty_fifteen")) == "VO2 63min (thirty fifteen)"
    assert session_name(A.threshold("over_unders")).startswith("Threshold ")
    assert session_name(A.lit(60, "steady"), prefix="2026-11-03") == "2026-11-03 Endurance 60min"


# ── superseding ───────────────────────────────────────────────────────────────

def _write(d, name: str, author: str) -> Path:
    p = d / name
    p.write_text(f"<workout_file>\n    <author>{author}</author>\n</workout_file>",
                 encoding="utf-8")
    return p


def test_supersede_removes_only_our_own_file_for_that_date(tmp_path):
    """
    The guards on a function that deletes files, in a folder holding the athlete's own workouts.

    `Path.unlink` is not undoable, so the test asserts what *survives* as much as what goes:
    a same-date file by another author, our own file for another date, and an undated hand-made
    workout all have to be left alone.
    """
    stale = _write(tmp_path, "2026-11-03_Sprints_45min_short.zwo", AUTHOR)
    fresh = _write(tmp_path, "2026-11-03_Kickstart_8x10s_neuro.zwo", AUTHOR)
    theirs = _write(tmp_path, "2026-11-03_My_Own_Workout.zwo", "iauge")
    other_day = _write(tmp_path, "2026-11-05_Endurance_60min.zwo", AUTHOR)
    handmade = _write(tmp_path, "2x20min.zwo", "iauge")

    removed = supersede(tmp_path, date(2026, 11, 3), {fresh})

    assert removed == [stale]
    assert not stale.exists()
    for survivor in (fresh, theirs, other_day, handmade):
        assert survivor.exists(), f"{survivor.name} should not have been touched"


def test_supersede_does_nothing_when_the_name_has_not_changed(tmp_path):
    """Rewriting the same prescription must not delete the file it just wrote."""
    same = _write(tmp_path, "2026-11-03_Sprints_45min_short.zwo", AUTHOR)
    assert supersede(tmp_path, date(2026, 11, 3), {same}) == []
    assert same.exists()


def test_a_generated_file_carries_the_author_tag_supersede_matches_on(tmp_path):
    """
    The two halves of the contract, checked against each other.

    `supersede` identifies our files by the author tag that `to_zwo` writes. If those two ever
    drift apart, nothing fails loudly — the folder just silently stops being cleaned.
    """
    session = A.lit(60, "steady")
    assert AUTHOR_TAG in to_zwo(session, "Endurance 60min")


# ── which folder Zwift actually reads ─────────────────────────────────────────

ROOTS = (("AppData", "Local", "Zwift"), ("Documents", "Zwift"))


def test_the_newest_location_wins_when_both_exist(tmp_path):
    """
    The bug this function exists to stop recurring.

    Zwift 1.0.167761 moved its data directory and migrated the .zwo files across, leaving both
    folders present and both full. A workout written to the old path was then never seen, while
    the migrated copy went on being listed — indistinguishable, from the outside, from Zwift
    refusing to pick up an edit. "The directory exists" is not evidence it is the live one.
    """
    for root in ROOTS:
        (tmp_path.joinpath(*root, "Workouts", "393440")).mkdir(parents=True)
    got = zwift_workouts_dir("393440", home=tmp_path, roots=ROOTS)
    assert got == tmp_path / "AppData" / "Local" / "Zwift" / "Workouts" / "393440"


def test_the_old_location_is_used_when_it_is_the_only_one(tmp_path):
    """An older Zwift install that has not migrated yet still has to work."""
    (tmp_path / "Documents" / "Zwift" / "Workouts" / "393440").mkdir(parents=True)
    got = zwift_workouts_dir("393440", home=tmp_path, roots=ROOTS)
    assert got == tmp_path / "Documents" / "Zwift" / "Workouts" / "393440"


def test_with_no_folder_at_all_the_newest_location_is_returned(tmp_path):
    """A fresh install should be pointed at where Zwift reads now, not where it used to."""
    got = zwift_workouts_dir("393440", home=tmp_path, roots=ROOTS)
    assert got == tmp_path / "AppData" / "Local" / "Zwift" / "Workouts" / "393440"


# ── folder layout ─────────────────────────────────────────────────────────────

def test_the_week_folder_names_both_the_plan_week_and_the_date():
    """
    The picker shows only this string, so it has to carry both.

    The week number alone does not say when; the date alone does not say where in the plan you
    are. Underscores rather than spaces because every workout Zwift itself writes to this folder
    uses them, which is the only evidence available about what it will sync.
    """
    got = week_dir(Path("root"), "Sustainable rolling", 1, date(2026, 11, 2))
    assert got == Path("root") / "Sustainable_rolling" / "W1_Nov_02"
    assert " " not in str(got)


def test_the_display_name_leads_with_the_day():
    """Inside a week folder a workout only has to answer "is this today's"."""
    assert display_name(A.lit(60, "steady"), 3) == "Thu - Endurance 60min"
    assert display_name(A.lit(90, "steady"), 5).startswith("Sat - ")


def test_display_names_stay_ascii():
    """A middot would read better, but filenames with special characters are reported to break
    Zwift's cloud sync and that is not worth risking for punctuation."""
    for weekday in range(7):
        name = display_name(A.sit("short"), weekday)
        assert name.isascii(), name


def test_pruning_removes_a_past_week_of_ours(tmp_path):
    base = plan_dir(tmp_path, "P")
    old = base / "W0_Oct_26"
    old.mkdir(parents=True)
    _write(old, "2026-10-27_Tue_-_Old.zwo", AUTHOR)
    current = base / "W1_Nov_02"
    current.mkdir()
    _write(current, "2026-11-03_Tue_-_New.zwo", AUTHOR)

    removed = prune_week_dirs(tmp_path, "P", {current})

    assert [p.name for p in removed] == ["W0_Oct_26"]
    assert not old.exists()
    assert current.exists()


def test_pruning_leaves_a_folder_holding_anything_that_is_not_ours(tmp_path):
    """
    This deletes directories, so the guard is asserted from the surviving side.

    A folder with one hand-made workout in it must stay whole, and so must a mixed folder — we
    do not get to remove someone else's file because it shares a directory with ours.
    """
    base = plan_dir(tmp_path, "P")
    theirs = base / "W0_My_Own"
    theirs.mkdir(parents=True)
    _write(theirs, "handmade.zwo", "iauge")
    mixed = base / "W0_Mixed"
    mixed.mkdir()
    _write(mixed, "ours.zwo", AUTHOR)
    _write(mixed, "theirs.zwo", "iauge")

    assert prune_week_dirs(tmp_path, "P", set()) == []
    assert theirs.exists() and mixed.exists()
    assert (mixed / "ours.zwo").exists()


def test_pruning_leaves_an_empty_folder_alone(tmp_path):
    """An empty directory is not evidence we created it."""
    empty = plan_dir(tmp_path, "P") / "W0_Empty"
    empty.mkdir(parents=True)
    assert prune_week_dirs(tmp_path, "P", set()) == []
    assert empty.exists()


def test_pruning_a_plan_with_no_folder_yet_is_not_an_error(tmp_path):
    assert prune_week_dirs(tmp_path, "never written", set()) == []

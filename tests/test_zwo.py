"""
Tests for the Zwift `.zwo` serializer.

The one that matters is `test_structure_survives_serialisation`: the archetypes were rewritten
so that a 30/15 bout emits real alternating 30s and 15s steps instead of describing itself in a
note string, and flattening them here would undo that and produce an unridable file.
"""

import re
import xml.etree.ElementTree as ET

import pytest

from src.planning import archetypes as A
from src.planning.zwo import safe_filename, session_name, to_zwo


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

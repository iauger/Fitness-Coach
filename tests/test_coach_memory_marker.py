"""
The "nothing worth keeping" marker is removed wherever it stands.

Found in the browser: the model wrote real bullets and then added NOTHING NOTABLE on a line of
its own, and the whole thing was stored, so the athlete saw the marker inside a note.
"""

import pytest

from src.coach.memory import NOTHING, clean_note


def test_the_marker_alone_means_there_is_nothing_to_keep():
    assert clean_note(NOTHING) is None
    assert clean_note(f"  {NOTHING}.  ") is None
    assert clean_note("") is None and clean_note(None) is None


def test_bullets_followed_by_the_marker_keep_the_bullets():
    """Mutant: only a leading marker is recognised. This exact output reached the screen."""
    out = clean_note(f"- sleeping better\n- knee is sore\n\n{NOTHING}")
    assert out == "- sleeping better\n- knee is sore"


def test_the_marker_in_the_middle_is_removed_too():
    assert clean_note(f"- a\n{NOTHING}\n- b") == "- a\n- b"


def test_a_bulleted_or_lowercase_marker_is_still_the_marker():
    assert clean_note(f"- {NOTHING.lower()}") is None
    assert clean_note(f"* {NOTHING}") is None


def test_a_real_note_that_merely_mentions_the_words_is_kept():
    text = "- nothing notable about the ride itself, but the knee needs watching"
    assert clean_note(text) == text          # only a line that is the marker and nothing else goes


@pytest.mark.parametrize("text", ["- felt strong", "- a\n- b\n- c"])
def test_an_ordinary_note_is_unchanged(text):
    assert clean_note(text) == text

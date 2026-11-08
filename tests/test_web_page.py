"""
What the served pages do with text that did not come from this project.

Mutants: an activity or planned-session name written into the calendar unescaped (the page shares
an origin with the API and can read its token); a screen painted with a bare `replaceChildren`,
which writes the word "null" into the page for any card that is absent when empty.
"""

import re
from pathlib import Path

from src import web
from src.visualizations import calendar_view as cv

HOSTILE = '<img src=x onerror="alert(1)"> & </div><script>window.x=1</script>'
INDEX = Path(web.__file__).parent.parent / "web" / "index.html"


def test_an_activity_name_cannot_inject_markup_into_the_calendar():
    html = cv._activity_html({"type": "Ride", "name": HOSTILE, "moving_time": 3600})
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&amp;" in html


def test_a_nameless_activity_falls_back_to_its_type():
    assert ">Ride<" in cv._activity_html({"type": "Ride", "name": None, "moving_time": 60})


def test_a_planned_session_name_is_escaped_too():
    html = cv._planned_html({"name": HOSTILE, "compliance_status": None, "planned_tss": 40})
    assert "<script" not in html and "&lt;script&gt;" in html


def test_every_screen_is_painted_through_show_which_drops_nulls():
    """
    Mutant: a raw `main.replaceChildren(..., maybeNullCard)`. The Plan screen did exactly that and
    printed "null" above the rung card for everyone with nothing on their watch list.
    """
    html = INDEX.read_text(encoding="utf-8")
    assert not re.search(r"\bmain\.replaceChildren\(", html)
    helper = re.search(r"function show\(parent, \.\.\.kids\) \{(.*?)\n\}", html, flags=re.S).group(1)
    assert "filter(k => k != null)" in helper and "flat(Infinity)" in helper

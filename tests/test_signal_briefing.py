"""
What the coach is told from the ledger, and the tool it can ask with.

Mutants: a header that stops saying the data wins; a watch list that is unbounded or unsorted; a
resolved or on-the-watch-list thread repeated as a "long-running theme"; a stale theme kept; the
tool returning the first N rows with no sign that it cut anything (the failure `query_history`
was fixed for); a dismissed signal still reaching the coach.
"""

import functools
import json

import pytest

from src import signal_briefing as sb
from src import signals
from src.coach import tools
from src.db import schema
from src.db.migrations import migrate

TODAY = "2026-11-10"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "brief.db"
    migrate(path)
    for module in (signals, tools):
        for name in ("read_connection", "write_connection"):
            monkeypatch.setattr(module, name, functools.partial(getattr(schema, name), path))
    return path


def put(kind="injury_pain", topic="left knee", severity=2, day="2026-11-08", **kw):
    return signals.record(kind, topic=topic, severity=severity, on_date=day, quote=kw.pop("quote", "it aches"), **kw)


# -- the block ----------------------------------------------------------------------------------

def test_an_empty_ledger_still_reads_cleanly_and_still_says_the_data_wins(db):
    text = sb.block(TODAY)
    assert "Watch list: nothing open." in text and "Nothing logged in the last 28 days." in text
    assert "the data wins" in text and "never diagnose" in text
    assert "Long-running themes" not in text


def test_the_header_tells_the_coach_how_far_to_trust_it():
    for phrase in ("filed automatically", "can be wrong", "delete", "you mentioned", "the data wins"):
        assert phrase in sb.HEADER


def test_a_watch_line_says_what_how_bad_how_often_and_how_long_ago(db):
    put(severity=2, day="2026-11-06", quote="knee aches on climbs")
    put(severity=2, day="2026-11-08", quote="knee still aches on climbs")
    (line,) = [ln for ln in sb.block(TODAY).splitlines() if "left knee" in ln]
    for part in ("injury pain / left knee", "affecting training", "2 mentions", "2026-11-06 to 2026-11-08",
                 "last 2 days ago", '"knee still aches on climbs"'):
        assert part in line


def test_a_thread_that_flared_says_it_is_rising(db):
    for d in ("2026-11-06", "2026-11-07", "2026-11-08"):
        put(kind="poor_sleep", topic="", severity=1, day=d)
    assert "rising" in sb.block(TODAY)


def test_a_long_quote_is_cut(db):
    put(severity=3, quote="x" * 300)
    line = next(ln for ln in sb.block(TODAY).splitlines() if "left knee" in ln)
    assert "x" * sb.QUOTE_CHARS in line and "x" * (sb.QUOTE_CHARS + 1) not in line and '..."' in line


def test_the_watch_list_is_bounded_and_worst_first(db):
    for i, kind in enumerate(["stress", "fatigue", "poor_sleep", "nutrition", "gear", "travel", "work_event", "illness"]):
        put(kind=kind, topic="", severity=2, day=f"2026-11-0{i + 1}")
    put(kind="burnout", topic="", severity=3, day="2026-10-20")
    lines = [ln for ln in sb.block(TODAY).splitlines() if ln.startswith("  - ")]
    assert len(lines) == sb.WATCH_MAX and lines[0].startswith("  - burnout")


def test_a_resolved_or_dismissed_signal_does_not_reach_the_coach(db):
    put(kind="illness", topic="", severity=3, state="resolved")
    sid = put(kind="burnout", topic="", severity=3)
    signals.dismiss(sid, "wrong")
    text = sb.block(TODAY)
    assert "nothing open" in text and "burnout" not in text      # a resolved illness may still be in the digest


def test_the_digest_line_is_part_of_the_block(db):
    put(kind="illness", topic="", severity=3)
    assert "Last 28 days: illness x1 (peak 3)." in sb.block(TODAY)


# -- long-running themes ------------------------------------------------------------------------

def test_a_theme_is_something_mentioned_often_that_is_not_already_on_the_watch_list(db):
    for d in ("2026-09-20", "2026-10-01", "2026-10-12"):         # quiet for 29 days: off the watch list
        put(kind="poor_sleep", topic="toddler", severity=1, day=d)
    text = sb.block(TODAY)
    assert "Long-running themes:" in text and "poor sleep / toddler: 3 mentions since 2026-09-20" in text


def test_a_thread_on_the_watch_list_is_not_repeated_as_a_theme(db):
    for d in ("2026-10-01", "2026-10-20", "2026-11-08"):
        put(severity=2, day=d)
    assert "Long-running themes" not in sb.block(TODAY)


def test_a_theme_can_be_one_long_thread_not_only_a_frequent_one_at_exactly_the_span_limit(db):
    put(kind="stress", topic="work", severity=1, day="2026-09-01")
    put(kind="stress", topic="work", severity=1, day="2026-09-29")
    assert "stress / work: 2 mentions since 2026-09-01" in sb.block(TODAY)


def test_resolved_and_stale_themes_are_left_out(db):
    for d in ("2026-10-01", "2026-10-12", "2026-10-25"):
        put(kind="fatigue", topic="", severity=1, day=d, state="resolved" if d == "2026-10-25" else "new")
    for d in ("2026-05-01", "2026-05-12", "2026-05-25"):
        put(kind="nutrition", topic="", severity=1, day=d)
    assert "Long-running themes" not in sb.block(TODAY)


def test_themes_are_limited(db):
    for i, kind in enumerate(["stress", "fatigue", "poor_sleep", "nutrition", "gear", "travel"]):
        for d in ("2026-09-20", "2026-10-01", "2026-10-12"):
            put(kind=kind, topic="", severity=1, day=d)
    assert sum(1 for ln in sb.block(TODAY).splitlines() if "mentions since" in ln) == sb.THEMES_MAX


def test_age_reads_naturally():
    assert [sb.age("2026-11-10", TODAY), sb.age("2026-11-09", TODAY), sb.age("2026-11-03", TODAY)] == \
        ["today", "yesterday", "7 days ago"]


# -- the tool -----------------------------------------------------------------------------------

def call(**kw):
    return json.loads(tools.execute_tool("query_signals", kw))


def test_the_tool_is_offered_and_changes_nothing(db):
    assert "query_signals" in [t["name"] for t in tools.TOOL_SCHEMAS]
    put()
    before = signals.timeline(limit=100, dismissed=True)
    for view in ("summary", "timeline", "watch"):
        call(view=view)
    assert signals.timeline(limit=100, dismissed=True) == before


def test_the_summary_view_groups_by_kind_and_topic(db):
    put(day="2026-11-01")
    put(day="2026-11-05")
    put(kind="illness", topic="", severity=3, day="2026-11-08")
    out = call(view="summary")
    assert out["total"] == 2 and not out["truncated"]
    assert [(r["kind"], r["mentions"]) for r in out["rows"]] == [("illness", 1), ("injury_pain", 2)]


def test_a_cut_timeline_says_how_much_was_cut(db):
    """Mutant: no total/truncated. The model would conclude the 3 it was shown were all there were."""
    for i in range(1, 8):
        put(day=f"2026-11-0{i}", severity=1)
    out = call(view="timeline", limit=3)
    assert out["total"] == 7 and out["truncated"] is True and len(out["rows"]) == 3
    assert out["rows"][0]["on_date"] == "2026-11-07"                  # newest first


def test_the_timeline_carries_the_quote_and_not_internal_columns(db):
    put(quote="knee is sore")
    (row,) = call(view="timeline")["rows"]
    assert row["quote"] == "knee is sore" and "dismissed_at" not in row and "id" not in row


def test_filters_apply_and_severity_zero_is_excluded_by_default(db):
    put(kind="nutrition", topic="fuelling", severity=0, day="2026-11-02")
    put(kind="nutrition", topic="fuelling", severity=1, day="2026-11-03")
    put(day="2026-10-01")
    assert call(view="timeline")["total"] == 3 - 1
    assert call(view="timeline", min_severity=0)["total"] == 3
    assert call(view="timeline", kind="nutrition")["total"] == 1
    assert call(view="timeline", topic="knee")["total"] == 1
    assert call(view="timeline", start_date="2026-11-01", end_date="2026-11-30")["total"] == 1


def test_the_watch_view_is_the_watch_list(db):
    put(kind="illness", topic="", severity=3, day=signals.today_local().isoformat())
    out = call(view="watch")
    assert [r["kind"] for r in out["rows"]] == ["illness"]


def test_a_bad_argument_is_an_error_result_not_a_crash(db):
    assert "error" in call(view="timeline", bogus=1)

"""
The signal ledger: validation, the severity maths, threads, the watch list, and the migration.

The judgement lives in pure functions (`signals.threads`), so most of this needs no database. Each
boundary is pinned on both sides, because an off-by-one in "when does a worry leave the coach's
mind" is exactly the bug that would pass a casual test. Mutants these exist to catch: the
escalation window using `<` for `<=`; the fade window one day long or short; escalation that
needs four mentions, or that is uncapped; a resolved thread still shown; a dismissed row still
counted; level 1 mentioned once reaching the coach; a replayed migration doubling the ledger.
"""

import functools
import sqlite3

import pytest

from src import signals
from src.coach import tools
from src.db import schema
from src.db.migrations import _m013_signals, migrate

TODAY = "2026-11-10"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "signals.db"
    migrate(path)
    for name in ("read_connection", "write_connection"):
        monkeypatch.setattr(signals, name, functools.partial(getattr(schema, name), path))
    return path


def row(i, on_date, kind="injury_pain", topic="left knee", severity=2, state="new", quote="q"):
    return {"id": i, "on_date": on_date, "kind": kind, "topic": topic, "severity": severity,
            "state": state, "quote": quote}


def only(rows, today=TODAY):
    found = signals.threads(rows, today)
    assert len(found) == 1
    return found[0]


# -- validation ---------------------------------------------------------------------------------

def test_an_unknown_kind_becomes_other_not_an_error():
    assert signals.clean("sore_toes")["kind"] == "other"


def test_a_known_kind_is_kept_and_case_insensitive():
    assert signals.clean(" Illness ")["kind"] == "illness"


@pytest.mark.parametrize("bad", [-1, 4, True, "2", 1.5, None])
def test_severity_outside_zero_to_three_is_refused(bad):
    with pytest.raises(signals.SignalError):
        signals.clean("illness", severity=bad)


@pytest.mark.parametrize("ok", [0, 1, 2, 3])
def test_every_severity_in_range_is_accepted(ok):
    assert signals.clean("illness", severity=ok)["severity"] == ok


def test_an_unknown_state_is_refused():
    with pytest.raises(signals.SignalError):
        signals.clean("illness", state="sort of better")


def test_dates_must_be_real_and_in_order():
    with pytest.raises(signals.SignalError):
        signals.clean("illness", on_date="yesterday")
    with pytest.raises(signals.SignalError):
        signals.clean("illness", start_date="2026-11-05", end_date="2026-11-01")
    assert signals.clean("illness", start_date="2026-11-05", end_date="2026-11-05")["end_date"] == "2026-11-05"


def test_topic_and_quote_are_tidied_and_bounded():
    got = signals.clean("illness", topic="  Left   KNEE ", quote="a\n  b " + "x" * 400)
    assert got["topic"] == "left knee"
    assert got["quote"].startswith("a b ") and len(got["quote"]) == signals.QUOTE_MAX


def test_the_day_defaults_to_the_athletes_today():
    from src.clock import today_local_iso
    assert signals.clean("illness")["on_date"] == today_local_iso()


# -- writing ------------------------------------------------------------------------------------

def test_record_stores_one_row_and_returns_its_id(db):
    sid = signals.record("illness", topic="flu", severity=3, quote="I have flu",
                         on_date="2026-11-08", source="conversation", source_ref="s1:4",
                         extractor_version="v1")
    c = sqlite3.connect(db)
    got = c.execute("SELECT kind, topic, severity, state, quote, on_date, source, source_ref, "
                    "extractor_version, dismissed_at FROM signals WHERE id = ?", (sid,)).fetchone()
    c.close()
    assert got == ("illness", "flu", 3, "new", "I have flu", "2026-11-08", "conversation", "s1:4", "v1", None)


def test_a_refused_signal_writes_nothing(db):
    with pytest.raises(signals.SignalError):
        signals.record("illness", severity=9)
    assert signals.timeline() == []


def test_a_dismissed_signal_stays_in_the_table_but_leaves_every_read(db):
    sid = signals.record("illness", severity=3, on_date="2026-11-08")
    assert signals.dismiss(sid, "that was my wife, not me")
    assert signals.timeline() == [] and signals.summary() == [] and signals.watch_list(TODAY) == []
    kept = signals.timeline(dismissed=True)
    assert [r["id"] for r in kept] == [sid] and kept[0]["dismissed_reason"] == "that was my wife, not me"


def test_dismissing_twice_keeps_the_first_reason(db):
    sid = signals.record("illness", on_date="2026-11-08")
    assert signals.dismiss(sid, "first") and not signals.dismiss(sid, "second")
    assert signals.timeline(dismissed=True)[0]["dismissed_reason"] == "first"


def test_a_dismissal_can_be_undone(db):
    sid = signals.record("illness", severity=3, on_date="2026-11-08")
    signals.dismiss(sid)
    assert signals.restore(sid) and len(signals.watch_list(TODAY)) == 1
    assert not signals.restore(sid)


# -- threads ------------------------------------------------------------------------------------

def test_mentions_of_one_thing_within_the_gap_are_one_thread():
    t = only([row(1, "2026-10-01"), row(2, "2026-10-29")])        # exactly 28 days apart
    assert t["count"] == 2 and t["first_seen"] == "2026-10-01" and t["ids"] == [1, 2]


def test_a_longer_gap_starts_a_new_thread():
    assert len(signals.threads([row(1, "2026-10-01"), row(2, "2026-10-30")], TODAY)) == 2


def test_a_different_topic_or_kind_is_a_different_thread():
    rows = [row(1, "2026-11-08"), row(2, "2026-11-08", topic="right knee"),
            row(3, "2026-11-08", kind="illness")]
    assert len(signals.threads(rows, TODAY)) == 3


def test_a_thread_takes_the_grade_state_and_quote_of_its_latest_mention():
    t = only([row(1, "2026-11-01", severity=3, quote="bad"), row(2, "2026-11-05", severity=1,
              state="improving", quote="better")])
    assert (t["graded"], t["state"], t["quote"]) == (1, "improving", "better")


# -- escalation ---------------------------------------------------------------------------------

def test_three_mentions_inside_two_weeks_raise_the_severity_one_level():
    t = only([row(1, "2026-10-27", severity=1), row(2, "2026-11-03", severity=1),
              row(3, "2026-11-09", severity=1)])
    assert t["severity"] == 2 and t["escalated"]


def test_the_two_week_window_is_inclusive_at_fourteen_days():
    assert only([row(1, "2026-10-27", severity=1), row(2, "2026-11-01", severity=1),
                 row(3, "2026-11-10", severity=1)])["escalated"]       # 14 days end to end


def test_three_mentions_over_fifteen_days_do_not_escalate():
    t = only([row(1, "2026-10-26", severity=1), row(2, "2026-11-01", severity=1),
              row(3, "2026-11-10", severity=1)])
    assert t["severity"] == 1 and not t["escalated"]


def test_two_mentions_never_escalate():
    assert not only([row(1, "2026-11-08", severity=1), row(2, "2026-11-09", severity=1)])["escalated"]


def test_escalation_is_capped_at_three():
    t = only([row(1, "2026-11-01", severity=3), row(2, "2026-11-03", severity=3),
              row(3, "2026-11-05", severity=3)])
    assert t["severity"] == 3


def test_a_burst_that_ended_still_counts_as_having_flared():
    rows = [row(1, "2026-10-12", severity=1), row(2, "2026-10-14", severity=1),
            row(3, "2026-10-16", severity=1), row(4, "2026-11-08", severity=1)]
    t = only(rows)
    assert t["escalated"] and t["severity"] == 2


# -- fading and staying -------------------------------------------------------------------------

def open_on(rows, today):
    return only(rows, today)["open"]


def test_a_single_level_two_mention_is_open_for_twenty_one_days_and_not_twenty_two():
    one = [row(1, "2026-10-01", severity=2)]
    assert open_on(one, "2026-10-22") and not open_on(one, "2026-10-23")


def test_an_ongoing_level_two_outlasts_the_fade_but_not_the_silence_limit():
    one = [row(1, "2026-10-01", severity=2, state="ongoing")]
    assert open_on(one, "2026-10-29") and not open_on(one, "2026-10-30")


def test_a_repeated_level_two_is_held_to_the_longer_limit():
    two = [row(1, "2026-09-25", severity=2), row(2, "2026-10-01", severity=2)]
    assert open_on(two, "2026-10-29") and not open_on(two, "2026-10-30")


def test_level_three_stays_for_twenty_eight_days_of_silence_then_goes():
    one = [row(1, "2026-10-01", severity=3)]
    assert open_on(one, "2026-10-29") and not open_on(one, "2026-10-30")


def test_a_resolved_thread_is_closed_whatever_its_severity():
    assert not open_on([row(1, "2026-11-08", severity=3, state="resolved")], TODAY)


def test_a_thread_reopens_when_it_is_mentioned_again_after_being_resolved():
    rows = [row(1, "2026-11-01", severity=3, state="resolved"), row(2, "2026-11-09", severity=2)]
    assert open_on(rows, TODAY)


def test_level_zero_is_never_open():
    assert not open_on([row(1, "2026-11-09", severity=0), row(2, "2026-11-10", severity=0)], TODAY)


def test_level_one_is_open_only_once_repeated():
    assert not open_on([row(1, "2026-11-09", severity=1)], TODAY)
    assert open_on([row(1, "2026-11-01", severity=1), row(2, "2026-11-09", severity=1)], TODAY)


def test_a_mention_dated_in_the_future_is_not_negatively_silent():
    assert open_on([row(1, "2026-11-12", severity=3)], TODAY)


# -- the watch list -----------------------------------------------------------------------------

def test_the_watch_list_holds_serious_or_recurring_open_threads_only(db):
    signals.record("injury_pain", topic="left knee", severity=2, on_date="2026-11-08")       # serious
    signals.record("poor_sleep", severity=1, on_date="2026-11-01")
    signals.record("poor_sleep", severity=1, on_date="2026-11-08")                           # recurring
    signals.record("nutrition", topic="fuelling", severity=1, on_date="2026-11-08")          # once, minor
    signals.record("illness", severity=3, state="resolved", on_date="2026-11-02")            # over
    signals.record("stress", severity=2, on_date="2026-09-01")                               # faded
    assert [(t["kind"], t["severity"]) for t in signals.watch_list(TODAY)] == [
        ("injury_pain", 2), ("poor_sleep", 1)]


def test_the_watch_list_is_ordered_by_severity_then_recurrence_then_recency(db):
    signals.record("stress", severity=2, on_date="2026-11-01")
    signals.record("fatigue", severity=2, on_date="2026-11-09")
    signals.record("illness", severity=3, on_date="2026-10-20")
    signals.record("poor_sleep", severity=2, on_date="2026-10-30")
    signals.record("poor_sleep", severity=2, on_date="2026-11-02")
    assert [t["kind"] for t in signals.watch_list(TODAY)] == [
        "illness", "poor_sleep", "fatigue", "stress"]


def test_an_empty_ledger_gives_an_empty_watch_list(db):
    assert signals.watch_list(TODAY) == []


# -- summary, timeline, digest ------------------------------------------------------------------

def test_the_timeline_is_newest_first_and_limited(db):
    for d in ("2026-11-01", "2026-11-05", "2026-11-03"):
        signals.record("fatigue", on_date=d)
    assert [r["on_date"] for r in signals.timeline()] == ["2026-11-05", "2026-11-03", "2026-11-01"]
    assert [r["on_date"] for r in signals.timeline(limit=2)] == ["2026-11-05", "2026-11-03"]


def test_filters_by_kind_topic_range_and_severity(db):
    signals.record("injury_pain", topic="left knee", severity=2, on_date="2026-11-01")
    signals.record("injury_pain", topic="right hip", severity=1, on_date="2026-11-05")
    signals.record("illness", severity=3, on_date="2026-11-08")
    assert len(signals.timeline(kind="injury_pain")) == 2
    assert [r["topic"] for r in signals.timeline(topic="KNEE")] == ["left knee"]
    assert [r["kind"] for r in signals.timeline(since="2026-11-05", until="2026-11-08")] == ["illness", "injury_pain"]
    assert [r["kind"] for r in signals.timeline(min_severity=2)] == ["illness", "injury_pain"]


def test_the_summary_counts_by_kind_and_topic_worst_first(db):
    signals.record("injury_pain", topic="left knee", severity=1, on_date="2026-11-01")
    signals.record("injury_pain", topic="left knee", severity=2, on_date="2026-11-05")
    signals.record("illness", severity=3, on_date="2026-11-08")
    assert signals.summary() == [
        {"kind": "illness", "topic": "", "mentions": 1, "max_severity": 3,
         "first_seen": "2026-11-08", "last_seen": "2026-11-08"},
        {"kind": "injury_pain", "topic": "left knee", "mentions": 2, "max_severity": 2,
         "first_seen": "2026-11-01", "last_seen": "2026-11-05"}]


def test_the_digest_of_an_empty_ledger_reads_cleanly(db):
    assert signals.digest(TODAY) == "Nothing logged in the last 28 days."


def test_the_digest_names_kinds_counts_and_peaks_and_ignores_the_old_and_the_trivial(db):
    signals.record("illness", severity=3, on_date="2026-11-08")
    signals.record("poor_sleep", severity=1, on_date="2026-11-01")
    signals.record("poor_sleep", severity=2, on_date="2026-11-05")
    signals.record("stress", severity=2, on_date="2026-09-01")           # outside the window
    signals.record("nutrition", severity=0, on_date="2026-11-05")        # a passing mention
    assert signals.digest(TODAY) == "Last 28 days: illness x1 (peak 3), poor sleep x2 (peak 2)."


# -- the life-event tool and the migration ------------------------------------------------------

def test_the_life_event_tool_writes_the_ledger_with_the_legacy_vocabulary_mapped(db, monkeypatch):
    for name in ("read_connection", "write_connection"):
        monkeypatch.setattr(tools, name, functools.partial(getattr(schema, name), db))
    assert tools.log_life_event("injury", "2026-11-05", severity="severe", note="knee")["status"] == "logged"
    tools.log_life_event("vacation", "2026-08-10", end_date="2026-08-17")
    got = {r["kind"]: r for r in signals.timeline()}
    assert got["injury_pain"]["severity"] == 3 and got["injury_pain"]["state"] == "ongoing"
    assert got["travel"]["state"] == "resolved" and got["travel"]["severity"] == 1


def test_a_bad_date_from_the_tool_is_an_error_result_not_a_crash(db, monkeypatch):
    assert "error" in tools.log_life_event("illness", "last tuesday")


def test_life_events_are_carried_into_the_ledger_once(tmp_path):
    path = tmp_path / "carry.db"
    migrate(path)
    c = sqlite3.connect(path)
    c.execute("INSERT INTO life_events (start_date, end_date, type, severity, note, logged_at, logged_via) "
              "VALUES ('2026-08-10', '2026-08-17', 'vacation', 'mild', 'a week away', '2026-08-18T03:17:42', 'chat')")
    c.execute("INSERT INTO life_events (start_date, end_date, type, severity, note, logged_at, logged_via) "
              "VALUES ('2026-09-01', NULL, 'gear_change', 'severe', NULL, '2026-09-01T08:00:00', 'chat')")
    c.commit()
    _m013_signals(c)
    _m013_signals(c)                                              # a replay must not double them
    c.commit()
    got = c.execute("SELECT kind, severity, state, on_date, start_date, end_date, quote, source, source_ref, "
                    "observed_at FROM signals ORDER BY id").fetchall()
    c.close()
    assert got == [
        ("travel", 1, "resolved", "2026-08-10", "2026-08-10", "2026-08-17", "a week away", "life_event", "1",
         "2026-08-18T03:17:42"),
        ("gear", 3, "ongoing", "2026-09-01", "2026-09-01", None, "", "life_event", "2", "2026-09-01T08:00:00")]


def test_the_severity_check_constraint_holds_in_the_table(db):
    c = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO signals (observed_at, on_date, kind, severity, source) "
                  "VALUES ('x', '2026-11-01', 'illness', 4, 'manual')")
    c.close()


# -- what the extractor is shown, and whether a source was already filed -------------------------

def test_recent_threads_include_minor_ones_but_not_resolved_or_stale(db):
    """Mutants: dropping the resolved filter (a closed illness is "continued"), or the silence limit."""
    signals.record("nutrition", topic="fuelling", severity=1, on_date="2026-11-08")         # minor: shown
    signals.record("illness", severity=3, state="resolved", on_date="2026-11-08")           # over: hidden
    signals.record("stress", severity=3, on_date="2026-10-12")                              # 29 days: hidden
    signals.record("fatigue", severity=3, on_date="2026-10-13")                             # 28 days: shown
    assert sorted(t["kind"] for t in signals.recent_threads(TODAY)) == ["fatigue", "nutrition"]


def test_recorded_is_per_source_and_reference_and_counts_dismissed_rows(db):
    sid = signals.record("illness", source="conversation", source_ref="abc", on_date="2026-11-08")
    assert signals.recorded("conversation", "abc")
    assert not signals.recorded("conversation", "abd") and not signals.recorded("feel_note", "abc")
    signals.dismiss(sid)
    assert signals.recorded("conversation", "abc")


# -- the TrainerRoad-era columns -----------------------------------------------------------------

def test_the_trainerroad_era_columns_are_gone_and_replaying_the_migration_is_harmless(tmp_path):
    """Mutant: an unguarded DROP COLUMN, which fails on the replay every fresh database performs."""
    from src.db.migrations import _m015_retire_trainerroad_columns
    path = tmp_path / "tr.db"
    migrate(path)
    c = sqlite3.connect(path)
    cols = lambda t: {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
    assert "tr_compliance" not in cols("subjective_feel") and "gcal_updated" not in cols("planned_workouts")
    _m015_retire_trainerroad_columns(c)                       # nothing left to drop
    c.close()


def test_the_drop_keeps_every_other_column_and_row(tmp_path):
    path = tmp_path / "keep.db"
    migrate(path)
    c = sqlite3.connect(path)
    c.execute("INSERT INTO subjective_feel (date, feel_score, notes, logged_at, logged_via) "
              "VALUES ('2026-09-01', 8, 'fine', 'x', 'chat')")
    c.commit()
    c.execute("ALTER TABLE subjective_feel ADD COLUMN tr_compliance TEXT")
    c.execute("UPDATE subjective_feel SET tr_compliance = 'completed_all'")
    from src.db.migrations import _m015_retire_trainerroad_columns
    _m015_retire_trainerroad_columns(c)
    c.commit()
    assert c.execute("SELECT date, feel_score, notes FROM subjective_feel").fetchall() == [("2026-09-01", 8, "fine")]
    c.close()


def test_nothing_outside_the_migrations_mentions_the_dropped_columns():
    """The grep guard: a reader of a column that no longer exists fails only when that path runs."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("src", "scripts"):
        for f in (root / folder).rglob("*.py"):
            if f.name == "migrations.py":
                continue
            text = f.read_text(encoding="utf-8")
            if "tr_compliance" in text or "gcal_updated" in text:
                offenders.append(str(f.relative_to(root)))
    assert offenders == []

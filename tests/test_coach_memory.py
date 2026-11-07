"""
What the coach remembers: one writer for the notes, for the app and the command line alike.

Mutants these exist to catch: a note written with tools available (it could act on what it was
summarising); a note for a conversation with nothing in it; the same conversation noted twice;
a conversation summarised while still being had; an abandoned one never caught up; the app's
conversations not reaching memory at all; and the coach shown only the single latest note.
"""

import functools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import clock
from src.analysis import report
from src.api import chat
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.coach import chat_store, memory, session
from src.coach import settings as coach_settings
from src.db import schema
from src.db.migrations import migrate

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    migrate(path)
    for module in (chat_store, memory):
        for name in ("get_connection", "write_connection"):
            monkeypatch.setattr(module, name, functools.partial(getattr(schema, name), path))
    monkeypatch.setattr(coach_settings, "model", lambda: "claude-test")
    return path


class FakeClient:
    """Records every request and answers with a fixed note."""

    def __init__(self, reply="- felt strong\n- sleeping better"):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.reply = reply

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.reply)])


def make(db, messages, mode="conversation", updated=None, summarised=None):
    sid = chat_store.new_session("claude-test", mode=mode)
    chat_store.replace_history(sid, messages)
    c = sqlite3.connect(db)
    if updated is not None:
        c.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?", (updated, sid))
    if summarised is not None:
        c.execute("UPDATE chat_sessions SET summarised_at = ? WHERE session_id = ?", (summarised, sid))
    c.commit()
    c.close()
    return sid


def talk(*lines):
    """A conversation of alternating user / assistant plain-text turns."""
    out = []
    for i, line in enumerate(lines):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": line if i % 2 == 0 else [{"type": "text", "text": line}]})
    return out


def rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


LONG = "My left knee has been aching on the long ride."


# -- what is worth noting ---------------------------------------------------------------------

def test_tool_results_are_not_the_athlete():
    msgs = talk(LONG, "ok") + [{"role": "user", "content": [{"type": "tool_result", "content": "x" * 99}]}]
    assert memory.athlete_turns(msgs) == [LONG]


def test_the_server_written_opening_of_a_check_in_is_not_the_athlete():
    msgs = talk("Give me my weekly check-in.", "Good week.")
    assert memory.athlete_turns(msgs, "checkin") == []
    assert memory.eligible(msgs, "checkin") is False
    assert memory.eligible(talk("Give me my weekly check-in.", "Good week.", LONG, "ok"), "checkin")


def test_a_one_word_message_is_not_worth_a_note():
    assert memory.eligible(talk("hi", "hello")) is False
    assert memory.eligible(talk(LONG, "ok")) is True


def test_the_transcript_names_who_spoke_and_what_was_recorded():
    msgs = talk(LONG, "I will note that.") + [
        {"role": "assistant", "content": [
            {"type": "text", "text": "Logged."},
            {"type": "tool_use", "name": "log_life_event", "input": {"type": "injury"}},
            {"type": "tool_use", "name": "query_history", "input": {"q": "rides"}}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "SECRET-RESULT"}]}]
    text = memory.render_transcript(msgs)
    assert f"ATHLETE: {LONG}" in text and "COACH: I will note that." in text
    assert "[coach recorded: log_life_event {'type': 'injury'}]" in text
    assert "query_history" not in text and "SECRET-RESULT" not in text


def test_a_very_long_conversation_keeps_its_tail():
    msgs = talk(*(f"line {i} " + "x" * 500 for i in range(200)))
    text = memory.render_transcript(msgs)
    assert len(text) == memory.MAX_TRANSCRIPT_CHARS and "line 199" in text and "line 0 " not in text


# -- writing the note -------------------------------------------------------------------------

def test_a_note_is_written_to_the_conversation_and_to_the_coachs_memory(db):
    sid = make(db, talk(LONG, "Noted."), updated="2026-11-05T14:00:00")
    note = memory.summarise_session(sid, FakeClient())
    assert note == "- felt strong\n- sleeping better"
    assert rows(db, f"SELECT summary FROM chat_sessions WHERE session_id='{sid}'") == [(note,)]
    assert rows(db, f"SELECT session_id, type, model, content, date FROM coaching_log") == \
        [(sid, "session_summary", "claude-test", note, "2026-11-05")]


def test_the_note_is_made_with_no_tools_and_its_own_short_prompt(db):
    """Mutant: reuse the coaching turn loop. A request to summarise could then call update_goal."""
    client = FakeClient()
    memory.summarise_session(make(db, talk(LONG, "Noted.")), client)
    (call,) = client.calls
    assert "tools" not in call and call["system"] == memory.SYSTEM
    assert call["max_tokens"] == memory.MAX_TOKENS and call["model"] == "claude-test"
    assert f"ATHLETE: {LONG}" in call["messages"][0]["content"]


def test_nothing_notable_leaves_no_note_but_counts_as_done(db):
    sid = make(db, talk(LONG, "ok"))
    assert memory.summarise_session(sid, FakeClient("NOTHING NOTABLE")) is None
    assert rows(db, "SELECT COUNT(*) FROM coaching_log") == [(0,)]
    assert rows(db, f"SELECT summary, summarised_at IS NOT NULL FROM chat_sessions "
                    f"WHERE session_id='{sid}'") == [(None, 1)]


def test_a_marker_the_model_tacked_onto_real_bullets_is_not_stored(db):
    """Mutant: only a leading marker is recognised. This exact reply reached the screen."""
    sid = make(db, talk(LONG, "ok"))
    note = memory.summarise_session(sid, FakeClient("- knee is sore\n- sleeping better\n\nNOTHING NOTABLE"))
    assert note == "- knee is sore\n- sleeping better"
    assert rows(db, "SELECT content FROM coaching_log") == [(note,)]


def test_an_empty_conversation_makes_no_call_and_is_marked_done(db):
    client = FakeClient()
    sid = make(db, talk("hi", "hello"))
    assert memory.summarise(sid, client) == (None, False)
    assert client.calls == []
    assert rows(db, f"SELECT summarised_at IS NOT NULL FROM chat_sessions WHERE session_id='{sid}'") == [(1,)]


def test_a_check_in_with_no_follow_up_makes_no_call(db):
    client = FakeClient()
    sid = make(db, talk("Give me my weekly check-in.", "A good week."), mode="checkin")
    assert memory.summarise(sid, client) == (None, False) and client.calls == []


def test_summarising_again_replaces_the_note_rather_than_adding_one(db):
    """Mutant: INSERT without the DELETE. The coach would be shown the same conversation twice."""
    sid = make(db, talk(LONG, "ok"))
    memory.summarise_session(sid, FakeClient("- first"))
    memory.summarise_session(sid, FakeClient("- second"))
    assert rows(db, "SELECT content FROM coaching_log") == [("- second",)]


def test_a_conversation_that_becomes_nothing_notable_loses_its_old_note(db):
    sid = make(db, talk(LONG, "ok"))
    memory.summarise_session(sid, FakeClient("- first"))
    memory.summarise_session(sid, FakeClient("NOTHING NOTABLE"))
    assert rows(db, "SELECT COUNT(*) FROM coaching_log") == [(0,)]


def test_an_unknown_session_is_an_error(db):
    with pytest.raises(KeyError):
        memory.summarise("nope", FakeClient())


def test_the_note_is_filed_under_the_athletes_day_not_utc(db, monkeypatch):
    """A 9pm Eastern conversation is stored as the next day in UTC; the note must not be dated ahead."""
    monkeypatch.setattr(clock, "athlete_timezone", lambda: ZoneInfo("America/New_York"))
    sid = make(db, talk(LONG, "ok"), updated="2026-11-07T01:30:00")
    memory.summarise_session(sid, FakeClient())
    assert rows(db, "SELECT date FROM coaching_log") == [("2026-11-06",)]


# -- which conversations are waiting ----------------------------------------------------------

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None).isoformat()


def test_a_conversation_still_being_had_is_left_alone(db):
    """Mutant: no idle rule. A note would be written from under the athlete mid-conversation."""
    make(db, talk(LONG, "ok"), updated=stamp(5))
    assert memory.stale_sessions(now=NOW) == []


def test_an_idle_conversation_is_closed_as_of_its_last_message_and_then_waits(db):
    sid = make(db, talk(LONG, "ok"), updated=stamp(90))
    assert memory.stale_sessions(now=NOW) == [sid]
    assert rows(db, f"SELECT ended_at FROM chat_sessions WHERE session_id='{sid}'") == [(stamp(90),)]


def test_an_ended_conversation_waits_even_if_it_was_just_ended(db):
    sid = make(db, talk(LONG, "ok"), updated=stamp(2))
    chat_store.end_session(sid)
    assert memory.stale_sessions(now=NOW) == [sid]


def test_a_noted_conversation_never_waits_again(db):
    """There is no continuing it, so there is nothing to re-note: the staleness rule is gone."""
    sid = make(db, talk(LONG, "ok"), updated=stamp(120), summarised=stamp(100))
    assert memory.stale_sessions(now=NOW) == []
    c = sqlite3.connect(db)
    c.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?", (stamp(50), sid))
    c.commit(); c.close()
    assert memory.stale_sessions(now=NOW) == []


def test_the_idle_threshold_is_a_parameter(db):
    sid = make(db, talk(LONG, "ok"), updated=stamp(10))
    assert memory.stale_sessions(idle_minutes=30, now=NOW) == []
    assert memory.stale_sessions(idle_minutes=5, now=NOW) == [sid]


def test_catching_up_spends_at_most_the_limit_in_model_calls(db):
    for _ in range(5):
        make(db, talk(LONG, "ok"), updated=stamp(90))
    client = FakeClient()
    assert len(memory.summarise_pending(limit=2, client=client)) == 2
    assert len(client.calls) == 2


def test_conversations_with_nothing_in_them_do_not_use_up_the_limit(db):
    for _ in range(3):
        make(db, talk("hi", "hello"), updated=stamp(90))
    real = make(db, talk(LONG, "ok"), updated=stamp(90))
    client = FakeClient()
    assert memory.summarise_pending(limit=1, client=client) == [real]
    assert len(client.calls) == 1


def test_a_failure_is_reported_and_the_rest_carry_on(db):
    bad = make(db, talk(LONG, "ok"), updated=stamp(95))
    good = make(db, talk(LONG, "ok"), updated=stamp(90))
    seen = []

    class Flaky(FakeClient):
        def _create(self, **kw):
            if not self.calls:
                self.calls.append(kw)
                raise RuntimeError("overloaded")
            return super()._create(**kw)
    out = memory.summarise_pending(client=Flaky(), on_error=lambda sid, e: seen.append((sid, str(e))))
    assert out == [good] and seen == [(bad, "overloaded")]


# -- bringing the old command-line transcripts across -----------------------------------------

def test_legacy_transcripts_become_ordinary_conversations_once(db, tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"messages": talk(LONG, "Noted.")}), encoding="utf-8")
    c = sqlite3.connect(db)
    c.execute("INSERT INTO transcripts (session_id, started_at, ended_at, model, turns, summary, "
              "file_path) VALUES ('old1', '2026-08-18T03:00:00', '2026-08-18T03:05:00', 'm', 1, "
              "'- old note', ?)", (str(path),))
    c.execute("INSERT INTO transcripts (session_id, started_at, summary, file_path) "
              "VALUES ('old2', '2026-08-19T03:00:00', '- no file', '/nowhere.json')")
    c.execute("INSERT INTO coaching_log (session_id, date, type, content, created_at) "
              "VALUES ('old1', '2026-08-18', 'session_summary', '- old note', 'x')")
    c.commit(); c.close()

    assert memory.import_legacy() == 2
    assert memory.import_legacy() == 0                               # idempotent
    assert rows(db, "SELECT title, summary FROM chat_sessions WHERE session_id='old1'") == \
        [(LONG, "- old note")]
    assert rows(db, "SELECT COUNT(*) FROM chat_messages WHERE session_id='old1'") == [(2,)]
    assert rows(db, "SELECT COUNT(*) FROM chat_messages WHERE session_id='old2'") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM coaching_log") == [(1,)]    # the memory was already there
    assert memory.stale_sessions(now=NOW) == []                       # and is not re-summarised


# -- the command line uses the same store and the same writer ----------------------------------

@pytest.fixture
def cli(db, monkeypatch):
    monkeypatch.setattr(session, "chat_store", chat_store)
    monkeypatch.setattr(session, "_client", lambda: FakeClient())
    monkeypatch.setattr(session, "incremental_sync", lambda **k: None)
    monkeypatch.setattr(session, "build_coaching_context", lambda: {})
    monkeypatch.setattr(session, "coaching_context_text", lambda ctx: "")
    monkeypatch.setattr(session, "build_system_prompt", lambda snap, mode: "S")

    def fake_turn(client, model, system, working, max_tokens):
        working.append({"role": "assistant", "content": [{"type": "text", "text": "Noted."}]})
        return "Noted."
    monkeypatch.setattr(session, "run_turn", fake_turn)
    return session.CoachSession(model="claude-test")


def test_a_command_line_conversation_is_stored_as_it_happens(db, cli):
    cli.ask(LONG)
    assert rows(db, f"SELECT COUNT(*) FROM chat_messages WHERE session_id='{cli.session_id}'") == [(2,)]
    assert rows(db, f"SELECT title FROM chat_sessions WHERE session_id='{cli.session_id}'") == [(LONG,)]


def test_quitting_writes_the_note_through_the_shared_writer(db, cli):
    cli.ask(LONG)
    note = cli.end_session()
    assert note == "- felt strong\n- sleeping better"
    assert rows(db, "SELECT type, session_id FROM coaching_log") == [("session_summary", cli.session_id)]
    assert rows(db, f"SELECT ended_at IS NOT NULL FROM chat_sessions WHERE session_id='{cli.session_id}'") == [(1,)]


def test_quitting_without_saying_anything_writes_nothing(db, cli):
    assert cli.end_session() is None
    assert rows(db, "SELECT COUNT(*) FROM coaching_log") == [(0,)]


def test_the_command_line_no_longer_writes_transcript_files_or_rows(db, cli, tmp_path):
    cli.ask(LONG)
    cli.end_session()
    assert rows(db, "SELECT COUNT(*) FROM transcripts") == [(0,)]
    assert not hasattr(session, "TRANSCRIPT_DIR")


def test_reset_closes_the_old_conversation_and_starts_a_new_one(db, cli):
    first = cli.session_id
    cli.ask(LONG)
    cli.reset()
    assert cli.session_id != first and cli.history == []
    assert rows(db, f"SELECT ended_at IS NOT NULL FROM chat_sessions WHERE session_id='{first}'") == [(1,)]


# -- the app: starting a conversation catches up, and ending one writes the note ---------------

@pytest.fixture
def web(db, monkeypatch):
    monkeypatch.setattr(coach_settings, "refusal", lambda: None)
    monkeypatch.setattr(chat, "_client", lambda: object())
    return TestClient(app, base_url=OWN)


def test_the_first_turn_of_a_new_conversation_catches_up_first(db, web, monkeypatch):
    waiting = make(db, talk(LONG, "ok"), updated=stamp(90))
    calls = []
    monkeypatch.setattr(memory, "summarise_pending", lambda **k: calls.append("caught up") or [waiting])
    monkeypatch.setattr(chat, "stream_turn", lambda *a, **k: iter(()))
    r = web.post("/api/chat/ask", headers=HEAD, json={"question": "How am I doing?"})
    assert calls == ["caught up"]
    assert "Updating the coach's notes" in r.text


def test_a_follow_up_in_an_open_conversation_does_not_catch_up(db, web, monkeypatch):
    sid = make(db, talk("first", "reply"))
    calls = []
    monkeypatch.setattr(memory, "summarise_pending", lambda **k: calls.append(1) or [])
    monkeypatch.setattr(chat, "stream_turn", lambda *a, **k: iter(()))
    web.post("/api/chat/ask", headers=HEAD, json={"question": "and another", "session_id": sid})
    assert calls == []


def test_nothing_is_said_when_there_is_nothing_to_catch_up(db, web, monkeypatch):
    monkeypatch.setattr(chat, "stream_turn", lambda *a, **k: iter(()))
    r = web.post("/api/chat/ask", headers=HEAD, json={"question": "hello there coach"})
    assert "Updating the coach's notes" not in r.text


def test_ending_a_conversation_schedules_its_note_and_returns_at_once(db, web, monkeypatch):
    sid = make(db, talk(LONG, "ok"))
    ran = []
    monkeypatch.setattr(memory, "summarise_session", lambda s, client=None: ran.append(s))
    monkeypatch.setattr(chat, "_EXECUTOR", SimpleNamespace(submit=lambda fn, *a: fn(*a)))
    r = web.post(f"/api/chat/sessions/{sid}/end", headers=HEAD)
    assert r.status_code == 200 and ran == [sid]
    assert rows(db, f"SELECT ended_at IS NOT NULL FROM chat_sessions WHERE session_id='{sid}'") == [(1,)]


def test_a_failing_note_does_not_break_ending(db, web, monkeypatch):
    sid = make(db, talk(LONG, "ok"))

    def boom(s, client=None):
        raise RuntimeError("api down")
    monkeypatch.setattr(memory, "summarise_session", boom)
    monkeypatch.setattr(chat, "_EXECUTOR", SimpleNamespace(submit=lambda fn, *a: fn(*a)))
    assert web.post(f"/api/chat/sessions/{sid}/end", headers=HEAD).status_code == 200


# -- what the coach is shown ------------------------------------------------------------------

def log(db, kind, day, content, created):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO coaching_log (session_id, date, type, content, created_at) "
              "VALUES ('s', ?, ?, ?, ?)", (day, kind, content, created))
    c.commit(); c.close()


@pytest.fixture
def shown(db, monkeypatch):
    monkeypatch.setattr(report, "read_connection", functools.partial(schema.read_connection, db))


def test_the_coach_sees_the_last_four_notes_and_one_check_in(db, shown):
    """Mutant: one slot each. Talking to the coach twice in a week made it forget the first."""
    for i in range(6):
        log(db, "session_summary", f"2026-11-0{i + 1}", f"note {i}", f"2026-11-0{i + 1}T10:00:00")
    for i in range(3):
        log(db, "checkin", f"2026-11-0{i + 1}", f"checkin {i}", f"2026-11-0{i + 1}T12:00:00")
    out = report._recent_coaching_memory()
    text = "\n".join(out)
    assert [n for n in range(6) if f"note {n}" in text] == [2, 3, 4, 5]
    assert [n for n in range(3) if f"checkin {n}" in text] == [2]


def test_the_notes_are_shown_oldest_first(db, shown):
    log(db, "session_summary", "2026-11-02", "later", "2026-11-02T10:00:00")
    log(db, "session_summary", "2026-11-01", "earlier", "2026-11-01T10:00:00")
    out = report._recent_coaching_memory()
    assert out[0].endswith("earlier") and out[1].endswith("later")


def test_a_kind_not_listed_gets_the_default(db, shown):
    for i in range(3):
        log(db, "other", "2026-11-01", f"o{i}", f"2026-11-01T1{i}:00:00")
    assert len(report._recent_coaching_memory()) == report.DEFAULT_MEMORY_PER_TYPE


def test_the_per_type_counts_are_what_the_comment_says():
    assert report.MEMORY_PER_TYPE == {"checkin": 1, "session_summary": 4}

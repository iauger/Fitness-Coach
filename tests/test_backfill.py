"""
Backfilling the ledger from what the athlete already wrote.

Mutants: a note written by a model (a weekly narrative) read as the athlete's; a conversation
still open read as finished; a duplicated feel note read twice; running twice filing twice; a
preview that writes; a conversation's existing note overwritten.
"""

import functools
import sqlite3
from types import SimpleNamespace

import pytest

from src import signals
from src.coach import backfill, chat_store, memory
from src.db import schema
from src.db.migrations import migrate

KNEE = {"kind": "injury_pain", "topic": "left knee", "severity": 2, "quote": "knee has been aching"}


class Client:
    def __init__(self):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="record_signals", input={
            "signals": [KNEE], "followups": [], "gist": "knee"})])


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "b.db"
    migrate(path)
    for module in (backfill, chat_store, memory):
        for name in ("get_connection", "write_connection"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, functools.partial(getattr(schema, name), path))
    for name in ("read_connection", "write_connection"):
        monkeypatch.setattr(signals, name, functools.partial(getattr(schema, name), path))
    c = sqlite3.connect(path)
    c.execute("INSERT INTO subjective_feel (date, feel_score, notes, logged_at, logged_via) "
              "VALUES ('2026-09-02', 8, 'My knee has been aching', '2026-09-02T10:00:00', 'chat')")
    c.execute("INSERT INTO subjective_feel (date, feel_score, notes, logged_at, logged_via) "
              "VALUES ('2026-09-02', 8, 'My knee has been aching', '2026-09-02T10:00:05', 'chat')")
    c.execute("INSERT INTO subjective_feel (date, feel_score, notes, logged_at, logged_via) "
              "VALUES ('2026-09-03', 8, '   ', '2026-09-03T10:00:00', 'chat')")
    c.execute("INSERT INTO activities (id, date, athlete_note) VALUES ('a1', '2026-09-04', 'knee has been aching')")
    c.execute("INSERT INTO activities (id, date, athlete_note) VALUES ('a2', '2026-09-05', NULL)")
    c.execute("INSERT INTO weekly_summaries (week_start, week_end, metrics_json, narrative, model, created_at) "
              "VALUES ('2026-08-24', '2026-08-30', '{}', 'a model-written narrative about knee has been aching', "
              "'claude', '2026-08-31T00:00:00')")
    c.commit()
    c.close()
    return path


def conversation(db, text, ended):
    sid = chat_store.new_session("m")
    chat_store.replace_history(sid, [{"role": "user", "content": text},
                                     {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}])
    if ended:
        chat_store.end_session(sid)
    c = sqlite3.connect(db)
    c.execute("UPDATE chat_sessions SET summary = 'old note' WHERE session_id = ?", (sid,))
    c.commit()
    c.close()
    return sid


def test_collects_feel_notes_ride_notes_and_finished_conversations_only(db):
    done = conversation(db, "My knee has been aching on rides", ended=True)
    conversation(db, "My knee has been aching still open", ended=False)
    conversation(db, "hi", ended=True)
    got = sorted((i.source, i.ref) for i in backfill.collect())
    assert got == sorted([("feel_note", "1"), ("ride_note", "a1"), ("conversation", done)])


def test_the_same_note_logged_twice_is_read_once_and_blank_notes_not_at_all(db):
    assert [i.ref for i in backfill.collect() if i.source == "feel_note"] == ["1"]


def test_a_model_written_weekly_narrative_is_not_the_athletes_voice(db):
    assert not [i for i in backfill.collect() if "narrative" in i.text]


def test_a_preview_calls_the_model_and_writes_nothing(db):
    client = Client()
    done = backfill.run(client, apply=False)
    assert len(done) == 2 and len(client.calls) == 2 and signals.timeline() == []


def test_applying_files_the_signals_with_their_source_and_the_day_they_were_written(db):
    backfill.run(Client(), apply=True)
    got = sorted((s["source"], s["source_ref"], s["on_date"]) for s in signals.timeline())
    assert got == [("feel_note", "1", "2026-09-02"), ("ride_note", "a1", "2026-09-04")]


def test_running_it_twice_files_nothing_the_second_time(db):
    """Mutant: collect() without the `recorded` filter. Every run would double the ledger."""
    backfill.run(Client(), apply=True)
    client = Client()
    assert backfill.run(client, apply=True) == [] and client.calls == []
    assert len(signals.timeline()) == 2


def test_a_conversation_keeps_the_note_it_already_has(db):
    sid = conversation(db, "My knee has been aching on rides", ended=True)
    backfill.run(Client(), apply=True)
    c = sqlite3.connect(db)
    got = c.execute("SELECT summary FROM chat_sessions WHERE session_id = ?", (sid,)).fetchone()
    c.close()
    assert got == ("old note",) and signals.recorded("conversation", sid)

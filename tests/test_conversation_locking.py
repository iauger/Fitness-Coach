"""
Conversations lock when they close.

A check-in is meant to be a discrete event, not an ongoing thread, and each conversation is noted
once, at the end. So a closed conversation can be read and never continued. Mutants: an ended
conversation that still accepts a question; an idle one that is never closed; one closed as of
now instead of as of its last message; re-ending that moves the end time; a locked conversation
that reports itself open to the page; a check-in that cannot take a follow-up while it is open.
"""

import functools
import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import chat
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.coach import chat_store, memory
from src.coach import settings as coach_settings
from src.coach.stream import Event
from src.db import schema
from src.db.migrations import migrate

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}
NOW = datetime.utcnow().replace(microsecond=0)      # real time: the routes read the real clock


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "lock.db"
    migrate(path)
    for module in (chat_store, memory):
        for name in ("get_connection", "write_connection"):
            monkeypatch.setattr(module, name, functools.partial(getattr(schema, name), path))
    monkeypatch.setattr(coach_settings, "refusal", lambda: None)
    monkeypatch.setattr(coach_settings, "model", lambda: "claude-test")
    monkeypatch.setattr(chat, "_client", lambda: object())
    return path


def stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def session(db, updated_minutes_ago=5, mode="conversation"):
    sid = chat_store.new_session("m", mode=mode)
    chat_store.replace_history(sid, [{"role": "user", "content": "hello there coach"},
                                     {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}])
    c = sqlite3.connect(db)
    c.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
              (stamp(updated_minutes_ago), sid))
    c.commit()
    c.close()
    return sid


def ended(db, sid):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT ended_at FROM chat_sessions WHERE session_id = ?", (sid,)).fetchone()[0]
    finally:
        c.close()


# -- the store --------------------------------------------------------------------------------

def test_ending_twice_keeps_the_first_end_time(db):
    """Mutant: the UPDATE without `ended_at IS NULL`. The moment it stopped would keep moving."""
    sid = session(db)
    chat_store.end_session(sid)
    first = ended(db, sid)
    c = sqlite3.connect(db)
    c.execute("UPDATE chat_sessions SET ended_at = '2026-01-01T00:00:00' WHERE session_id = ?", (sid,))
    c.commit(); c.close()
    chat_store.end_session(sid)
    assert first is not None and ended(db, sid) == "2026-01-01T00:00:00"


def test_an_idle_conversation_is_closed_as_of_its_last_message(db):
    sid = session(db, updated_minutes_ago=90)
    assert chat_store.close_idle(60, NOW) == [sid]
    assert ended(db, sid) == stamp(90)                  # not "now": it ended when it stopped


def test_a_recent_conversation_is_left_open(db):
    sid = session(db, updated_minutes_ago=59)
    assert chat_store.close_idle(60, NOW) == [] and ended(db, sid) is None


def test_the_limit_is_inclusive_at_the_boundary(db):
    sid = session(db, updated_minutes_ago=60)
    assert chat_store.close_idle(60, NOW) == [sid]


def test_an_already_ended_conversation_is_not_touched_again(db):
    sid = session(db, updated_minutes_ago=200)
    chat_store.end_session(sid)
    before = ended(db, sid)
    assert chat_store.close_idle(60, NOW) == [] and ended(db, sid) == before


def test_is_closed_covers_ended_and_idle_and_not_open_or_unknown(db):
    done = session(db)
    chat_store.end_session(done)
    idle = session(db, updated_minutes_ago=120)
    live = session(db, updated_minutes_ago=3)
    assert chat_store.is_closed(done, now=NOW)
    assert chat_store.is_closed(idle, now=NOW) and ended(db, idle) is not None
    assert not chat_store.is_closed(live, now=NOW)
    assert not chat_store.is_closed("nope", now=NOW)


def test_the_default_limit_is_an_hour():
    assert chat_store.IDLE_CLOSE_MINUTES == 60


# -- the route --------------------------------------------------------------------------------

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(chat, "stream_turn", lambda *a, **k: iter(
        [Event("text", text="ok"), Event("turn_end", text="ok")]))
    monkeypatch.setattr(memory, "stale_sessions", lambda *a, **k: [])
    return TestClient(app, base_url=OWN)


def test_a_question_to_an_ended_conversation_is_refused(client, db):
    """Mutant: no lock check. The conversation would grow for ever and be noted from a moving target."""
    sid = session(db)
    chat_store.end_session(sid)
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "one more thing please", "session_id": sid})
    assert r.status_code == 409 and "Start a new one" in r.json()["detail"]
    assert len(chat_store.load_history(sid)) == 2          # nothing was added


def test_a_question_to_an_idle_conversation_is_refused_and_closes_it(client, db, monkeypatch):
    sid = session(db, updated_minutes_ago=0)
    c = sqlite3.connect(db)
    old = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    c.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?", (old, sid))
    c.commit(); c.close()
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "still there coach?", "session_id": sid})
    assert r.status_code == 409 and ended(db, sid) == old


def test_an_open_conversation_still_takes_a_question(client, db):
    sid = chat_store.new_session("m")
    chat_store.replace_history(sid, [{"role": "user", "content": "first question here"},
                                     {"role": "assistant", "content": [{"type": "text", "text": "a"}]}])
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "and a follow up", "session_id": sid})
    assert r.status_code == 200


def test_a_check_in_takes_a_follow_up_while_it_is_open(client, db):
    """A check-in is a discrete event, but your reply to it is some of the best signal you give."""
    sid = session(db, updated_minutes_ago=2, mode="checkin")
    r = client.post("/api/chat/ask", headers=HEAD, json={"question": "I will try four days", "session_id": sid})
    assert r.status_code == 200


def test_a_new_conversation_is_never_refused(client, db):
    assert client.post("/api/chat/ask", headers=HEAD, json={"question": "a brand new question"}).status_code == 200


def test_the_page_is_told_whether_a_conversation_is_locked(client, db):
    live, done = session(db, updated_minutes_ago=1), session(db)
    chat_store.end_session(done)
    assert client.get(f"/api/chat/sessions/{live}").json()["closed"] is False
    assert client.get(f"/api/chat/sessions/{done}").json()["closed"] is True


def test_the_list_carries_the_end_time_so_the_page_can_mark_closed_ones(client, db):
    sid = session(db)
    chat_store.end_session(sid)
    row = next(r for r in client.get("/api/chat/sessions").json() if r["session_id"] == sid)
    assert row["ended_at"] is not None


def test_ending_through_the_route_is_idempotent(client, db, monkeypatch):
    monkeypatch.setattr(chat, "_EXECUTOR", type("E", (), {"submit": staticmethod(lambda fn, *a: None)})())
    sid = session(db)
    client.post(f"/api/chat/sessions/{sid}/end", headers=HEAD)
    first = ended(db, sid)
    client.post(f"/api/chat/sessions/{sid}/end", headers=HEAD)
    assert ended(db, sid) == first

"""
Seeing and correcting what the coach remembers.

The notes decide what the coach believes about the athlete in every later conversation, so the
athlete can read them, see which are in its briefing right now, correct one, or make it forget.
Mutants: the screen listing a different selection from the briefing; a check-in editable here
(the due-status reads check-ins); an edit that leaves the briefing unchanged; a blank edit
accepted; no token needed.
"""

import functools
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.analysis import report
from src.api import chat
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.coach import chat_store
from src.db import schema
from src.db.migrations import migrate

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    migrate(path)
    for module, names in ((chat, ("read_connection", "write_connection")),
                          (report, ("read_connection",)),
                          (chat_store, ("get_connection", "write_connection"))):
        for name in names:
            monkeypatch.setattr(module, name, functools.partial(getattr(schema, name), path))
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=OWN)


def rows(db, sql):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def log(db, kind, day, content, created, session="s"):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO coaching_log (session_id, date, type, content, created_at) "
              "VALUES (?, ?, ?, ?, ?)", (session, day, kind, content, created))
    c.commit()
    c.close()


def note_id(db, kind="session_summary"):
    return rows(db, f"SELECT id FROM coaching_log WHERE type='{kind}'")[0][0]


def conversation(db, summary=None):
    sid = chat_store.new_session("m")
    chat_store.replace_history(sid, [{"role": "user", "content": "I have a sore knee."},
                                     {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}])
    if summary:
        c = sqlite3.connect(db)
        c.execute("UPDATE chat_sessions SET summary = ? WHERE session_id = ?", (summary, sid))
        c.commit()
        c.close()
    return sid


def test_the_screen_marks_what_the_coach_is_actually_shown(db, client):
    for i in range(6):
        log(db, "session_summary", f"2026-11-0{i + 1}", f"note {i}", f"2026-11-0{i + 1}T10:00:00")
    body = client.get("/api/chat/memory").json()
    assert sorted(e["content"] for e in body["entries"] if e["shown"]) == \
        ["note 2", "note 3", "note 4", "note 5"]
    assert [e["content"] for e in body["entries"]][:2] == ["note 5", "note 4"]        # newest first
    assert body["per_type"] == report.MEMORY_PER_TYPE


def test_the_screens_selection_is_the_briefings_selection(db, client):
    """Mutant: the route re-implements the choice. They would drift the day one changed."""
    for i in range(5):
        log(db, "session_summary", f"2026-11-0{i + 1}", f"n{i}", f"2026-11-0{i + 1}T10:00:00")
    log(db, "checkin", "2026-11-05", "c", "2026-11-05T12:00:00")
    shown_ids = {e["id"] for e in client.get("/api/chat/memory").json()["entries"] if e["shown"]}
    assert shown_ids == {e["id"] for e in report.coaching_memory_entries()}


def test_only_conversation_notes_are_editable(db, client):
    log(db, "checkin", "2026-11-01", "a check-in", "2026-11-01T10:00:00")
    log(db, "session_summary", "2026-11-02", "a note", "2026-11-02T10:00:00")
    flags = {e["content"]: e["editable"] for e in client.get("/api/chat/memory").json()["entries"]}
    assert flags == {"a check-in": False, "a note": True}


def test_correcting_a_note_changes_what_the_coach_will_be_told(db, client):
    sid = conversation(db, "- has a knee injury")
    log(db, "session_summary", "2026-11-02", "- has a knee injury", "2026-11-02T10:00:00", session=sid)
    r = client.put(f"/api/chat/memory/{note_id(db)}", headers=HEAD, json={"content": "- no injury, tired"})
    assert r.status_code == 200
    briefing = " ".join(report._recent_coaching_memory())
    assert "no injury" in briefing and "knee injury" not in briefing
    assert rows(db, f"SELECT summary FROM chat_sessions WHERE session_id='{sid}'") == [("- no injury, tired",)]


def test_forgetting_a_note_removes_it_and_keeps_the_conversation(db, client):
    sid = conversation(db, "- wrong fact")
    log(db, "session_summary", "2026-11-02", "- wrong fact", "2026-11-02T10:00:00", session=sid)
    assert client.delete(f"/api/chat/memory/{note_id(db)}", headers=HEAD).status_code == 200
    assert report._recent_coaching_memory() == []
    assert rows(db, "SELECT COUNT(*) FROM chat_messages") == [(2,)]
    assert rows(db, f"SELECT summary FROM chat_sessions WHERE session_id='{sid}'") == [(None,)]


def test_a_check_in_cannot_be_edited_or_deleted_here(db, client):
    """Mutant: no type check. The due-status reads check-ins, so deleting one would re-open a week."""
    log(db, "checkin", "2026-11-01", "kept", "2026-11-01T10:00:00")
    cid = note_id(db, "checkin")
    assert client.put(f"/api/chat/memory/{cid}", headers=HEAD, json={"content": "x"}).status_code == 403
    assert client.delete(f"/api/chat/memory/{cid}", headers=HEAD).status_code == 403
    assert rows(db, "SELECT content FROM coaching_log") == [("kept",)]


def test_an_unknown_note_is_a_404_and_a_blank_edit_is_refused(db, client):
    assert client.delete("/api/chat/memory/999", headers=HEAD).status_code == 404
    log(db, "session_summary", "2026-11-02", "a note", "2026-11-02T10:00:00")
    nid = note_id(db)
    assert client.put(f"/api/chat/memory/{nid}", headers=HEAD, json={"content": "   "}).status_code == 422
    assert rows(db, "SELECT content FROM coaching_log") == [("a note",)]


def test_changing_the_coachs_memory_needs_the_token(db, client):
    log(db, "session_summary", "2026-11-02", "a note", "2026-11-02T10:00:00")
    nid = note_id(db)
    assert client.put(f"/api/chat/memory/{nid}", json={"content": "x"}).status_code == 403
    assert client.delete(f"/api/chat/memory/{nid}").status_code == 403
    assert rows(db, "SELECT content FROM coaching_log") == [("a note",)]

"""
The weekly check-in and the block review, run from the app.

Mutants these exist to catch: a run recorded when the model returned nothing (the log row is what
silences the next prompt, and an empty review row marks a block reviewed for good); no sync
before the context is built; a structured run joined onto an old session; the note lost; a
failed sync aborting the run; and the CLI and the app building different check-ins.
"""

import functools
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import chat
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.coach import chat_store, memory, session
from src.coach import settings as coach_settings
from src.coach.stream import Event
from src.db import schema
from src.db.migrations import migrate

OWN = "http://localhost:8800"
ORIGINAL_SYNC = chat._sync_quietly      # captured before any fixture replaces it
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


def frames(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp database behind the chat store, the coach switched on, and every model call faked."""
    path = tmp_path / "runs.db"
    migrate(path)
    for name in ("get_connection", "write_connection"):
        monkeypatch.setattr(chat_store, name, functools.partial(getattr(schema, name), path))
        monkeypatch.setattr(memory, name, functools.partial(getattr(schema, name), path))
    monkeypatch.setattr(coach_settings, "refusal", lambda: None)
    monkeypatch.setattr(coach_settings, "model", lambda: "claude-test")
    monkeypatch.setattr(chat, "_client", lambda: object())

    calls = SimpleNamespace(order=[], recorded=[], reviews=[], notes=[], sync_fails=False)

    def sync(queue):
        calls.order.append("sync")
        if calls.sync_fails:
            queue.put({"type": "status", "text": "Could not sync (offline); using what is already stored."})
    monkeypatch.setattr(chat, "_sync_quietly", sync)

    def inputs(note=""):
        calls.order.append("inputs")
        calls.notes.append(note)
        return {"system": "S", "model": "claude-test",
                "messages": [{"role": "user", "content": "Give me my weekly check-in."}]}
    monkeypatch.setattr(session, "checkin_inputs", inputs)
    monkeypatch.setattr(session, "finish_checkin",
                        lambda text, model, sid=None, verbose=False: calls.recorded.append((text, model, sid)))

    def script(events):
        def fake(client, model, system, history, max_tokens):
            for e in events:
                yield e
            history.append({"role": "assistant", "content": [{"type": "text", "text": "answer"}]})
        monkeypatch.setattr(chat, "stream_turn", fake)
    calls.script = script
    calls.script([Event("text", text="Good week."), Event("turn_end", text="Good week.")])
    calls.path = path
    return calls


@pytest.fixture
def client():
    return TestClient(app, base_url=OWN)


def rows(path, sql):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


# -- the weekly check-in ----------------------------------------------------------------------

def test_a_check_in_syncs_then_builds_then_streams_then_records(env, client):
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    kinds = [f["type"] for f in frames(r)]
    assert env.order == ["sync", "inputs"]                     # sync first, then the briefing
    assert kinds[0] == "session" and "text" in kinds and kinds[-1] == "recorded"
    assert env.recorded and env.recorded[0][0] == "Good week."


def test_the_record_carries_the_session_id_the_page_was_given(env, client):
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    sid = frames(r)[0]["session_id"]
    assert env.recorded[0][2] == sid


def test_the_conversation_is_stored_so_it_can_be_reopened(env, client):
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    sid = frames(r)[0]["session_id"]
    assert len(rows(env.path, f"SELECT role FROM chat_messages WHERE session_id='{sid}'")) == 2
    assert rows(env.path, f"SELECT title, mode FROM chat_sessions WHERE session_id='{sid}'") == \
        [("Weekly check-in", "checkin")]


def test_the_note_reaches_the_briefing(env, client):
    client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin", "note": "slept badly"})
    assert env.notes == ["slept badly"]


def test_an_empty_answer_is_not_recorded(env, client):
    """Mutant: record whatever came back. An empty row would silence the next prompt."""
    env.script([Event("turn_end", text="   ")])
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    assert env.recorded == []
    assert any(f["type"] == "error" and "nothing was recorded" in f["message"] for f in frames(r))
    assert rows(env.path, "SELECT COUNT(*) FROM chat_messages") == [(0,)]


def test_a_model_error_with_no_text_is_not_recorded(env, client):
    env.script([Event("error", message="APIError: overloaded")])
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    assert env.recorded == []
    assert [f["type"] for f in frames(r)][-1] == "error"


def test_a_failed_sync_does_not_stop_the_check_in(env, client):
    env.sync_fails = True
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    texts = [f.get("text", "") for f in frames(r) if f["type"] == "status"]
    assert any("Could not sync" in t for t in texts) and env.recorded


def test_an_exception_while_recording_is_reported_not_swallowed(env, client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(session, "finish_checkin", boom)
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin"})
    assert any(f["type"] == "error" and "disk full" in f["message"] for f in frames(r))
    assert "recorded" not in [f["type"] for f in frames(r)]


# -- request rules ----------------------------------------------------------------------------

def test_a_check_in_cannot_join_an_existing_session(env, client):
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "checkin", "session_id": "abc"})
    assert r.status_code == 422 and env.order == []


def test_a_conversation_still_needs_a_question(env, client):
    assert client.post("/api/chat/ask", headers=HEAD, json={"question": "  "}).status_code == 422
    assert client.post("/api/chat/ask", headers=HEAD, json={}).status_code == 422


def test_a_structured_run_needs_the_token(env, client):
    assert client.post("/api/chat/ask", json={"mode": "checkin"}).status_code == 403
    assert env.order == []


def test_an_unknown_mode_is_refused(env, client):
    assert client.post("/api/chat/ask", headers=HEAD, json={"mode": "therapy"}).status_code == 422


# -- the block review -------------------------------------------------------------------------

CYC = {"plan_name": "P", "start_date": "2026-11-02", "end_date": "2026-11-22", "weeks": 3,
       "closed": True, "plan_week_range": (1, 3)}


def test_a_review_with_no_closed_block_is_a_409_and_creates_nothing(env, client, monkeypatch):
    monkeypatch.setattr(chat, "_pending_review_cycle", lambda today=None: None)
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "cycle_review"})
    assert r.status_code == 409
    assert rows(env.path, "SELECT COUNT(*) FROM chat_sessions") == [(0,)]


def test_a_review_runs_for_the_pending_block_and_files_it(env, client, monkeypatch):
    seen = {}
    monkeypatch.setattr(chat, "_pending_review_cycle", lambda today=None: CYC)
    monkeypatch.setattr(session, "cycle_review_inputs", lambda cyc, verbose=False: (
        seen.update(cyc=cyc) or {"system": "S", "model": "claude-test", "cyc": cyc, "metrics": {},
                                 "plan_name": "P",
                                 "messages": [{"role": "user", "content": "review"}]}))
    monkeypatch.setattr(session, "finish_cycle_review",
                        lambda inputs, text: env.reviews.append((inputs["cyc"], text)))
    r = client.post("/api/chat/ask", headers=HEAD, json={"mode": "cycle_review"})
    assert seen["cyc"] == CYC and env.reviews == [(CYC, "Good week.")]
    assert frames(r)[-1] == {"type": "recorded", "what": "cycle_review"}
    assert rows(env.path, "SELECT title FROM chat_sessions") == [("Block review",)]


def test_an_empty_review_is_not_filed(env, client, monkeypatch):
    """The review table is UNIQUE per block: an empty row would mark it reviewed for ever."""
    monkeypatch.setattr(chat, "_pending_review_cycle", lambda today=None: CYC)
    monkeypatch.setattr(session, "cycle_review_inputs", lambda cyc, verbose=False: {
        "system": "S", "model": "m", "cyc": cyc, "metrics": {}, "plan_name": "P",
        "messages": [{"role": "user", "content": "review"}]})
    monkeypatch.setattr(session, "finish_cycle_review", lambda *a: env.reviews.append(a))
    env.script([Event("turn_end", text="")])
    client.post("/api/chat/ask", headers=HEAD, json={"mode": "cycle_review"})
    assert env.reviews == []


# -- the due route ----------------------------------------------------------------------------

def test_the_due_route_reports_both_statuses(env, client, monkeypatch):
    monkeypatch.setattr(chat, "read_connection", functools.partial(schema.read_connection, env.path))
    monkeypatch.setattr("src.coach.due.review_status", lambda conn, today, cycle_list=None: {"state": "ok"})
    body = client.get("/api/chat/due").json()
    assert body["checkin"]["state"] == "overdue" and body["review"]["state"] == "ok"
    assert body["attention"] == 1


# -- the recording itself ---------------------------------------------------------------------

@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "log.db"
    migrate(path)
    monkeypatch.setattr(session, "write_connection", functools.partial(schema.write_connection, path))
    monkeypatch.setattr(session, "_summarise_last_week", lambda verbose=False: None)
    return path


def test_finishing_a_check_in_writes_the_log_row_the_status_reads(log):
    sid = session.finish_checkin("All good.", "claude-test", "sess1234")
    assert sid == "sess1234"
    row = rows(log, "SELECT session_id, type, model, content FROM coaching_log")
    assert row == [("sess1234", "checkin", "claude-test", "All good.")]


def test_finishing_with_no_session_id_makes_one(log):
    assert len(session.finish_checkin("ok", "m")) == 8


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_check_in_is_refused_and_leaves_no_row(log, empty):
    with pytest.raises(RuntimeError, match="empty check-in"):
        session.finish_checkin(empty, "m")
    assert rows(log, "SELECT COUNT(*) FROM coaching_log") == [(0,)]


def test_a_summary_failure_does_not_lose_the_check_in(log, monkeypatch):
    def boom(verbose=False):
        raise RuntimeError("summariser down")
    monkeypatch.setattr(session, "_summarise_last_week", boom)
    session.finish_checkin("kept", "m", "s1")
    assert rows(log, "SELECT content FROM coaching_log") == [("kept",)]


def test_an_empty_review_is_refused_by_the_filer():
    with pytest.raises(RuntimeError, match="empty review"):
        session.finish_cycle_review({"cyc": CYC, "plan_name": "P", "metrics": {}, "model": "m"}, " ")


# -- the command line builds the same check-in ------------------------------------------------

def test_the_command_line_syncs_before_it_builds_the_briefing(monkeypatch):
    """Mutant: briefing built first. The coach was briefed on data a sync was about to replace."""
    order = []
    monkeypatch.setattr(session, "incremental_sync", lambda **k: order.append("sync"))
    monkeypatch.setattr(session, "checkin_inputs", lambda feel="": order.append("inputs") or {
        "system": "S", "model": "m", "messages": [{"role": "user", "content": "x"}]})
    monkeypatch.setattr(session, "_client", lambda: object())
    monkeypatch.setattr(session, "_run_tool_loop", lambda *a: order.append("model") or "text")
    monkeypatch.setattr(session, "finish_checkin", lambda *a, **k: order.append("record"))
    assert session.checkin() == "text"
    assert order == ["sync", "inputs", "model", "record"]


def test_the_note_becomes_part_of_the_opening_message(monkeypatch):
    monkeypatch.setattr(session, "build_coaching_context", lambda: {})
    monkeypatch.setattr(session, "coaching_context_text", lambda ctx: "snap")
    monkeypatch.setattr(session, "build_system_prompt", lambda snap, mode: f"{mode}:{snap}")
    monkeypatch.setattr(session, "coach_model", lambda: "m")
    plain = session.checkin_inputs()
    noted = session.checkin_inputs("legs heavy")
    assert plain["messages"][0]["content"] == "Give me my weekly check-in."
    assert "legs heavy" in noted["messages"][0]["content"]
    assert plain["system"] == "checkin:snap"


# -- the sync that precedes a structured run ---------------------------------------------------

@pytest.fixture
def lock_in_tmp(tmp_path, monkeypatch):
    from src import maintenance
    monkeypatch.setattr(maintenance, "LOCK_PATH", tmp_path / "m.lock")


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def test_the_real_sync_runs_under_the_maintenance_lock(lock_in_tmp, monkeypatch):
    """Mutant: sync outside the lock. It could write while the background sync does."""
    import queue

    from src import maintenance, sync
    seen = {}
    monkeypatch.setattr(sync, "incremental_sync",
                        lambda **k: seen.update(locked=maintenance.LOCK_PATH.exists()))
    q = queue.Queue()
    real = ORIGINAL_SYNC
    real(q)
    assert seen == {"locked": True}
    assert [m["text"] for m in drain(q)] == ["Syncing your latest rides..."]


def test_a_sync_already_running_is_not_an_error(lock_in_tmp, monkeypatch):
    import queue

    from src import maintenance, sync
    ran = []
    monkeypatch.setattr(sync, "incremental_sync", lambda **k: ran.append(1))
    real = ORIGINAL_SYNC
    q = queue.Queue()
    with maintenance.exclusive():
        real(q)
    assert ran == []
    assert all("Could not sync" not in m.get("text", "") for m in drain(q))


def test_a_sync_failure_is_reported_and_swallowed(lock_in_tmp, monkeypatch):
    import queue

    from src import sync

    def offline(**k):
        raise ConnectionError("offline")
    monkeypatch.setattr(sync, "incremental_sync", offline)
    real = ORIGINAL_SYNC
    q = queue.Queue()
    real(q)
    assert any("Could not sync (offline)" in m.get("text", "") for m in drain(q))

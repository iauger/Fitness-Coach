"""
Chat over Server-Sent Events — phase 12C.

SSE rather than WebSocket: the traffic is one-directional once a question is asked (the server
streams, the client listens), it is plain HTTP so it needs no protocol upgrade or extra
dependency, and browsers reconnect it for free. A WebSocket would buy bidirectionality this
does not use.

The system prompt is built once per session, not per turn. It carries the full athlete snapshot,
which is ~8,100 tokens; rebuilding it every turn would re-query the whole analysis layer and
defeat prompt caching, which is the main cost lever for multi-turn chat.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.analysis.report import MEMORY_PER_TYPE, coaching_context_text, coaching_memory_entries
from src.api.cache import freshness_key
from src import maintenance
from src.clock import today_local
from src.coach import chat_store, due, memory
from src.coach import session as coach_session
from src.coach.prompt import build_system_prompt
from src.coach import settings as coach_settings
from src.coach.session import MAX_TOKENS, _client
from src.coach.stream import Event, stream_turn
from src.db.schema import read_connection, write_connection
from src.planning.constants import plan_name

router = APIRouter(prefix="/api/chat", tags=["chat"])

# The Anthropic SDK is synchronous, so a turn runs on a worker thread and its events are handed
# to the event loop through a queue. Single-user app: one worker is enough and keeps the
# SQLite write path serialised.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="coach")

# The built system prompt, memoized against the database rather than against the session.
#
# It used to be keyed by session_id and only evicted when a session was explicitly ended,
# which was wrong twice over. It grew without bound — one ~20KB prompt per session, for the
# life of the server — and, worse, it went stale: the prompt carries the whole coaching
# snapshot, so a conversation left open across a sync kept answering from the data that
# existed when its first question was asked. A coach quoting yesterday's numbers at you, with
# no way to tell.
#
# Keying on the database's freshness instead fixes both. Every session shares one entry,
# because every session builds the identical prompt, and that entry is rebuilt exactly when
# the data behind it changes.
_SYSTEM_CACHE: dict[tuple, str] = {}

_SENTINEL = object()


class AskRequest(BaseModel):
    """
    `conversation` is an open question. `checkin` and `cycle_review` are the two structured
    runs: the server writes their opening message, they always start a new session, and a
    successful one is *recorded* (a log row, a filed review) so the due-status can see it.
    """
    question: str | None = Field(default=None, max_length=4000)
    session_id: str | None = None
    mode: Literal["conversation", "checkin", "cycle_review"] = "conversation"
    note: str | None = Field(default=None, max_length=1000)     # check-in only: how you feel


def _system_for(session_id: str) -> str:
    """The system prompt for this turn. `session_id` is unused: see _SYSTEM_CACHE."""
    key = freshness_key()
    cached = _SYSTEM_CACHE.get(key)
    if cached is None:
        # Only the current key is kept. Holding the old one would reintroduce the unbounded
        # growth this replaced, and nothing ever wants a prompt built from stale data.
        _SYSTEM_CACHE.clear()
        cached = _SYSTEM_CACHE[key] = build_system_prompt(
            coaching_context_text(), mode="conversation")
    return cached


def _pending_review_cycle(today=None) -> dict | None:
    """The block a review would cover now, by the same rule the due-status shows."""
    from src.analysis.training_plan import cycles
    today = today or today_local()
    with read_connection() as conn:
        status = due.review_status(conn, today)
    if status["cycle"] is None:
        return None
    return next((c for c in cycles(plan_name()) if c["start_date"] == status["cycle"]["start"]),
                None)


def _sync_quietly(queue: Queue) -> None:
    """
    Bring the data up to date before a structured run, as the command line always did.

    Under the same lock as the background sync, so the two never write together; if that lock is
    held the sync is already happening and its data is good enough. A failed sync (no network)
    is reported and the run carries on with what is stored, because the athlete asked for a
    check-in, not for a sync.
    """
    queue.put({"type": "status", "text": "Syncing your latest rides..."})
    from src.sync import incremental_sync
    try:
        with maintenance.exclusive():
            incremental_sync(silent=True)
    except maintenance.AlreadyRunning:
        pass
    except Exception as exc:
        queue.put({"type": "status", "text": f"Could not sync ({exc}); using what is already stored."})


def _catch_up_memory(queue: Queue) -> None:
    """
    Write the notes for conversations that were left open, before a new one begins.

    There is no "end" in the app, so an abandoned conversation is only summarised when something
    else starts. Best-effort and quiet unless there is work: a failure is reported as a status
    line and the conversation carries on.
    """
    if not memory.stale_sessions():
        return
    queue.put({"type": "status", "text": "Updating the coach's notes from earlier conversations..."})
    memory.summarise_pending(on_error=lambda sid, exc: queue.put(
        {"type": "status", "text": f"Could not summarise {sid}: {exc}"}))


def _run_structured(session_id: str, mode: str, note: str | None, queue: Queue) -> None:
    """A check-in or block review: sync, build the prompt, stream it, and record it if it worked."""
    try:
        _sync_quietly(queue)
        _catch_up_memory(queue)
        if mode == "checkin":
            inputs = coach_session.checkin_inputs(note or "")
        else:
            cyc = _pending_review_cycle()
            if cyc is None:
                raise RuntimeError("there is no closed block waiting for a review")
            inputs = coach_session.cycle_review_inputs(cyc)
        history = list(inputs["messages"])

        text = ""
        for event in stream_turn(_client(), inputs["model"], inputs["system"], history, MAX_TOKENS):
            if event.type == "turn_end":
                text = event.text
            queue.put(event)

        if not text.strip():
            queue.put(Event("error", message="The coach returned no answer, so nothing was recorded."))
            return
        if mode == "checkin":
            coach_session.finish_checkin(text, inputs["model"], session_id)
        else:
            coach_session.finish_cycle_review(inputs, text)
        chat_store.replace_history(session_id, history)
        queue.put({"type": "recorded", "what": mode})
    except Exception as exc:
        queue.put(Event("error", message=f"{type(exc).__name__}: {exc}"))
    finally:
        queue.put(_SENTINEL)


def _summarise_quietly(session_id: str) -> None:
    try:
        memory.summarise_session(session_id)
    except Exception:
        pass


def _sse(event: Event | dict) -> str:
    payload = event.to_json() if isinstance(event, Event) else json.dumps(event)
    return f"data: {payload}\n\n"


def _run_and_enqueue(session_id: str, question: str, queue: Queue) -> None:
    """Run one turn on a worker thread, pushing events onto the queue as they arrive."""
    try:
        history = chat_store.load_history(session_id)
        if not history:
            _catch_up_memory(queue)          # a new conversation: first, remember the old ones
        history.append({"role": "user", "content": question})
        system = _system_for(session_id)

        for event in stream_turn(_client(), coach_settings.model(), system, history, MAX_TOKENS):
            queue.put(event)

        # Persisted after the turn completes, so a failed turn does not leave a half-written
        # conversation that the next request would replay to the API.
        chat_store.replace_history(session_id, history)
        chat_store.set_title_if_unset(session_id, question)
    except Exception as exc:
        queue.put(Event("error", message=f"{type(exc).__name__}: {exc}"))
    finally:
        queue.put(_SENTINEL)


@router.post("/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    """Stream one turn as SSE. Creates a session when `session_id` is omitted."""
    # Refused before a session is created or a stream opened, so a switched-off coach or a missing
    # key is a plain error the page can show, not a half-open stream that ends in a stack trace.
    refused = coach_settings.refusal()
    if refused:
        raise HTTPException(*refused)
    structured = req.mode != "conversation"
    if structured and req.session_id is not None:
        raise HTTPException(422, f"a {req.mode} always starts a new session")
    if not structured and not (req.question or "").strip():
        raise HTTPException(422, "a question is required")
    if req.mode == "cycle_review" and _pending_review_cycle() is None:
        raise HTTPException(409, "There is no closed block waiting for a review.")

    session_id = req.session_id
    if session_id is not None and chat_store.session_exists(session_id) \
            and chat_store.is_closed(session_id):
        # Conversations lock when they close, so a check-in stays a discrete event.
        raise HTTPException(409, "This conversation has ended. Start a new one to keep talking.")
    if session_id is None:
        session_id = chat_store.new_session(
            coach_settings.model(), mode=req.mode,
            title={"checkin": "Weekly check-in", "cycle_review": "Block review"}.get(req.mode))
    elif not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")

    queue: Queue = Queue()
    loop = asyncio.get_running_loop()
    if structured:
        loop.run_in_executor(_EXECUTOR, _run_structured, session_id, req.mode, req.note, queue)
    else:
        loop.run_in_executor(_EXECUTOR, _run_and_enqueue, session_id, req.question, queue)

    async def events():
        # Tells the client which session this is before any model output, so a page that started
        # without an id can store it and resume later.
        yield _sse({"type": "session", "session_id": session_id})
        while True:
            item = await loop.run_in_executor(None, queue.get)
            if item is _SENTINEL:
                break
            yield _sse(item)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this a reverse proxy will buffer the stream and deliver it in one lump,
            # which is exactly the behaviour this phase exists to remove.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/due")
def due_status() -> dict:
    """Whether a weekly check-in or a block review is worth doing now, and why."""
    with read_connection() as conn:
        return due.status(conn, today_local())


# ── what the coach remembers ──────────────────────────────────────────────────

class NoteBody(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


def _note(conn, note_id: int):
    row = conn.execute("SELECT id, session_id, type FROM coaching_log WHERE id = ?",
                       (note_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no note {note_id}")
    if row["type"] != "session_summary":
        # A check-in is a record of what was said to the athlete and the due-status reads it;
        # only the notes the coach wrote *about a conversation* are the athlete's to correct.
        raise HTTPException(403, "only a conversation note can be changed here")
    return row


@router.get("/memory")
def memory_list(limit: int = 15) -> dict:
    """
    What the coach has on file, newest first, and which of it is in its briefing right now
    (`shown`). The briefing selection is the one the coach is actually given, not a copy.
    """
    shown = {e["id"] for e in coaching_memory_entries()}
    with read_connection() as conn:
        rows = conn.execute(
            "SELECT id, session_id, date, type, content FROM coaching_log "
            "WHERE type IN ('checkin', 'session_summary') ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),)).fetchall()
    return {"per_type": MEMORY_PER_TYPE,
            "entries": [{**dict(r), "shown": r["id"] in shown,
                         "editable": r["type"] == "session_summary"} for r in rows]}


@router.put("/memory/{note_id}")
def memory_edit(note_id: int, body: NoteBody) -> dict:
    """Correct a conversation note: the coach will be told the corrected version."""
    text = body.content.strip()
    if not text:
        raise HTTPException(422, "a note cannot be empty; delete it instead")
    with write_connection() as conn:
        row = _note(conn, note_id)
        conn.execute("UPDATE coaching_log SET content = ? WHERE id = ?", (text, note_id))
        conn.execute("UPDATE chat_sessions SET summary = ? WHERE session_id = ?",
                     (text, row["session_id"]))
    return {"id": note_id, "content": text}


@router.delete("/memory/{note_id}")
def memory_delete(note_id: int) -> dict:
    """
    Make the coach forget a conversation note. The conversation itself stays; only the note goes,
    and it is not rewritten unless the conversation later continues.
    """
    with write_connection() as conn:
        row = _note(conn, note_id)
        conn.execute("DELETE FROM coaching_log WHERE id = ?", (note_id,))
        conn.execute("UPDATE chat_sessions SET summary = NULL WHERE session_id = ?",
                     (row["session_id"],))
    return {"deleted": note_id}


@router.get("/status")
def status() -> dict:
    """Whether the coach can run, and on what: the page reads this before offering a chat."""
    return coach_settings.status()


@router.get("/sessions")
def sessions(limit: int = 25) -> list[dict]:
    return chat_store.list_sessions(limit=limit)


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    if not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")
    return {"session_id": session_id, "closed": chat_store.is_closed(session_id),
            "messages": chat_store.load_history(session_id)}


@router.post("/sessions/{session_id}/end")
def session_end(session_id: str) -> dict:
    if not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")
    chat_store.end_session(session_id)
    # The note is written in the background: it is one model call, and the athlete who just
    # started a new conversation should not wait for it. If it fails, the conversation is still
    # stale and is caught up the next time anything starts.
    _EXECUTOR.submit(_summarise_quietly, session_id)
    return {"ended": session_id}

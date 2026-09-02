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
from typing import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.analysis.report import coaching_context_text
from src.coach import chat_store
from src.coach.prompt import build_system_prompt
from src.coach.session import DEFAULT_MODEL, MAX_TOKENS, _client
from src.coach.stream import Event, stream_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])

# The Anthropic SDK is synchronous, so a turn runs on a worker thread and its events are handed
# to the event loop through a queue. Single-user app: one worker is enough and keeps the
# SQLite write path serialised.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="coach")

# Sessions are recreated from the database on demand, so this only caches the expensive
# system-prompt build for the life of the process.
_SYSTEM_CACHE: dict[str, str] = {}

_SENTINEL = object()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


def _system_for(session_id: str) -> str:
    if session_id not in _SYSTEM_CACHE:
        _SYSTEM_CACHE[session_id] = build_system_prompt(
            coaching_context_text(), mode="conversation")
    return _SYSTEM_CACHE[session_id]


def _sse(event: Event | dict) -> str:
    payload = event.to_json() if isinstance(event, Event) else json.dumps(event)
    return f"data: {payload}\n\n"


def _run_and_enqueue(session_id: str, question: str, queue: Queue) -> None:
    """Run one turn on a worker thread, pushing events onto the queue as they arrive."""
    try:
        history = chat_store.load_history(session_id)
        history.append({"role": "user", "content": question})
        system = _system_for(session_id)

        for event in stream_turn(_client(), DEFAULT_MODEL, system, history, MAX_TOKENS):
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
    session_id = req.session_id
    if session_id is None:
        session_id = chat_store.new_session(DEFAULT_MODEL)
    elif not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")

    queue: Queue = Queue()
    loop = asyncio.get_running_loop()
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


@router.get("/sessions")
def sessions(limit: int = 25) -> list[dict]:
    return chat_store.list_sessions(limit=limit)


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    if not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")
    return {"session_id": session_id, "messages": chat_store.load_history(session_id)}


@router.post("/sessions/{session_id}/end")
def session_end(session_id: str) -> dict:
    if not chat_store.session_exists(session_id):
        raise HTTPException(404, f"unknown session {session_id}")
    chat_store.end_session(session_id)
    _SYSTEM_CACHE.pop(session_id, None)
    return {"ended": session_id}

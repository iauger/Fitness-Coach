"""
Persistence for chat sessions — phase 12C.

`CoachSession` kept its history in process memory, which suits a CLI that exits when the
conversation ends and is impossible for a web UI where the client can reload at any moment.
These functions store each turn as it happens so a conversation can be resumed by id.

Content is stored as JSON, not text, because a turn is not always a string: an assistant turn
that used a tool is a list of content blocks, and those must round-trip exactly or the next
request to the API is malformed.
"""

import json
import uuid
from datetime import datetime

from src.db.schema import get_connection


def new_session(model: str, mode: str = "conversation", title: str | None = None) -> str:
    session_id = str(uuid.uuid4())[:8]
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO chat_sessions (session_id, started_at, updated_at, model, mode, title) "
            "VALUES (?, ?, ?, ?, ?, ?)", (session_id, now, now, model, mode, title))
    conn.close()
    return session_id


def session_exists(session_id: str) -> bool:
    conn = get_connection()
    try:
        return conn.execute("SELECT 1 FROM chat_sessions WHERE session_id = ?",
                            (session_id,)).fetchone() is not None
    finally:
        conn.close()


def load_history(session_id: str) -> list[dict]:
    """The conversation in the exact shape the Messages API expects."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY seq",
            (session_id,)).fetchall()
    finally:
        conn.close()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]


def replace_history(session_id: str, messages: list[dict]) -> None:
    """
    Persist the whole conversation, replacing what was stored.

    Written wholesale rather than appended because the agentic loop mutates the message list in
    place — a single question can add an assistant turn, a tool-result turn, another assistant
    turn, and so on. Rewriting the run keeps the stored history exactly what the next API call
    will send, which is the only version that matters.
    """
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO chat_messages (session_id, seq, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(session_id, i, m["role"], json.dumps(m["content"]), now)
             for i, m in enumerate(messages)])
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                     (now, session_id))
    conn.close()


def set_title_if_unset(session_id: str, text: str) -> None:
    """Name a session after its opening question, so a session list is readable."""
    title = " ".join(text.split())[:80]
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE chat_sessions SET title = ? WHERE session_id = ? AND "
            "(title IS NULL OR title = '')", (title, session_id))
    conn.close()


def list_sessions(limit: int = 25) -> list[dict]:
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT s.session_id, s.started_at, s.updated_at, s.model, s.mode, s.title, "
            "       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.session_id "
            "        AND m.role = 'user') AS turns "
            "FROM chat_sessions s ORDER BY s.updated_at DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def end_session(session_id: str) -> None:
    conn = get_connection()
    with conn:
        conn.execute("UPDATE chat_sessions SET ended_at = ? WHERE session_id = ?",
                     (datetime.utcnow().isoformat(), session_id))
    conn.close()

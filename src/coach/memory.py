"""
What the coach remembers from a conversation.

A conversation is stored in full (`chat_sessions` / `chat_messages`), and once it is over it is
read once by the extractor (`extract.py`): what the athlete said about themselves becomes
signals in the ledger, and a gist of at most 40 words goes into `coaching_log` as a
`session_summary`, with any follow-ups on the conversation itself. This module is the only thing
that does that, whether the conversation happened in the app or on the command line.

It used to be two mechanisms. The command line wrote a note when you typed `quit`; the app's
conversations were never summarised, so anything said to the coach in the app was forgotten by
the next one. And the command line's note was written by re-running the whole coaching turn
loop on the whole conversation, with the full athlete snapshot and every tool available: a
request to "summarise" could have called `update_goal`, and it paid for ~8,000 tokens of
context to produce five bullets. Filing a conversation is a classification job. It gets its own
short prompt, sees only the athlete's words, and its one tool is never run.

**When a note is written.** Once, when the conversation closes. A conversation closes when the
athlete ends it or starts a new one, when the command line quits, or when it has been idle for
`chat_store.IDLE_CLOSE_MINUTES` (closed as of its last message, the next time anything asks). A
closed conversation is locked: it can be read, never continued, so a note is never out of date and
is never written twice. One whose attempt failed stays unnoted and is picked up at the next
catch-up, which runs when a conversation or check-in begins.

**What is not remembered.** A conversation with nothing the athlete said worth carrying forward
gets no note, and neither does one that is only a server-written check-in opening: the check-in
is already filed on its own.
"""

import json
from datetime import datetime
from src import applog

from src.clock import local_date_of, utc_now_iso
from src.coach import chat_store, extract
from src.coach import settings as coach_settings
from src.db.schema import get_connection, write_connection

# What the athlete said has to be at least this long to be worth reading ("hi" is not).
MIN_CHARS = 20


def athlete_turns(messages: list[dict], mode: str = "conversation") -> list[str]:
    """
    What the athlete actually typed.

    Tool results come back as user-role messages and are not the athlete. And in a check-in or
    a block review the first user message is written by the server ("Give me my weekly
    check-in."), so it is dropped: a check-in with no follow-up has nothing of the athlete's in it.
    """
    turns = [m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str)]
    return turns[1:] if mode != "conversation" else turns


def eligible(messages: list[dict], mode: str = "conversation") -> bool:
    return any(len(t.strip()) >= MIN_CHARS for t in athlete_turns(messages, mode))


def _client():
    import os

    from anthropic import Anthropic

    from src.coach.settings import api_key
    return Anthropic(api_key=api_key())


def _write(session_id: str, summary: str | None, model: str, local_date: str,
           followups: list[str] | None = None) -> None:
    """Record the outcome. NULL means "looked at it, nothing to keep" and still counts as done."""
    now = utc_now_iso()
    with write_connection() as conn:
        conn.execute("UPDATE chat_sessions SET summary = ?, summarised_at = ?, followups = ? "
                     "WHERE session_id = ?",
                     (summary, now, json.dumps(followups) if followups else None, session_id))
        # One note per conversation: a re-summary replaces the old one, and a conversation that
        # now has nothing notable must not leave a stale note behind.
        conn.execute("DELETE FROM coaching_log WHERE type = 'session_summary' AND session_id = ?",
                     (session_id,))
        if summary:
            conn.execute(
                "INSERT INTO coaching_log (session_id, date, type, model, content, created_at) "
                "VALUES (?, ?, 'session_summary', ?, ?, ?)",
                (session_id, local_date, model, summary, now))


def summarise(session_id: str, client=None) -> tuple[str | None, bool]:
    """
    `(gist, called_the_model)` for one conversation. The gist is None when there is nothing
    worth keeping; the second value says whether that cost an API call. Signals are filed as a
    side effect, once per conversation.
    """
    conn = get_connection()
    try:
        row = conn.execute("SELECT mode, model, updated_at FROM chat_sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError(f"unknown session {session_id}")

    messages = chat_store.load_history(session_id)
    when = local_date_of(row["updated_at"]).isoformat()
    if not eligible(messages, row["mode"]):
        _write(session_id, None, row["model"] or "", when)
        return None, False

    client = client or _client()
    # Only the athlete's words reach the model, and its one tool is never run: it can file, not act.
    text = "\n\n".join(t.strip() for t in athlete_turns(messages, row["mode"]) if t.strip())
    found = extract.extract(text, when, client)
    extract.file_signals(found, source="conversation", source_ref=session_id,
                         observed_at=row["updated_at"])
    note = found.gist or None
    _write(session_id, note, coach_settings.extractor_model(), when, found.followups)
    return note, True


def summarise_session(session_id: str, client=None) -> str | None:
    return summarise(session_id, client)[0]


def stale_sessions(idle_minutes: int = chat_store.IDLE_CLOSE_MINUTES,
                   now: datetime | None = None) -> list[str]:
    """
    Closed conversations that have no note yet, oldest first.

    A conversation is noted once, when it closes, and a closed one never continues, so there is
    no "out of date" case. Idle ones are closed first (as of their last message), so a
    conversation still being had is never summarised from under the athlete. A failed attempt
    leaves it unnoted, and it is picked up again here.
    """
    chat_store.close_idle(idle_minutes, now.replace(tzinfo=None) if now and now.tzinfo else now)
    conn = get_connection()
    try:
        rows = conn.execute("SELECT session_id FROM chat_sessions "
                            "WHERE ended_at IS NOT NULL AND summarised_at IS NULL "
                            "ORDER BY ended_at").fetchall()
    finally:
        conn.close()
    return [r["session_id"] for r in rows]


def summarise_pending(idle_minutes: int = chat_store.IDLE_CLOSE_MINUTES, limit: int = 3, client=None,
                      on_error=None) -> list[str]:
    """
    Catch up on abandoned conversations before a new one begins. Best-effort and bounded: at
    most `limit` model calls, and a failure is reported through `on_error` rather than raised,
    because the athlete asked for a conversation, not for housekeeping.
    """
    done, calls = [], 0
    for session_id in stale_sessions(idle_minutes):
        if calls >= limit:
            break
        try:
            note, called = summarise(session_id, client)
        except Exception as exc:
            applog.warning("memory.summarise_failed", exc)
            if on_error:
                on_error(session_id, exc)
            continue
        calls += int(called)
        if note:
            done.append(session_id)
    return done


def import_legacy() -> int:
    """
    Bring the command line's old transcripts into the conversation tables. Idempotent.

    Before the two mechanisms were joined the command line kept a JSON file and a `transcripts`
    row per conversation. Those become ordinary conversations, with the note they already had,
    so the old ones can be opened and managed beside the new. The `coaching_log` rows are left
    as they are: they are already the memory.
    """
    import json
    from pathlib import Path

    conn = get_connection()
    try:
        have = {r[0] for r in conn.execute("SELECT session_id FROM chat_sessions")}
        legacy = [dict(r) for r in conn.execute("SELECT * FROM transcripts")]
    finally:
        conn.close()

    imported = 0
    for t in legacy:
        if t["session_id"] in have:
            continue
        messages = []
        if t.get("file_path") and Path(t["file_path"]).exists():
            try:
                messages = json.loads(Path(t["file_path"]).read_text(encoding="utf-8"))["messages"]
            except (ValueError, KeyError, OSError):
                messages = []
        first = next((m["content"] for m in messages
                      if m["role"] == "user" and isinstance(m["content"], str)), "")
        ended = t.get("ended_at") or t["started_at"]
        with write_connection() as c:
            c.execute(
                "INSERT INTO chat_sessions (session_id, started_at, updated_at, ended_at, model, "
                "mode, title, summary, summarised_at) VALUES (?, ?, ?, ?, ?, 'conversation', ?, ?, ?)",
                (t["session_id"], t["started_at"], ended, t.get("ended_at"), t.get("model"),
                 " ".join(first.split())[:80] or None, t.get("summary"), ended))
            c.executemany(
                "INSERT INTO chat_messages (session_id, seq, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(t["session_id"], i, m["role"], json.dumps(m["content"]), t["started_at"])
                 for i, m in enumerate(messages)])
        imported += 1
    return imported

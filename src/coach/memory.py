"""
What the coach remembers from a conversation.

A conversation is stored in full (`chat_sessions` / `chat_messages`), and once it is over a
short note of it goes into `coaching_log` as a `session_summary`, which is what the next
conversation and the next check-in are briefed from. This module is the only thing that writes
that note, whether the conversation happened in the app or on the command line.

It used to be two mechanisms. The command line wrote a note when you typed `quit`; the app's
conversations were never summarised, so anything said to the coach in the app was forgotten by
the next one. And the command line's note was written by re-running the whole coaching turn
loop on the whole conversation, with the full athlete snapshot and every tool available: a
request to "summarise" could have called `update_goal`, and it paid for ~8,000 tokens of
context to produce five bullets. A note is a compression job. It gets its own short prompt and
no tools.

**When a note is written.** There is no "end" in the app, since conversations resume, so three
things stand in for one: the athlete starting a new conversation (the previous one is over);
a conversation that has been idle for `IDLE_MINUTES` when the next conversation or check-in
begins (an abandoned one is caught up then); and the command line's `quit`. A note is written
again if the conversation continued afterwards (`summarised_at < updated_at`), replacing the old.

**What is not remembered.** A conversation with nothing the athlete said worth carrying forward
gets no note, and neither does one that is only a server-written check-in opening: the check-in
is already filed on its own.
"""

import os
from datetime import datetime, timedelta, timezone

from src.clock import local_date_of, utc_now_iso
from src.coach import chat_store
from src.coach import settings as coach_settings
from src.db.schema import get_connection, write_connection

IDLE_MINUTES = 30
# What the athlete said has to be at least this long to be worth a note ("hi" is not).
MIN_CHARS = 20
# The tail of a very long conversation, not the whole of it, is what reaches the model.
MAX_TRANSCRIPT_CHARS = 24000
MAX_TOKENS = 500
NOTHING = "NOTHING NOTABLE"

# Tools that change a record. A read-only lookup is not worth a line in the note; a write is
# something the athlete should be able to see was acted on.
WRITE_TOOLS = {"log_life_event", "log_subjective_feel", "update_goal"}

SYSTEM = f"""
You take notes on a conversation between an endurance athlete and their coach, for the coach to
read in a later conversation when the transcript is no longer in front of them.

Write four to six short hyphen bullets. Keep what only this conversation can tell a coach: what
the athlete shared about how they feel, their life, injuries, sleep or motivation; decisions or
priorities they settled on; anything the coach recorded (lines marked "[coach recorded ...]" are
real records); and any follow-up worth raising next time. Keep the athlete's own framing.

Drop anything the database already holds: do not restate training load, fitness numbers, HRV,
adherence or session counts. Do not give advice and do not coach: you are taking notes.

If the athlete said nothing worth carrying forward, reply with exactly: {NOTHING}
""".strip()


def _parse(stamp: str | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime. They are naive UTC in the database."""
    if not stamp:
        return None
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _visible_text(content) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text").strip()


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


def render_transcript(messages: list[dict]) -> str:
    """The conversation as plain labelled lines: what was said, and which records were changed."""
    lines = []
    for m in messages:
        content = m["content"]
        if m["role"] == "user" and isinstance(content, str):
            lines.append(f"ATHLETE: {content}")
        elif m["role"] == "assistant":
            text = _visible_text(content)
            if text:
                lines.append(f"COACH: {text}")
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_use" \
                        and block.get("name") in WRITE_TOOLS:
                    lines.append(f"[coach recorded: {block['name']} {block.get('input', {})}]")
    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:]


def clean_note(text: str) -> str | None:
    """
    The note, or None if the model said there is nothing to keep.

    The instruction is to reply with exactly NOTHING NOTABLE when there is nothing. Models do
    not always keep to "exactly": one wrote real bullets and then added the phrase on a line of
    its own, and it was stored as part of the note and shown to the athlete. So the marker is
    removed wherever it stands, as a line, and what remains is the note (possibly nothing).
    """
    kept = [line for line in (text or "").splitlines()
            if line.strip().strip("-*. ").upper() != NOTHING]
    note = "\n".join(kept).strip()
    return note or None


def _client():
    from anthropic import Anthropic
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _write(session_id: str, summary: str | None, model: str, local_date: str) -> None:
    """Record the outcome. NULL means "looked at it, nothing to keep" and still counts as done."""
    now = utc_now_iso()
    with write_connection() as conn:
        conn.execute("UPDATE chat_sessions SET summary = ?, summarised_at = ? WHERE session_id = ?",
                     (summary, now, session_id))
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
    `(note, called_the_model)` for one conversation. The note is None when there is nothing
    worth keeping; the second value says whether that cost an API call.
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
    model = coach_settings.model()
    # No tools, a short system prompt, and only the words: the cheap path, and one that cannot
    # act on anything it reads.
    reply = client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=SYSTEM,
        messages=[{"role": "user", "content": render_transcript(messages)}])
    text = " ".join(b.text for b in reply.content if hasattr(b, "text")).strip()
    note = clean_note(text)
    _write(session_id, note, model, when)
    return note, True


def summarise_session(session_id: str, client=None) -> str | None:
    return summarise(session_id, client)[0]


def stale_sessions(idle_minutes: int = IDLE_MINUTES, now: datetime | None = None) -> list[str]:
    """
    Conversations that have gone quiet and whose note is missing or out of date, oldest first.

    "Out of date" is `summarised_at < updated_at`: the conversation continued after it was
    summarised. Idle is measured from the last message, so a conversation still being had is
    never summarised from under the athlete.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=idle_minutes)
    conn = get_connection()
    try:
        rows = conn.execute("SELECT session_id, updated_at, summarised_at FROM chat_sessions "
                            "ORDER BY updated_at").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        updated, done = _parse(r["updated_at"]), _parse(r["summarised_at"])
        if updated is None or updated > cutoff:
            continue
        if done is None or done < updated:
            out.append(r["session_id"])
    return out


def summarise_pending(idle_minutes: int = IDLE_MINUTES, limit: int = 3, client=None,
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

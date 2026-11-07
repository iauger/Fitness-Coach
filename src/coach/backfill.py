"""
Read what the athlete has already written and file it as signals.

Three places hold the athlete's own words from before the ledger existed: the notes on a feel log,
the note on a ride, and finished conversations. Each becomes one extractor call. A source that was
already filed is skipped, so this is safe to run again, and a conversation keeps the note it
already has (only its signals are added).

Nothing here is a model of the athlete's week: the coach's own weekly narratives are written by a
model, not by the athlete, so they are not read.
"""

from dataclasses import dataclass

from src import signals
from src.clock import local_date_of
from src.coach import chat_store, extract, memory
from src.db.schema import get_connection


@dataclass
class Item:
    source: str
    ref: str
    day: str
    text: str
    observed_at: str | None = None


def collect() -> list[Item]:
    """Everything the athlete wrote that has not been filed yet, oldest first."""
    items: list[Item] = []
    conn = get_connection()
    try:
        seen = set()
        for r in conn.execute("SELECT id, date, notes, logged_at FROM subjective_feel "
                              "WHERE notes IS NOT NULL AND TRIM(notes) != '' ORDER BY date, id"):
            if (r["date"], r["notes"]) in seen:           # the same note logged twice is one note
                continue
            seen.add((r["date"], r["notes"]))
            items.append(Item("feel_note", str(r["id"]), r["date"], r["notes"], r["logged_at"]))
        for r in conn.execute("SELECT id, date, athlete_note FROM activities "
                              "WHERE athlete_note IS NOT NULL AND TRIM(athlete_note) != '' "
                              "ORDER BY date, id"):
            items.append(Item("ride_note", str(r["id"]), r["date"], r["athlete_note"]))
        for r in conn.execute("SELECT session_id, mode, updated_at FROM chat_sessions "
                              "WHERE ended_at IS NOT NULL ORDER BY ended_at"):
            messages = chat_store.load_history(r["session_id"])
            if not memory.eligible(messages, r["mode"]):
                continue
            text = "\n\n".join(t.strip() for t in memory.athlete_turns(messages, r["mode"]) if t.strip())
            items.append(Item("conversation", r["session_id"], local_date_of(r["updated_at"]).isoformat(),
                              text, r["updated_at"]))
    finally:
        conn.close()
    return [i for i in items if not signals.recorded(i.source, i.ref)]


def run(client, *, apply: bool, items: list[Item] | None = None, on_item=None) -> list[tuple[Item, extract.Extraction]]:
    """Extract from each item and, if `apply`, file it. Returns what was found either way."""
    done = []
    for item in items if items is not None else collect():
        found = extract.extract(item.text, item.day, client)
        if apply:
            extract.file_signals(found, source=item.source, source_ref=item.ref,
                                 observed_at=item.observed_at)
        done.append((item, found))
        if on_item:
            on_item(item, found)
    return done

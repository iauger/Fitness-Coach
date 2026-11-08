"""
Reading what the athlete said and filing it as signals.

One model call per piece of the athlete's own writing: a closed conversation, a ride note, a
feel note. The model is given only the athlete's words (never the coach's, which would let it
file the coach's guesses as the athlete's feelings), the day it is, and the threads already open,
so it can continue one instead of starting a duplicate. It has one tool and is forced to call it:
the tool is never executed, it is simply the way to get validated JSON back.

Three guards stand between the model and the ledger, because a wrong entry here is a wrong
sentence in the coach's mouth later:

- **A quote, word for word.** Every signal must carry a quote that appears in the text. One that
  does not is discarded. This is the cheap defence against invented signals, and it is also what
  makes every row checkable by the athlete at a glance.
- **Code validates** (`signals.clean`): a bad severity or state is dropped, not repaired; an
  unknown kind becomes `other`.
- **Dates are code's too.** The model reports a date only for what it refers to; one that is
  malformed is replaced by the day of writing, and one in the future is moved into
  `start_date`, because a signal is an observation and cannot have been observed tomorrow.

Nothing here changes a prescription or calls a coaching tool.
"""

from dataclasses import dataclass, field
from datetime import date
import time

from src import signals
from src import applog
from src.coach import settings as coach_settings

VERSION = "x1"
MAX_TOKENS = 1500
GIST_WORDS = 40
FOLLOWUP_WORDS = 25
MAX_FOLLOWUPS = 4
MIN_QUOTE_CHARS = 4
MAX_TEXT_CHARS = 24000

TOOL = "record_signals"

SYSTEM = f"""
You read what an endurance athlete wrote or said to their coach and file the facts about how they,
their life and their training are going. You are a filing clerk: you do not coach, advise or
reassure, and you only file what the athlete themselves said.

Call {TOOL} exactly once.

signals: one entry per distinct thing the athlete said about themselves. Rules:
- "quote" is copied word for word from the athlete's text, a short span (a clause or sentence) that
  shows the signal. If you cannot quote it, do not file it.
- Training numbers (power, TSS, durations) are not signals; the database has them. Questions to
  the coach are not signals unless they reveal how the athlete is doing; a request that reveals a
  constraint or preference ("Saturdays won't work for the next month") is one. Something said
  about another person is a signal only where it affects the athlete (a sick child that costs
  sleep is family_caregiving).
- Comments on the training plan, the prescribed intensity or a fitness estimate are not signals
  about the athlete, unless they say how the sessions felt or what they cost. Hopes about future
  fitness are not feeling_strong; that means the athlete's own state now.
- File each distinct thing once. Several quotes about one problem are one signal.
- The kinds, where they are close: work_event is a specific work demand (a deadline, long hours,
  a work trip); stress is general strain not tied to one event; family_caregiving is family
  demands on time or sleep; travel is being away from the usual setup, for any reason;
  schedule_change is a change to when the athlete can train, or a request to move sessions;
  missed_sessions is sessions skipped and why; wants_more / wants_less are requests for more or
  less training; goal_change is a change of target or event; preference is how the athlete likes
  to train.
- "kind" is the closest of the allowed kinds; use "other" only if none fits.
- "topic" is two to four words naming the specific thing ("left knee", "night shifts", "evening
  fuelling"). If a thread is already open for the same thing, reuse its kind and topic exactly.
- "severity": 0 a passing mention, not affecting anything; 1 worth knowing; 2 affecting training
  (missing or changing sessions, or a persistent problem); 3 needs action now (illness, injury or
  pain that stops training, burnout, or language about quitting). Grade what the athlete says,
  not what you would worry about. A good week is severity 0 or 1, never higher.
- "state": new, or ongoing / improving / resolved when the athlete says so or continues an open
  thread.
- "on_date" is the day the signal is about, YYYY-MM-DD, resolved from today's date ("last week" is
  a day last week). Omit it when it is today. "start_date" / "end_date" only for an episode with
  stated limits ("away 10th to 17th", "travelling next week").
- Positive signals count: feeling_strong, motivation_high, workouts_too_easy.
- If the text holds nothing worth filing, send an empty list. That is a normal outcome.

followups: things the athlete committed to or asked the coach to raise later, each under
{FOLLOWUP_WORDS} words, at most {MAX_FOLLOWUPS}. Empty if none.

gist: what this text tells a coach, in at most {GIST_WORDS} words, in the athlete's framing. No
numbers the database already holds.
""".strip()


def _schema() -> dict:
    return {
        "name": TOOL,
        "description": "File the signals, follow-ups and gist for this text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "signals": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(signals.KINDS)},
                        "topic": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 0, "maximum": 3},
                        "state": {"type": "string", "enum": list(signals.STATES)},
                        "quote": {"type": "string"},
                        "on_date": {"type": "string"},
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                    },
                    "required": ["kind", "severity", "quote"],
                }},
                "followups": {"type": "array", "items": {"type": "string"}},
                "gist": {"type": "string"},
            },
            "required": ["signals", "followups", "gist"],
        },
    }


@dataclass
class Extraction:
    signals: list[dict] = field(default_factory=list)       # cleaned, ready for `signals.record`
    rejected: list[dict] = field(default_factory=list)      # what was dropped, and why
    followups: list[str] = field(default_factory=list)
    gist: str = ""


_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", "…": "..."})


def _flat(text: str) -> str:
    return " ".join((text or "").translate(_QUOTES).casefold().split())


def quoted(quote: str, text: str) -> bool:
    """Whether `quote` appears in `text`, ignoring case, whitespace and typographic punctuation."""
    q = _flat(quote).strip(" .")
    return len(q) >= MIN_QUOTE_CHARS and q in _flat(text)


def _words(text: str, limit: int) -> str:
    return " ".join((text or "").split()[:limit])


def _day(value, fallback: str) -> str:
    """A valid ISO date, or `fallback` for a missing or malformed one: the signal is kept, the date is not trusted."""
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return fallback


def _open_threads(rows: list[dict]) -> str:
    if not rows:
        return "No threads are open."
    return "Open threads (reuse the kind and topic to continue one):\n" + "\n".join(
        f"- {t['kind']} / {t['topic'] or '(no topic)'}: {t['state']}, last mentioned {t['last_seen']}"
        for t in rows[:12])


def parse(raw: dict, text: str, today: str) -> Extraction:
    """Turn the model's tool input into an Extraction, applying every guard."""
    out = Extraction(gist=_words(str(raw.get("gist") or ""), GIST_WORDS))
    out.followups = [_words(str(f), FOLLOWUP_WORDS) for f in (raw.get("followups") or [])
                     if str(f).strip()][:MAX_FOLLOWUPS]
    for item in raw.get("signals") or []:
        if not isinstance(item, dict):
            continue
        if not quoted(str(item.get("quote") or ""), text):
            out.rejected.append({**item, "why": "quote is not in the text"})
            continue
        on_date = _day(item.get("on_date"), today)
        start = item.get("start_date") or None
        if on_date > today:                      # a future date is a plan, not a sighting
            on_date, start = today, start or on_date
        try:
            row = signals.clean(
                item.get("kind", ""), topic=item.get("topic") or "", severity=item.get("severity"),
                state=item.get("state") or "new", quote=str(item["quote"]),
                on_date=on_date, start_date=start, end_date=item.get("end_date") or None)
        except signals.SignalError as e:
            out.rejected.append({**item, "why": str(e)})
            continue
        out.signals.append(row)
    return _once(out)


def _once(found: Extraction) -> Extraction:
    """One signal per kind and topic in a single text: the most severe wins, the first quote stays."""
    kept: dict[tuple[str, str], dict] = {}
    for row in found.signals:
        key = (row["kind"], row["topic"])
        if key not in kept:
            kept[key] = row
        elif row["severity"] > kept[key]["severity"]:
            kept[key] = {**row, "quote": kept[key]["quote"]}
    found.signals = list(kept.values())
    return found


def extract(text: str, today: str, client, model: str | None = None,
            open_threads: list[dict] | None = None) -> Extraction:
    """
    One forced-tool call over the athlete's own words, returning the guarded result.

    `open_threads` defaults to what the ledger holds; the evaluation passes `[]` so that scoring
    the gold set neither reads nor depends on the athlete's real database.
    """
    text = (text or "").strip()[-MAX_TEXT_CHARS:]
    if open_threads is None:
        open_threads = signals.recent_threads(today)
    started = time.monotonic()
    reply = client.messages.create(
        model=model or coach_settings.extractor_model(), max_tokens=MAX_TOKENS, system=SYSTEM,
        tools=[_schema()], tool_choice={"type": "tool", "name": TOOL},
        messages=[{"role": "user", "content":
                   f"Today is {today}.\n\n{_open_threads(open_threads)}\n\n"
                   f"The athlete's words:\n<<<\n{text}\n>>>"}])
    raw = next((b.input for b in reply.content
                if getattr(b, "type", "") == "tool_use" and b.name == TOOL), None)
    usage = getattr(reply, "usage", None)
    if not isinstance(raw, dict):
        applog.warning("extract.no_filing", model=model or coach_settings.extractor_model())
        raise RuntimeError("the extractor did not return a filing")
    found = parse(raw, text, today)
    # What the extractor did, as counts. `rejected` is the quote guard at work: a rising number means
    # the model is paraphrasing instead of quoting, which is the early sign of a prompt gone stale.
    applog.event("extract.done", model=model or coach_settings.extractor_model(),
                 signals=len(found.signals), rejected=len(found.rejected),
                 input_tokens=getattr(usage, "input_tokens", None),
                 output_tokens=getattr(usage, "output_tokens", None),
                 ms=int((time.monotonic() - started) * 1000))
    return found


def file_signals(found: Extraction, *, source: str, source_ref: str, observed_at: str | None = None) -> int:
    """Write what was extracted. A source already filed is left alone, so a retry cannot double it."""
    if signals.recorded(source, source_ref):
        return 0
    for row in found.signals:
        fields = dict(row)
        signals.record(fields.pop("kind"), source=source, source_ref=source_ref,
                       extractor_version=VERSION, observed_at=observed_at, **fields)
    return len(found.signals)

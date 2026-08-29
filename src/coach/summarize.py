"""
The one place a model is used for compression rather than coaching.

Item 10's cost mechanic: without summarization, every check-in re-injects raw history for
however far back it looks, so the same weeks are paid for again on every future call.
Summarizing once converts that recurring cost into a one-time one.

The split that keeps it cheap: numbers are already compact as numbers, so they are rolled up
in Python for free (analysis/weekly.py). A model is called only to compress *free text* —
the athlete's session notes and check-in comments — and only when a week actually has some.
A week with no notes costs nothing and stores a null narrative.
"""

import os
from anthropic import Anthropic
from dotenv import load_dotenv

from src.analysis.weekly import has_qualitative_content

load_dotenv()

SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", os.environ.get("COACH_MODEL", "claude-haiku-4-5"))
# A weekly narrative is two or three sentences. This is a compression job, not a coaching one,
# so the ceiling is deliberately far below the coach's 2048.
MAX_TOKENS = 400

SYSTEM = """
You compress a week of an endurance athlete's own words into a short factual note that a coach
will read weeks later, when the raw sessions are no longer in front of them.

Keep what only the athlete could tell you: how efforts actually felt, where in a session
fatigue appeared, anything about life, sleep, illness or motivation, and any doubt or question
he raised about the training itself. Preserve his framing rather than reinterpreting it.

Drop anything a database already holds. Do not restate TSS, CTL, adherence percentages, HRV,
or how many sessions he did — those numbers are stored alongside this note and repeating them
wastes the space.

Two or three sentences of plain prose. No headers, no bullets, no bold. Write it as a record,
not as advice — you are not coaching here, you are taking notes. If the week's text says
nothing worth carrying forward, reply with exactly: NOTHING NOTABLE
""".strip()


def _render_qualitative(m: dict) -> str:
    parts = [f"Week of {m['week_start']}."]
    for s in m["sessions"]:
        if s.get("note"):
            rpe = f" (RPE {int(s['rpe'])}/10)" if s.get("rpe") else ""
            parts.append(f"\n{s['date']} — {s['name']}{rpe}\n{s['note']}")
    for s in m["subjective"]:
        if s.get("notes"):
            parts.append(f"\n{s['date']} check-in — {s['notes']}")
    return "\n".join(parts)


def weekly_narrative(m: dict) -> tuple[str | None, str | None]:
    """
    (narrative, model) for one week's rollup. Returns (None, None) without calling the API
    when the week has no free text — which is the common case for a quiet week and is the
    whole reason this is cheap.
    """
    if not has_qualitative_content(m):
        return None, None

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": _render_qualitative(m)}],
    )
    text = " ".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    if not text or text.upper().startswith("NOTHING NOTABLE"):
        return None, SUMMARY_MODEL
    return text, SUMMARY_MODEL

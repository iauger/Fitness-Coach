"""
Claude coaching layer — check-in and conversational modes with tool use.
Model: the COACH_MODEL environment variable if set, else coach.model in the config (src/coach/settings.py).
"""

import os
import json
import uuid
from datetime import date
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

from src.analysis.report import build_coaching_context, coaching_context_text
from src.analysis.cycle import (
    cycle_for_review, cycle_metrics, cycle_context_text, previous_review, save_review,
)
from src.analysis.training_plan import current_week
from src.analysis.weekly import (
    week_metrics, save_summary, last_completed_week, summarized_weeks, has_qualitative_content,
)
from src.coach.summarize import weekly_narrative
from src.coach.prompt import build_system_prompt
from src.coach.settings import model as coach_model
from src.coach.stream import run_turn
from src.db.schema import write_connection
from src.sync import incremental_sync
from src.clock import today_local_iso, utc_now_iso

load_dotenv()

TRANSCRIPT_DIR = Path(__file__).parent.parent.parent / "data" / "transcripts"
# Headroom for the 500-700 word check-in target set in prompt.py. The old 1024 ceiling
# (~750 words) left almost no margin — a longer reasoned answer would have been cut mid-sentence.
MAX_TOKENS = 2048


def _client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _run_tool_loop(client: Anthropic, model: str, system: str,
                   messages: list[dict]) -> str:
    """
    Blocking agentic turn — call Claude, execute any tool calls, append results, repeat until
    the turn ends. Returns the final text.

    Since phase 12C this delegates to coach/stream.py, which runs the same loop as a generator
    of typed events. The loop logic lives there once; this wrapper exists so every CLI caller
    keeps the simple string-returning interface it was written against.
    """
    return run_turn(client, model, system, messages, MAX_TOKENS)


def checkin(feel_context: str = "", verbose: bool = False) -> str:
    """
    Single-shot weekly check-in. Builds full context, runs tool loop, returns response.
    feel_context: optional pre-session subjective input from the user.
    """
    ctx = build_coaching_context()
    snapshot = coaching_context_text(ctx)
    system = build_system_prompt(snapshot, mode="checkin")
    model = coach_model()

    incremental_sync(silent=not verbose)

    if verbose:
        print(f"[coach] model: {model}")
        print(f"[coach] system prompt lines: {len(system.splitlines())}")
        print()

    user_content = "Give me my weekly check-in."
    if feel_context:
        user_content += f"\n\nBefore you start: {feel_context}"

    client = _client()
    messages = [{"role": "user", "content": user_content}]
    response = _run_tool_loop(client, model, system, messages)

    # Summarise the week that just ended, as a byproduct of the check-in rather than as its own
    # scheduled job — the check-in is already the weekly ritual, and this is the moment the
    # week's notes are complete. Best-effort: a summarisation failure must not lose the
    # check-in the athlete just paid for.
    try:
        _summarise_last_week(verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[coach] weekly summary skipped: {e}")

    # persist to coaching_log
    session_id = str(uuid.uuid4())[:8]
    with write_connection() as conn:
        conn.execute("""
            INSERT INTO coaching_log (session_id, date, type, model, content, created_at)
            VALUES (?, ?, 'checkin', ?, ?, ?)
        """, (session_id, today_local_iso(), model, response,
              utc_now_iso()))

    return response


def _summarise_last_week(verbose: bool = False) -> None:
    """Write the previous week's summary if it isn't already stored."""
    week = last_completed_week()
    if week.isoformat() in summarized_weeks():
        return
    m = week_metrics(week)
    if has_qualitative_content(m):
        narrative, model = weekly_narrative(m)
    else:
        narrative, model = None, None
    save_summary(m, narrative, model)
    if verbose:
        print(f"[coach] summarised week of {m['week_start']}"
              + (" (no notes — stats only)" if narrative is None else ""))


def cycle_review(on: date | None = None, verbose: bool = False) -> str | None:
    """
    4-week mesocycle review. Returns None if no cycle has closed yet.

    Deliberately not auto-fired from incremental_sync(): sync runs at the start of every coach
    session, so an automatic trigger would mean surprise API calls, and a cycle whose Monday
    passed without any session being run would be missed silently. Invoked explicitly by
    scripts/cycle_review.py; the weekly check-in points at it when a cycle has just closed.

    Stored in cycle_reviews rather than coaching_log — see the schema comment for why.
    """
    cyc = cycle_for_review(on)
    if cyc is None:
        return None

    incremental_sync(silent=not verbose)

    # Resolved before the API call, not after. This used to sit below _run_tool_loop and
    # subscript current_week(...) directly, which is declared `-> dict | None` — so a cycle
    # whose start Monday is outside any seeded plan raised TypeError *after* the model had been
    # paid for, and the review text was lost with it.
    week = current_week(date.fromisoformat(cyc["start_date"]))
    if week is None:
        raise RuntimeError(
            f"cycle starting {cyc['start_date']} is not inside any seeded training plan, so "
            f"the review cannot be filed against one. Seed training_plan_weeks for that date "
            f"first — reviews are keyed on (plan_name, cycle_start).")
    plan_name = week["plan_name"]

    metrics = cycle_metrics(cyc)
    prev = previous_review(cyc["start_date"])
    # Full athlete context plus the cycle rollup: the review still needs to know goals,
    # timeline and where the plan goes next, not just what happened in the block.
    snapshot = coaching_context_text()
    system = build_system_prompt(
        f"{snapshot}\n\n{'=' * 70}\n\n{cycle_context_text(metrics, prev)}",
        mode="cycle_review",
    )
    model = coach_model()

    if verbose:
        print(f"[coach] model: {model}")
        print(f"[coach] cycle: {cyc['start_date']} .. {cyc['end_date']} "
              f"(weeks {cyc['plan_week_range'][0]}-{cyc['plan_week_range'][1]})")
        print(f"[coach] comparing against: {prev['cycle_start'] if prev else 'no prior review'}")
        print()

    client = _client()
    messages = [{"role": "user", "content":
                 "That block just finished. Give me the cycle review."}]
    response = _run_tool_loop(client, model, system, messages)

    if not (response or "").strip():
        # cycle_reviews is UNIQUE(plan_name, cycle_start), so an empty row is not a harmless
        # no-op: the cycle is thereafter treated as reviewed and never offered again.
        raise RuntimeError(
            f"the model returned no text for the cycle starting {cyc['start_date']}; refusing "
            f"to file an empty review, which would mark the cycle reviewed permanently.")

    save_review(plan_name, metrics, response, model)
    return response


class CoachSession:
    """
    Multi-turn conversational session with tool use and transcript saving.
    """

    def __init__(self, model: str = None, verbose: bool = False):
        self.model = model or coach_model()
        self.verbose = verbose
        self.client = _client()
        self.session_id = str(uuid.uuid4())[:8]
        self.started_at = utc_now_iso()
        self.history: list[dict] = []

        incremental_sync(silent=not verbose)

        ctx = build_coaching_context()
        snapshot = coaching_context_text(ctx)
        self.system = build_system_prompt(snapshot, mode="conversation")

        if verbose:
            print(f"[coach] session {self.session_id}  model={self.model}")

    def ask(self, question: str) -> str:
        self.history.append({"role": "user", "content": question})
        # The loop appends to this list as it goes — assistant turns and tool results included —
        # so the history it leaves behind is the complete, replayable conversation.
        working = list(self.history)
        reply = run_turn(self.client, self.model, self.system, working, MAX_TOKENS)
        # No separate assistant append here. Before 12C the loop returned on end_turn *without*
        # recording the final assistant turn, so this method had to add it as plain text;
        # stream_turn now records every turn as content blocks, and appending again would
        # duplicate the answer in the history sent with the next question.
        self.history = working
        return reply

    def end_session(self) -> str | None:
        """Save transcript, generate summary, store in coaching_log. Returns summary."""
        if not self.history:
            return None

        ended_at = utc_now_iso()
        turns = sum(1 for m in self.history if m["role"] == "user")

        # save raw transcript
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        date_str = today_local_iso()
        transcript_path = TRANSCRIPT_DIR / f"{date_str}-{self.session_id}.json"
        transcript_data = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "model": self.model,
            "messages": self.history,
        }
        transcript_path.write_text(json.dumps(transcript_data, indent=2))

        # generate session summary
        summary_messages = list(self.history) + [{
            "role": "user",
            "content": (
                "Summarize this coaching session in 4-6 bullet points. Focus only on: "
                "key things the athlete shared, decisions or priorities discussed, "
                "anything logged (life events, goals, feel), and any follow-up to watch for. "
                "Be terse — this summary will be injected into future coaching sessions as memory."
            ),
        }]
        summary = _run_tool_loop(
            self.client, self.model, self.system, summary_messages
        )

        # persist everything
        with write_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO transcripts
                (session_id, started_at, ended_at, model, turns, summary, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (self.session_id, self.started_at, ended_at, self.model,
                  turns, summary, str(transcript_path)))
            conn.execute("""
                INSERT INTO coaching_log (session_id, date, type, model, content, created_at)
                VALUES (?, ?, 'session_summary', ?, ?, ?)
            """, (self.session_id, date_str, self.model, summary,
                  utc_now_iso()))

        return summary

    def reset(self) -> None:
        self.history = []

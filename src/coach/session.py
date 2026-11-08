"""
Claude coaching layer — check-in and conversational modes with tool use.
Model: the COACH_MODEL environment variable if set, else coach.model in the config (src/coach/settings.py).
"""

import os
import uuid
from datetime import date
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
from src.coach import chat_store, memory
from src.coach.settings import model as coach_model
from src.coach.stream import run_turn
from src.db.schema import write_connection
from src.sync import incremental_sync
from src.clock import today_local_iso, utc_now_iso

load_dotenv()

# Headroom for the 500-700 word check-in target set in prompt.py, plus the model's own thinking:
# Haiku 5.5 thinks before it answers and that counts against this ceiling (one turn used 3,573
# tokens, 1,400 of them thought). At 2048 two of six test questions were cut off before any text.
MAX_TOKENS = 8192


def _client() -> Anthropic:
    from src.coach.settings import api_key
    return Anthropic(api_key=api_key())


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


def checkin_inputs(feel_context: str = "") -> dict:
    """
    Everything a weekly check-in turn needs: the system prompt, the opening message, the model.

    Shared by the command line and the app, so the two cannot build a different check-in. The
    caller syncs first: the context is built from the database, and building it before the sync
    (as the CLI once did) briefed the coach on data that was about to be replaced.
    """
    system = build_system_prompt(coaching_context_text(build_coaching_context()), mode="checkin")
    user_content = "Give me my weekly check-in."
    if feel_context:
        user_content += f"\n\nBefore you start: {feel_context}"
    return {"system": system, "model": coach_model(),
            "messages": [{"role": "user", "content": user_content}]}


def finish_checkin(response: str, model: str, session_id: str | None = None,
                   verbose: bool = False) -> str:
    """
    Record a completed check-in: summarise the week that just ended, then log it.

    Refuses an empty answer, the same guard the cycle review has: the log row is what the
    due-status reads, so an empty one would silence the next prompt for a week.
    """
    if not (response or "").strip():
        raise RuntimeError("the model returned no text; not recording an empty check-in")

    # Summarise the week that just ended, as a byproduct of the check-in rather than as its own
    # scheduled job — the check-in is already the weekly ritual, and this is the moment the
    # week's notes are complete. Best-effort: a summarisation failure must not lose the
    # check-in the athlete just paid for.
    try:
        _summarise_last_week(verbose=verbose)
    except Exception as e:
        if verbose:
            print(f"[coach] weekly summary skipped: {e}")

    session_id = session_id or str(uuid.uuid4())[:8]
    with write_connection() as conn:
        conn.execute("""
            INSERT INTO coaching_log (session_id, date, type, model, content, created_at)
            VALUES (?, ?, 'checkin', ?, ?, ?)
        """, (session_id, today_local_iso(), model, response, utc_now_iso()))
    return session_id


def checkin(feel_context: str = "", verbose: bool = False) -> str:
    """
    Single-shot weekly check-in. Builds full context, runs tool loop, returns response.
    feel_context: optional pre-session subjective input from the user.
    """
    incremental_sync(silent=not verbose)
    inputs = checkin_inputs(feel_context)

    if verbose:
        print(f"[coach] model: {inputs['model']}")
        print(f"[coach] system prompt lines: {len(inputs['system'].splitlines())}")
        print()

    response = _run_tool_loop(_client(), inputs["model"], inputs["system"], inputs["messages"])
    finish_checkin(response, inputs["model"], verbose=verbose)
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


def cycle_review_inputs(cyc: dict, verbose: bool = False) -> dict:
    """
    What a block review needs for `cyc`: the system prompt (athlete context plus the block's
    deterministic rollup), the opening message, the model, and what is needed to file the result.

    Shared by the command line and the app. Everything that can fail does so here, before the
    model is paid for: a block whose start Monday is outside any seeded plan used to raise only
    *after* the review had been generated, and the text was lost with it.
    """
    week = current_week(date.fromisoformat(cyc["start_date"]))
    if week is None:
        raise RuntimeError(
            f"cycle starting {cyc['start_date']} is not inside any seeded training plan, so "
            f"the review cannot be filed against one. Seed training_plan_weeks for that date "
            f"first — reviews are keyed on (plan_name, cycle_start).")
    metrics = cycle_metrics(cyc)
    prev = previous_review(cyc["start_date"])
    # Full athlete context plus the cycle rollup: the review still needs to know goals,
    # timeline and where the plan goes next, not just what happened in the block.
    snapshot = coaching_context_text()
    system = build_system_prompt(
        f"{snapshot}\n\n{'=' * 70}\n\n{cycle_context_text(metrics, prev)}",
        mode="cycle_review",
    )
    if verbose:
        print(f"[coach] cycle: {cyc['start_date']} .. {cyc['end_date']} "
              f"(weeks {cyc['plan_week_range'][0]}-{cyc['plan_week_range'][1]})")
        print(f"[coach] comparing against: {prev['cycle_start'] if prev else 'no prior review'}")
        print()
    return {"system": system, "model": coach_model(), "cyc": cyc, "metrics": metrics,
            "plan_name": week["plan_name"],
            "messages": [{"role": "user",
                          "content": "That block just finished. Give me the cycle review."}]}


def finish_cycle_review(inputs: dict, response: str) -> None:
    """File a completed review, refusing an empty one."""
    if not (response or "").strip():
        # cycle_reviews is UNIQUE(plan_name, cycle_start), so an empty row is not a harmless
        # no-op: the cycle is thereafter treated as reviewed and never offered again.
        raise RuntimeError(
            f"the model returned no text for the cycle starting {inputs['cyc']['start_date']}; "
            f"refusing to file an empty review, which would mark the cycle reviewed permanently.")
    save_review(inputs["plan_name"], inputs["metrics"], response, inputs["model"])


def cycle_review(on: date | None = None, verbose: bool = False) -> str | None:
    """
    4-week mesocycle review. Returns None if no cycle has closed yet.

    Deliberately not auto-fired from incremental_sync(): sync runs at the start of every coach
    session, so an automatic trigger would mean surprise API calls, and a cycle whose Monday
    passed without any session being run would be missed silently. Invoked explicitly by
    scripts/cycle_review.py, or from the app when `coach.due` says a block has closed.

    Stored in cycle_reviews rather than coaching_log — see the schema comment for why.
    """
    cyc = cycle_for_review(on)
    if cyc is None:
        return None

    incremental_sync(silent=not verbose)
    inputs = cycle_review_inputs(cyc, verbose=verbose)
    if verbose:
        print(f"[coach] model: {inputs['model']}")
    response = _run_tool_loop(_client(), inputs["model"], inputs["system"], inputs["messages"])
    finish_cycle_review(inputs, response)
    return response


class CoachSession:
    """
    Multi-turn conversational session with tool use, kept in the same tables as the app's.

    The conversation is stored as it happens (`chat_store`), so a crash loses nothing and the
    command line and the app see the same conversations. When it ends the note for the coach's
    memory is written by `memory.summarise_session`, the one place that does it.
    """

    def __init__(self, model: str = None, verbose: bool = False):
        self.model = model or coach_model()
        self.verbose = verbose
        self.client = _client()
        self.session_id = chat_store.new_session(self.model)
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
        # Stored after every answer, not only at the end, so quitting badly loses nothing.
        chat_store.replace_history(self.session_id, self.history)
        chat_store.set_title_if_unset(self.session_id, question)
        return reply

    def end_session(self) -> str | None:
        """Close the conversation and write its note to the coach's memory. Returns the note."""
        if not self.history:
            return None
        chat_store.end_session(self.session_id)
        return memory.summarise_session(self.session_id, self.client)

    def reset(self) -> None:
        """Start again. The conversation so far stays stored and is noted later like any other."""
        if self.history:
            chat_store.end_session(self.session_id)
        self.history = []
        self.session_id = chat_store.new_session(self.model)

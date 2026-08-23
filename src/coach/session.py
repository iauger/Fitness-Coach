"""
Claude coaching layer — check-in and conversational modes with tool use.
Model controlled by COACH_MODEL env var (default: claude-haiku-4-5).
"""

import os
import json
import uuid
from datetime import datetime
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv

from src.analysis.report import build_coaching_context, coaching_context_text
from src.coach.prompt import build_system_prompt
from src.coach.tools import TOOL_SCHEMAS, execute_tool
from src.db.schema import get_connection
from src.sync import incremental_sync

load_dotenv()

DEFAULT_MODEL = os.environ.get("COACH_MODEL", "claude-haiku-4-5")
TRANSCRIPT_DIR = Path(__file__).parent.parent.parent / "data" / "transcripts"
MAX_TOKENS = 1024


def _client() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _run_tool_loop(client: Anthropic, model: str, system: str,
                   messages: list[dict]) -> str:
    """
    Runs the agentic tool loop: call Claude → execute any tool calls →
    append results → repeat until stop_reason is end_turn.
    Returns the final text response.
    """
    while True:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        if response.stop_reason == "tool_use":
            # append assistant turn with all content blocks
            messages.append({
                "role": "assistant",
                "content": [b.model_dump() for b in response.content],
            })
            # execute each tool call and collect results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # unexpected stop reason — return whatever text exists
        texts = [b.text for b in response.content if hasattr(b, "text")]
        return " ".join(texts)


def checkin(feel_context: str = "", verbose: bool = False) -> str:
    """
    Single-shot weekly check-in. Builds full context, runs tool loop, returns response.
    feel_context: optional pre-session subjective input from the user.
    """
    ctx = build_coaching_context()
    snapshot = coaching_context_text(ctx)
    system = build_system_prompt(snapshot)
    model = DEFAULT_MODEL

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

    # persist to coaching_log
    session_id = str(uuid.uuid4())[:8]
    conn = get_connection()
    with conn:
        conn.execute("""
            INSERT INTO coaching_log (session_id, date, type, model, content, created_at)
            VALUES (?, ?, 'checkin', ?, ?, ?)
        """, (session_id, datetime.utcnow().date().isoformat(), model, response,
              datetime.utcnow().isoformat()))
    conn.close()

    return response


class CoachSession:
    """
    Multi-turn conversational session with tool use and transcript saving.
    """

    def __init__(self, model: str = None, verbose: bool = False):
        self.model = model or DEFAULT_MODEL
        self.verbose = verbose
        self.client = _client()
        self.session_id = str(uuid.uuid4())[:8]
        self.started_at = datetime.utcnow().isoformat()
        self.history: list[dict] = []

        incremental_sync(silent=not verbose)

        ctx = build_coaching_context()
        snapshot = coaching_context_text(ctx)
        self.system = build_system_prompt(snapshot)

        if verbose:
            print(f"[coach] session {self.session_id}  model={self.model}")

    def ask(self, question: str) -> str:
        self.history.append({"role": "user", "content": question})
        # run tool loop on a copy so the loop can append tool turns
        working = list(self.history)
        reply = _run_tool_loop(self.client, self.model, self.system, working)
        # sync working history back (tool turns included)
        self.history = working
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def end_session(self) -> str | None:
        """Save transcript, generate summary, store in coaching_log. Returns summary."""
        if not self.history:
            return None

        ended_at = datetime.utcnow().isoformat()
        turns = sum(1 for m in self.history if m["role"] == "user")

        # save raw transcript
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
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
        conn = get_connection()
        with conn:
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
                  datetime.utcnow().isoformat()))
        conn.close()

        return summary

    def reset(self) -> None:
        self.history = []

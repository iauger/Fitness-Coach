"""
Streaming agentic turn — phase 12C.

`_run_tool_loop()` was a blocking `while True` around `messages.create()`. Fine for a CLI: you
wait, then text appears. Unusable in a chat UI, where a 700-word check-in takes ~30 seconds with
no output at all, and where the tool calls the coach makes (`query_history`, `log_life_event`)
happen invisibly.

This turns the same loop into a generator of typed events. Text arrives token by token; tool
calls announce themselves before running and report when they finish. The loop structure is
unchanged — call, execute tools, append results, repeat until `end_turn` — so behaviour is the
same and only the delivery differs.

`run_turn()` consumes this generator and returns the final text, which is how the existing
blocking callers (`checkin()`, `CoachSession.ask()`, `cycle_review()`) keep working untouched.
"""

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator, Literal

from anthropic import Anthropic

from src import applog
from src.coach.tools import TOOL_SCHEMAS, execute_tool

EventType = Literal[
    "start",        # a request to the model has begun (may fire more than once per turn)
    "text",         # a token or fragment of the visible answer
    "tool_start",   # the model asked for a tool; `name` and `input` are set
    "tool_end",     # that tool finished; `result` is set
    "turn_end",     # the whole turn is complete; `text` holds the full answer
    "error",        # something failed; `message` is set
]


@dataclass
class Event:
    """One thing that happened during a turn, in a shape an SSE frame can carry directly."""
    type: EventType
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    message: str = ""
    iteration: int = 0

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v not in ("", {}, 0)}
                          | {"type": self.type})


# A turn that keeps asking for tools without ever finishing is a bug, not a long answer. Bound it
# rather than letting a runaway loop bill indefinitely against the API.
MAX_ITERATIONS = 12


def stream_turn(
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
) -> Iterator[Event]:
    """
    Run one agentic turn, yielding events as they happen.

    `messages` is mutated in place exactly as the blocking loop did, so the caller ends up with
    the full conversation including assistant turns and tool results — that is what makes the
    history persistable and what lets a follow-up question continue correctly.
    """
    tools = TOOL_SCHEMAS if tools is None else tools
    final_text: list[str] = []

    # One event per turn, whichever way it ends (`finally` also runs when the consumer walks away
    # mid-stream, which is the "abandoned" case). Tokens, tool names and timing: never the
    # question, the answer or a tool's result.
    started = time.monotonic()
    turn = {"iterations": 0, "input_tokens": 0, "output_tokens": 0, "tools": [], "suggested": False,
            "ended": "abandoned", "exc": None}

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            yield Event("start", iteration=iteration)
            assistant_blocks: list[dict] = []
            stop_reason = None
            turn["iterations"] = iteration

            try:
                with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                ) as stream:
                    for chunk in stream.text_stream:
                        final_text.append(chunk)
                        yield Event("text", text=chunk, iteration=iteration)
                    response = stream.get_final_message()
            except Exception as exc:     # not logged here: the finally block records it, with the totals
                turn.update(ended="error", exc=exc)
                yield Event("error", message=f"{type(exc).__name__}: {exc}", iteration=iteration)
                return

            usage = getattr(response, "usage", None)
            turn["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            turn["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

            stop_reason = response.stop_reason
            assistant_blocks = [b.model_dump() for b in response.content]

            if stop_reason != "tool_use":
                # end_turn, max_tokens, or anything unexpected: the visible text is what we have.
                messages.append({"role": "assistant", "content": assistant_blocks})
                turn["ended"] = "max_tokens" if stop_reason == "max_tokens" else "done"
                if stop_reason == "max_tokens":
                    yield Event("error", message="response hit max_tokens and was cut off",
                                iteration=iteration)
                yield Event("turn_end", text="".join(final_text), iteration=iteration)
                return

            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                yield Event("tool_start", name=block.name, input=dict(block.input),
                            iteration=iteration)
                turn["tools"].append(block.name)
                try:
                    result = execute_tool(block.name, block.input)
                except Exception as exc:
                    # Report the failure to the model rather than aborting the turn — it can often
                    # recover, and an is_error result is the documented way to say so.
                    applog.warning("coach.tool_crashed", exc, tool=block.name)
                    result = f"error: {type(exc).__name__}: {exc}"
                    yield Event("error", message=f"tool {block.name} failed: {exc}",
                                iteration=iteration)
                if block.name == "suggest_change" and '"status": "shown"' in str(result):
                    turn["suggested"] = True
                yield Event("tool_end", name=block.name, result=str(result)[:2000],
                            iteration=iteration)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                })
            messages.append({"role": "user", "content": tool_results})

        turn["ended"] = "iteration_limit"
        yield Event("error", message=f"turn exceeded {MAX_ITERATIONS} tool iterations")
        yield Event("turn_end", text="".join(final_text))
    finally:
        fields = {"model": model, "iterations": turn["iterations"],
                  "input_tokens": turn["input_tokens"], "output_tokens": turn["output_tokens"],
                  "tools": turn["tools"], "suggested": turn["suggested"],
                  "ended": turn["ended"], "ms": int((time.monotonic() - started) * 1000)}
        if turn["exc"] is not None:
            applog.error("coach.turn", turn["exc"], **fields)
        elif turn["ended"] == "done":
            applog.event("coach.turn", **fields)
        else:
            applog.warning("coach.turn", **fields)


def run_turn(client: Anthropic, model: str, system: str, messages: list[dict],
             max_tokens: int) -> str:
    """
    Blocking equivalent of stream_turn — drains the generator and returns the final text.

    Every existing caller goes through here, so the streaming rewrite changed no behaviour for
    the CLI: same loop, same messages mutation, same returned string.
    """
    text = ""
    saw_turn_end = False
    errors: list[str] = []
    for event in stream_turn(client, model, system, messages, max_tokens):
        if event.type == "turn_end":
            text, saw_turn_end = event.text, True
        elif event.type == "error":
            errors.append(event.message)

    # An empty answer is a failure, however the turn ended.
    #
    # The first version of this guard only caught a missing turn_end, which is what an API
    # exception produces. It missed the other two routes to the same place: a turn that burns
    # all MAX_ITERATIONS on tool calls without ever emitting text yields `error` *and* a
    # turn_end carrying "", and a model that simply returns nothing yields a clean turn_end
    # carrying "". Both returned "" to a caller that treats the return value as the answer —
    # checkin() writes it into coaching_log, and cycle_review() files it against a
    # UNIQUE(plan_name, cycle_start) row, after which that cycle counts as reviewed forever.
    if not saw_turn_end or not text.strip():
        why = "; ".join(errors) or ("empty response" if saw_turn_end else "no turn_end")
        raise RuntimeError(f"the model turn produced no answer: {why}")
    return text

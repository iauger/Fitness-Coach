"""
Whether the coach can run, and on which model: one place, read at call time.

`coach.enabled` and `coach.model` were declared in the config and read by nothing: the model
came from the COACH_MODEL environment variable alone, and switching the coach off did nothing.
A setting that states an intent the code ignores is worse than no setting, so both are read
here and every entry point asks this module.

Read at call time, never at import. `session.py` once bound its model at import, which made
`--model` on the command line print one model while the session ran on another, and a config
read at import is how a bare `pytest` once created a config file.
"""

import os

from dotenv import load_dotenv

from src.config import config

ENV_MODEL = "COACH_MODEL"
ENV_KEY = "ANTHROPIC_API_KEY"
ENV_EXTRACTOR = "COACH_EXTRACTOR_MODEL"
# Reading what an athlete said and filing it is a classification job, not coaching: the small
# model is the right one, and swapping it is a one-line change here or an environment variable.
DEFAULT_EXTRACTOR = "claude-haiku-4-5"


def model() -> str:
    """The environment wins, so a one-off `COACH_MODEL=... python scripts/ask.py` still works."""
    return os.environ.get(ENV_MODEL) or config().coach.model


def extractor_model() -> str:
    return os.environ.get(ENV_EXTRACTOR) or DEFAULT_EXTRACTOR


def enabled() -> bool:
    return config().coach.enabled


def has_key() -> bool:
    load_dotenv()                      # the key lives in .env; every entry point loads it
    return bool((os.environ.get(ENV_KEY) or "").strip())


def refusal() -> tuple[int, str] | None:
    """`(http status, reason)` if the coach cannot run right now, else None."""
    if not enabled():
        return 409, "The coach is switched off (coach.enabled is false in the config)."
    if not has_key():
        return 503, f"The coach needs an Anthropic API key: add {ENV_KEY} to .env and restart."
    return None


def status() -> dict:
    return {"enabled": enabled(), "model": model(), "has_key": has_key(),
            "available": refusal() is None}

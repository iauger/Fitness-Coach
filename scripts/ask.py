"""
Conversational coaching session with tool use, transcript saving, and session summary.

Usage:
    python scripts/ask.py
    python scripts/ask.py --model claude-sonnet-4-6
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.coach.session import CoachSession

if __name__ == "__main__":
    from src.cli import friendly_errors
    friendly_errors()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Override COACH_MODEL env var")
    args = parser.parse_args()

    import os
    if args.model:
        os.environ["COACH_MODEL"] = args.model

    # Passed explicitly. src.coach.session is imported at line 18, above, so its
    # The model is read here, after --model has edited the environment, and passed explicitly:
    # it used to be bound at import, so the override had no effect and this line printed the
    # requested model while the session ran on the other one.
    from src.coach.settings import model as coach_model
    model = coach_model()
    session = CoachSession(model=model, verbose=True)
    print(f"\nCoach ready ({model}). Commands: 'quit' to end session, 'reset' to clear history.\n")

    try:
        while True:
            try:
                question = input("You: ").strip()
            except EOFError:
                break

            if not question:
                continue
            if question.lower() == "quit":
                break
            if question.lower() == "reset":
                session.reset()
                print("[history cleared]\n")
                continue

            print("\nCoach:", session.ask(question), "\n")

    except KeyboardInterrupt:
        pass

    print("\nEnding session — saving transcript and generating summary...")
    summary = session.end_session()
    if summary:
        print("\n--- Session summary (saved to coaching log) ---")
        print(summary)

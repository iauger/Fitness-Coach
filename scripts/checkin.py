"""
Weekly check-in — prompts for subjective feel, then generates coaching response.

Usage:
    python scripts/checkin.py
    python scripts/checkin.py --model claude-sonnet-4-6
    python scripts/checkin.py --no-prompt   (skip feel questions, data only)
    python scripts/checkin.py --verbose
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.coach.session import checkin


def prompt_for_feel() -> str:
    """Ask a few quick subjective questions and return context string."""
    print("Quick check-in before we look at your data:\n")
    lines = []

    overall = input("  Overall feeling this week (1-10, or press Enter to skip): ").strip()
    if overall:
        lines.append(f"Overall feel: {overall}/10")

    energy = input("  Energy levels (1-10, or Enter to skip): ").strip()
    if energy:
        lines.append(f"Energy: {energy}/10")

    legs = input("  Leg freshness (1-10, or Enter to skip): ").strip()
    if legs:
        lines.append(f"Leg freshness: {legs}/10")

    print("  TrainerRoad compliance this week:")
    print("    1) Completed all workouts")
    print("    2) Skipped some")
    print("    3) Modified workouts")
    print("    4) Rest week")
    print("    5) Not on a TR plan right now")
    tr = input("  Choice (1-5, or Enter to skip): ").strip()
    tr_map = {
        "1": "completed_all", "2": "skipped_some",
        "3": "modified", "4": "rest_week", "5": "not_on_plan",
    }
    if tr in tr_map:
        lines.append(f"TR compliance: {tr_map[tr]}")

    notes = input("  Anything else worth flagging? (injury, illness, stress, etc): ").strip()
    if notes:
        lines.append(f"Notes: {notes}")

    print()
    return "  ".join(lines) if lines else ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Override COACH_MODEL env var")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip subjective feel questions")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import os
    if args.model:
        os.environ["COACH_MODEL"] = args.model

    feel_context = "" if args.no_prompt else prompt_for_feel()
    print(checkin(feel_context=feel_context, verbose=args.verbose))

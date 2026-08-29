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
from src.analysis.training_plan import just_completed_cycle
from src.coach.tools import log_subjective_feel


def _int_or_none(raw: str) -> int | None:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 10 else None


def prompt_for_feel() -> str:
    """
    Ask the weekly subjective questions, write them to subjective_feel, and return a short
    context string for the prompt.

    The answers are written here rather than left for the model to log. These are the most
    structured inputs in the whole system — numbers on a fixed scale and a compliance value
    from a closed enum — and until now they were formatted into prose and passed to the model,
    reaching the database only if it happened to call log_subjective_feel. Across 7 check-ins
    that produced 4 stored rows.
    """
    print("Quick check-in before we look at your data:\n")
    lines = []

    overall = _int_or_none(input("  Overall feeling this week (1-10, or press Enter to skip): ").strip())
    if overall:
        lines.append(f"Overall feel: {overall}/10")

    energy = _int_or_none(input("  Energy levels (1-10, or Enter to skip): ").strip())
    if energy:
        lines.append(f"Energy: {energy}/10")

    legs = _int_or_none(input("  Leg freshness (1-10, or Enter to skip): ").strip())
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
    tr_compliance = tr_map.get(tr)
    if tr_compliance:
        lines.append(f"TR compliance: {tr_compliance}")

    notes = input("  Anything else worth flagging? (injury, illness, stress, etc): ").strip()
    if notes:
        lines.append(f"Notes: {notes}")

    # feel_score is the column's NOT-NULL-in-practice anchor; with nothing to anchor to there
    # is no row worth writing, though the answers still go to the model as context.
    if overall is not None:
        log_subjective_feel(
            feel_score=overall, energy=energy, legs=legs,
            tr_compliance=tr_compliance, notes=notes or None, logged_via="checkin",
        )
        print("  [logged to subjective_feel]")
    elif lines:
        print("  [not logged — an overall feel score is needed to store the rest]")

    print()
    return "  ".join(lines) if lines else ""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Override COACH_MODEL env var")
    parser.add_argument("--no-prompt", action="store_true",
                        help="Skip subjective feel questions")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--weekly", action="store_true",
                        help="Force the weekly check-in even if a training cycle just closed")
    args = parser.parse_args()

    import os
    if args.model:
        os.environ["COACH_MODEL"] = args.model

    # A closed cycle supersedes that week's check-in rather than sitting alongside it — the
    # two would fire on the same Monday and overlap almost entirely, and a block review
    # subsumes the week it ends on. Redirect rather than run silently, so it stays the
    # athlete's call and no API call happens unasked.
    closed = just_completed_cycle()
    if closed and not args.weekly:
        print(f"A training cycle closed yesterday — plan weeks "
              f"{closed['plan_week_range'][0]}-{closed['plan_week_range'][1]}, "
              f"{closed['start_date']} to {closed['end_date']}.\n")
        print("The 4-week review covers this week too, so run that instead:\n")
        print("    python scripts/cycle_review.py\n")
        print("Or pass --weekly to this script to get the ordinary check-in anyway.")
        sys.exit(0)

    feel_context = "" if args.no_prompt else prompt_for_feel()
    print(checkin(feel_context=feel_context, verbose=args.verbose))

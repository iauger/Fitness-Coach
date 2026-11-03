"""
Plan-wide identifiers, in a module that imports nothing.

This exists for one structural reason. Three readers in `src.analysis` need to filter
`training_plan_weeks` by plan name (`cycle.py`, `derive.py`, `weekly.py`), and the name used to
live in `src.planning.blocks`. But `blocks.py` imports `src.analysis.derive` for its intensity
thresholds, so having `derive` import `blocks` would close a cycle. A leaf module breaks it.

`blocks.py` re-exports `PLAN_NAME`, so `from src.planning.blocks import PLAN_NAME` keeps working.
"""

# No year in the name: the plan has no end date. A year-stamped second name is what made the
# prescribed -> planned join impossible before — plan_build wrote "Sustainable 2026-27" while
# seed_training_plan used "TrainerRoad 2026-27" — and that join is what current_week, cycles,
# plan_summary, cycle_metrics and week_metrics all key on.
PLAN_NAME = "Sustainable rolling"

# The primary goal: cyclocross, autumn 2027. Held as a constant rather than read from `goals` so
# a stored plan row's end date cannot drift when the goals table is edited, and deliberately not
# a taper target — nothing in the rotation peaks for it. `plan_end_date` on a training_plan_weeks
# row is labelled as a goal, not as a dated commitment.
GOAL_DATE = "2027-10-01"

# The June 2027 gravel event, kept because reports still reference it. Opt-in per the rebuild:
# the rotation does not taper for it, and a block would be *inserted* a few weeks out if wanted.
GRAVEL_DATE = "2027-06-18"

# The Monday the rolling plan became the plan of record — the first week the rebuilt
# generator produced sessions for. `plan_progression` holds decided weeks from here on and
# nothing before: replaying the gate over earlier history is a *counterfactual* (no gate was
# running then), and storing counterfactuals in an append-only decision log would make it
# impossible to tell a decision that was acted on from one that was merely computed. The
# earlier replay is used only to pick the starting rung. src/db/migrations.py keeps its own
# frozen copy of this date, deliberately: a migration that reads a code constant stops
# replaying to the same database the moment that constant is edited.
PLAN_START_MONDAY = "2026-11-02"

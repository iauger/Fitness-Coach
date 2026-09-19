"""
Plan structure — blocks of weeks, expanded into dated session prescriptions.

A block says *what kind of training* runs for *how many weeks*, with a weekday pattern and a
progression rule. Expansion turns that into one prescription per session per day.

Two constraints from the source template are enforced here rather than left to discipline:

- **At most two structured hard sessions per week.** `Block.pattern` is validated against it,
  because the failure this plan exists to correct was 78-100% of riding above sweet spot.
- **Variants rotate across weeks.** TrainerRoad's thousands of workouts are one archetype at
  many doses; the equivalent variety here comes from cycling `hard_variants` and progressing
  the dose, which also answers the monotony risk Arnold flags.

Blocks are modular by design — Arnold deliberately omits a fixed base phase and says they can
run in whatever order suits. Periodisation is therefore a property of how DEFAULT_PLAN orders
them, not of the block machinery.

What this does NOT do: pick a specific TrainerRoad workout. It emits a prescription
("3x15min @ 93%, 65min, 77 TSS") that a library workout gets matched to. That keeps TR as the
execution layer while this owns the structure, and defers .zwo generation entirely.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from src.planning import archetypes as A

# Weekday roles a slot can take.
ROLES = ("hard", "long", "lit", "strength")

MAX_HARD_PER_WEEK = 2


@dataclass
class Block:
    name: str
    block_type: str                     # sit | hiit | threshold | base | recovery
    weeks: int
    pattern: list[tuple[int, str]]      # (weekday 0=Mon, role)
    hard_variants: list[str] = field(default_factory=list)
    recovery_weeks: set[int] = field(default_factory=set)   # 1-based, within the block
    long_start_min: int = 90
    long_step_min: int = 15
    long_cap_min: int = 240
    lit_min: int = 60
    note: str = ""

    def __post_init__(self) -> None:
        hard = sum(1 for _, role in self.pattern if role == "hard")
        if hard > MAX_HARD_PER_WEEK:
            raise ValueError(
                f"block {self.name!r} schedules {hard} hard sessions a week; the template caps "
                f"it at {MAX_HARD_PER_WEEK}")
        for _, role in self.pattern:
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r} in block {self.name!r}")


def _hard_session(block: Block, week_in_block: int, index: int) -> A.Session:
    """
    Resolve a hard slot to a concrete session.

    Variant rotates by (week, slot) so consecutive hard days differ and weeks do not repeat
    the same pair. Dose steps up with the week, and steps back down on a recovery week.
    """
    variants = block.hard_variants or [None]
    variant = variants[(week_in_block - 1 + index) % len(variants)]
    recovering = week_in_block in block.recovery_weeks
    # Progression: one extra rep from the third week of a run, dropped in recovery weeks.
    bump = 0 if recovering else (week_in_block - 1) // 2

    if block.block_type == "sit":
        s = A.sit(variant or "medium")
        if bump and not recovering:
            s = A.sit(variant or "medium", reps=s.params["reps"] + bump)
        return s
    if block.block_type == "hiit":
        base = A.HIIT_VARIANTS[variant or "straight_5"]
        work = base["work_min"] + (0.5 * bump)
        reps = max(2, base["reps"] - 1) if recovering else base["reps"]
        return A.hiit(variant or "straight_5", reps=reps, work_min=work)
    # threshold and base blocks both use threshold work for their hard slots
    base = A.THRESHOLD_VARIANTS[variant or "long_reps"]
    work = base["work_min"] + (2 * bump)
    reps = max(1, base["reps"] - 1) if recovering else base["reps"]
    return A.threshold(variant or "long_reps", reps=reps, work_min=work)


def _long_minutes(block: Block, week_in_block: int) -> int:
    if week_in_block in block.recovery_weeks:
        # Hold the long ride rather than dropping it: duration is the adaptation this plan is
        # for, and it is the least fatiguing thing to keep during a down week.
        week_in_block = max(1, week_in_block - 1)
    minutes = block.long_start_min + block.long_step_min * (week_in_block - 1)
    return int(min(minutes, block.long_cap_min))


def expand_block(block: Block, start: date, plan_name: str,
                 block_index: int) -> list[dict]:
    """One dict per prescribed session, dated."""
    out: list[dict] = []
    for week in range(1, block.weeks + 1):
        monday = start + timedelta(weeks=week - 1)
        hard_seen = 0
        strength_seen = 0
        for weekday, role in sorted(block.pattern):
            day = monday + timedelta(days=weekday)
            if role == "hard":
                session = _hard_session(block, week, hard_seen)
                hard_seen += 1
            elif role == "long":
                session = A.lit(_long_minutes(block, week), "endurance_sprints"
                                if week % 3 == 0 else "steady")
            elif role == "lit":
                session = A.lit(block.lit_min, "cadence_drills" if week % 2 == 0 else "steady")
            else:
                # Rotate by slot as well as week, so the two strength days in a week are not the
                # same session - the whole point of lifting twice is to cover different patterns.
                session = A.strength(40, ("lower", "upper", "full")[(week + strength_seen) % 3])
                strength_seen += 1
            out.append({
                "plan_name": plan_name,
                "date": day.isoformat(),
                "block_index": block_index,
                "block_name": block.name,
                "week_in_block": week,
                "is_recovery_week": week in block.recovery_weeks,
                "weekday": weekday,
                "role": role,
                "archetype": session.archetype,
                "variant": session.variant,
                "prescription": session.summary(),
                "minutes": session.minutes,
                "tss": session.tss,
                "intensity_factor": session.intensity_factor,
                "params": session.params,
            })
    return out


def expand_plan(blocks: list[Block], start: date, plan_name: str) -> list[dict]:
    sessions: list[dict] = []
    cursor = start
    for i, block in enumerate(blocks, start=1):
        sessions += expand_block(block, cursor, plan_name, i)
        cursor += timedelta(weeks=block.weeks)
    return sessions


def weekly_rollup(sessions: list[dict]) -> list[dict]:
    """Per-week totals — the view that shows whether the intensity balance actually holds."""
    weeks: dict[str, dict] = {}
    for s in sessions:
        d = date.fromisoformat(s["date"])
        monday = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(monday, {
            "week_start": monday, "block": s["block_name"],
            "recovery": s["is_recovery_week"], "sessions": 0, "rides": 0,
            "hard": 0, "minutes": 0.0, "ride_minutes": 0.0, "tss": 0.0, "longest": 0.0,
        })
        w["sessions"] += 1
        w["minutes"] += s["minutes"]
        w["tss"] += s["tss"]
        if s["archetype"] != "strength":
            w["rides"] += 1
            w["ride_minutes"] += s["minutes"]
            w["longest"] = max(w["longest"], s["minutes"])
        if s["role"] == "hard":
            w["hard"] += 1
    return [weeks[k] for k in sorted(weeks)]


# ── the proposed plan ─────────────────────────────────────────────────────────
#
# Three rides a week with one long, at most two hard, strength twice - five sessions total.
# Fewer sessions than the current TrainerRoad block (6-7/week) but MORE ride hours, because the
# volume goes into one long ride rather than being spread across more 60-minute ones. A fourth
# ride is available via add_lit_day() below; at two young kids and ~6.3h of sleep, six sessions
# a week is the more likely thing to quietly stop happening.
# Ordered Base -> Build ->
# Specialty toward the 2027-06-18 gravel event, using Arnold's modular blocks as the engine.
#
# The long ride is the point. Every ride in the current TrainerRoad block was 60-75 minutes,
# against an eight-hour target event; durability over that distance is a duration-dependent
# adaptation that session count does not produce. It starts at 90min and steps up 15min a week.

DEFAULT_PLAN: list[Block] = [
    Block(
        name="Reset — consistency and easy volume",
        block_type="base", weeks=4,
        pattern=[(1, "hard"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")],
        hard_variants=["sweet_spot", "continuous"],
        recovery_weeks={4},
        long_start_min=90, long_step_min=15, lit_min=60,
        note="One hard session only. Re-establish the long ride after a block of 60-75min rides.",
    ),
    Block(
        name="Base — threshold and durability",
        block_type="threshold", weeks=6,
        pattern=[(1, "hard"), (2, "strength"), (3, "hard"), (5, "long"), (6, "strength")],
        hard_variants=["long_reps", "medium_reps", "continuous", "over_unders"],
        recovery_weeks={3, 6},
        long_start_min=135, long_step_min=15, lit_min=60,
        note="Threshold hedged low, per the source. Long ride carries the durability load.",
    ),
    Block(
        name="Build — VO2 and sustained power",
        block_type="hiit", weeks=6,
        pattern=[(1, "hard"), (2, "strength"), (3, "hard"), (5, "long"), (6, "strength")],
        hard_variants=["straight_5", "straight_4", "thirty_fifteen", "straight_6"],
        recovery_weeks={3, 6},
        long_start_min=180, long_step_min=15, long_cap_min=240,
        note="Sub-maximal by design — extend time in the domain, keep half a rep in reserve.",
    ),
    Block(
        name="Sharpen — neuromuscular",
        block_type="sit", weeks=3,
        pattern=[(1, "hard"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")],
        hard_variants=["medium", "long", "short"],
        recovery_weeks={3},
        long_start_min=210, long_step_min=0, long_cap_min=240,
        note="Short, hard, cheap. Keeps top-end alive without eating into long-ride recovery.",
    ),
]


def add_lit_day(blocks: list[Block], weekday: int = 0, minutes: int = 60) -> list[Block]:
    """
    Return the plan with one extra low-intensity ride per week.

    Offered rather than assumed. It lifts weekly ride hours by about an hour and pushes the week
    to six sessions, which is a real commitment against the athlete's stated life load - and an
    unkept fourth ride is worse than a planned three, because the plan stops being trusted.
    """
    out = []
    for b in blocks:
        if any(d == weekday for d, _ in b.pattern):
            out.append(b)
            continue
        out.append(Block(**{**b.__dict__, "pattern": sorted(b.pattern + [(weekday, "lit")])}))
    return out


def intensity_split(sessions: list[dict], easy_ceiling: float = 0.75) -> dict:
    """
    Share of ride time below/above the easy threshold, on the same basis analysis/derive.py uses.

    This is the number the plan exists to move: the current block measured 0% easy and 78-100%
    hard. Strength is excluded - 80/20 is an endurance rule, and resistance work scores easy on
    any power or HR measure while costing real fatigue.
    """
    easy = hard = 0.0
    for s in sessions:
        if s["archetype"] == "strength":
            continue
        if s["intensity_factor"] < easy_ceiling:
            easy += s["minutes"]
        else:
            hard += s["minutes"]
    total = easy + hard
    return {
        "easy_hours": round(easy / 60, 1),
        "hard_hours": round(hard / 60, 1),
        "easy_pct": round(easy / total * 100, 1) if total else None,
        "hard_pct": round(hard / total * 100, 1) if total else None,
    }

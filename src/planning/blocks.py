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

from src.analysis.dates import monday_of
from src.analysis.derive import EASY_IF_CEILING, HARD_IF_FLOOR
from src.planning import archetypes as A

# The one plan name. scripts/plan_build.py used "Sustainable 2026-27" while
# scripts/seed_training_plan.py used "TrainerRoad 2026-27", so prescribed_sessions could never
# join training_plan_weeks - and that join is what current_week, cycles, plan_summary,
# cycle_metrics and week_metrics all key on.
PLAN_NAME = "Sustainable 2026-27"

# Weekday roles a slot can take.
ROLES = ("hard", "long", "long2", "lit", "strength")

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
    # Second weekend endurance ride. Kept short and capped well below the Saturday ride: its
    # value comes from being ridden on Saturday's fatigue, not from its own duration, and it is
    # constrained by having to finish before the household wakes up.
    long2_start_min: int = 60
    long2_step_min: int = 5
    long2_cap_min: int = 105
    # Readable phase name for training_plan_weeks / cycle reviews. Defaults to block_type, but
    # Reset and Taper are both block_type="base" and would otherwise be indistinguishable in a
    # review that says which phase a cycle closed out.
    phase_label: str = ""
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


def _long2_minutes(block: Block, week_in_block: int) -> int:
    if week_in_block in block.recovery_weeks:
        week_in_block = max(1, week_in_block - 1)
    minutes = block.long2_start_min + block.long2_step_min * (week_in_block - 1)
    return int(min(minutes, block.long2_cap_min))


def _strength_for_week(block: Block, week: int) -> dict[int, str]:
    """
    Assign a lift pattern to each strength day, resolving both constraints at once.

    Two rules interact and the order matters. Legs must not be loaded on either side of a long
    ride, so those slots are pinned to upper body first; only then do the remaining slots rotate,
    which stops a week lifting the same pattern twice and wasting the second session.

    Adjacency is computed from the pattern rather than hardcoded. An earlier version pinned
    weekdays 0 and 4 on the assumption the long ride sat on Saturday with strength on Friday and
    Monday. Every block in DEFAULT_PLAN actually schedules strength on 2 and 6, so the pin never
    fired and the rotation was free to put lower body on **Sunday, the day after the Saturday
    long ride** - which happened in 6 of 19 weeks. The rule existed and the plan violated it.

    Wrapping mod 7 is deliberate: the plan repeats weekly, so Monday follows the previous week's
    Sunday. If both strength days end up adjacent to a long ride they both go upper - protecting
    the legs matters more than varying the pattern.
    """
    days = sorted(d for d, r in block.pattern if r == "strength")
    long_days = {d for d, r in block.pattern if r in ("long", "long2")}
    out: dict[int, str] = {}
    for d in days:
        if long_days & {(d - 1) % 7, (d + 1) % 7}:
            out[d] = "upper"
    remaining = [d for d in days if d not in out]
    pool = [v for v in ("lower", "full", "upper") if v not in out.values()] or ["full"]
    for i, d in enumerate(remaining):
        out[d] = pool[(week + i) % len(pool)]
    return out


def expand_block(block: Block, start: date, plan_name: str,
                 block_index: int) -> list[dict]:
    """One dict per prescribed session, dated."""
    out: list[dict] = []
    for week in range(1, block.weeks + 1):
        monday = start + timedelta(weeks=week - 1)
        hard_seen = 0
        strength_variants = _strength_for_week(block, week)
        for weekday, role in sorted(block.pattern):
            day = monday + timedelta(days=weekday)
            if role == "hard":
                session = _hard_session(block, week, hard_seen)
                hard_seen += 1
            elif role == "long":
                session = A.lit(_long_minutes(block, week), "endurance_sprints"
                                if week % 3 == 0 else "steady")
            elif role == "long2":
                session = A.lit(_long2_minutes(block, week), "steady")
            elif role == "lit":
                session = A.lit(block.lit_min, "cadence_drills" if week % 2 == 0 else "steady")
            else:
                session = A.strength(40, strength_variants[weekday])
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
        monday = monday_of(date.fromisoformat(s["date"])).isoformat()
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
        name="Reset — consistency and easy volume", phase_label="reset",
        block_type="base", weeks=4,
        pattern=[(1, "hard"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")],
        hard_variants=["sweet_spot", "continuous"],
        recovery_weeks={4},
        long_start_min=90, long_step_min=15, lit_min=60,
        note="One hard session only. Re-establish the long ride after a block of 60-75min rides.",
    ),
    Block(
        name="Base — threshold and durability", phase_label="base",
        block_type="threshold", weeks=6,
        pattern=[(1, "hard"), (2, "strength"), (3, "hard"), (5, "long"), (6, "strength")],
        hard_variants=["long_reps", "medium_reps", "continuous", "over_unders"],
        recovery_weeks={3, 6},
        long_start_min=135, long_step_min=15, lit_min=60,
        note="Threshold hedged low, per the source. Long ride carries the durability load.",
    ),
    Block(
        name="Build — VO2 and sustained power", phase_label="build",
        block_type="hiit", weeks=6,
        pattern=[(1, "hard"), (2, "strength"), (3, "hard"), (5, "long"), (6, "strength")],
        hard_variants=["straight_5", "straight_4", "thirty_fifteen", "straight_6"],
        recovery_weeks={3, 6},
        long_start_min=180, long_step_min=15, long_cap_min=240,
        note="Sub-maximal by design — extend time in the domain, keep half a rep in reserve.",
    ),
    Block(
        name="Specialty — race-specific endurance", phase_label="specialty",
        block_type="threshold", weeks=10,
        pattern=[(1, "hard"), (2, "strength"), (3, "hard"), (5, "long"), (6, "strength")],
        hard_variants=["over_unders", "sweet_spot", "long_reps", "continuous"],
        recovery_weeks={4, 8},
        long_start_min=210, long_step_min=15, long_cap_min=300,
        note="Long ride carries toward event duration; threshold work keeps the ceiling up.",
    ),
    Block(
        name="Sharpen — neuromuscular", phase_label="sharpen",
        block_type="sit", weeks=4,
        pattern=[(1, "hard"), (2, "strength"), (3, "lit"), (5, "long"), (6, "strength")],
        hard_variants=["medium", "long", "short"],
        recovery_weeks={4},
        long_start_min=210, long_step_min=0, long_cap_min=240,
        note="Short, hard, cheap. Keeps top-end alive without eating into long-ride recovery.",
    ),
    Block(
        name="Taper — into the gravel A race", phase_label="taper",
        block_type="base", weeks=2,
        pattern=[(1, "hard"), (3, "lit"), (5, "long"), (6, "strength")],
        hard_variants=["short_reps"],
        recovery_weeks={2},
        long_start_min=120, long_step_min=-60, long_cap_min=120, lit_min=45,
        note="Volume down, a little intensity retained. Race sits in the final week.",
    ),
]


def _respec(block: Block, **changes) -> Block:
    """
    Rebuild a Block with changes applied, deep-copying its mutable fields.

    `Block(**{**b.__dict__, ...})` shallow-copies, so a rebuilt block shared `hard_variants`
    and `recovery_weeks` with the original. Mutating either afterwards would silently alter
    both, and these helpers hand back new plans that callers may well edit.
    """
    fields = {**block.__dict__, **changes}
    fields["hard_variants"] = list(fields["hard_variants"])
    fields["recovery_weeks"] = set(fields["recovery_weeks"])
    fields["pattern"] = sorted(fields["pattern"])
    return Block(**fields)


def add_lit_day(blocks: list[Block], weekday: int = 0, minutes: int | None = None) -> list[Block]:
    """
    Return the plan with one extra low-intensity ride per week.

    Offered rather than assumed. It lifts weekly ride hours by about an hour and pushes the week
    to six sessions, which is a real commitment against the athlete's stated life load - and an
    unkept fourth ride is worse than a planned three, because the plan stops being trusted.

    `minutes` overrides the block's `lit_min` for the added day. It previously sat in the
    signature and was never read, so `add_lit_day(blocks, minutes=90)` silently produced
    60-minute rides.
    """
    out = []
    for b in blocks:
        if any(d == weekday for d, _ in b.pattern):
            out.append(b)
            continue
        changes = {"pattern": b.pattern + [(weekday, "lit")]}
        if minutes is not None:
            changes["lit_min"] = minutes
        out.append(_respec(b, **changes))
    return out


def intensity_split(sessions: list[dict]) -> dict:
    """
    Share of ride time by intensity band, on exactly the basis analysis/derive.py uses.

    This claimed to match `derive.py` while using **two** bands against its three: everything at
    or above 0.75 was reported as hard, where `derive` calls 0.75-0.85 moderate. 31% of this
    plan's minutes fall in that gap, so the plan's "% hard" and the measured "% hard" printed
    beside it were never comparable numbers. The thresholds are imported rather than redefined
    so the two cannot drift apart again.

    Strength is excluded - 80/20 is an endurance rule, and resistance work scores easy on any
    power or HR measure while costing real fatigue.
    """
    bands = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    for s in sessions:
        if s["archetype"] == "strength":
            continue
        intensity = s["intensity_factor"]
        band = ("easy" if intensity < EASY_IF_CEILING
                else "moderate" if intensity < HARD_IF_FLOOR else "hard")
        bands[band] += s["minutes"]
    total = sum(bands.values())
    return {
        "hours": {k: round(v / 60, 1) for k, v in bands.items()},
        "pct": {k: (round(v / total * 100, 1) if total else None) for k, v in bands.items()},
    }


def add_second_weekend_ride(blocks: list[Block], strength_to: int = 4) -> list[Block]:
    """
    Add a Sunday endurance ride behind the Saturday long ride, relocating Sunday strength.

    Back-to-back endurance days are the cheapest way to train durability: the Sunday ride is
    ridden on Saturday's fatigue, which rehearses the back half of a long event without
    requiring a ride that long. That is the adaptation the June target actually needs, and it
    is why this is a better use of a fourth ride than another midweek hour.

    Deliberately short - 60min growing to 105 - because it is constrained by finishing before
    the household is up, and because a second *long* ride would compete with Saturday for
    recovery rather than complementing it.

    The displaced Sunday strength session moves to Friday by default rather than Monday: Monday
    lands on two days of accumulated weekend fatigue, while Friday leaves the weekend clear and
    is forced to upper body so it costs Saturday nothing. Pass strength_to=0 for Monday instead.

    The trade to watch is sleep, not training load. The athlete already averages ~6.3-6.7h with
    9 nights under 6h in 30 days; an early Sunday alarm that costs an hour of it can easily be
    a net negative. That is a readiness signal to track, not a reason to skip the ride.
    """
    out: list[Block] = []
    carried: int | None = None
    for b in blocks:
        pattern = [(d, r) for d, r in b.pattern if not (d == 6 and r == "strength")]
        displaced = len(pattern) != len(b.pattern)

        if displaced:
            # Relocate, never drop. This used to delete the session outright when the target day
            # was occupied, quietly taking the week from two strength days to one.
            target = strength_to
            if any(d == target for d, _ in pattern):
                free = [d for d in (4, 0, 3, 1) if not any(x == d for x, _ in pattern)]
                if not free:
                    raise ValueError(
                        "block %r: cannot relocate Sunday strength - weekday %d is taken and "
                        "no fallback day is free" % (b.name, strength_to))
                target = free[0]
            pattern.append((target, "strength"))

        if any(d == 6 for d, _ in pattern):
            # Sunday taken by something other than strength (e.g. add_lit_day(weekday=6) ran
            # first). Adding nothing silently is worse than saying so.
            raise ValueError(
                "block %r: Sunday is already taken by %r; cannot add the second weekend ride"
                % (b.name, dict(pattern)[6]))
        pattern.append((6, "long2"))

        # Carry the Sunday duration across blocks, starting one step ABOVE where the previous
        # block finished. Seeding with the finishing value made every boundary repeat a week,
        # and because _long2_minutes steps back during a recovery week - the last week of every
        # DEFAULT_PLAN block - the progression also lost a step each time. Traced 70, 70, 90,
        # 105, then flat.
        changes = {"pattern": pattern}
        if carried is not None:
            changes["long2_start_min"] = min(carried + b.long2_step_min, b.long2_cap_min)
        block = _respec(b, **changes)
        # Nominal final-week duration, ignoring the recovery-week step-back.
        carried = min(block.long2_start_min + block.long2_step_min * (block.weeks - 1),
                      block.long2_cap_min)
        out.append(block)
    return out


def plan_weeks_rows(blocks: list[Block], start: date, plan_name: str = PLAN_NAME) -> list[dict]:
    """
    Rows for `training_plan_weeks`, derived from the same blocks the sessions come from.

    That table was hand-seeded from TrainerRoad's phase view by scripts/seed_training_plan.py.
    Now that the plan is generated here, seeding it from a plan we no longer follow would leave
    plan position, cycle boundaries and every weekly/cycle rollup describing the wrong thing.
    Generating both from one source also makes the plan_name join correct by construction
    rather than by remembering to keep two constants in step.

    `week_type` is 'rest' on a recovery week, otherwise the block's own phase - matching the
    enum the table has always used, where a rest week overwrites the phase name and `phase`
    carries the parent block.
    """
    rows: list[dict] = []
    total_weeks = sum(b.weeks for b in blocks)
    plan_start = start
    plan_end = start + timedelta(weeks=total_weeks) - timedelta(days=1)
    cursor = start
    plan_week = 0
    for phase_number, b in enumerate(blocks, start=1):
        phase = b.phase_label or ("rest" if b.block_type == "recovery" else b.block_type)
        for week_in_block in range(1, b.weeks + 1):
            plan_week += 1
            week_start = cursor + timedelta(weeks=week_in_block - 1)
            rows.append({
                "plan_name": plan_name,
                "plan_start_date": plan_start.isoformat(),
                "plan_end_date": plan_end.isoformat(),
                "week_start_date": week_start.isoformat(),
                "week_end_date": (week_start + timedelta(days=6)).isoformat(),
                "week_type": "rest" if week_in_block in b.recovery_weeks else phase,
                "phase": phase,
                "phase_number": phase_number,
                "phase_week_number": week_in_block,
                "plan_week_number": plan_week,
            })
        cursor += timedelta(weeks=b.weeks)
    return rows

"""
Plan structure — a rotation of blocks, expanded one week at a time at a given tier.

A block says *what kind of training* runs for *how many weeks* and on which weekdays. The dose
comes from the tier (`planning/progression.py`), never from the week number. That split is the
whole point of the rebuild: this module used to compute duration as
`long_start_min + long_step_min * (week_in_block - 1)`, so week 7 of a block asked for more than
week 6 whether or not weeks 1-6 had happened.

Three constraints are enforced here rather than left to discipline:

- **At most two structured hard sessions a week** (`MAX_HARD_PER_WEEK`), validated on every
  Block. From the source: "zero to two structured high-intensity sessions each week at most".
  In practice every rotation block declares one; the tier can reduce it to zero but never raise.
- **Strength is never trimmed.** Not at the floor tier, not in a recovery week, not on a re-entry
  week. It is the habit that survived 8 of 21 zero-ride weeks in the reference history.
- **Legs are never loaded next to a long ride.** `_strength_for_week` resolves that from the
  pattern it is actually given.

The rotation has no terminus and nothing tapers. Blocks are modular by design — the source
deliberately omits a fixed base phase and says they run "in whatever order is most appropriate".
Periodisation is a property of how ROTATION is ordered, not of this machinery.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from src.analysis.dates import monday_of
from src.analysis.derive import EASY_IF_CEILING, HARD_IF_FLOOR
from src.planning import archetypes as A
from src.planning.progression import TIERS

# From planning/constants.py, which imports nothing but src.config. The three readers in
# src.analysis that need to filter on the plan name cannot import this module — blocks.py already
# imports src.analysis.derive above, so the dependency would be circular.
#
# Called, not assigned to a module constant: a constant read into a default argument binds at
# import and no later config change can reach it, which is the defect this step removes.
from src.planning.constants import (
    goal_date as configured_goal_date, plan_name as configured_plan_name)

# Weekday roles a slot can take.
ROLES = ("hard", "long", "lit", "strength")

# How _hard_session resolves a block's hard slot. "base" carries no hard slot at all.
BLOCK_TYPES = ("sit", "hiit", "threshold", "base")

MAX_HARD_PER_WEEK = 2

# The most ride slots any tier asks for. A block must declare at least this many, since
# pattern_for only reduces.
MAX_TIER_RIDES = max(t.rides for t in TIERS.values())

# Ride slots are dropped in this order when a tier asks for fewer rides than the block declares.
# The long ride is never dropped — it is the session the plan exists to protect — and a hard slot
# becomes an easy ride rather than disappearing, so the week keeps its session count where it can.
_DROP_ORDER = ("lit", "hard")


@dataclass
class Block:
    name: str
    block_type: str                     # sit | hiit | threshold | base
    weeks: int
    pattern: list[tuple[int, str]]      # (weekday 0=Mon, role) — the NOMINAL full week
    hard_variants: list[str] = field(default_factory=list)
    recovery_weeks: set[int] = field(default_factory=set)   # 1-based, within the block
    # Readable phase name for training_plan_weeks / cycle reviews.
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
        # One session per weekday was the rule until ride days became configurable. At six or
        # seven riding days there is no empty day left for a lift, and a ride plus a lift on one
        # day is ordinary practice rather than a mistake — `prescribed_sessions` is keyed on
        # (plan, date, role), so both store and push. What stays forbidden is *two rides* on one
        # day, which is a generator error every time.
        if len(set(self.pattern)) != len(self.pattern):
            raise ValueError(f"block {self.name!r} repeats a (weekday, role) slot")
        rides_per_day = {}
        for day, role in self.pattern:
            if role != "strength":
                rides_per_day[day] = rides_per_day.get(day, 0) + 1
        doubled = [d for d, n in rides_per_day.items() if n > 1]
        if doubled:
            raise ValueError(
                f"block {self.name!r} schedules two rides on weekday {doubled[0]}")
        if self.block_type not in BLOCK_TYPES:
            # Unvalidated, a typo fell through _hard_session's if/elif chain to threshold, so a
            # block labelled "hiit " would silently prescribe threshold work for six weeks.
            raise ValueError(
                f"block {self.name!r} has unknown block_type {self.block_type!r}; "
                f"expected one of {', '.join(sorted(BLOCK_TYPES))}")
        if not any(r == "long" for _, r in self.pattern):
            raise ValueError(f"block {self.name!r} declares no long ride")
        if not any(r != "strength" for _, r in self.pattern):
            raise ValueError(f"block {self.name!r} declares no ride at all")
        # The "enough ride slots for the top tier" check used to live here, against the module
        # ladder. It cannot: the pattern is now built per athlete and the ladder is the plan's,
        # so a block has no way to know what it is being asked for. `rotation_for` checks it,
        # because that is the one place that holds both.

    @property
    def phase(self) -> str:
        return self.phase_label or self.block_type


# ── the rotation ──────────────────────────────────────────────────────────────
#
# Nominal weekdays are Tue hard/easy, Wed strength, Thu easy, Sat long, Sun strength — which is
# where the athlete's rides actually fall. Consistency carries no hard slot at all; the three
# emphasis blocks each carry one, and which archetype it resolves to is what "rotation" means.
#
# Six weeks with recovery in weeks 3 and 6 is the source's one worked block structure, quoted
# verbatim. SIT is short because the source warns it "would not be able to just repeat this
# workout forever", and it sits early because the source uses it to "kick-start the first couple
# weeks of a plan" — the opposite end of the plan from where the old 32-week ramp put it.

# The weekly template, generated rather than written down.
#
# It was two literal lists with three ride slots each, and because `pattern_for` only ever
# *reduces* a pattern, three was a hard ceiling for every athlete the project would ever have.
# Someone who rides five days a week had no way to say so, and `bootstrap` ended up clamping
# its derivation to a limit that was really just the shape of these two lines.
#
# Generated from `(ride_days, strength_days)` instead. For (3, 2) it reproduces the two lists
# exactly, which is asserted in tests/test_patterns.py — same discipline as the config
# refactor's byte-identical check, because this template decides every athlete's week.

LONG_DAY = 5          # Saturday. The long ride anchors the week and everything places around it.
HARD_DAY = 1          # Tuesday. Early enough to leave the long ride intact at the weekend.

# The order ride days are filled in after the long ride and the emphasis session, chosen to
# spread sessions rather than block them together: Thursday, then Sunday beside the long ride,
# then midweek, then the Monday and Friday that are rest days in a three-ride week.
_RIDE_DAY_PREFERENCE = (3, 6, 2, 0, 4)


def build_pattern(ride_days: int, strength_days: int = 2, hard: bool = True
                  ) -> list[tuple[int, str]]:
    """
    One week's slots as `(weekday, role)`, for an athlete who rides `ride_days` days.

    This is the *nominal* week at the top of the ladder. `pattern_for` reduces it for lower
    tiers, which is why it is built at the cap and never per tier.

    Strength days are chosen rather than fixed, by preferring rest days, then maximum spacing,
    then the day after a ride. At six or seven ride days there are no rest days left and
    strength doubles up onto a riding day — never onto the long ride or the hard session, which
    is the one placement rule here that is about safety rather than tidiness.
    """
    if not 1 <= ride_days <= 7:
        raise ValueError(f"ride_days must be 1-7, got {ride_days}")
    if not 0 <= strength_days <= 7:
        raise ValueError(f"strength_days must be 0-7, got {strength_days}")

    rides = [LONG_DAY]
    if ride_days > 1:
        rides.append(HARD_DAY)
    for day in _RIDE_DAY_PREFERENCE:
        if len(rides) >= ride_days:
            break
        rides.append(day)
    rides = sorted(rides[:ride_days])

    pattern = []
    for day in rides:
        if day == LONG_DAY:
            pattern.append((day, "long"))
        elif day == HARD_DAY and hard and ride_days > 1:
            pattern.append((day, "hard"))
        else:
            pattern.append((day, "lit"))

    # Appended, not merged: at six or seven ride days a lift shares a day with a ride, and both
    # are real sessions. `prescribed_sessions` is keyed on (plan, date, role), so two roles on
    # one day store and push correctly.
    pattern += [(day, "strength") for day in _strength_days(rides, strength_days)]
    return sorted(pattern)


def _strength_days(rides: list[int], count: int) -> list[int]:
    """
    Which days carry a lift, given the ride days.

    Scored, not hardcoded, so the answer survives any ride-day count. Priority is rest days
    first (a lift on an otherwise empty day is the cheapest place to put one), then spacing
    between the lifts, then landing the day after a ride. For three ride days that produces
    Wednesday and Sunday, which is what the hand-written template said.
    """
    from itertools import combinations

    if count <= 0:
        return []
    candidates = [d for d in range(7) if d not in (LONG_DAY, HARD_DAY)]
    if count > len(candidates):
        raise ValueError(f"cannot place {count} strength days without using the long ride "
                         f"or the hard session")

    def gap(days: tuple[int, ...]) -> int:
        if len(days) < 2:
            return 7
        ordered = sorted(days)
        wrap = (ordered[0] + 7) - ordered[-1]
        return min(min(b - a for a, b in zip(ordered, ordered[1:])), wrap)

    def score(days: tuple[int, ...]) -> tuple[int, int, int]:
        rest_days = sum(1 for d in days if d not in rides)
        follows_ride = sum(1 for d in days if (d - 1) % 7 in rides)
        return rest_days, gap(days), follows_ride

    return sorted(max(combinations(candidates, count), key=score))


_EASY_WEEK = build_pattern(3, hard=False)
_EMPHASIS_WEEK = build_pattern(3, hard=True)

ROTATION: list[Block] = [
    Block(
        name="Consistency", phase_label="consistency",
        block_type="base", weeks=4, pattern=list(_EASY_WEEK),
        recovery_weeks={4},
        note="Entry block, and where a long absence restarts. No hard session at any tier: "
             "when starting from a lower baseline the first thing to build is consistency.",
    ),
    Block(
        name="Sprints", phase_label="sit",
        block_type="sit", weeks=3, pattern=list(_EMPHASIS_WEEK),
        hard_variants=["short", "medium", "long"],
        recovery_weeks={3},
        note="Short block by design — the source is explicit that SIT cannot be repeated "
             "indefinitely. Cheap, and it costs the long ride almost nothing.",
    ),
    Block(
        name="Threshold", phase_label="threshold",
        block_type="threshold", weeks=6, pattern=list(_EMPHASIS_WEEK),
        hard_variants=["continuous", "long_reps", "sweet_spot", "medium_reps",
                       "short_reps", "over_unders"],
        recovery_weeks={3, 6},
        note="Targets hedged the full 10% below threshold, rested ~10:1. Six variants so no "
             "two weeks of the block repeat a session.",
    ),
    Block(
        name="VO2", phase_label="hiit",
        block_type="hiit", weeks=6, pattern=list(_EMPHASIS_WEEK),
        hard_variants=["straight_5", "straight_4", "straight_6", "thirty_fifteen",
                       "straight_8", "hard_start"],
        recovery_weeks={3, 6},
        note="The source's worked example: six weeks, recovery in 3 and 6. Sub-maximal — the "
             "aim is the highest load sustainable for the whole session, half a rep in hand.",
    ),
]

# After the entry block the rotation loops over the emphasis blocks indefinitely. Index 0 is
# re-entered only by a long absence.
ROTATION_LOOP_FROM = 1


def rotation_after(block_index: int) -> int:
    """The next block in the rotation, looping over the emphasis blocks."""
    nxt = block_index + 1
    return nxt if nxt < len(ROTATION) else ROTATION_LOOP_FROM


def advance_position(block_index: int, week_in_block: int, rotation: str) -> tuple[int, int]:
    """
    Where the next week sits in the rotation, given the gate's `rotation` verdict.

    The three verdicts come from `progression.next_state` and mean what they say:

    - `continue` — the next week of this block, rolling into the next block at its end.
    - `restart_block` — week 1 of the same block. A 2-3 week absence loses the block's
      accumulated progression but not its emphasis; resuming at week 5 of a 6-week threshold
      block after a fortnight off asks for a position that was never earned.
    - `restart_rotation` — week 1 of block 0, which is Consistency. Four weeks off is a cold
      start, and Consistency is the only block carrying no hard session at any tier.

    Separated from the gate because the gate is deliberately free of any knowledge of blocks —
    it decides dose and re-entry, and the rotation is this module's business.
    """
    if rotation == "restart_rotation":
        return 0, 1
    if rotation == "restart_block":
        return block_index, 1
    nxt = week_in_block + 1
    if nxt > ROTATION[block_index].weeks:
        return rotation_after(block_index), 1
    return block_index, nxt


# ── tier-aware pattern ────────────────────────────────────────────────────────

def pattern_for(block: Block, tier: int, recovery: bool = False,
                reentry: bool = False,
                tiers: dict | None = None) -> list[tuple[int, str]]:
    """
    The nominal pattern reduced to what this tier actually asks for.

    Reduce, never build up — a tier can take a session away but never add one the block did not
    declare, which keeps the hard-session cap true by construction rather than by re-validation.

    Strength slots pass through untouched at every tier, including the floor, recovery weeks and
    re-entry weeks.
    """
    spec = (tiers or TIERS)[tier]
    rides = [(d, r) for d, r in block.pattern if r != "strength"]
    strength = [(d, r) for d, r in block.pattern if r == "strength"]

    hard_allowed = min(spec.hard, sum(1 for _, r in rides if r == "hard"))
    if recovery or reentry:
        hard_allowed = 0

    # A disallowed hard slot becomes an easy ride rather than vanishing: the session count is
    # what the athlete is being asked to keep, and dropping a day is a bigger ask than easing it.
    out = []
    hard_seen = 0
    for day, role in rides:
        if role == "hard":
            hard_seen += 1
            out.append((day, "hard" if hard_seen <= hard_allowed else "lit"))
        else:
            out.append((day, role))

    # Trim to the tier's ride count, dropping easy rides before hard ones and never the long.
    for role in _DROP_ORDER:
        while len(out) > spec.rides:
            droppable = [i for i, (_, r) in enumerate(out) if r == role]
            if not droppable:
                break
            out.pop(droppable[-1])          # latest in the week first
    return sorted(out + strength)


def _strength_for_week(pattern: list[tuple[int, str]], week: int) -> dict[int, str]:
    """
    Assign a lift pattern to each strength day in the pattern it is actually given.

    Two rules interact and the order matters. Legs must not be loaded on either side of a long
    ride, so those slots are pinned to upper body first; only then do the remaining slots rotate,
    which stops a week lifting the same pattern twice and wasting the second session.

    **Takes a pattern, not a Block.** It read `block.pattern` until 2026-11-02, which is the
    nominal full week — so once a tier reduces the pattern it would compute adjacency against
    sessions that are not in the week being generated. An earlier version of the same bug
    hardcoded weekdays 0 and 4 while every block scheduled strength on 2 and 6, so the pin never
    fired and 6 of 19 weeks put lower body on the Sunday after the Saturday long ride.

    Wrapping mod 7 is deliberate: the plan repeats weekly, so Monday follows the previous week's
    Sunday. If both strength days abut a long ride they both go upper — protecting the legs
    matters more than varying the pattern.
    """
    days = sorted(d for d, r in pattern if r == "strength")
    long_days = {d for d, r in pattern if r == "long"}
    out: dict[int, str] = {}
    for d in days:
        if long_days & {(d - 1) % 7, (d + 1) % 7}:
            out[d] = "upper"
    remaining = [d for d in days if d not in out]
    pool = [v for v in ("lower", "full", "upper") if v not in out.values()] or ["full"]
    for i, d in enumerate(remaining):
        out[d] = pool[(week + i) % len(pool)]
    return out


def _hard_session(block: Block, week_in_block: int) -> A.Session:
    """
    Resolve the hard slot to a concrete session.

    The variant rotates by week so no two weeks of a block repeat a session. There is no dose
    progression here any more — that was `bump = (week_in_block - 1) // 2`, which added reps
    purely because the calendar advanced.
    """
    variants = block.hard_variants or [None]
    variant = variants[(week_in_block - 1) % len(variants)]
    if block.block_type == "sit":
        return A.sit(variant or "medium")
    if block.block_type == "hiit":
        return A.hiit(variant or "straight_5")
    return A.threshold(variant or "continuous")


def expand_week(block: Block, week_in_block: int, tier: int, monday: date,
                plan_name: str | None = None, block_index: int = 1,
                week_type: str = "normal", tiers: dict | None = None) -> list[dict]:
    """
    One week of dated prescriptions, at a tier.

    Durations come from `TIERS[tier]`; `week_in_block` selects only the hard variant and whether
    this is a recovery week. A recovery or re-entry week drops the hard session and shortens the
    long ride rather than scaling everything, because duration is the adaptation being protected
    and it is the least fatiguing thing to keep.
    """
    plan_name = plan_name or configured_plan_name()
    spec = (tiers or TIERS)[tier]
    recovery = week_in_block in block.recovery_weeks
    reentry = week_type == "reentry"
    pattern = pattern_for(block, tier, recovery=recovery, reentry=reentry, tiers=tiers)
    strength_variants = _strength_for_week(pattern, week_in_block)

    long_min = spec.long_min - (15 if (recovery or reentry) else 0)

    out: list[dict] = []
    for weekday, role in pattern:
        day = monday_of(monday) + timedelta(days=weekday)
        if role == "hard":
            session = _hard_session(block, week_in_block)
        elif role == "long":
            # A sprint every ~25min on the long ride, which is the source's own prescription
            # ("throw in a sprint every 20-30 minutes"), on alternating weeks.
            session = A.lit(long_min, "endurance_sprints" if week_in_block % 2 == 0 else "steady")
        elif role == "lit":
            session = A.lit(spec.lit_min,
                            "cadence_drills" if week_in_block % 2 == 0 else "steady")
        else:
            session = A.strength(40, strength_variants[weekday])
        out.append({
            "plan_name": plan_name,
            "date": day.isoformat(),
            "block_index": block_index,
            "block_name": block.name,
            "week_in_block": week_in_block,
            "is_recovery_week": recovery,
            "week_type": week_type,
            "tier": tier,
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


def plan_week_row(monday: date, block: Block, block_index: int, week_in_block: int,
                  plan_week_number: int, tier: int, week_type: str = "normal",
                  plan_name: str | None = None, goal_date: str | None = None,
                  plan_start: str | None = None) -> dict:
    """
    One row for `training_plan_weeks`, derived from the same block the sessions come from.

    That table was hand-seeded from TrainerRoad's phase view. Generating both from one source
    makes the plan_name join correct by construction rather than by remembering to keep two
    constants in step.

    `plan_end_date` is `plan.goal_date` from configuration rather than a plan end, because the
    rotation has no end — it is labelled as a goal wherever it is rendered.
    """
    plan_name = plan_name or configured_plan_name()
    goal_date = goal_date or configured_goal_date()
    start = monday_of(monday)
    recovery = week_in_block in block.recovery_weeks
    return {
        "plan_name": plan_name,
        "plan_start_date": plan_start or start.isoformat(),
        "plan_end_date": goal_date,
        "week_start_date": start.isoformat(),
        "week_end_date": (start + timedelta(days=6)).isoformat(),
        "week_type": ("reentry" if week_type == "reentry"
                      else "rest" if recovery else block.phase),
        "phase": block.phase,
        "phase_number": block_index,
        "phase_week_number": week_in_block,
        "plan_week_number": plan_week_number,
        "volume_tier": tier,
    }


# ── reporting ─────────────────────────────────────────────────────────────────

def weekly_rollup(sessions: list[dict]) -> list[dict]:
    """Per-week totals — the view that shows whether the intensity balance actually holds."""
    weeks: dict[str, dict] = {}
    for s in sessions:
        monday = monday_of(date.fromisoformat(s["date"])).isoformat()
        w = weeks.setdefault(monday, {
            "week_start": monday, "block": s["block_name"],
            "recovery": s["is_recovery_week"], "tier": s.get("tier"), "sessions": 0, "rides": 0,
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


def intensity_split(sessions: list[dict]) -> dict:
    """
    Share of ride time by intensity band, on exactly the basis analysis/derive.py uses.

    This claimed to match `derive.py` while using **two** bands against its three: everything at
    or above 0.75 was reported as hard, where `derive` calls 0.75-0.85 moderate. The thresholds
    are imported rather than redefined so the two cannot drift apart again.

    Strength is excluded - 80/20 is an endurance rule, and resistance work scores easy on any
    power or HR measure while costing real fatigue.
    """
    bands = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    hard_sessions = 0
    for s in sessions:
        if s["archetype"] == "strength":
            continue
        if s["role"] == "hard":
            hard_sessions += 1
        intensity = s["intensity_factor"]
        band = ("easy" if intensity < EASY_IF_CEILING
                else "moderate" if intensity < HARD_IF_FLOOR else "hard")
        bands[band] += s["minutes"]
    total = sum(bands.values())
    return {
        "hours": {k: round(v / 60, 1) for k, v in bands.items()},
        "pct": {k: (round(v / total * 100, 1) if total else None) for k, v in bands.items()},
        # Reported alongside the time split because the time split alone is actively
        # misleading for sprint work. A SIT session is 80 seconds of all-out effort inside 45
        # minutes, so its whole-session IF is 0.59 and it bands as "easy" — a week built around
        # 8x10s maximal sprints read as "100% easy". The time figure is not wrong, it is just
        # answering a different question, and the source's own rule is about session *count*
        # ("zero to two structured high-intensity sessions each week"), not time in zone.
        "hard_sessions": hard_sessions,
    }


def rotation_for(ride_days: int, strength_days: int = 2) -> list[Block]:
    """
    The rotation, with its weekly template built for an athlete who rides `ride_days` days.

    `ROTATION` is this at (3, 2) and stays the module-level default, because every test and the
    whole progression suite reasons about that shape. A plan whose derived ladder asks for more
    builds its own.

    The check that used to sit in `Block.__post_init__` lives here: `pattern_for` only ever
    *reduces* a pattern, so a block declaring fewer ride slots than the top tier asks for hands
    back a short week at that tier rather than failing. Only this function holds both the
    pattern and the ladder it is being built for, so only this function can check it.
    """
    easy = build_pattern(ride_days, strength_days, hard=False)
    emphasis = build_pattern(ride_days, strength_days, hard=True)
    return [
        Block(name=b.name, phase_label=b.phase_label, block_type=b.block_type, weeks=b.weeks,
              pattern=list(easy if b.block_type == "base" else emphasis),
              hard_variants=list(b.hard_variants), recovery_weeks=set(b.recovery_weeks),
              note=b.note)
        for b in ROTATION
    ]


def rotation_rides(rotation: list[Block]) -> int:
    """How many ride slots the thinnest block in a rotation declares."""
    return min(sum(1 for _, r in b.pattern if r != "strength") for b in rotation)

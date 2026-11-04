"""
The twelve-week view: shape without a promise.

**Computed on demand and never stored.** A dated twelve-week plan is exactly what this rebuild
removed. Progression depends on what gets ridden, so any week beyond the horizon is a
projection, and storing it would turn it into a commitment the athlete can fall behind — which
is the mechanism that ended the September plan.

What it can honestly say is the *shape*: which emphasis block comes when, where the recovery
weeks fall, and what tier you reach under an assumption that is stated rather than hidden.
"At 3 of 4 weeks met you reach T4 by March" is a projection with its premise visible. "Week 19:
4h30" is a lie with a date on it.

The default assumption is deliberately not "every week met". The reference history's best run
was twelve consecutive weeks; its median was closer to two weeks on, one off. A projection that
assumes perfection produces the same unreachable curve as the old ramp.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from src.analysis.dates import monday_of
from src.planning.blocks import ROTATION, advance_position
from src.planning.constants import PLAN_NAME
from src.planning.progression import (
    CAP_TIER, FLOOR_TIER, STRENGTH_GATED_TIER, STRENGTH_GATED_WEEKS,
    STRENGTH_WINDOW, TIERS,
)

OUTLINE_WEEKS = 12

# Weeks met out of every four, for the default projection. Three of four is close to the
# athlete's own best sustained stretch without assuming the best stretch repeats forever.
DEFAULT_MET_IN_FOUR = 3


@dataclass(frozen=True)
class OutlineWeek:
    week_start: str
    plan_week: int
    block: str
    week_in_block: int
    recovery: bool
    tier: int
    hours: float
    projected: bool          # False only for weeks inside the committed horizon
    met_assumed: bool | None  # None for horizon weeks, where nothing is assumed


def outline(start_monday: date, tier: int, block_index: int, week_in_block: int,
            weeks: int = OUTLINE_WEEKS, horizon_weeks: int = 3,
            met_in_four: int = DEFAULT_MET_IN_FOUR) -> list[OutlineWeek]:
    """
    The next `weeks` weeks of shape, from a starting position the gate supplies.

    Pure: no database, no clock. The tier only moves on an assumed-met week, and only outside
    the horizon — inside it, the tier is what the gate actually decided and nothing is guessed.

    A recovery week is never counted as a promotion opportunity. It asks for less by design, so
    treating it as a week that could advance the tier would project growth out of a rest.
    """
    out: list[OutlineWeek] = []
    met_cycle = 0
    for offset in range(weeks):
        monday = monday_of(start_monday) + timedelta(weeks=offset)
        block = ROTATION[block_index]
        recovery = week_in_block in block.recovery_weeks
        in_horizon = offset < horizon_weeks

        met = None
        if not in_horizon and not recovery:
            # Spread the assumption evenly rather than front-loading it: met on the first
            # `met_in_four` of each four, which is what "3 of 4 weeks" means as a cadence.
            met = (met_cycle % 4) < met_in_four
            met_cycle += 1

        out.append(OutlineWeek(
            week_start=monday.isoformat(),
            plan_week=offset + 1,
            block=block.name,
            week_in_block=week_in_block,
            recovery=recovery,
            tier=tier,
            hours=TIERS[tier].target_hours,
            projected=not in_horizon,
            met_assumed=met,
        ))

        if met:
            tier = min(CAP_TIER, tier + 1)
        block_index, week_in_block = advance_position(block_index, week_in_block, "continue")
    return out


def summarise(rows: list[OutlineWeek], met_in_four: int = DEFAULT_MET_IN_FOUR,
              strength_recently_held: bool | None = None) -> str:
    """
    One paragraph a coach or a person can read, with the assumption stated in it.

    The assumption is in the same sentence as the conclusion on purpose. Put it in a footnote
    and the number travels without it.
    """
    if not rows:
        return "No outline available."
    committed = [r for r in rows if not r.projected]
    projected = [r for r in rows if r.projected]
    end_tier = rows[-1].tier
    start_tier = rows[0].tier

    blocks: list[str] = []
    for r in rows:
        if not blocks or not blocks[-1].startswith(r.block):
            blocks.append(r.block)
    sequence = " -> ".join(blocks)

    lines = [
        f"Committed: {len(committed)} weeks of dated sessions at T{start_tier} "
        f"({TIERS[start_tier].target_hours}h/wk).",
        f"Beyond that, {len(projected)} weeks of shape only: {sequence}.",
    ]
    if end_tier > start_tier:
        when = next(r.week_start for r in rows if r.tier == end_tier)
        lines.append(
            f"At {met_in_four} of 4 weeks met you reach T{end_tier} "
            f"({TIERS[end_tier].target_hours}h/wk) by {when} — a projection on that "
            f"assumption, not a schedule."
        )
    elif end_tier == CAP_TIER:
        lines.append(f"Already at the T{CAP_TIER} ceiling; going higher is a decision, "
                     f"not a consequence of a good streak.")
    else:
        lines.append(f"Tier holds at T{end_tier} across the window on that assumption.")
    # Volume met is not the only condition on the way up, and the other one is easy to hide.
    # Entering the 3.5-4h band costs the anchor habit; unsaid, the tier curve above reads as
    # though riding alone gets you there.
    if start_tier < STRENGTH_GATED_TIER <= end_tier:
        caveat = (f"T{STRENGTH_GATED_TIER} additionally requires strength in "
                  f"{STRENGTH_GATED_WEEKS} of the last {STRENGTH_WINDOW} weeks")
        if strength_recently_held is False:
            caveat += (f" — not currently being met, so on today's evidence the climb stops "
                       f"at T{STRENGTH_GATED_TIER - 1}")
        lines.append(caveat + ".")
    if start_tier == FLOOR_TIER:
        lines.append("Starting at the floor: the first job is consistency, not load.")
    return "\n".join(lines)


def from_state(state: dict, today: date, weeks: int = OUTLINE_WEEKS,
               met_in_four: int = DEFAULT_MET_IN_FOUR) -> dict:
    """Outline plus summary, from a `progression_store.current_state` dict."""
    rows = outline(monday_of(today), state["tier"], state["block_index"],
                   state["week_in_block"], weeks=weeks, met_in_four=met_in_four)
    recent = state.get("recent") or []
    held = sum(1 for r in recent[-STRENGTH_WINDOW:] if r.get("habit_held"))
    strength_ok = (held >= STRENGTH_GATED_WEEKS) if recent else None
    return {
        "plan_name": PLAN_NAME,
        "assumption": f"{met_in_four} of every 4 non-recovery weeks met",
        "strength_recently_held": strength_ok,
        "weeks": [vars(r) for r in rows],
        "summary": summarise(rows, met_in_four, strength_ok),
    }

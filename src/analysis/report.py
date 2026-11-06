"""
Assembles all analysis signals into a structured coaching context.
This is what gets passed to Claude as the data payload.
"""

from datetime import date, timedelta
from .fitness import current_fitness, ctl_trend, peak_ctl, ctl_history
from .activities import recent_activities, sport_distribution, weekly_load_by_sport, yearly_volume
from src.config import config
from src.planning import outgrown as outgrown_check, plans, tiers_store
from src import goals as goals_store
from src.planning.constants import plan_name
from src.planning.outline import from_state as plan_outline
from src.planning.progression import TIERS
from src.planning.progression_store import current_state
from .wellness import hrv_summary, sleep_summary, rhr_trend, recovery_flags
from .compliance import compliance_summary, recent_planned_workouts
from .training_plan import plan_summary, current_cycle
from .cycle import latest_review
from .weekly import summaries_between, summary_lines
from .load import load_correction_text
from .derive import derived_text
from src.athlete.profile import current_profile
from src.db.schema import read_connection
from src.clock import today_local


def _with_weekday(iso_date: str) -> str:
    """'2026-08-27' -> '2026-08-27 Thu'. The model otherwise infers weekdays from the ISO date
    and gets them wrong, which matters because athlete notes refer to sessions by weekday."""
    try:
        return f"{iso_date} {date.fromisoformat(iso_date).strftime('%a')}"
    except (ValueError, TypeError):
        return iso_date


# Total characters of coaching memory allowed into the snapshot. A check-in runs 1500-3000
# chars, so this holds the most recent of each type in full with room to spare.
MEMORY_CHAR_BUDGET = 6000
# Most recent N of each coaching_log.type. One is enough: the job of this block is to stop the
# coach repeating last week verbatim, and the longer arc now lives in cycle_reviews.
MEMORY_PER_TYPE = 1


# How far back raw per-session detail (including full athlete notes) stays in the snapshot.
# Two weeks covers the current week plus the last one, which is the span a weekly check-in
# actually reasons about session by session. Earlier weeks in the cycle arrive as summaries.
RAW_ACTIVITY_DAYS = 14


def _raw_activity_horizon() -> str:
    return (today_local() - timedelta(days=RAW_ACTIVITY_DAYS)).isoformat()


def _earlier_week_summaries() -> list[dict]:
    """
    Stored summaries for weeks in the current cycle that fall outside the raw horizon.

    Scoped to the current cycle rather than a rolling window: the cycle is the unit the athlete
    trains in, and once a cycle closes its own review supersedes the weekly summaries inside it.
    """
    cyc = current_cycle()
    if not cyc:
        return []
    horizon = _raw_activity_horizon()
    return [s for s in summaries_between(cyc["start_date"], cyc["end_date"])
            if s["week_end"] < horizon]


def _recent_coaching_memory(limit: int = MEMORY_PER_TYPE) -> list[str]:
    """
    Most recent `limit` entries of *each* coaching_log type, in full.

    Replaces an `ORDER BY created_at DESC LIMIT 3` with no type filter and a 300-char
    truncation at render. Both were doing real damage: every one of the stored entries ran
    longer than 300 chars, so roughly 85% of each was discarded mid-sentence, and because the
    query was type-blind a run of weekly check-ins would push session summaries out entirely.

    Entries are trimmed only if the whole block exceeds MEMORY_CHAR_BUDGET, oldest first, and
    a trimmed entry is cut at a paragraph boundary with an explicit marker rather than
    silently mid-word.
    """
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT date, type, content FROM coaching_log
            WHERE id IN (
                SELECT id FROM coaching_log c2
                WHERE c2.type = coaching_log.type
                ORDER BY created_at DESC LIMIT ?
            )
            ORDER BY created_at
        """, (limit,)).fetchall()

    entries = [(r["date"], r["type"], r["content"] or "") for r in rows]
    total = sum(len(c) for _, _, c in entries)
    out = []
    for i, (d, t, content) in enumerate(entries):
        # Trim the oldest entries first while the block is over budget.
        remaining = len(entries) - i
        if total > MEMORY_CHAR_BUDGET and remaining > 1:
            allowance = max(600, MEMORY_CHAR_BUDGET // len(entries))
            if len(content) > allowance:
                cut = content.rfind("\n\n", 0, allowance)
                content = (content[:cut if cut > 300 else allowance].rstrip()
                           + "\n  [...trimmed for length]")
                total -= len(content)
        out.append(f"[{d} {t}] {content}")
    return out


def _recent_life_events(days: int = 90) -> list[dict]:
    """Life events from the last N days."""
    since = (today_local() - timedelta(days=days)).isoformat()
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT start_date, end_date, type, severity, note
            FROM life_events WHERE start_date >= ? ORDER BY start_date DESC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def _recent_subjective_feel(days: int = 14) -> list[dict]:
    """Recent subjective feel entries."""
    since = (today_local() - timedelta(days=days)).isoformat()
    with read_connection() as conn:
        rows = conn.execute("""
            SELECT date, feel_score, energy, motivation, legs, tr_compliance, notes
            FROM subjective_feel WHERE date >= ? ORDER BY date DESC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def athlete_profile() -> dict:
    """
    Who the athlete is, from configuration — the block the coach prompt opens with.

    This was a module-level dict with a real name, a real device and a real sentence about
    small children in it. None of that is derivable from the data, which is why it was written
    down; all of it is personal, which is why it now comes from a file that is not committed.

    Goals are not here: they live in the `goals` table (src/goals.py), which is the one an edit
    from the app or from a conversation writes to.
    """
    from src.planning.constants import available_days, long_ride_day

    a = config().athlete
    days, long_day = available_days(), long_ride_day()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {
        "name": a.name,
        "history_start": str(a.history_start) if a.history_start else None,
        "primary_sport": a.primary_sport,
        "current_tools": list(a.tools),
        "equipment": a.equipment,
        "notes": a.notes,
        # Only when configured. Absent means every day, and a coach told "can train any day"
        # would be saying something the athlete never said. Present, it is what stops a
        # suggestion to "add an easy spin Monday" landing on someone who cannot train Mondays.
        "available_days": [names[d] for d in days] if days is not None else None,
        "long_ride_day": names[long_day] if long_day is not None else None,
    }


def _rolling_plan(today: date | None = None) -> dict | None:
    """
    Where the gate has put the athlete, what it asks for next, and the shape beyond that.

    The coach could see plan *position* and plan *adherence* but had no way to answer "what am
    I doing this week" — the sessions existed in the database and were read by nothing. That
    left the model reasoning about compliance against workouts it could not name.

    Read-only: `persist=False`, because building a report must never record a training
    decision. The gate's own catch-up runs on sync, which is where that belongs.
    """
    today = today or today_local()
    try:
        with read_connection() as conn:
            # The plan's own ladder, so what is reported is what was prescribed. Falling back
            # to the module constant here would print 3.42h beside a calendar showing 3.5h.
            plan = plans.by_name(conn, plan_name())
            ladder = tiers_store.load(conn, plan.id) if plan else None
            state = current_state(conn, today, persist=False, tiers=ladder)
            upcoming = [dict(r) for r in conn.execute(
                "SELECT date, role, prescription, minutes, tss, volume_tier, week_type "
                "FROM prescribed_sessions WHERE plan_name = ? AND date >= ? "
                "ORDER BY date LIMIT 10", (plan_name(), today.isoformat()))]
            outgrown = _outgrown(conn, plan, ladder, today) if plan and ladder else None
        return {
            "tier": state["tier"],
            "tier_hours": (ladder or TIERS)[state["tier"]].target_hours,
            "outgrown": outgrown,
            "week_type": state["week_type"],
            "reason": state["reason"],
            "decided_through": state["decided_through"],
            "upcoming": upcoming,
            "outline": plan_outline(state, today, tiers=ladder),
        }
    except Exception:
        # Advisory context. A failure here must not take down the whole snapshot, which the
        # coach needs for everything else it reasons about.
        return None


def build_coaching_context(
    recent_days: int = 28,
    trend_weeks: int = 8,
    history_weeks: int = 52,
) -> dict:
    """Full coaching context snapshot. Pass this to the Claude coaching layer."""
    today = today_local().isoformat()

    fitness = current_fitness()
    peak = peak_ctl(years=9)
    trend = ctl_trend(weeks=trend_weeks)
    weekly_history = ctl_history(weeks=history_weeks)

    recent = recent_activities(days=recent_days)
    sport_dist = sport_distribution(days=90)
    weekly_load = weekly_load_by_sport(weeks=12)
    annual = yearly_volume()

    hrv = hrv_summary(days=30)
    sleep = sleep_summary(days=30)
    rhr = rhr_trend(days=30)
    # 30-day window: the flag strings name a 30-day baseline, and their 7-day sub-averages are
    # computed from the tail of it. Passing 7 here made those strings inaccurate (Session 14).
    flags = recovery_flags(days=30)

    plan_adherence = compliance_summary(weeks=8)
    recent_planned = recent_planned_workouts(days=14)

    # Weeks until each dated goal, soonest first, from the goals table. This used to
    # name two specific events in source — a CX date and a gravel date — and count down to
    # them. Which events those are is the athlete's business, and an `opt_in` goal is one the
    # rotation deliberately does not taper for: the old plan peaked for the secondary goal and
    # tapered four months before the primary one, which is how a secondary goal ended up
    # shaping a whole season. Saying the priority beside the count is what stops that.
    today_d = today_local()
    with read_connection() as conn:
        live_goals = goals_store.active(conn)
        dated_goals = goals_store.dated(conn)
    goal_timeline = [
        {"description": desc, "date": when.isoformat(), "priority": priority,
         "weeks_away": (when - today_d).days // 7}
        for desc, when, priority in dated_goals
    ]
    goals_list = [g["description"] for g in live_goals]

    # Manually-maintained stats (src/athlete/profile.py) — source of truth over
    # intervals.icu's icu_ftp, which has no user-facing edit path and lags real changes.
    stats = current_profile()

    return {
        "generated": today,
        "athlete": {**athlete_profile(), "goals": goals_list, "stats": stats},
        "timeline": {"goals": goal_timeline},
        "fitness": {
            "current": fitness,
            "trend": trend,
            "peak_ever": peak,
            "pct_of_peak": round(fitness.get("ctl", 0) / peak.get("ctl", 1) * 100, 1) if peak else None,
            "weekly_history": weekly_history[-16:],
        },
        "recovery": {
            "hrv": hrv,
            "sleep": sleep,
            "resting_hr": rhr,
            "flags": flags,
        },
        "training": {
            "recent_activities": recent,
            "sport_distribution_90d": sport_dist,
            "weekly_load_12w": weekly_load,
            "annual_volume": annual,
            "plan_adherence_8w": plan_adherence,
            "recent_planned_workouts": recent_planned,
            # None when today falls outside any seeded plan — see seed_training_plan.py.
            "plan_position": plan_summary(),
            "rolling_plan": _rolling_plan(),
            "earlier_week_summaries": _earlier_week_summaries(),
        },
        "memory": {
            # Injected as its own field rather than competing for _recent_coaching_memory()'s
            # three type-blind slots, where three weekly check-ins would evict it inside a
            # month — exactly when the next cycle needs it.
            "last_cycle_review": latest_review(),
            "coaching_notes": _recent_coaching_memory(limit=3),
            "life_events": _recent_life_events(days=90),
            "subjective_feel": _recent_subjective_feel(days=14),
        },
    }


def coaching_context_text(ctx: dict | None = None) -> str:
    """Render the coaching context as formatted text for the Claude system prompt."""
    if ctx is None:
        ctx = build_coaching_context()

    fitness = ctx["fitness"]
    cur = fitness["current"]
    trend = fitness["trend"]
    peak = fitness["peak_ever"]
    pct = fitness["pct_of_peak"]
    recovery = ctx["recovery"]
    timeline = ctx["timeline"]
    sport_dist = ctx["training"]["sport_distribution_90d"]
    annual = ctx["training"]["annual_volume"]
    flags = recovery["flags"]

    # Only the fields that have something in them. These come from a configuration file now,
    # where every one of them is optional, and "Life context:" followed by nothing tells the
    # model less than the absent line does — it reads as an answer rather than as a gap.
    who = ctx["athlete"]
    lines = [
        f"=== ATHLETE SNAPSHOT — {ctx['generated']} ===",
        "",
        "ATHLETE",
        f"  Name: {who['name']}",
    ]
    for label, value in (("Training since", who.get("history_start")),
                         ("Primary sport", who.get("primary_sport")),
                         ("Equipment", who.get("equipment")),
                         ("Life context", who.get("notes")),
                         ("Can train on", ", ".join(who.get("available_days") or [])),
                         ("Long ride day", who.get("long_ride_day")),
                         ("Tools", ", ".join(who.get("current_tools") or []))):
        if value:
            lines.append(f"  {label}: {value}")
    stats = ctx["athlete"].get("stats") or {}
    if stats.get("ftp"):
        parts = [f"FTP {stats['ftp']:.0f}W"]
        if stats.get("weight_lbs"):
            parts.append(f"weight {stats['weight_lbs']:.0f}lbs")
        if stats.get("height_in"):
            parts.append(f"height {stats['height_in']:.0f}in")
        lines.append(f"  Current stats (manually tracked, not intervals.icu's synced FTP): {', '.join(parts)}")
    lines += [
        "",
        "GOALS & TIMELINE",
    ]
    for g in ctx["athlete"]["goals"]:
        lines.append(f"  - {g}")
    for g in timeline["goals"]:
        note = " — the rotation does not taper for it" if g["priority"] == "opt_in" else ""
        lines.append(f"  Weeks to {g['description']} ({g['date']}, {g['priority']}{note}): "
                     f"{g['weeks_away']}")
    lines += [
        "",
        "FITNESS (PMC)",
        f"  CTL (fitness):  {cur.get('ctl')}",
        f"  ATL (fatigue):  {cur.get('atl')}",
        f"  TSB (form):     {cur.get('tsb')}",
        f"  Est. FTP:       {round(cur['eftp'], 0) if cur.get('eftp') else 'N/A'}W",
        f"  Peak CTL ever:  {peak.get('ctl')} (on {peak.get('date')})",
        f"  Current vs peak: {pct}% of peak fitness",
        # Ramp rate and the 8-week trend direction used to be printed here. They said opposite
        # things — a positive weekly ramp above a "declining" label — and the model had no
        # basis to reconcile them. DERIVED METRICS below states the ramp over four windows
        # instead, which is the same information without the apparent contradiction.
    ]
    # Immediately under the CTL/ATL/TSB figures it qualifies. It sat further down at first,
    # after ANNUAL VOLUME, and a live run quoted "11 percent of your fitness ceiling" straight
    # off an understated CTL without ever mentioning the caveat. Distance from the numbers it
    # applies to is the whole problem — same failure as the format rules in Session 14.
    correction = load_correction_text()
    if correction:
        lines += ["", correction]
    lines += [
        "",
        "RECOVERY SIGNALS",
        f"  Sleep score 30d avg: {recovery['sleep'].get('avg_score_30d')}",
    ]
    if flags:
        lines.append("  FLAGS:")
        for f in flags:
            lines.append(f"    ! {f}")
    # HRV/RHR/sleep averages and their "stable/declining" word-labels also lived here. The
    # labels were doing the interpreting; DERIVED METRICS gives 7/28/90-day figures and the
    # deltas so the coach can judge for itself.
    lines += ["", derived_text()]
    lines += [
        "",
        "SPORT MIX (last 90 days)",
    ]
    for sport, stats in sport_dist.items():
        lines.append(f"  {sport:<12} {stats['activities']:>3} activities  {stats['hours']:>5.1f}h  ({stats['pct_time']}% of time)")
    lines += [
        "",
        "ANNUAL VOLUME",
    ]
    for yr in annual:
        lines.append(f"  {yr['year']}: {yr['tss']:>6.0f} TSS  (~{yr['hours_est']:>4.0f}h est.)")

    pos = ctx["training"].get("plan_position")
    if pos:
        wk = pos["week"]
        cyc = pos["cycle"]
        lines += ["", "TRAINING PLAN POSITION"]
        lines.append(f"  {pos['plan_name']}  ({pos['plan_start_date']} to {pos['plan_end_date']}, "
                     f"{pos['total_weeks']} weeks)")
        lines.append(f"  Week {wk['plan_week_number']} of {pos['total_weeks']}  "
                     f"({wk['week_start_date']} to {wk['week_end_date']})  —  "
                     f"{wk['week_type']} week, {wk['phase']} phase "
                     f"(week {wk['phase_week_number']} of that phase)")
        if cyc:
            # Position within the cycle, not cyc["closed"] — that flag means "ends in a rest
            # week", which reads as "already finished" if surfaced directly.
            in_cycle = wk["plan_week_number"] - cyc["plan_week_range"][0] + 1
            tail = "" if cyc["closed"] else " (no rest week — runs to the end of the plan)"
            lines.append(f"  Current cycle: plan weeks {cyc['plan_week_range'][0]}-"
                         f"{cyc['plan_week_range'][1]} — week {in_cycle} of {cyc['weeks']}{tail}")
        if pos["weeks_to_rest"] is not None:
            lines.append(f"  Next rest week: {pos['next_rest_week']} "
                         f"({pos['weeks_to_rest']} weeks away)")
        if not pos["cycle_data_complete"]:
            lines.append("  (Rest weeks are not fully seeded for this plan, so cycle boundaries "
                         "and next-rest-week are unknown — don't infer them.)")

    plan = ctx["training"].get("rolling_plan")
    if plan:
        lines += ["", "THE ROLLING PLAN"]
        lines.append(f"  Tier {plan['tier']} — {plan['tier_hours']}h/wk asked for"
                     f"{'  (re-entry week)' if plan['week_type'] == 'reentry' else ''}")
        lines.append(f"  Why: {plan['reason']}")
        if plan["decided_through"]:
            lines.append(f"  Decided through {plan['decided_through']}")
        lines.append("  Volume is earned, not scheduled: the tier moves only when a completed"
                     " week meets what was asked,")
        lines.append("  so there is no such thing as being behind schedule on this plan.")
        if plan["upcoming"]:
            lines.append("  Next sessions:")
            for s_ in plan["upcoming"][:7]:
                lines.append(f"    {_with_weekday(s_['date'])}  {s_['role']:<9} "
                             f"{s_['prescription']}")
        lines.append("  Outline:")
        for ln in plan["outline"]["summary"].splitlines():
            lines.append(f"    {ln}")
        # Raised to the model rather than acted on. Re-deriving the ladder changes what the
        # tiers already recorded in plan_progression meant, so it is the athlete's decision —
        # but nothing used to notice it was worth making, and the loop is self-reinforcing:
        # the plan asks for three days, three days get ridden, a re-derivation finds three.
        if plan.get("outgrown"):
            lines += ["", "  LADDER CHECK — raise this with them:",
                      f"    {plan['outgrown']['summary']}"]

    adherence = ctx["training"].get("plan_adherence_8w") or {}
    if adherence.get("total_planned"):
        lines += [
            "",
            "PLAN ADHERENCE (last 8 weeks)",
            f"  {adherence['completed']}/{adherence['total_planned']} completed "
            f"({adherence['adherence_pct']}%)  |  {adherence['partial']} partial  |  {adherence['skipped']} skipped",
        ]
        recent_planned = ctx["training"].get("recent_planned_workouts") or []
        # compliance_status is None both for genuinely future workouts and for past
        # non-power sessions (mobility/strength) that are never scored by design —
        # date >= today is what actually distinguishes "upcoming" from either of those.
        upcoming = [w for w in recent_planned
                   if w["compliance_status"] is None and w["date"] >= ctx["generated"]]
        if upcoming:
            lines.append("  Upcoming:")
            for w in sorted(upcoming, key=lambda w: w["date"])[:5]:
                lines.append(f"    {_with_weekday(w['date'])}  {w['name']}"
                             f"  ({w.get('planned_duration_min') or '?'}min"
                             f", {w.get('planned_tss') or '?'} TSS)")

    # Context tiering (item 10): recent weeks in full, earlier weeks in the current cycle as
    # their stored summaries. Raw activity detail is what grows without bound as the athlete
    # logs a note per session, so it is the block that gets a horizon; the summaries keep the
    # qualitative content reachable at a fraction of the size. Anything older stays out of the
    # default context entirely and is still reachable through query_history.
    recent = [a for a in ctx["training"]["recent_activities"]
              if a["date"] >= _raw_activity_horizon()]
    if recent:
        lines += ["", f"RECENT ACTIVITIES (last {RAW_ACTIVITY_DAYS} days, in full — "
                      f"{len(recent)} sessions)"]
        for a in recent[:10]:
            dur = f"{int(a['duration_min'])}min"
            dist = f"  {a['distance_km']}km" if a["distance_km"] else ""
            hr = f"  HR {int(a['avg_hr'])}bpm" if a["avg_hr"] else ""
            load = f"  load {a['load']}" if a["load"] else ""
            rpe = f"  RPE {int(a['rpe'])}/10" if a.get("rpe") else ""
            lines.append(f"  {_with_weekday(a['date'])}  {(a['type'] or 'Unknown'):<16} {dur}{dist}{hr}{load}{rpe}  {a['name']}")
            # Not truncated: the note is the athlete's own read of the session, and the detail
            # that makes it worth having ("legs went midway through the final set") is exactly
            # what a character cap would cut.
            if a.get("note"):
                for para in a["note"].split("\n\n"):
                    lines.append(f"      note: {para}")

    earlier = ctx["training"].get("earlier_week_summaries") or []
    if earlier:
        lines += ["", "EARLIER WEEKS THIS CYCLE (summarised — raw sessions available via "
                      "query_history)"]
        lines += summary_lines(earlier)

    memory = ctx.get("memory", {})

    review = memory.get("last_cycle_review")
    if review:
        lines += [
            "",
            f"LAST CYCLE REVIEW ({review['cycle_start']} to {review['cycle_end']}, "
            f"{review['phase']} phase, plan weeks {review['plan_week_start']}-"
            f"{review['plan_week_end']})",
            f"  CTL {review['ctl_start']} -> {review['ctl_end']} "
            f"({review['ctl_ramp_per_week']} CTL/week)  |  adherence {review['adherence_pct']}%"
            f"  |  {review['actual_tss']:.0f} of {review['planned_tss']:.0f} planned TSS",
        ]
        # Untruncated on purpose: coaching_log's 300-char cap is what this table exists to avoid.
        if review.get("content"):
            for para in review["content"].split("\n\n"):
                lines.append(f"  {para}")

    life_events = memory.get("life_events", [])
    if life_events:
        lines += ["", "LIFE EVENTS (last 90 days)"]
        for e in life_events:
            end = f" to {e['end_date']}" if e.get("end_date") else ""
            sev = f" [{e['severity']}]" if e.get("severity") else ""
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"  {e['start_date']}{end}  {e['type']}{sev}{note}")

    subjective = memory.get("subjective_feel", [])
    if subjective:
        lines += ["", "SUBJECTIVE FEEL (last 14 days)"]
        for s in subjective:
            parts = [f"feel {s['feel_score']}/10"]
            if s.get("energy"):
                parts.append(f"energy {s['energy']}/10")
            if s.get("legs"):
                parts.append(f"legs {s['legs']}/10")
            if s.get("tr_compliance"):
                parts.append(f"TR: {s['tr_compliance']}")
            note = f" — {s['notes']}" if s.get("notes") else ""
            lines.append(f"  {s['date']}  {', '.join(parts)}{note}")

    coaching_notes = memory.get("coaching_notes", [])
    if coaching_notes:
        lines += ["", "COACHING MEMORY (most recent of each kind, in full)"]
        for note in coaching_notes:
            # No truncation here. The 300-char cut this replaces was discarding ~85% of every
            # stored entry mid-sentence; _recent_coaching_memory() now enforces a budget at
            # selection time, where it can trim on a paragraph boundary and say that it did.
            for para in note.split("\n"):
                lines.append(f"  {para}")

    return "\n".join(lines)


if __name__ == "__main__":
    ctx = build_coaching_context()
    print(coaching_context_text(ctx))


def _outgrown(conn, plan, ladder: dict, today: date) -> dict | None:
    """
    Whether the athlete's riding has moved past the ladder they are prescribed from.

    Advisory, and a suggestion rather than an action: re-deriving the ladder changes what the
    tiers already recorded in `plan_progression` meant, which is the athlete's call. This puts
    the observation in front of the coach so it gets raised at a check-in instead of going
    unnoticed for a year.

    Wrapped in its own try: a failure here must not cost the coach the rest of the plan block,
    which is the part it reasons about every session.
    """
    from src.planning.blocks import plan_limits
    from src.planning.constants import max_ride_days, ride_day_setting
    from src.planning.progression_store import rows as progression_rows
    from src.planning.tiers_store import age_weeks, weeks_for

    try:
        found = outgrown_check.assess(
            weeks=weeks_for(conn, today, weeks_back=outgrown_check.WINDOW_WEEKS * 2),
            ladder=ladder,
            decided=progression_rows(conn, plan.name),
            max_ride_days=max_ride_days(),
            ladder_age_weeks=age_weeks(conn, plan.id, today),
            ceiling_setting=ride_day_setting(),
            limits=plan_limits(ladder[max(ladder)].rides),
        )
    except Exception:
        return None
    if not found.summary:
        return None
    return {"outgrown": found.outgrown, "blocked_by_config": found.blocked_by_config,
            "signals": list(found.signals), "summary": found.summary, "detail": found.detail}

"""
Assembles all analysis signals into a structured coaching context.
This is what gets passed to Claude as the data payload.
"""

import json
from datetime import date, timedelta
from .fitness import current_fitness, ctl_trend, peak_ctl, ctl_history
from .activities import recent_activities, sport_distribution, weekly_load_by_sport, yearly_volume
from .wellness import hrv_summary, sleep_summary, rhr_trend, recovery_flags
from .compliance import compliance_summary, recent_planned_workouts
from .training_plan import plan_summary
from .cycle import latest_review
from .load import load_correction_text
from .derive import derived_text
from src.athlete.profile import current_profile
from src.db.schema import get_connection


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
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, type, content FROM coaching_log
        WHERE id IN (
            SELECT id FROM coaching_log c2
            WHERE c2.type = coaching_log.type
            ORDER BY created_at DESC LIMIT ?
        )
        ORDER BY created_at
    """, (limit,)).fetchall()
    conn.close()

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
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT start_date, end_date, type, severity, note
        FROM life_events WHERE start_date >= ? ORDER BY start_date DESC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _recent_subjective_feel(days: int = 14) -> list[dict]:
    """Recent subjective feel entries."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, feel_score, energy, motivation, legs, tr_compliance, notes
        FROM subjective_feel WHERE date >= ? ORDER BY date DESC
    """, (since,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _active_goals() -> list[dict]:
    """Goals from the goals table (falls back to profile defaults if empty)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT description, target_date, priority FROM goals
        WHERE status = 'active' ORDER BY priority, target_date
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


ATHLETE_PROFILE = {
    "name": "Ian Auger",
    "history_start": "2017",
    "primary_sport": "cycling",
    "current_tools": ["TrainerRoad (plan generation)", "intervals.icu (data aggregation)"],
    "goals": [
        "Return to consistent training rhythm after 6-month gap",
        "Gravel races spring/summer 2027",
        "Cyclocross racing fall 2027 (primary target)",
        "Baseline: 120-mile ride not an overreach",
        "Introduce multi-sport variety (yoga, strength, running, swimming)",
    ],
    "equipment": "Garmin Epix Pro Gen 2",
    "notes": "Finished grad program, started new job, two young kids — life load is high.",
}


def build_coaching_context(
    recent_days: int = 28,
    trend_weeks: int = 8,
    history_weeks: int = 52,
) -> dict:
    """Full coaching context snapshot. Pass this to the Claude coaching layer."""
    today = date.today().isoformat()

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

    # weeks until key events
    cx_target = date(2027, 10, 1)
    gravel_target = date(2027, 5, 1)
    today_d = date.today()
    weeks_to_cx = (cx_target - today_d).days // 7
    weeks_to_gravel = (gravel_target - today_d).days // 7

    db_goals = _active_goals()
    goals_list = (
        [g["description"] for g in db_goals]
        if db_goals
        else ATHLETE_PROFILE["goals"]
    )

    # Manually-maintained stats (src/athlete/profile.py) — source of truth over
    # intervals.icu's icu_ftp, which has no user-facing edit path and lags real changes.
    stats = current_profile()

    return {
        "generated": today,
        "athlete": {**ATHLETE_PROFILE, "goals": goals_list, "stats": stats},
        "timeline": {
            "weeks_to_gravel_target": weeks_to_gravel,
            "weeks_to_cx_target": weeks_to_cx,
        },
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

    lines = [
        f"=== ATHLETE SNAPSHOT — {ctx['generated']} ===",
        "",
        "ATHLETE",
        f"  Name: {ctx['athlete']['name']}",
        f"  Training since: {ctx['athlete']['history_start']}",
        f"  Life context: {ctx['athlete']['notes']}",
        f"  Tools: {', '.join(ctx['athlete']['current_tools'])}",
    ]
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
    lines += [
        f"  Weeks to gravel target (May 2027): {timeline['weeks_to_gravel_target']}",
        f"  Weeks to CX target (Oct 2027):     {timeline['weeks_to_cx_target']}",
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

    adherence = ctx["training"].get("plan_adherence_8w") or {}
    if adherence.get("total_planned"):
        lines += [
            "",
            "TRAINERROAD PLAN ADHERENCE (last 8 weeks)",
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

    recent = ctx["training"]["recent_activities"]
    if recent:
        lines += ["", f"RECENT ACTIVITIES (last 28 days — {len(recent)} total)"]
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

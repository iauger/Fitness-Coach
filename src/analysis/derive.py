"""
Derived metrics — every comparison the coach would otherwise do by eye.

The point of this module is that the model should never be doing arithmetic. Anything it can
assert about a trend, a ramp, or a distribution should be a figure it read, not one it
produced. Everything here is deterministic and recomputed on read: it is all derivable from
activities + wellness + planned_workouts, on a few thousand SQLite rows, so persisting it
would be denormalisation that can go stale when a late activity syncs.

Contrast cycle_reviews, which *is* persisted — those numbers have to stay pinned to the
narrative written alongside them at the time.
"""

import json
from datetime import date, timedelta
from src.db.schema import get_connection
from src.athlete.profile import get_metric

# Seiler's three-zone model in intensity-factor terms. LT1 (the aerobic threshold, above which
# a session stops being conversational) sits near IF 0.75; LT2 near 0.85, which is also the
# bottom of TrainerRoad's sweet-spot band. Anything at or above that is "hard" for 80/20
# purposes — sweet spot is not easy training, whatever its name suggests.
EASY_IF_CEILING = 0.75
HARD_IF_FLOOR = 0.85


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── ramp reconciliation ───────────────────────────────────────────────────────

def ctl_ramps(conn, today: date) -> dict:
    """
    CTL change per week over several windows.

    The snapshot currently shows intervals.icu's `rampRate` (a 7-day figure) directly above an
    8-week trend, and during a return to training those point in opposite directions — a
    positive short ramp inside a long decline. Both are true; showing them unlabelled forces
    the model to reconcile them, and it has no basis to do so.
    """
    def ctl_on(d: date):
        r = conn.execute(
            "SELECT ctl FROM wellness WHERE date <= ? AND ctl IS NOT NULL "
            "ORDER BY date DESC LIMIT 1", (d.isoformat(),)
        ).fetchone()
        return r["ctl"] if r else None

    now = ctl_on(today)
    out = {"ctl_now": round(now, 1) if now is not None else None}
    for label, days in (("7d", 7), ("28d", 28), ("56d", 56), ("90d", 90)):
        then = ctl_on(today - timedelta(days=days))
        out[label] = (round((now - then) / (days / 7), 2)
                      if None not in (now, then) else None)
    return out


# ── weekly load, planned vs actual ────────────────────────────────────────────

def weekly_load(conn, today: date, weeks: int = 8) -> list[dict]:
    start = _monday(today) - timedelta(weeks=weeks - 1)
    rows = []
    for i in range(weeks):
        ws = start + timedelta(weeks=i)
        we = ws + timedelta(days=6)
        planned = conn.execute(
            "SELECT COALESCE(SUM(planned_tss),0) t FROM planned_workouts "
            "WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL",
            (ws.isoformat(), we.isoformat())).fetchone()["t"]
        act = conn.execute(
            "SELECT COALESCE(SUM(tss),0) t, COUNT(*) n FROM activities "
            "WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL",
            (ws.isoformat(), we.isoformat())).fetchone()
        ctl = conn.execute(
            "SELECT ctl, atl FROM wellness WHERE date <= ? AND ctl IS NOT NULL "
            "AND atl IS NOT NULL ORDER BY date DESC LIMIT 1", (we.isoformat(),)).fetchone()
        wk = conn.execute(
            "SELECT week_type, plan_week_number FROM training_plan_weeks "
            "WHERE week_start_date = ?", (ws.isoformat(),)).fetchone()
        is_current = ws <= today <= we
        # Planned load for a week in progress counts sessions that haven't come round yet, so
        # the percentage would read as underperformance rather than as an unfinished week.
        # Compare against only the part of the week that has actually elapsed.
        planned_to_date = planned
        if is_current:
            planned_to_date = conn.execute(
                "SELECT COALESCE(SUM(planned_tss),0) t FROM planned_workouts "
                "WHERE date BETWEEN ? AND ? AND planned_tss IS NOT NULL",
                (ws.isoformat(), today.isoformat())).fetchone()["t"]
        rows.append({
            "week_start": ws.isoformat(),
            "is_current": is_current,
            "days_elapsed": (today - ws).days + 1 if is_current else 7,
            "week_type": wk["week_type"] if wk else None,
            "plan_week": wk["plan_week_number"] if wk else None,
            "planned_tss": round(planned, 0),
            "planned_to_date": round(planned_to_date, 0),
            "actual_tss": round(act["t"], 0),
            "pct_of_planned": (round(act["t"] / planned_to_date * 100, 0)
                               if planned_to_date else None),
            "sessions": act["n"],
            "ctl_end": round(ctl["ctl"], 1) if ctl else None,
            "tsb_end": round(ctl["ctl"] - ctl["atl"], 1) if ctl else None,
        })
    return rows


# ── intensity distribution (Seiler 80/20) ─────────────────────────────────────

def intensity_distribution(conn, today: date, days: int = 28) -> dict:
    """
    Share of endurance training time spent easy vs moderate vs hard, by session intensity.

    Basis: normalised power over the FTP that athlete_profile says was true on that date —
    the same calculation `compliance._manual_tss()` uses. Two other bases were tried and both
    are wrong here:

    - **HR zones are unusable while LTHR is stale.** The stored LTHR is 177, but across every
      hard session in this block the athlete peaks at 159-173 and averages 142-146 while
      riding at 87-96% of FTP. The zone table puts the top of Z2 at 158, so his entire
      threshold workload lands inside "Z2 = easy". An HR-based split returned 90% easy for a
      block of nothing but sweet spot and threshold.
    - **Stored power zones and stored IF/TSS inherit whatever FTP intervals.icu analysed the
      ride under.** Two rides 2W apart in normalised power (2026-08-25 at 208W, 2026-08-27 at
      210W) were recorded as 97% easy and 15% easy purely because the first was analysed at
      FTP 297 and the second at 238.

    Strength and yoga are reported alongside but deliberately excluded from the ratio: the
    80/20 rule is about endurance training, and folding in resistance work — which is easy by
    any HR or power measure while costing real neuromuscular fatigue — inflates "easy" without
    meaning anything.

    Session-level, not time-in-zone: per-second power streams would need the intervals.icu
    streams endpoint, which we don't pull. For structured interval sessions the session IF is
    the honest summary anyway.
    """
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT date, name, type, moving_time, np, raw_json FROM activities "
        "WHERE date >= ? AND moving_time IS NOT NULL ORDER BY date", (since,)).fetchall()

    buckets = {"easy": 0.0, "moderate": 0.0, "hard": 0.0}
    sessions, other_sports = [], {}
    for r in rows:
        raw = json.loads(r["raw_json"])
        np_ = r["np"] or raw.get("icu_weighted_avg_watts")
        hours = (r["moving_time"] or 0) / 3600
        if not np_:
            other_sports.setdefault(r["type"] or "?", 0.0)
            other_sports[r["type"] or "?"] += hours
            continue
        ftp = get_metric("ftp", as_of=r["date"])
        if not ftp:
            continue
        rif = np_ / ftp
        band = ("easy" if rif < EASY_IF_CEILING
                else "moderate" if rif < HARD_IF_FLOOR else "hard")
        buckets[band] += hours
        sessions.append({"date": r["date"], "name": r["name"], "np": np_,
                         "ftp": ftp, "if": round(rif, 2), "hours": round(hours, 2),
                         "band": band,
                         "if_as_stored": round(raw.get("icu_intensity", 0) / 100, 2) or None,
                         "ftp_at_analysis": raw.get("icu_ftp")})

    total = sum(buckets.values())
    return {
        "days": days,
        "basis": "session normalised power / tracked FTP on that date",
        "if_bands": {"easy": f"<{EASY_IF_CEILING}", "moderate":
                     f"{EASY_IF_CEILING}-{HARD_IF_FLOOR}", "hard": f">={HARD_IF_FLOOR}"},
        "hours": {k: round(v, 1) for k, v in buckets.items()},
        "pct": {k: (round(v / total * 100, 1) if total else None) for k, v in buckets.items()},
        "endurance_hours": round(total, 1),
        "sessions": sessions,
        "excluded_no_power": {k: round(v, 1) for k, v in
                              sorted(other_sports.items(), key=lambda kv: -kv[1])},
        # Rides whose stored IF/TSS/zone data was computed against a different FTP than the
        # one we track for that date — their intervals.icu-derived figures understate load.
        "analysed_at_wrong_ftp": [s for s in sessions
                                  if s["ftp_at_analysis"] and s["ftp_at_analysis"] != s["ftp"]],
    }


# ── recovery against baseline ─────────────────────────────────────────────────

def recovery_baselines(conn, today: date) -> dict:
    """HRV/RHR/sleep over 7, 28 and 90 days, with the deltas stated rather than labelled."""
    def avg(days: int) -> dict:
        since = (today - timedelta(days=days)).isoformat()
        r = conn.execute(
            "SELECT AVG(hrv) hrv, AVG(rhr) rhr, AVG(sleep_hrs) sleep, COUNT(hrv) n "
            "FROM wellness WHERE date >= ? AND date <= ?", (since, today.isoformat())
        ).fetchone()
        return {"hrv": round(r["hrv"], 1) if r["hrv"] else None,
                "rhr": round(r["rhr"], 1) if r["rhr"] else None,
                "sleep": round(r["sleep"], 1) if r["sleep"] else None,
                "n": r["n"]}

    w7, w28, w90 = avg(7), avg(28), avg(90)
    delta = {k: (round(w7[k] - w28[k], 1) if None not in (w7[k], w28[k]) else None)
             for k in ("hrv", "rhr", "sleep")}
    return {"last_7d": w7, "last_28d": w28, "last_90d": w90, "delta_7d_vs_28d": delta}


# ── RPE against prescribed intensity ──────────────────────────────────────────

def rpe_vs_prescribed(conn, today: date, days: int = 90) -> dict:
    """
    Matched pairs of what was prescribed and what it cost.

    The gap between prescribed IF and reported RPE is the signal `prompt.py` asks the coach to
    read. Pairing them is a join, not a judgement, so it belongs here — the interpretation
    stays the model's.
    """
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT a.date, a.name, a.feel rpe, p.planned_if, p.planned_tss, a.tss,
               p.workout_type, p.compliance_pct
        FROM activities a
        JOIN planned_workouts p ON p.matched_activity_id = a.id
        WHERE a.date >= ? AND a.feel IS NOT NULL
        ORDER BY a.date
    """, (since,)).fetchall()
    pairs = [dict(r) for r in rows]
    with_if = [p for p in pairs if p["planned_if"]]
    return {
        "n": len(pairs),
        "pairs": pairs,
        "mean_rpe": round(sum(p["rpe"] for p in pairs) / len(pairs), 1) if pairs else None,
        "mean_if": (round(sum(p["planned_if"] for p in with_if) / len(with_if), 2)
                    if with_if else None),
    }


# ── consistency ───────────────────────────────────────────────────────────────

def consistency(conn, today: date, days: int = 28) -> dict:
    since = (today - timedelta(days=days)).isoformat()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM activities WHERE date >= ? AND moving_time IS NOT NULL "
        "ORDER BY date", (since,))]
    last_ride = conn.execute(
        "SELECT MAX(date) d FROM activities WHERE type LIKE '%Ride%' AND moving_time IS NOT NULL"
    ).fetchone()["d"]
    gaps = []
    for a, b in zip(dates, dates[1:]):
        gaps.append((date.fromisoformat(b) - date.fromisoformat(a)).days - 1)
    return {
        "days": days,
        "training_days": len(dates),
        "sessions_per_week": round(len(dates) / (days / 7), 1),
        "longest_gap_days": max(gaps) if gaps else None,
        "days_since_last_ride": ((today - date.fromisoformat(last_ride)).days
                                 if last_ride else None),
    }


# ── assembly ──────────────────────────────────────────────────────────────────

def derived_metrics(today: date | None = None) -> dict:
    today = today or date.today()
    conn = get_connection()
    try:
        return {
            "ramps": ctl_ramps(conn, today),
            "weekly_load": weekly_load(conn, today),
            "intensity": intensity_distribution(conn, today),
            "recovery": recovery_baselines(conn, today),
            "rpe": rpe_vs_prescribed(conn, today),
            "consistency": consistency(conn, today),
        }
    finally:
        conn.close()


def derived_text(m: dict | None = None) -> str:
    m = m or derived_metrics()
    r, c = m["ramps"], m["consistency"]
    lines = [
        "DERIVED METRICS (computed, not estimated — use these figures directly)",
        "",
        "  CTL ramp over different windows (CTL/week):",
        f"    last 7d {r['7d']}   |   28d {r['28d']}   |   56d {r['56d']}   |   90d {r['90d']}",
        "    A positive short ramp inside a negative long one means a rebuild in progress,",
        "    not a contradiction.",
        "",
        "  WEEKLY LOAD — planned vs actual",
        f"    {'week':<12} {'wk':>3} {'type':<10} {'plan':>6} {'actual':>7} {'%':>5} "
        f"{'sess':>5} {'CTL':>6} {'TSB':>7}",
    ]
    for w in m["weekly_load"]:
        mark = (f" <- current week, {w['days_elapsed']} of 7 days elapsed; "
                f"% is against the {w['planned_to_date']:.0f} TSS planned so far, "
                f"not the full {w['planned_tss']:.0f}") if w["is_current"] else ""
        lines.append(
            f"    {w['week_start']:<12} {str(w['plan_week'] or '-'):>3} "
            f"{str(w['week_type'] or '-'):<10} {w['planned_tss']:>6.0f} {w['actual_tss']:>7.0f} "
            f"{str(w['pct_of_planned'] or '-'):>5} {w['sessions']:>5} "
            f"{str(w['ctl_end'] or '-'):>6} {str(w['tsb_end'] or '-'):>7}{mark}"
        )
    lines += [
        "    'plan' is every prescribed power session that week, including ones never started.",
        "    That is a different denominator from the adherence figure elsewhere in this",
        "    snapshot, which scores only sessions matched to an activity — so completing every",
        "    session you start can sit alongside a load percentage well under 100.",
    ]

    i = m["intensity"]
    p, h = i["pct"], i["hours"]
    lines += [
        "",
        f"  INTENSITY DISTRIBUTION, last {i['days']} days — {i['endurance_hours']}h of "
        f"power-based endurance work",
        f"    basis: {i['basis']}",
        f"    easy (IF {i['if_bands']['easy']})      {str(p['easy']):>5}%  ({h['easy']}h)",
        f"    moderate (IF {i['if_bands']['moderate']})  {str(p['moderate']):>5}%  "
        f"({h['moderate']}h)",
        f"    hard (IF {i['if_bands']['hard']})     {str(p['hard']):>5}%  ({h['hard']}h)",
        "    Seiler's target is 80%+ easy. Sweet spot counts as hard, not easy.",
        "",
        "    every session:",
    ]
    for s in i["sessions"]:
        flag = (f"   [stored IF {s['if_as_stored']} — analysed at FTP {s['ftp_at_analysis']}, "
                f"understates load]") if s in i["analysed_at_wrong_ftp"] else ""
        lines.append(f"      {s['date']}  {s['name'][:34]:<34} NP {s['np']:>3}W / FTP "
                     f"{s['ftp']:.0f} = IF {s['if']:.2f}  {s['band']}{flag}")
    if i["excluded_no_power"]:
        excl = ", ".join(f"{k} {v}h" for k, v in i["excluded_no_power"].items())
        lines.append(f"    excluded from the ratio (no power, and 80/20 is an endurance "
                     f"rule): {excl}")
    if i["analysed_at_wrong_ftp"]:
        lines.append(f"    NOTE: {len(i['analysed_at_wrong_ftp'])} of these rides were analysed "
                     f"by intervals.icu against a different FTP")
        lines.append("    than the one tracked for that date, so their stored IF, TSS and zone "
                     "times are all")
        lines.append("    too low — and CTL, which is built from that TSS, is too low with "
                     "them.")

    rec = m["recovery"]
    d = rec["delta_7d_vs_28d"]
    lines += [
        "",
        "  RECOVERY vs BASELINE",
        f"    {'':<8} {'7d':>7} {'28d':>7} {'90d':>7} {'7d vs 28d':>11}",
        f"    {'HRV':<8} {str(rec['last_7d']['hrv']):>7} {str(rec['last_28d']['hrv']):>7} "
        f"{str(rec['last_90d']['hrv']):>7} {_signed(d['hrv']):>11}",
        f"    {'RHR':<8} {str(rec['last_7d']['rhr']):>7} {str(rec['last_28d']['rhr']):>7} "
        f"{str(rec['last_90d']['rhr']):>7} {_signed(d['rhr']):>11}",
        f"    {'Sleep':<8} {str(rec['last_7d']['sleep']):>7} {str(rec['last_28d']['sleep']):>7} "
        f"{str(rec['last_90d']['sleep']):>7} {_signed(d['sleep']):>11}",
    ]

    rpe = m["rpe"]
    lines += ["", f"  RPE vs PRESCRIBED  (n={rpe['n']} matched sessions with an RPE logged)"]
    if rpe["n"]:
        lines.append(f"    mean RPE {rpe['mean_rpe']}/10 against mean prescribed IF "
                     f"{rpe['mean_if']}")
        for p in rpe["pairs"]:
            lines.append(f"      {p['date']}  {(p['workout_type'] or '?'):<12} "
                         f"IF {p['planned_if']}  ->  RPE {int(p['rpe'])}/10   "
                         f"({p['compliance_pct']}% of planned load)")
    else:
        lines.append("    no matched sessions carry an RPE yet")

    lines += [
        "",
        f"  CONSISTENCY, last {c['days']} days",
        f"    {c['training_days']} training days ({c['sessions_per_week']}/week)   "
        f"longest gap {c['longest_gap_days']}d   last ride {c['days_since_last_ride']}d ago",
    ]
    return "\n".join(lines)


def _signed(v) -> str:
    return "-" if v is None else f"{v:+}"

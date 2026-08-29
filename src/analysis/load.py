"""
Training load that accounts for intervals.icu having analysed some rides at the wrong FTP.

intervals.icu bakes `icu_ftp` into an activity at analysis time and never revisits it, so every
figure derived from it — `icu_training_load`, `icu_intensity`, `icu_zone_times` — is frozen
against whatever FTP was set that day. When the athlete corrected `icu_ftp` from 297 to 238 on
2026-08-27, the rides already analysed kept the old basis: 2026-08-25 (208W NP) was stored at
IF 0.70 / 56 TSS while 2026-08-27 (210W NP) came out at IF 0.88 / 81 TSS.

This module resolves that on read rather than by storing a correction. Three reasons:
  - a stored column would be clobbered by the next sync unless carefully excluded, which is
    the trap that would have destroyed the athlete notes;
  - the correction applies to a closed set of four rides and stops applying automatically once
    the analysis FTP agrees with the tracked one, which it has since 2026-08-27;
  - it keeps one source of truth. Nothing has to be kept consistent with anything else.

Deliberately NOT corrected: `wellness.ctl`/`atl`. Those are computed server-side by
intervals.icu from its own load figures, so we cannot fix them locally without maintaining a
parallel PMC that permanently disagrees with intervals.icu's own charts. The error there is
transient — see ctl_correction() — and is reported rather than recomputed.
"""

import json
from datetime import date, timedelta
from src.db.schema import get_connection

CTL_DAYS = 42
ATL_DAYS = 7

_FTP_CACHE: list[tuple[str, float]] | None = None


def tracked_ftp_strict(activity_date: str) -> float | None:
    """
    FTP effective on or before `activity_date`, with NO fallback — None if we weren't tracking
    FTP yet.

    profile.get_metric() deliberately falls back to the earliest known value when asked about a
    date before tracking began, which is right for display but catastrophic here: it made every
    ride back to 2017 compare against the 238 recorded on 2026-08-19, and the correction claimed
    1,584 mis-analysed rides across nine years. We have no idea what FTP was true in 2017, and
    the athlete's explicit call was to start fresh from the restart — so no record means no
    correction.
    """
    for eff, value in _ftp_history():
        if eff <= activity_date:
            return value
    return None


def _ftp_history() -> list[tuple[str, float]]:
    """FTP records newest-first. Read once per process — this is consulted per activity, and
    opening a connection for each of a few thousand rows dominated the cost otherwise."""
    global _FTP_CACHE
    if _FTP_CACHE is None:
        conn = get_connection()
        try:
            _FTP_CACHE = [
                (r["effective_date"], r["value"]) for r in conn.execute(
                    "SELECT effective_date, value FROM athlete_profile WHERE metric = 'ftp' "
                    "ORDER BY effective_date DESC, id DESC")
            ]
        finally:
            conn.close()
    return _FTP_CACHE


def reset_ftp_cache() -> None:
    """Call after writing a new FTP record in a long-lived process."""
    global _FTP_CACHE
    _FTP_CACHE = None


def _stored_load(row, raw: dict) -> float:
    """Whatever intervals.icu recorded, including the HR-based estimate for non-power sports."""
    return (row["tss"] if row["tss"] is not None
            else raw.get("icu_training_load") or raw.get("hr_load") or 0.0)


def effective_tss(row, raw: dict | None = None) -> tuple[float, bool]:
    """
    (tss, was_corrected) for one activity row.

    Needs `date`, `moving_time`, `tss` and `raw_json` (or a pre-parsed `raw`). Recomputes only
    when the activity carries power AND the FTP it was analysed under differs from the FTP
    athlete_profile says was true on its date. Everything else passes through untouched.
    """
    raw = raw if raw is not None else json.loads(row["raw_json"] or "{}")
    stored = _stored_load(row, raw)

    np = (row["np"] if "np" in row.keys() else None) or raw.get("icu_weighted_avg_watts")
    analysed_ftp = raw.get("icu_ftp")
    if not np or not analysed_ftp or not row["moving_time"]:
        return stored, False

    tracked_ftp = tracked_ftp_strict(row["date"])
    if not tracked_ftp or round(analysed_ftp) == round(tracked_ftp):
        return stored, False

    intensity_factor = np / tracked_ftp
    return round((row["moving_time"] * intensity_factor ** 2 / 3600) * 100, 1), True


def daily_load(conn, start: str, end: str) -> dict[str, float]:
    """Corrected TSS per day across a date range."""
    rows = conn.execute("""
        SELECT date, moving_time, np, tss, raw_json FROM activities
        WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL
    """, (start, end)).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        tss, _ = effective_tss(r)
        out[r["date"]] = out.get(r["date"], 0.0) + tss
    return out


def sum_effective_tss(conn, start: str, end: str) -> tuple[float, int, int]:
    """(total corrected TSS, activity count, how many were corrected) over a range."""
    rows = conn.execute("""
        SELECT date, moving_time, np, tss, raw_json FROM activities
        WHERE date BETWEEN ? AND ? AND moving_time IS NOT NULL
    """, (start, end)).fetchall()
    total = 0.0
    corrected = 0
    for r in rows:
        tss, was = effective_tss(r)
        total += tss
        corrected += was
    return round(total, 1), len(rows), corrected


def corrected_activities(conn, since: str | None = None) -> list[dict]:
    """Every activity whose stored load disagrees with its recomputed load."""
    rows = conn.execute("""
        SELECT date, name, moving_time, np, tss, raw_json FROM activities
        WHERE moving_time IS NOT NULL AND (? IS NULL OR date >= ?)
        ORDER BY date
    """, (since, since)).fetchall()
    out = []
    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        tss, was = effective_tss(r, raw)
        if was:
            out.append({
                "date": r["date"], "name": r["name"],
                "stored_tss": _stored_load(r, raw), "corrected_tss": tss,
                "analysed_ftp": raw.get("icu_ftp"),
                "tracked_ftp": tracked_ftp_strict(r["date"]),
            })
    return out


def ctl_correction(conn, today: date | None = None) -> dict | None:
    """
    How far intervals.icu's CTL sits below where the corrected load would put it.

    Replays the PMC's EWMA from the day before the first mis-analysed ride, once with the
    stored load and once with the corrected load, and reports the gap. This is reported to the
    coach rather than used to overwrite anything: CTL is a 42-day EWMA, so the error decays
    with a half-life of about four weeks and retires itself. Building a parallel PMC to fix a
    self-correcting error would mean explaining the divergence from intervals.icu's own charts
    forever.

    Returns None once no mis-analysed rides remain in the window, which is the steady state.
    """
    today = today or date.today()
    bad = corrected_activities(conn)
    if not bad:
        return None

    anchor = date.fromisoformat(bad[0]["date"]) - timedelta(days=1)
    seed = conn.execute(
        "SELECT ctl, atl FROM wellness WHERE date <= ? AND ctl IS NOT NULL "
        "ORDER BY date DESC LIMIT 1", (anchor.isoformat(),)).fetchone()
    if not seed:
        return None

    rows = conn.execute("""
        SELECT date, moving_time, np, tss, raw_json FROM activities
        WHERE date > ? AND date <= ? AND moving_time IS NOT NULL
    """, (anchor.isoformat(), today.isoformat())).fetchall()
    stored_by_day: dict[str, float] = {}
    fixed_by_day: dict[str, float] = {}
    for r in rows:
        raw = json.loads(r["raw_json"] or "{}")
        tss, _ = effective_tss(r, raw)
        stored_by_day[r["date"]] = stored_by_day.get(r["date"], 0.0) + _stored_load(r, raw)
        fixed_by_day[r["date"]] = fixed_by_day.get(r["date"], 0.0) + tss

    ctl_s = ctl_f = seed["ctl"]
    d = anchor + timedelta(days=1)
    while d <= today:
        k = d.isoformat()
        ctl_s += (stored_by_day.get(k, 0.0) - ctl_s) / CTL_DAYS
        ctl_f += (fixed_by_day.get(k, 0.0) - ctl_f) / CTL_DAYS
        d += timedelta(days=1)

    gap = ctl_f - ctl_s
    reported = conn.execute(
        "SELECT ctl FROM wellness WHERE date <= ? AND ctl IS NOT NULL ORDER BY date DESC LIMIT 1",
        (today.isoformat(),)).fetchone()
    decay = {f"+{w}w": round(gap * (1 - 1 / CTL_DAYS) ** (w * 7), 2) for w in (4, 8, 12)}
    return {
        "affected_rides": len(bad),
        "first_affected": bad[0]["date"],
        "last_affected": bad[-1]["date"],
        "reported_ctl": round(reported["ctl"], 1) if reported else None,
        "adjusted_ctl": (round(reported["ctl"] + gap, 1) if reported else None),
        "gap": round(gap, 2),
        "pct_understated": (round(gap / reported["ctl"] * 100, 0)
                            if reported and reported["ctl"] else None),
        "decay": decay,
    }


def load_correction_text(today: date | None = None) -> str | None:
    """The correction as a block for the coaching snapshot. None when nothing is affected."""
    conn = get_connection()
    try:
        c = ctl_correction(conn, today)
        if not c:
            return None
        bad = corrected_activities(conn)
        lines = [
            "LOAD CORRECTION — intervals.icu analysed some rides against the wrong FTP",
            f"  {c['affected_rides']} rides between {c['first_affected']} and "
            f"{c['last_affected']} were scored by intervals.icu",
            "  against an FTP that differs from the one tracked for those dates. Their stored",
            "  TSS, IF and zone times are all too low, and CTL is built from that TSS.",
            "",
            f"  {'date':<12} {'analysed at':>11} {'tracked':>8} {'stored TSS':>11} "
            f"{'corrected':>10}",
        ]
        for b in bad:
            lines.append(f"  {b['date']:<12} {b['analysed_ftp']:>11} {b['tracked_ftp']:>8.0f} "
                         f"{b['stored_tss']:>11.0f} {b['corrected_tss']:>10.1f}")
        lines += [
            "",
            f"  CTL as reported by intervals.icu: {c['reported_ctl']}",
            f"  CTL adjusted for the correction:  {c['adjusted_ctl']}  "
            f"(understated by {c['pct_understated']:.0f}%)",
            "  Every TSS figure elsewhere in this snapshot is already corrected. CTL, ATL and",
            "  TSB are not — intervals.icu computes those server-side, so treat the PMC as",
            "  reading low by roughly the gap above.",
            f"  The gap decays on its own as the 42-day window rolls past these rides: "
            f"{', '.join(f'{k} {v}' for k, v in c['decay'].items())}.",
        ]
        return "\n".join(lines)
    finally:
        conn.close()

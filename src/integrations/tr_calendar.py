"""
TrainerRoad planned workouts, read from the calendar's public iCal feed.

Replaces the Google Calendar OAuth client (Session 15). That path needed a Cloud project, a
consent screen, a token file, and a refresh flow whose token expired every 7 days while the
consent screen sat in Testing — and in exchange returned only ~14 days of forward data, because
the API was being queried over a narrow window. The iCal feed is an unauthenticated GET that
returns the whole plan: 319 events reaching a year forward, versus 40 rows via the API.

Set GCAL_ICAL_URL in .env to the "Public address in iCal format" from
Google Calendar → Settings → <the TrainerRoad calendar> → Integrate calendar.

Note the feed is public to anyone holding the URL — that is a property of the calendar's
sharing setting, not of this module. Google can regenerate the address if it needs rotating.

Latency: Google re-fetches the calendar it subscribes to on its own schedule, sometimes
8-24h behind TrainerRoad. That was equally true of the API path. Pointing GCAL_ICAL_URL at
TrainerRoad's own iCal export instead would cut Google out and be fresher; the parsing here
works either way, since it reads standard VEVENTs.
"""

import os
import re
from datetime import date, datetime
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

FEED_URL_ENV = "GCAL_ICAL_URL"
REQUEST_TIMEOUT = 60

# Ordered most-specific-first: text is checked top to bottom, first match wins.
# A Sweet Spot workout's description often also mentions "recovery" between intervals,
# so generic terms like Recovery/Rest must sit at the bottom or they'd shadow the real type.
WORKOUT_TYPE_KEYWORDS = [
    ("vo2", "VO2 Max"),
    ("anaerobic", "Anaerobic"),
    ("sprint", "Sprint"),
    # Over-unders alternate just under and just over FTP; TrainerRoad describes them purely as
    # "over-under intervals" and never uses the word "threshold", so without this they fell
    # through unclassified. They were 20 of the 51 unclassified power sessions in the plan.
    ("over-under", "Threshold"),
    ("over/under", "Threshold"),
    ("threshold", "Threshold"),
    ("sweet spot", "Sweet Spot"),
    ("tempo", "Tempo"),
    ("endurance", "Endurance"),
    ("recovery", "Recovery"),
    ("rest", "Rest"),
]


# ── iCal fetch + decode ────────────────────────────────────────────────────────

def feed_url() -> str:
    url = os.environ.get(FEED_URL_ENV)
    if not url:
        raise RuntimeError(
            f"{FEED_URL_ENV} is not set. Copy the 'Public address in iCal format' from "
            "Google Calendar > Settings > (TrainerRoad calendar) > Integrate calendar, "
            "and add it to .env."
        )
    return url


def fetch_feed(url: Optional[str] = None) -> str:
    resp = requests.get(url or feed_url(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _unfold(text: str) -> str:
    """
    RFC 5545 folds long lines by inserting CRLF followed by a space or tab. Descriptions
    carry the TSS/IF/kJ values we parse and are always folded, so this has to happen before
    any field matching.
    """
    return re.sub(r"\r?\n[ \t]", "", text)


def _unescape(value: str) -> str:
    """iCal escaping: \\n is a newline, and , ; \\ are backslash-escaped."""
    out = re.sub(r"\\n", "\n", value, flags=re.IGNORECASE)
    return re.sub(r"\\([,;\\])", r"\1", out)


def _field(block: str, name: str) -> Optional[str]:
    """Value of a property, ignoring any ;PARAM=... between the name and the colon."""
    m = re.search(rf"^{name}(;[^:\r\n]*)?:(.*)$", block, re.MULTILINE)
    return _unescape(m.group(2).strip()) if m else None


def _ical_date(value: Optional[str]) -> Optional[str]:
    """'20260824' or '20260824T120000Z' -> '2026-08-24'."""
    if not value or len(value) < 8 or not value[:8].isdigit():
        return None
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}"


def parse_ics(text: str) -> list[dict]:
    """
    Decode VEVENTs into the same dict shape the Google Calendar API returned.

    Keeping the shape identical is deliberate: parse_event() and its helpers below are the
    Google-era code, unchanged, and they stay correct as long as this adapter matches the
    contract they were written against.
    """
    unfolded = _unfold(text)
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.DOTALL):
        uid = _field(block, "UID")
        raw_start = _field(block, "DTSTART")
        if not uid or not raw_start:
            continue
        start_key = "date" if len(raw_start) == 8 else "dateTime"
        event = {
            "id": uid,
            "summary": _field(block, "SUMMARY") or "",
            "description": _field(block, "DESCRIPTION") or "",
            "start": {start_key: _ical_date(raw_start) if start_key == "date" else raw_start},
            "end": {},
            # LAST-MODIFIED is absent on some feeds; DTSTAMP is mandatory, so it's the fallback.
            "updated": _field(block, "LAST-MODIFIED") or _field(block, "DTSTAMP"),
        }
        raw_end = _field(block, "DTEND")
        if raw_end:
            end_key = "date" if len(raw_end) == 8 else "dateTime"
            event["end"] = {end_key: _ical_date(raw_end) if end_key == "date" else raw_end}
        events.append(event)
    return events


def sync_planned_workouts(start: date, end: date, url: Optional[str] = None) -> list[dict]:
    """Fetch the feed and return parsed planned_workout records within [start, end]."""
    return fetch_planned_workouts(parse_ics(fetch_feed(url)), start, end)


def fetch_planned_workouts(events: list[dict], start: date, end: date) -> list[dict]:
    """Filter decoded events to a date range and parse each into a planned_workout record."""
    lo, hi = start.isoformat(), end.isoformat()
    out = []
    for event in events:
        parsed = parse_event(event)
        if parsed and lo <= parsed["date"] <= hi:
            out.append(parsed)
    return sorted(out, key=lambda w: w["date"])


# ── Event parsing (carried over unchanged from the Google Calendar client) ─────

# Upper bound of each zone as a % of FTP, from the athlete's own intervals.icu settings
# (icu_power_zones = [55, 75, 90, 105, 120, 150, 999]). Sweet Spot is handled separately
# because TR's band (sweet_spot_min/max = 84/97) straddles the Tempo and Threshold zones.
_ZONE_CEILINGS = [(55, "Recovery"), (75, "Endurance"), (90, "Tempo"),
                  (105, "Threshold"), (120, "VO2 Max"), (150, "Anaerobic")]
_SWEET_SPOT = (84, 97)

# "94% FTP", "between 86-92% FTP", "between 120 and 125% FTP"
_PCT_RANGE = re.compile(r"(\d{2,3})\s*(?:-|–|\s+and\s+|\s+to\s+)\s*(\d{2,3})\s*%")
_PCT_SINGLE = re.compile(r"(\d{2,3})\s*%")


def _classify_by_intensity(text: str) -> Optional[str]:
    """
    Fall back to the percentages TR states in prose when no zone keyword appears.

    Roughly a third of this plan's power sessions describe intensity only numerically
    ("3x15-minute efforts between 86-92% FTP"), which left them unclassified and invisible
    to the coach's 80/20 intensity reasoning. Takes the hardest interval mentioned — recovery
    valleys are quoted too, and the work interval is what characterises the session.
    """
    candidates = [(int(a) + int(b)) / 2 for a, b in _PCT_RANGE.findall(text)]
    if not candidates:
        candidates = [int(v) for v in _PCT_SINGLE.findall(text)]
    # Percentages above ~200 are almost certainly not FTP references; drop them.
    candidates = [c for c in candidates if 30 <= c <= 200]
    if not candidates:
        return None

    peak = max(candidates)
    if _SWEET_SPOT[0] <= peak <= _SWEET_SPOT[1]:
        return "Sweet Spot"
    for ceiling, label in _ZONE_CEILINGS:
        if peak <= ceiling:
            return label
    return "Sprint"


def _classify_workout_type(text: str) -> Optional[str]:
    text_l = text.lower()
    for keyword, label in WORKOUT_TYPE_KEYWORDS:
        if keyword in text_l:
            return label
    return _classify_by_intensity(text_l)


def _parse_description(desc: str) -> dict:
    """
    Extract TSS, IF, kJ, description text, and workout type from a TR calendar
    event description. Real TR format looks like:
      "TSS 72, IF 0.85, kJ(Cal) 655.  Description: <workout text>  Goals: <goal text>"
    Falls back to looser matching (colons, embedded duration) for other formats.
    """
    result: dict = {}
    if not desc:
        return result

    # TSS: "TSS 72" or "TSS: 85" or "85 TSS"
    m = re.search(r"TSS[:\s]+([0-9.]+)", desc, re.IGNORECASE)
    if not m:
        m = re.search(r"([0-9.]+)\s*TSS", desc, re.IGNORECASE)
    if m:
        result["planned_tss"] = float(m.group(1))

    # IF: "IF 0.85" or "IF: 0.75" or "Intensity Factor: 0.75"
    m = re.search(r"IF[:\s]+([0-9.]+)", desc, re.IGNORECASE)
    if not m:
        m = re.search(r"[Ii]ntensity\s*[Ff]actor[:\s]+([0-9.]+)", desc)
    if m:
        result["planned_if"] = float(m.group(1))

    # kJ / Calories: "kJ(Cal) 655" or "kJ: 655"
    m = re.search(r"kJ\s*\(?\s*Cal\)?\s*[:\s]+([0-9.]+)", desc, re.IGNORECASE)
    if m:
        result["planned_kj"] = float(m.group(1))

    # Workout description text: everything between "Description:" and "Goals:" (or end)
    m = re.search(r"Description:\s*(.*?)(?:\s*Goals:|$)", desc, re.IGNORECASE | re.DOTALL)
    if m:
        result["description"] = m.group(1).strip()

    result["workout_type"] = _classify_workout_type(result.get("description") or desc)

    # Duration text fallback — the real TR format has no embedded duration text
    # (it comes from the event title prefix instead, see _title_duration_and_name).
    m = re.search(r"(\d+):(\d{2}):\d{2}", desc)
    if m:
        result["planned_duration_min"] = int(m.group(1)) * 60 + int(m.group(2))
    else:
        m = re.search(r"(\d+)\s*h(?:our)?s?\s*(\d+)?\s*m?", desc, re.IGNORECASE)
        if m:
            h = int(m.group(1))
            mins = int(m.group(2)) if m.group(2) else 0
            result["planned_duration_min"] = h * 60 + mins
        else:
            m = re.search(r"(\d+)\s*min", desc, re.IGNORECASE)
            if m:
                result["planned_duration_min"] = int(m.group(1))

    # Workout URL
    m = re.search(r"(https?://(?:www\.)?trainerroad\.com/\S+)", desc)
    if m:
        result["workout_url"] = m.group(1).rstrip(")")

    return result


def _event_date(event: dict) -> Optional[str]:
    start = event.get("start", {})
    if "date" in start:
        return start["date"]
    if "dateTime" in start:
        return _ical_date(start["dateTime"])
    return None


def _event_duration_min(event: dict) -> Optional[float]:
    """Derive duration from event start/end if not in the title."""
    start = event.get("start", {})
    end = event.get("end", {})
    try:
        s = datetime.strptime(start["dateTime"][:15], "%Y%m%dT%H%M%S")
        e = datetime.strptime(end["dateTime"][:15], "%Y%m%dT%H%M%S")
        return (e - s).total_seconds() / 60
    except Exception:
        return None


def _title_duration_and_name(summary: str) -> tuple[str, Optional[float]]:
    """
    TR's real calendar titles are "H:MM - Workout Name" (e.g. "1:00 - Galena -2",
    "0:30 - STR-A") — that prefix is the actual planned duration and is far more
    reliable than anything guessable from the description text. Plan-level events
    like "Rest Day" or "Recovery Week" have no prefix.
    """
    m = re.match(r"^(\d+):(\d{2})\s*-\s*(.+)$", summary.strip())
    if m:
        hours, minutes, rest = m.groups()
        return rest.strip(), int(hours) * 60 + int(minutes)
    return summary.strip(), None


def parse_event(event: dict) -> Optional[dict]:
    """
    Convert a decoded calendar event to a planned_workout record.
    No title filtering here — the feed is already scoped to the TR calendar, so every event
    on it is a planned session, including non-power ones like "Mobility" or "STR-B" that
    don't match a workout-name pattern.
    """
    summary = event.get("summary", "")
    if not summary:
        return None

    event_date = _event_date(event)
    if not event_date:
        return None

    desc = event.get("description") or ""
    parsed = _parse_description(desc)

    name, title_dur = _title_duration_and_name(summary)
    # Clean workout name — also strip "- TrainerRoad" suffix if present
    name = re.sub(r"\s*[-–]\s*TrainerRoad\s*$", "", name, flags=re.IGNORECASE).strip()

    # Duration priority: title prefix (real, TR-authored) > event start/end time
    # (these calendar entries are all-day blocks, so this rarely applies in practice)
    # > description-text regex (last resort — easy to false-positive on prose like
    # "5 minutes of recovery between intervals").
    if title_dur:
        parsed["planned_duration_min"] = title_dur
    else:
        event_dur = _event_duration_min(event)
        if event_dur:
            parsed["planned_duration_min"] = event_dur

    # Workout-type classification only makes sense for power-based (TSS-bearing)
    # sessions — strength/mobility descriptions often contain the word "rest"
    # ("60 seconds rest between sets"), which would otherwise mislabel them.
    if parsed.get("planned_tss") is None:
        parsed["workout_type"] = None

    return {
        "id":                   event["id"],
        "date":                 event_date,
        "name":                 name,
        "planned_tss":          parsed.get("planned_tss"),
        "planned_duration_min": parsed.get("planned_duration_min"),
        "planned_if":           parsed.get("planned_if"),
        "planned_kj":           parsed.get("planned_kj"),
        "description":          parsed.get("description"),
        "workout_type":         parsed.get("workout_type"),
        "workout_url":          parsed.get("workout_url"),
        "gcal_updated":         event.get("updated"),
    }

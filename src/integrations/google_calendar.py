"""
Google Calendar client — fetches TrainerRoad planned workouts.

Auth flow (one-time):
    python scripts/auth_google_calendar.py

Subsequent syncs use the saved token at data/google_token.json.
"""

import os
import re
import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_PATH = Path(__file__).parent.parent.parent / "data" / "google_credentials.json"
TOKEN_PATH       = Path(__file__).parent.parent.parent / "data" / "google_token.json"

# Keywords that identify the TrainerRoad calendar
TR_CALENDAR_KEYWORDS = ["trainerroad", "trainer road", "training plan"]

# Ordered most-specific-first: text is checked top to bottom, first match wins.
# A Sweet Spot workout's description often also mentions "recovery" between intervals,
# so generic terms like Recovery/Rest must sit at the bottom or they'd shadow the real type.
WORKOUT_TYPE_KEYWORDS = [
    ("vo2", "VO2 Max"),
    ("anaerobic", "Anaerobic"),
    ("sprint", "Sprint"),
    ("threshold", "Threshold"),
    ("sweet spot", "Sweet Spot"),
    ("tempo", "Tempo"),
    ("endurance", "Endurance"),
    ("recovery", "Recovery"),
    ("rest", "Rest"),
]


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not CREDENTIALS_PATH.exists():
            raise FileNotFoundError(
                f"Google credentials not found at {CREDENTIALS_PATH}.\n"
                "Download OAuth 2.0 credentials from Google Cloud Console and save there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def get_service():
    return build("calendar", "v3", credentials=get_credentials())


def find_tr_calendar(service) -> Optional[dict]:
    """Find the TrainerRoad calendar by name."""
    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        name = cal.get("summary", "").lower()
        if any(kw in name for kw in TR_CALENDAR_KEYWORDS):
            return cal
    return None


def list_calendars(service) -> list[dict]:
    """Return all calendars — useful for setup/debugging."""
    return service.calendarList().list().execute().get("items", [])


def resolve_calendar_id(service) -> str:
    """GCAL_TR_CALENDAR_ID env var takes precedence; falls back to name-based detection."""
    override = os.environ.get("GCAL_TR_CALENDAR_ID")
    if override:
        return override
    cal = find_tr_calendar(service)
    if not cal:
        raise RuntimeError(
            "No TrainerRoad calendar found. Run `python scripts/auth_google_calendar.py` "
            "to list calendar IDs, then set GCAL_TR_CALENDAR_ID in .env."
        )
    return cal["id"]


def sync_planned_workouts(start: date, end: date) -> list[dict]:
    """One-call convenience: authenticate, resolve the TR calendar, fetch + parse events."""
    service = get_service()
    calendar_id = resolve_calendar_id(service)
    return fetch_planned_workouts(service, calendar_id, start, end)


# ── Event parsing ──────────────────────────────────────────────────────────────

def _classify_workout_type(text: str) -> Optional[str]:
    text_l = text.lower()
    for keyword, label in WORKOUT_TYPE_KEYWORDS:
        if keyword in text_l:
            return label
    return None


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
    # (it comes from the event start/end time instead, see _event_duration_min).
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
        return start["dateTime"][:10]
    return None


def _event_duration_min(event: dict) -> Optional[float]:
    """Derive duration from event start/end if not in description."""
    start = event.get("start", {})
    end = event.get("end", {})
    try:
        s = datetime.fromisoformat(start.get("dateTime", "").replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.get("dateTime", "").replace("Z", "+00:00"))
        return (e - s).total_seconds() / 60
    except Exception:
        return None


def _title_duration_and_name(summary: str) -> tuple[str, Optional[float]]:
    """
    TR's real calendar titles are "H:MM - Workout Name" (e.g. "1:00 - Galena -2",
    "0:30 - STR-A") — that prefix is the actual planned duration and is far more
    reliable than anything guessable from the description text. Plan-level events
    like "Rest Day" or "Summer Gravel Fitness" have no prefix.
    """
    m = re.match(r"^(\d+):(\d{2})\s*-\s*(.+)$", summary.strip())
    if m:
        hours, minutes, rest = m.groups()
        return rest.strip(), int(hours) * 60 + int(minutes)
    return summary.strip(), None


def parse_event(event: dict) -> Optional[dict]:
    """
    Convert a Google Calendar event to a planned_workout record.
    No title filtering here — the calendar itself is already scoped to the TR
    calendar (via resolve_calendar_id), so every event on it is a planned session,
    including non-power ones like "Mobility" or "Soul" that don't match a workout-name pattern.
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


def fetch_planned_workouts(
    service,
    calendar_id: str,
    start: date,
    end: date,
) -> list[dict]:
    """Fetch and parse TR events from a date range."""
    time_min = datetime.combine(start, datetime.min.time()).isoformat() + "Z"
    time_max = datetime.combine(end,   datetime.min.time()).isoformat() + "Z"

    events = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
            maxResults=500,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    results = []
    for ev in events:
        parsed = parse_event(ev)
        if parsed:
            results.append(parsed)
    return results

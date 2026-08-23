"""
One-time Google Calendar OAuth authorization.

Run this once after placing google_credentials.json in data/:
    python scripts/auth_google_calendar.py

Opens a browser window, you approve access, token saved to data/google_token.json.
All future syncs use the saved token (auto-refreshes).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.google_calendar import (
    get_credentials, get_service, find_tr_calendar, list_calendars
)

if __name__ == "__main__":
    print("Authorizing Google Calendar access...")
    creds = get_credentials()
    print("  Authorization successful. Token saved to data/google_token.json")

    service = get_service()
    calendars = list_calendars(service)
    print(f"\nFound {len(calendars)} calendars:")
    for cal in calendars:
        print(f"  [{cal['id'][:40]}] {cal.get('summary', '?')}")

    tr_cal = find_tr_calendar(service)
    if tr_cal:
        print(f"\nTrainerRoad calendar detected: \"{tr_cal['summary']}\"")
        print(f"  Calendar ID: {tr_cal['id']}")
        print(f"\nRun  python scripts/sync_calendar.py  to import planned workouts.")
    else:
        print("\nNo TrainerRoad calendar auto-detected.")
        print("Check the calendar list above and add the ID to your .env as GCAL_TR_CALENDAR_ID=...")

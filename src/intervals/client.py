import os
import requests
from datetime import date, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://intervals.icu/api/v1"


class IntervalsClient:
    def __init__(self, api_key: Optional[str] = None, athlete_id: Optional[str] = None):
        self.api_key = api_key or os.environ["INTERVALS_API_KEY"]
        self.athlete_id = athlete_id or os.environ.get("INTERVALS_ATHLETE_ID", "")
        self.session = requests.Session()
        # intervals.icu uses HTTP Basic Auth: username="API_KEY", password=<key>
        self.session.auth = ("API_KEY", self.api_key)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_athlete(self) -> dict:
        return self._get(f"/athlete/{self.athlete_id}")

    def get_activities(
        self,
        start: date,
        end: date,
        only_types: Optional[list[str]] = None,
    ) -> list[dict]:
        params = {
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
        }
        activities = self._get(f"/athlete/{self.athlete_id}/activities", params=params)
        if only_types:
            activities = [a for a in activities if a.get("type") in only_types]
        return activities

    def get_wellness(self, start: date, end: date) -> list[dict]:
        params = {
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
        }
        return self._get(f"/athlete/{self.athlete_id}/wellness", params=params)

    def get_fitness(self, start: date, end: date) -> list[dict]:
        """CTL/ATL/TSB fitness curves."""
        params = {
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
        }
        return self._get(f"/athlete/{self.athlete_id}/fitness-data", params=params)

    def get_events(self, start: date, end: date) -> list[dict]:
        """Races and A/B/C events."""
        params = {
            "oldest": start.isoformat(),
            "newest": end.isoformat(),
        }
        return self._get(f"/athlete/{self.athlete_id}/events", params=params)

    def ping(self) -> bool:
        """Quick connectivity check — returns True if auth works."""
        try:
            self.get_athlete()
            return True
        except requests.HTTPError:
            return False

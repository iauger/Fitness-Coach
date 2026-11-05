import os
import requests
from datetime import date
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
        self.session.headers.update({
            "Accept": "application/json",
            # Cloudflare sits in front of intervals.icu and the forum reports it challenging or
            # blocking plain library user-agents. requests sends "python-requests/x.y" by
            # default, which is exactly the shape that gets challenged.
            "User-Agent": "FitnessCoach/1.0 (personal training planner)",
        })

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{BASE_URL}{path}"
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _send(self, method: str, path: str, params: dict | None = None,
              json: object | None = None) -> dict | list | None:
        """
        A write request. Separate from `_get` so every mutating call goes through one place.

        intervals.icu returns an empty body on some deletes, so a bare `.json()` would raise
        on success. Returns None in that case rather than pretending there was a payload.
        """
        resp = self.session.request(method, f"{BASE_URL}{path}", params=params, json=json)
        resp.raise_for_status()
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _post(self, path: str, json: object, params: dict | None = None):
        return self._send("POST", path, params=params, json=json)

    def _put(self, path: str, json: object, params: dict | None = None):
        return self._send("PUT", path, params=params, json=json)

    def _delete(self, path: str, params: dict | None = None):
        return self._send("DELETE", path, params=params)

    # ── calendar writes ───────────────────────────────────────────────────────

    def create_events(self, events: list[dict]) -> list[dict]:
        """
        Create calendar events in bulk. Returns the created events, each carrying its `id`.

        **Deliberately does not pass `upsert=true`.** The parameter exists and matches on
        `external_id`, but the API documents it as "Update events with matching external_id and
        created by the same **OAuth application**" — and this client authenticates with an API
        key, not as an OAuth app. Nothing in the docs or the forum says whether an API-key
        caller counts as such an application, and the failure mode if it does not is silent:
        every re-push duplicates the whole horizon on a real calendar.

        So identity is tracked on our side instead. The caller stores each returned `id` in
        `prescribed_sessions.remote_event_id` and deletes by that id before re-creating. Bulk
        delete by `id` carries no equivalent caveat — only its `external_id` form does.

        `upsertOnUid` and `updatePlanApplied` are declared **required** in the OpenAPI schema,
        so they are sent explicitly rather than left to defaults.
        """
        params = {"upsert": "false", "upsertOnUid": "false", "updatePlanApplied": "false"}
        created = self._post(f"/athlete/{self.athlete_id}/events/bulk", events, params)
        return created or []

    def delete_events_by_id(self, event_ids: list[int]) -> int:
        """
        Delete specific events by the ids we stored. Returns how many were deleted.

        `PUT`, not `DELETE` — the bulk-delete endpoint is a PUT, which is easy to get wrong.
        Events that do not exist are ignored, so a stale id is harmless.

        Only ids we recorded are ever passed, which is what keeps this from touching the
        athlete's own calendar entries. The range form (`DELETE /events?oldest=&category=`)
        would be fewer calls and is not used: it deletes every event of that category in the
        window regardless of who created it.
        """
        if not event_ids:
            return 0
        body = [{"id": int(i)} for i in event_ids]
        resp = self._put(f"/athlete/{self.athlete_id}/events/bulk-delete", body)
        return (resp or {}).get("eventsDeleted", 0) if isinstance(resp, dict) else 0

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

    def get_activity_messages(self, activity_id: str) -> list[dict]:
        """
        Athlete-authored notes on an activity.

        intervals.icu stores post-ride commentary as a chat thread, not as a field on the
        activity — `description` is null on every activity we have ever synced, including
        ones with notes visibly attached in the web UI. This endpoint is the only way to
        reach that text.

        Only call this for activities whose `icu_chat_id` is non-null; that field is set
        exactly when a thread exists, so it avoids a request per activity.
        """
        return self._get(f"/activity/{activity_id}/messages")

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

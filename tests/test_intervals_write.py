"""
Tests for the intervals.icu write calls themselves — the request, not the payload.

These exist because the riskiest line in the export path is invisible from the payload: the
query parameters on the bulk create. `upsert=true` looks harmless and is documented as matching
`external_id` only for events "created by the same OAuth application". This client authenticates
with an API key, and nothing in the docs or forum says whether that counts. If it does not, the
server silently creates instead of updating and every push duplicates the whole horizon on a
real calendar.

Nothing here touches the network; `session.request` is replaced with a recorder.
"""

import pytest

from src.intervals.client import IntervalsClient


class FakeResponse:
    def __init__(self, payload=None, content=b"{}"):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "test-key")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    c = IntervalsClient()
    c.calls = []

    def record(method, url, params=None, json=None):
        c.calls.append({"method": method, "url": url, "params": params, "json": json})
        if url.endswith("/bulk-delete"):
            return FakeResponse({"eventsDeleted": len(json or [])})
        return FakeResponse([{"id": 7, "external_id": (json or [{}])[0].get("external_id")}])

    monkeypatch.setattr(c.session, "request", record)
    return c


def test_create_does_not_ask_the_server_to_upsert(client):
    """
    The one that matters.

    `upsert` matches on external_id only for OAuth applications. We use an API key, so relying
    on it risks silent duplication; identity is tracked locally instead.
    """
    client.create_events([{"external_id": "a", "name": "x"}])
    params = client.calls[0]["params"]
    assert params["upsert"] == "false"


def test_create_sends_the_parameters_the_schema_marks_required(client):
    """`upsertOnUid` and `updatePlanApplied` are declared required in the OpenAPI schema, so
    they are sent explicitly rather than left to a default that may not exist."""
    client.create_events([{"external_id": "a"}])
    params = client.calls[0]["params"]
    assert params["upsertOnUid"] == "false"
    assert params["updatePlanApplied"] == "false"


def test_create_posts_to_the_bulk_endpoint(client):
    client.create_events([{"external_id": "a"}])
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/athlete/i1/events/bulk")


def test_bulk_delete_is_a_put_not_a_delete(client):
    """Easy to get wrong: intervals.icu's bulk-delete endpoint is a PUT."""
    client.delete_events_by_id([1, 2])
    call = client.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/athlete/i1/events/bulk-delete")


def test_bulk_delete_sends_ids_only_never_a_date_range(client):
    """
    A range delete (`DELETE /events?oldest=&category=`) removes every event of that category in
    the window regardless of who created it — including the athlete's own. Only ids we recorded
    are ever sent.
    """
    client.delete_events_by_id([5, 6])
    call = client.calls[0]
    assert call["json"] == [{"id": 5}, {"id": 6}]
    assert not (call["params"] or {})


def test_deleting_nothing_makes_no_request(client):
    assert client.delete_events_by_id([]) == 0
    assert client.calls == []


def test_an_empty_response_body_is_not_an_error(client, monkeypatch):
    """Some intervals.icu deletes return no body; a bare .json() would raise on success."""
    monkeypatch.setattr(c := client.session, "request",
                        lambda *a, **k: FakeResponse(None, content=b""))
    assert client._delete("/whatever") is None

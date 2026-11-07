"""
The Signals screen's API, and the Plan screen's strip.

Mutants: dismissal that deletes the row (the evaluation loses its known-wrong examples); a
dismissed signal still on the watch list; a list that hides truncation; a filter that is ignored;
an unknown kind accepted; a plan overview that reads signals from the wrong connection.
"""

import functools
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from conftest import seed_ladder
from src import signals
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.clock import today_local_iso
from src.db import schema
from src.db.migrations import migrate
from src.planning.overview import overview
from src.planning.roller import store_horizon

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    migrate(path)
    for name in ("read_connection", "write_connection"):
        monkeypatch.setattr(signals, name, functools.partial(getattr(schema, name), path))
    return path


@pytest.fixture
def client(db):
    return TestClient(app, base_url=OWN)


def put(kind="injury_pain", severity=2, day=None, topic="left knee", **kw):
    return signals.record(kind, topic=topic, severity=severity, on_date=day or today_local_iso(),
                          quote=kw.pop("quote", "it aches"), **kw)


def test_the_list_is_newest_first_and_carries_what_the_screen_needs(client):
    put(day="2026-11-01")
    put(kind="illness", severity=3, day="2026-11-05", topic="")
    body = client.get("/api/signals").json()
    assert [r["kind"] for r in body["rows"]] == ["illness", "injury_pain"]
    assert body["total"] == 2 and body["truncated"] is False
    assert "illness" in body["kinds"] and body["severity_labels"]["3"] == "needs action"
    assert {"id", "quote", "source", "on_date", "state", "severity"} <= set(body["rows"][0])
    assert body["digest"]


def test_filters_apply(client):
    put(day="2026-11-01")
    put(kind="illness", severity=3, day="2026-11-05", topic="")
    put(kind="nutrition", severity=0, day="2026-11-06", topic="fuelling")
    get = lambda **q: client.get("/api/signals", params=q).json()["total"]
    assert (get(), get(kind="illness"), get(topic="knee"), get(min_severity=2), get(since="2026-11-05")) == (3, 1, 1, 2, 2)
    assert get(until="2026-11-01") == 1


def test_an_unknown_kind_or_severity_is_refused(client):
    assert client.get("/api/signals", params={"kind": "bogus"}).status_code == 422
    assert client.get("/api/signals", params={"min_severity": 7}).status_code == 422


def test_a_long_list_says_it_was_cut(client):
    for i in range(5):
        put(day=f"2026-11-0{i + 1}")
    body = client.get("/api/signals", params={"limit": 2}).json()
    assert len(body["rows"]) == 2 and body["total"] == 5 and body["truncated"] is True


def test_dismissing_keeps_the_row_and_removes_it_from_everything_the_coach_reads(client, db):
    """Mutant: DELETE. The row, and its reason, are the evaluation's known-wrong examples."""
    sid = put(kind="illness", severity=3, topic="")
    assert signals.watch_list()
    r = client.post(f"/api/signals/{sid}/dismiss", headers=HEAD, json={"reason": "that was my wife"})
    assert r.status_code == 200 and r.json() == {"id": sid, "dismissed": True}
    assert signals.watch_list() == [] and client.get("/api/signals").json()["total"] == 0
    kept = client.get("/api/signals", params={"dismissed": True}).json()["rows"]
    assert [(x["id"], x["dismissed_reason"]) for x in kept] == [(sid, "that was my wife")]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM signals").fetchone() == (1,)


def test_dismissing_something_already_dismissed_or_missing_is_a_404(client):
    sid = put()
    assert client.post(f"/api/signals/{sid}/dismiss", headers=HEAD, json={}).status_code == 200
    assert client.post(f"/api/signals/{sid}/dismiss", headers=HEAD, json={}).status_code == 404
    assert client.post("/api/signals/999/dismiss", headers=HEAD, json={}).status_code == 404


def test_restoring_brings_it_back(client):
    sid = put(kind="illness", severity=3, topic="")
    client.post(f"/api/signals/{sid}/dismiss", headers=HEAD, json={})
    assert client.post(f"/api/signals/{sid}/restore", headers=HEAD).status_code == 200
    assert signals.watch_list() and client.post(f"/api/signals/{sid}/restore", headers=HEAD).status_code == 404


def test_writes_need_the_token(client):
    sid = put()
    assert client.post(f"/api/signals/{sid}/dismiss", headers={"origin": OWN}, json={}).status_code == 403


# -- the Plan screen's strip ---------------------------------------------------------------------

TODAY = date(2026, 11, 3)


def test_the_plan_overview_carries_the_watch_list_and_what_it_would_do_not_applied(tmp_path):
    path = tmp_path / "o.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn)
    store_horizon(conn, TODAY)
    conn.execute("INSERT INTO signals (observed_at, on_date, kind, topic, severity, state, quote, source) "
                 "VALUES ('x', '2026-11-02', 'injury_pain', 'left knee', 3, 'new', 'knee is bad', 'manual')")
    conn.commit()
    s = overview(conn, TODAY)["signals"]
    conn.close()
    assert [(t["kind"], t["label"], t["age"]) for t in s["watch"]] == [("injury_pain", "needs action", "yesterday")]
    assert [(a["action"], a["applied"]) for a in s["advisories"]] == [("ease", False)]
    assert "Last 28 days" in s["digest"]

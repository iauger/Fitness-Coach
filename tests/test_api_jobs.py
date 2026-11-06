"""
The setup screen's background job and the first-run routes built on it.

Mutants these exist to catch: a second history fetch started while one runs; a job that
swallows its error and ends with silence; a job that does not take the maintenance lock (so a
fetch and the background sync write together); an FTP range check that lets 5000 W through to
size every prescription; and a route that skips the token.
"""

import threading
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src import maintenance
from src.api import jobs, setup
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER
from src.clock import today_local

OWN = "http://localhost:8800"
HEAD = {TOKEN_HEADER: TOKEN, "origin": OWN}


@pytest.fixture(autouse=True)
def lock_in_tmp(tmp_path, monkeypatch):
    """The real lock file lives in data/; never touch it from a test."""
    monkeypatch.setattr(maintenance, "LOCK_PATH", tmp_path / "maintenance.lock")


@pytest.fixture
def job():
    return jobs.Job("t")


# -- the job ----------------------------------------------------------------------------------

def test_a_job_runs_and_reports_its_result_and_log(job):
    def work(progress):
        progress("one")
        progress("two")
        return {"n": 3}
    job.start(work, wait=True)
    s = job.snapshot()
    assert (s["running"], s["result"], s["error"], s["log"]) == (False, {"n": 3}, None, ["one", "two"])
    assert s["started_at"] and s["finished_at"]


def test_a_second_start_while_running_is_refused(job):
    """Mutant: no running check. Two fetches would interleave their writes."""
    release = threading.Event()
    started = threading.Event()

    def work(progress):
        started.set()
        release.wait(5)
        return {}
    job.start(work)
    assert started.wait(5) and job.snapshot()["running"] is True
    with pytest.raises(jobs.JobBusy):
        job.start(lambda p: {})
    release.set()
    for _ in range(100):
        if not job.snapshot()["running"]:
            break
        threading.Event().wait(0.02)
    assert job.snapshot()["running"] is False
    job.start(lambda p: {"again": True}, wait=True)          # free again afterwards
    assert job.snapshot()["result"] == {"again": True}


def test_a_failure_ends_with_the_reason_not_with_silence(job):
    def work(progress):
        raise RuntimeError("the key was rejected")
    job.start(work, wait=True)
    s = job.snapshot()
    assert s["running"] is False and s["error"] == "the key was rejected" and s["result"] is None


def test_the_job_holds_the_maintenance_lock_while_it_works(job):
    """Mutant: work run outside `exclusive()`. The lock is what keeps it off the sync."""
    seen = {}

    def work(progress):
        seen["held"] = maintenance.LOCK_PATH.exists()
        return {}
    job.start(work, wait=True)
    assert seen["held"] is True and not maintenance.LOCK_PATH.exists()   # and released after


def test_if_a_sync_holds_the_lock_the_job_says_so_and_does_not_run(job):
    ran = []
    with maintenance.exclusive():
        job.start(lambda p: ran.append(1) or {}, wait=True)
    s = job.snapshot()
    assert ran == [] and "Another sync" in s["error"] and s["running"] is False


def test_each_run_starts_with_a_clean_slate(job):
    job.start(lambda p: p("old") or {"a": 1}, wait=True)
    job.start(lambda p: {"b": 2}, wait=True)
    s = job.snapshot()
    assert s["log"] == [] and s["result"] == {"b": 2}


def test_the_log_is_capped(job):
    def work(progress):
        for i in range(jobs.LOG_LINES + 50):
            progress(str(i))
        return {}
    job.start(work, wait=True)
    log = job.snapshot()["log"]
    assert len(log) == jobs.LOG_LINES and log[-1] == str(jobs.LOG_LINES + 49)


# -- the routes -------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(jobs, "HISTORY", jobs.Job("history"))
    return TestClient(app, base_url=OWN)


def wait_done(timeout=5.0):
    end = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if not jobs.HISTORY.snapshot()["running"]:
            return jobs.HISTORY.snapshot()
        end.wait(0.02)
    raise AssertionError("job did not finish")


def test_starting_the_history_fetch_needs_the_token(client, monkeypatch):
    import src.history_fetch as hf
    called = []
    monkeypatch.setattr(hf, "fetch_all", lambda *a, **k: called.append(1) or {})
    assert client.post("/api/history", json={}).status_code == 403
    assert called == []


def test_the_fetch_runs_with_the_requested_dates_and_reports_progress(client, monkeypatch):
    import src.history_fetch as hf
    seen = {}

    def fake(start, end, progress):
        seen.update(start=start, end=end)
        progress("fetching")
        return {"activities": 5, "wellness": 4, "events": 1, "errors": 0}
    monkeypatch.setattr(hf, "fetch_all", fake)
    r = client.post("/api/history", headers=HEAD, json={"start": "2020-01-01"})
    assert r.status_code == 200
    done = wait_done()
    assert seen == {"start": date(2020, 1, 1), "end": today_local()}
    assert done["result"]["activities"] == 5 and done["log"] == ["fetching"]
    assert client.get("/api/history").json()["result"]["activities"] == 5


def test_the_default_start_is_the_first_year_of_history(client, monkeypatch):
    import src.history_fetch as hf
    seen = {}
    monkeypatch.setattr(hf, "fetch_all", lambda start, end, progress: seen.update(s=start) or {})
    client.post("/api/history", headers=HEAD, json={})
    wait_done()
    assert seen["s"] == date(2017, 1, 1)


def test_a_second_fetch_while_one_runs_is_a_409(client, monkeypatch):
    import src.history_fetch as hf
    release = threading.Event()
    monkeypatch.setattr(hf, "fetch_all", lambda s, e, p: release.wait(5) or {})
    assert client.post("/api/history", headers=HEAD, json={}).status_code == 200
    assert client.post("/api/history", headers=HEAD, json={}).status_code == 409
    release.set()
    wait_done()


def test_a_start_in_the_future_is_refused(client):
    tomorrow = (today_local() + timedelta(days=1)).isoformat()
    assert client.post("/api/history", headers=HEAD, json={"start": tomorrow}).status_code == 422


def test_a_failed_fetch_shows_the_reason_in_the_status(client, monkeypatch):
    import src.history_fetch as hf

    def boom(s, e, p):
        raise hf.HistoryError("intervals.icu rejected the API key")
    monkeypatch.setattr(hf, "fetch_all", boom)
    client.post("/api/history", headers=HEAD, json={})
    assert "rejected the API key" in wait_done()["error"]


# -- ftp --------------------------------------------------------------------------------------

@pytest.fixture
def ftp_calls(monkeypatch):
    import src.athlete.profile as profile
    calls = []
    monkeypatch.setattr(profile, "set_metric", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(setup.firstrun, "checks", lambda *a, **k: [])
    return calls


@pytest.mark.parametrize("watts", [49.9, 0, -5, 600.1, 5000])
def test_an_implausible_ftp_is_refused_and_nothing_is_recorded(client, ftp_calls, watts):
    r = client.post("/api/profile/ftp", headers=HEAD, json={"watts": watts})
    assert r.status_code == 422 and ftp_calls == []


@pytest.mark.parametrize("watts", [50, 260, 600])
def test_a_plausible_ftp_is_recorded_as_a_dated_entry(client, ftp_calls, watts):
    r = client.post("/api/profile/ftp", headers=HEAD, json={"watts": watts})
    assert r.status_code == 200 and r.json()["ftp"] == watts
    assert ftp_calls == [("ftp", watts, None)]               # None: today, as the athlete sees it


def test_an_ftp_can_be_backdated_but_not_future_dated(client, ftp_calls):
    client.post("/api/profile/ftp", headers=HEAD, json={"watts": 250, "effective_date": "2026-10-01"})
    assert ftp_calls == [("ftp", 250, "2026-10-01")]
    tomorrow = (today_local() + timedelta(days=1)).isoformat()
    assert client.post("/api/profile/ftp", headers=HEAD,
                       json={"watts": 250, "effective_date": tomorrow}).status_code == 422


def test_ftp_needs_the_token(client, ftp_calls):
    assert client.post("/api/profile/ftp", json={"watts": 250}).status_code == 403
    assert ftp_calls == []


# -- database ---------------------------------------------------------------------------------

def test_setting_up_the_database_migrates_and_returns_the_checklist(client, monkeypatch):
    import src.db.schema as schema
    ran = []
    monkeypatch.setattr(schema, "migrate_db", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(setup.firstrun, "checks", lambda *a, **k: [])
    assert client.post("/api/database").status_code == 403 and ran == []
    r = client.post("/api/database", headers=HEAD)
    assert r.status_code == 200 and ran == [1] and "checks" in r.json()

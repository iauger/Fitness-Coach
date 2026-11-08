"""
The API refuses requests that did not come from this server's own pages.

Each test names the mutant it exists to catch. The route-sweep at the bottom is the one that
matters for the future: it walks every route the app has, so a write route added next month is
tested without anyone writing a test for it.
"""

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.api import guard
from src.api.app import app
from src.api.guard import TOKEN, TOKEN_HEADER, refusal

HOST = "localhost:8800"
OWN = f"http://{HOST}"


@pytest.fixture
def client():
    # No `with`: the lifespan would start the maintenance scheduler.
    return TestClient(app, base_url=OWN)


def _h(**kw):
    return {"host": HOST, **kw}


# -- Host: DNS rebinding ----------------------------------------------------------------------

def test_a_foreign_host_is_refused_even_for_a_read(client):
    """Mutant: Host checked only on writes. Rebinding is a *read* attack as well."""
    r = client.get("/chat-test", headers={"host": "evil.example:8800"})
    assert r.status_code == 403 and "Host" in r.json()["detail"]


@pytest.mark.parametrize("host", ["127.0.0.1:8800", "localhost", "[::1]:8800", "LOCALHOST:9"])
def test_loopback_names_are_accepted(host):
    assert refusal("GET", {"host": host}) is None


def test_a_lookalike_host_is_refused():
    """Mutant: endswith/startswith instead of equality on the hostname."""
    assert refusal("GET", {"host": "localhost.evil.com"})
    assert refusal("GET", {"host": "127.0.0.1.evil.com"})


def test_extra_hosts_are_opt_in(monkeypatch):
    assert refusal("GET", {"host": "coach.lan"})
    monkeypatch.setenv("FITNESS_COACH_ALLOWED_HOSTS", "coach.lan, other")
    assert refusal("GET", {"host": "coach.lan"}) is None


# -- Origin: cross-site fetches ---------------------------------------------------------------

def test_a_cross_site_origin_is_refused_even_with_the_token():
    """Mutant: token alone is enough. It is meant to be the *second* lock."""
    h = _h(origin="http://evil.example", **{TOKEN_HEADER: TOKEN})
    assert "Origin" in refusal("POST", h)


def test_a_cross_site_origin_is_refused_on_a_read_too(client):
    """Mutant: Origin only inspected for writes (it would leak /api/session to a CORS fetch)."""
    r = client.get("/api/session", headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_another_port_on_localhost_is_another_origin():
    """A dev server on :3000 is a different origin and is not trusted by default."""
    assert refusal("POST", _h(origin="http://localhost:3000", **{TOKEN_HEADER: TOKEN}))


def test_null_origin_is_refused():
    """Sandboxed iframes and file:// pages send 'null'."""
    assert refusal("POST", _h(origin="null", **{TOKEN_HEADER: TOKEN}))


def test_no_origin_is_fine_for_a_script_with_the_token():
    """curl and scripts send no Origin; refusing them would make the API unusable by hand."""
    assert refusal("POST", _h(**{TOKEN_HEADER: TOKEN})) is None


# -- token ------------------------------------------------------------------------------------

def test_a_write_needs_the_token():
    assert refusal("POST", _h(origin=OWN))
    assert refusal("POST", _h(origin=OWN, **{TOKEN_HEADER: "wrong"}))
    assert refusal("POST", _h(origin=OWN, **{TOKEN_HEADER: TOKEN[:-1]}))
    assert refusal("POST", _h(origin=OWN, **{TOKEN_HEADER: TOKEN})) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_unsafe_method_needs_the_token(method):
    """Mutant: only POST checked."""
    assert refusal(method, _h(origin=OWN))


def test_reads_do_not_need_the_token(client):
    assert client.get("/chat-test").status_code == 200  # static: touches no database


def test_the_token_is_served_to_the_app_itself(client):
    assert client.get("/api/session", headers={"origin": OWN}).json() == {"token": TOKEN}


def test_the_token_is_random_and_long():
    assert len(TOKEN) >= 32 and TOKEN != guard.secrets.token_urlsafe(32)


# -- through the real app ---------------------------------------------------------------------

def test_a_write_is_refused_without_the_token_and_runs_with_it(client):
    assert client.post("/api/cache/clear").status_code == 403
    ok = client.post("/api/cache/clear", headers={TOKEN_HEADER: TOKEN, "origin": OWN})
    assert ok.status_code == 200 and ok.json() == {"cleared": True}


def test_the_chat_route_is_refused_before_it_can_spend_anything(client):
    """Mutant: Guard not installed on the router. /ask calls the Anthropic API."""
    r = client.post("/api/chat/ask", json={"question": "hi"})
    assert r.status_code == 403


def test_no_cors_headers_are_ever_set(client):
    """Mutant: a CORS middleware added to make a dev frontend work. It would let any page read
    the token."""
    r = client.get("/api/session", headers={"origin": OWN})
    assert not any(k.lower().startswith("access-control-") for k in r.headers)


def test_every_write_route_the_app_has_is_guarded(client):
    """The sweep: whatever routes exist now or later, an unauthenticated write is a 403."""
    writes = [(m, r.path) for r in guard.routes(app) if isinstance(r, APIRoute)
              for m in r.methods if m not in guard.SAFE_METHODS]
    assert writes, "the sweep found no write routes, so it is not testing anything"
    # An independent count: the OpenAPI document is built from the routes FastAPI actually serves.
    # The sweep once walked `app.routes` and saw 1 write route of 13 because included routers hide
    # theirs; it still passed. Disagreeing with the schema is how that shows up.
    served = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops if m.upper() not in guard.SAFE_METHODS}
    assert set(writes) == served
    for method, path in writes:
        url = path.replace("{session_id}", "x").replace("{start_date}", "2026-01-05")
        assert client.request(method, url).status_code == 403, (method, path)

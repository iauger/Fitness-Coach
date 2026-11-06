"""
One navigation bar on every page the server serves.

Found by using it: the app had links to nowhere from the dashboard and the calendar, which are
generated pages, so the only way back from either was the browser's back button and none of the
newer screens could be reached. Mutants: a page served without the bar; the bar on the wrong
side of <body>; every link marked current; the app's own header drifting from the shared list.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.api.app as web
from src.web import nav
from src.web.nav import ITEMS, nav_html, with_nav

PAGE = "<!DOCTYPE html><html><head><title>x</title></head><body class='a'><h1>Hello</h1></body></html>"


def test_the_bar_goes_first_inside_the_body():
    out = with_nav(PAGE, "dashboard")
    assert out.index("<body class='a'>") < out.index('id="fc-nav"') < out.index("<h1>Hello</h1>")
    assert out.count('<nav id="fc-nav"') == 1


def test_a_page_with_no_body_is_left_alone():
    assert with_nav("<p>fragment</p>", "plan") == "<p>fragment</p>"


def test_body_matching_ignores_case():
    assert 'id="fc-nav"' in with_nav("<HTML><BODY><p>x</p></BODY></HTML>", "plan")


def test_every_destination_is_linked():
    out = nav_html("plan")
    for label, href, _ in ITEMS:
        assert f'<a href="{href}"' in out and f">{label}</a>" in out


@pytest.mark.parametrize("key", [k for _, _, k in ITEMS])
def test_exactly_the_open_page_is_marked_current(key):
    """Mutant: aria-current on every link, or on none."""
    out = nav_html(key)
    assert out.count('aria-current="page">') == 1
    label, href, _ = next(i for i in ITEMS if i[2] == key)
    assert f'<a href="{href}" aria-current="page">{label}</a>' in out


def test_the_calendar_bar_bleeds_into_its_padding_and_the_dashboard_does_not():
    assert "class=bleed" in nav_html("calendar", bleed=True)
    assert "class=bleed" not in nav_html("dashboard")


def test_the_apps_own_header_links_to_the_same_places():
    """
    index.html highlights the open screen from the URL hash, so it carries its own links:
    `#plan` where the shared bar says `/app#plan`. Same labels, same order, same targets.
    """
    html = (Path(web.__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")
    header = re.search(r"<nav[^>]*>(.*?)</nav>", html, flags=re.S).group(1)
    own = re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', header)
    shared = [(href.replace("/app", "", 1) if href.startswith("/app#") else href, label)
              for label, href, _ in ITEMS]
    assert own == shared


# -- served -----------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web, "build_dashboard", lambda: PAGE)
    monkeypatch.setattr(web, "build_calendar_html", lambda weeks=6, ahead=2: PAGE)
    web.CACHE.clear()
    yield TestClient(web.app, base_url="http://localhost:8800", follow_redirects=False)
    web.CACHE.clear()


def test_the_root_is_the_plan_not_the_dashboard(client):
    r = client.get("/")
    assert r.status_code in (302, 307) and r.headers["location"] == "/app"


def test_the_dashboard_has_moved_and_carries_the_bar(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and "<h1>Hello</h1>" in r.text
    assert 'href="/app#plan"' in r.text and 'aria-current="page">Dashboard<' in r.text


def test_the_calendar_carries_the_bar(client):
    r = client.get("/calendar")
    assert r.status_code == 200 and "<h1>Hello</h1>" in r.text
    assert 'aria-current="page">Calendar<' in r.text and "class=bleed" in r.text


def test_the_old_dashboard_address_still_works(client):
    r = client.get("/dashboard.html")
    assert r.headers["location"] == "/dashboard"


def test_the_cached_page_is_not_stored_with_the_bar_in_it(client):
    """Mutant: inject before caching. The bar would be added again on every request."""
    client.get("/dashboard")
    client.get("/dashboard")
    assert client.get("/dashboard").text.count('<nav id="fc-nav"') == 1


def test_the_bar_asks_the_server_whether_a_check_in_is_due():
    """The count on the Coach link comes from the server's rules, not from the bar."""
    out = nav_html("dashboard")
    assert "fetch('/api/chat/due')" in out and "d.attention" in out
    assert 'a[href="/app#coach"]' in out

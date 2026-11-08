"""
Who may talk to this server.

The server binds to loopback, which keeps the network out but not the browser: any page the
athlete has open can make their browser send requests to localhost, and DNS rebinding can make
a hostile hostname resolve to 127.0.0.1 so that page may read the answers too. Reads were
harmless; the first route that changes something is not. So the rule is applied by method, in
one place, and a route added later is covered without anyone remembering to cover it.

    every request    Host must be a loopback name          (stops DNS rebinding)
    every request    Origin, when sent, must be this server (stops cross-site fetches)
    writes           a valid X-Fitness-Token header         (second lock; see below)

The token is generated per process and handed out by GET /api/session. A page on another
origin can send that request but cannot read the reply (no CORS headers are ever set), so only
the app's own pages learn it. It is a second lock, not the first: it holds if an Origin or Host
check is ever loosened.

Writes with no Origin are allowed through to the token check, because scripts and curl do not
send one and a browser always does on a POST. `FITNESS_COACH_ALLOWED_HOSTS` (comma separated)
adds names for a container or reverse proxy; it is a decision to widen, so it is not a default.
"""

import hmac
import json
import os
import secrets
from urllib.parse import urlsplit

TOKEN_HEADER = "x-fitness-token"
TOKEN_PATH = "/api/session"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = ("127.0.0.1", "localhost", "::1")

TOKEN = secrets.token_urlsafe(32)


def routes(app):
    """Every route the app answers, including the ones its included routers hold.

    `app.routes` is not that list. Since FastAPI 0.141 `include_router` leaves a single
    `_IncludedRouter` entry in it, so iterating it and keeping the `APIRoute`s finds only the routes
    declared on `app` itself. The route-sweep test did exactly that and went on passing while
    covering one write route instead of thirteen. Each router's own routes already carry their
    prefix in `.path`.
    """
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from routes(inner)
        else:
            yield route


def allowed_hosts() -> frozenset[str]:
    extra = os.environ.get("FITNESS_COACH_ALLOWED_HOSTS", "")
    return frozenset(LOOPBACK) | {h.strip().lower() for h in extra.split(",") if h.strip()}


def _hostname(netloc: str) -> str:
    """'localhost:8800' -> 'localhost'; '[::1]:8800' -> '::1'."""
    return (urlsplit("//" + netloc).hostname or "").lower()


def refusal(method: str, headers: dict[str, str]) -> str | None:
    """Why this request is refused, or None. Pure, so it is tested without a server."""
    host = headers.get("host", "")
    if _hostname(host) not in allowed_hosts():
        return f"Host {host!r} is not a name this server answers to"
    origin = headers.get("origin")
    if origin is not None and origin != "null" and urlsplit(origin).netloc.lower() != host.lower():
        return f"Origin {origin!r} is not this server"
    if origin == "null":
        return "Origin 'null' (a sandboxed or file page) is not this server"
    if method.upper() not in SAFE_METHODS:
        sent = headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(sent.encode(), TOKEN.encode()):
            return f"missing or wrong {TOKEN_HEADER} header (GET {TOKEN_PATH} for it)"
    return None


class Guard:
    """Pure ASGI, not BaseHTTPMiddleware: the chat route streams, and this must not buffer it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # A websocket upgrade carries Host and Origin too; none exist, and refusing the
            # lot is safer than letting the first one through unchecked.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        why = refusal(scope["method"], headers)
        if why is None:
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": why}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

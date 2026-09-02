"""
Run the local Fitness Coach app.

    python scripts/serve.py                 # http://127.0.0.1:8800
    python scripts/serve.py --port 9000
    python scripts/serve.py --reload        # auto-restart on code changes (development)
    python scripts/serve.py --open          # open a browser once the server is up

Read-only in this phase (12A) — nothing here writes to the database.

Binding is deliberately restricted to 127.0.0.1. This process reads `.env`, so it holds the
intervals.icu and Anthropic keys, and it has no authentication of any kind; on a routable
interface it would be an open proxy to both. Remote access is roadmap item 16 and belongs to a
VPN, not to this server. `--host` exists for cases like a container's 0.0.0.0, and warns.
"""

import sys
import argparse
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn

from src.db.schema import init_db, migrate_db

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8800


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"interface to bind (default {DEFAULT_HOST}; see the warning above)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--reload", action="store_true", help="auto-restart on code changes")
    p.add_argument("--open", action="store_true", dest="open_browser")
    args = p.parse_args()

    # Cheap and idempotent; means a fresh clone can serve without running a seed script first.
    init_db()
    migrate_db()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: binding to {args.host}, not loopback. This server has no auth and "
              f"holds your API keys.\n           Only do this behind a VPN or inside a "
              f"container.\n")

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"  Fitness Coach — {url}")
    print(f"  dashboard {url}/   |   calendar {url}/calendar   |   API docs {url}/docs\n")

    if args.open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # --reload needs an import string rather than the app object, and cannot be combined with
    # a pre-imported app.
    uvicorn.run("src.api.app:app" if args.reload else _app(),
                host=args.host, port=args.port, reload=args.reload, log_level="info")


def _app():
    from src.api.app import app
    return app


if __name__ == "__main__":
    main()

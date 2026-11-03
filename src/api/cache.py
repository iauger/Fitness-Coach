"""
Freshness-keyed cache for expensive page builds.

`build_dashboard()` takes ~3.8s — fine as a one-off script, far too slow to run per request.
Everything it renders derives from the SQLite database, so the database's own mtime is a
sufficient cache key: if nothing has been written, the page cannot have changed.

Keyed on the main database's mtime and size, plus the **size** of its -wal sidecar. The
connection runs in WAL mode (`PRAGMA journal_mode=WAL` in db/schema.py), so a pending write
lands in fitness.db-wal and may leave the main file's mtime untouched until a checkpoint —
keying on the main file alone would serve a stale page after a sync.

The WAL's mtime is deliberately excluded; see `freshness_key`.
"""

from pathlib import Path
from threading import Lock
from typing import Callable

from src.db.schema import DB_PATH


def freshness_key() -> tuple:
    """
    Cheap stat-based fingerprint of the database, including its WAL sidecar.

    The main file contributes mtime and size. The WAL contributes **size only**, deliberately:
    SQLite creates the -wal when a connection opens and deletes it when the last one closes, so
    its mtime flips constantly in a server that opens a connection per request, and including
    it invalidated the cache on reads that had changed nothing — a 3.8s rebuild each time.
    Its size does not flip: an open WAL with nothing pending measures 0, exactly like an absent
    one, while a pending write grows it. A checkpoint, which is what removes the WAL, updates
    the main file's mtime, so that transition is still caught.

    `PRAGMA data_version` is the textbook answer and does not work here: it reports changes seen
    by one living connection, and every call site opens a fresh one, so it never moves.
    """
    try:
        st = DB_PATH.stat()
        main = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        main = (None, None)
    try:
        wal = Path(str(DB_PATH) + "-wal").stat().st_size
    except FileNotFoundError:
        wal = 0
    return (main, wal)


class BuildCache:
    """Memoizes a builder against the database's freshness key."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[tuple, str]] = {}
        # One lock per cache, not per entry: uvicorn runs sync endpoints in a threadpool, so two
        # concurrent requests for a cold page both missed and both ran the 3.8s build. Not a
        # correctness bug — dict assignment is atomic under the GIL — but the whole point of
        # this class is to not pay that cost twice.
        self._lock = Lock()

    def get(self, name: str, builder: Callable[[], str]) -> str:
        key = freshness_key()
        hit = self._entries.get(name)
        if hit and hit[0] == key:
            return hit[1]
        with self._lock:
            # Re-check: another thread may have built it while this one waited.
            hit = self._entries.get(name)
            if hit and hit[0] == key:
                return hit[1]
            value = builder()
            self._entries[name] = (key, value)
            return value

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict:
        return {"cached": sorted(self._entries), "key": str(freshness_key())}


CACHE = BuildCache()

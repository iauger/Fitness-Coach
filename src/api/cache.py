"""
Freshness-keyed cache for expensive page builds.

`build_dashboard()` takes ~3.8s — fine as a one-off script, far too slow to run per request.
Everything it renders derives from the SQLite database, so the database's own mtime is a
sufficient cache key: if nothing has been written, the page cannot have changed.

Keyed on the -wal file as well as the main database. The connection runs in WAL mode
(`PRAGMA journal_mode=WAL` in db/schema.py), so a write lands in fitness.db-wal and may leave
the main file's mtime untouched until a checkpoint — keying on the main file alone would serve
a stale page after every sync.
"""

from pathlib import Path
from typing import Callable

from src.db.schema import DB_PATH


def freshness_key() -> tuple:
    """Cheap stat-based fingerprint of the database, including its WAL sidecar."""
    parts = []
    for p in (DB_PATH, Path(str(DB_PATH) + "-wal")):
        try:
            st = p.stat()
            parts.append((p.name, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            parts.append((p.name, None, None))
    return tuple(parts)


class BuildCache:
    """Memoizes a builder against the database's freshness key."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[tuple, str]] = {}

    def get(self, name: str, builder: Callable[[], str]) -> str:
        key = freshness_key()
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

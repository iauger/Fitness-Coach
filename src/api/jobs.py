"""
One long task at a time, run off the request thread, with a status the page can poll.

Fetching nine years of history is a few dozen network calls and takes minutes, which is too long
to hold a request open and too long to leave the page saying nothing. A job here is deliberately
the smallest thing that works: one thread, one slot per name, state in memory.

It takes the maintenance lock, so a history fetch and the background sync cannot write at the
same time (the lock is a file, so this also holds against a separate `sync_now.py`). If that
lock is held the job says so and ends, rather than queueing behind it.

State lives in this process only. A restart forgets a finished job, which is fine: what a job
*did* is in the database, and `GET /api/setup` reads it from there.
"""

import threading
from src import applog
from datetime import datetime, timezone
from typing import Callable

from src import maintenance

LOG_LINES = 200


class JobBusy(Exception):
    pass


class Job:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._state: dict = {"running": False, "started_at": None, "finished_at": None,
                             "log": [], "result": None, "error": None}

    def snapshot(self) -> dict:
        with self._lock:
            return {**self._state, "log": list(self._state["log"])}

    def _log(self, message: str) -> None:
        with self._lock:
            self._state["log"] = (self._state["log"] + [message])[-LOG_LINES:]

    def start(self, work: Callable[[Callable[[str], None]], dict], *, wait: bool = False) -> None:
        """Run `work(progress)` in a thread. Raises JobBusy if this job is already running."""
        with self._lock:
            if self._state["running"]:
                raise JobBusy(f"{self.name} is already running")
            self._state = {"running": True, "started_at": _now(), "finished_at": None,
                           "log": [], "result": None, "error": None}
        thread = threading.Thread(target=self._run, args=(work,), daemon=True,
                                  name=f"job-{self.name}")
        thread.start()
        if wait:
            thread.join()

    def _run(self, work) -> None:
        result, error = None, None
        try:
            with maintenance.exclusive():
                result = work(self._log)
        except maintenance.AlreadyRunning as exc:
            error = f"Another sync is running ({exc}). Try again in a minute."
        except Exception as exc:          # a job must end with a reason, never with silence
            applog.error("job.failed", exc, job=self.name)
            error = str(exc) or exc.__class__.__name__
        with self._lock:
            self._state.update(running=False, finished_at=_now(), result=result, error=error)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


HISTORY = Job("history")

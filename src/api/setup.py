"""
What the setup screen and the settings screen talk to.

Both are thin: the checklist is `firstrun.checks()` and the saves are `config_write.update()`,
so the UI cannot disagree with `status.py` or write something the loader would refuse. What is
new here is *which* settings the browser may change. Not the server's host or port (a page that
could rebind the server is the thing the guard exists to stop), not the plan slug (never
recomputed, every calendar event id is built from it), and not the coach model (it is a bill).
"""

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src import config as cfg
from src import config_write, firstrun
from src import maintenance
from src.planning import ladder_ops
from src.planning.bootstrap import BootstrapError

router = APIRouter(prefix="/api", tags=["setup"])

# dotted path -> reader of the current value from a loaded Config. The keys are the whole
# whitelist: a path not here is refused, however valid it would be in the file.
_FIELDS = {
    "athlete.name": lambda c: c.athlete.name,
    "athlete.timezone": lambda c: c.athlete.timezone,
    "athlete.notes": lambda c: c.athlete.notes,
    "athlete.equipment": lambda c: c.athlete.equipment,
    "plan.name": lambda c: c.plan.name,
    "plan.start_monday": lambda c: c.plan.start_monday.isoformat(),
    "plan.sustainable_band_hours": lambda c: list(c.plan.sustainable_band_hours),
    "plan.long_ride_ceiling_min": lambda c: c.plan.long_ride_ceiling_min,
    "plan.max_ride_days": lambda c: c.plan.max_ride_days,
    "plan.available_days": lambda c: _days(c.plan.available_days),
    "plan.long_ride_day": lambda c: _day(c.plan.long_ride_day),
    "plan.max_ride_min": lambda c: c.plan.max_ride_min,
    "plan.day_max_ride_min": lambda c: (
        None if c.plan.day_max_ride_min is None
        else {_day(k): v for k, v in c.plan.day_max_ride_min.items()}),
    "plan.strength_days": lambda c: c.plan.strength_days,
    "integrations.intervals_icu.push_calendar": lambda c: c.integrations.intervals_icu.push_calendar,
}


def _day(name: Any) -> str | None:
    return None if name is None else cfg.WEEKDAYS[cfg.weekday(name, "day")]


def _days(names) -> list[str] | None:
    return None if names is None else [_day(n) for n in names]


def current(config: cfg.Config | None = None) -> dict:
    config = config or cfg.load(create_if_missing=False)
    return {path: read(config) for path, read in _FIELDS.items()}


def _checks() -> dict:
    found = firstrun.checks()
    return {
        "ready": all(c.ok for c in found if c.blocking),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail, "fix": c.fix,
                    "blocking": c.blocking} for c in found],
    }


@router.get("/setup")
def setup() -> dict:
    """The first-run checklist, exactly as `scripts/status.py` prints it."""
    return _checks()


@router.get("/settings")
def settings() -> dict:
    try:
        return {"values": current()}
    except cfg.ConfigError as exc:
        raise HTTPException(409, str(exc))


class Changes(BaseModel):
    changes: dict[str, Any]


@router.put("/settings")
def save_settings(body: Changes) -> dict:
    """
    Apply a set of changes, all or nothing. The response carries the checklist again, because
    the interesting consequence of a save is rarely the save: moving a limit can leave the stored
    ladder stale, and that is a warning the athlete should see on the same screen.
    """
    refused = sorted(set(body.changes) - set(_FIELDS))
    if refused:
        raise HTTPException(403, f"not editable from the browser: {', '.join(refused)}")
    try:
        new = config_write.update(body.changes)
    except cfg.ConfigError as exc:
        raise HTTPException(422, str(exc))
    return {"values": current(new), **_checks()}


# ── the ladder ────────────────────────────────────────────────────────────────

@router.get("/ladder")
def ladder() -> dict:
    """What a re-derive would produce under the current settings, beside what is stored, with
    every changed field named. Writes nothing."""
    return ladder_ops.preview()


class LadderBody(BaseModel):
    replace: bool = False


@router.post("/ladder")
def keep_ladder(body: LadderBody) -> dict:
    """Derive and store. `replace` is a new version; weeks already decided keep their old one."""
    try:
        done = ladder_ops.store(replace=body.replace)
    except BootstrapError as exc:
        raise HTTPException(422, str(exc))
    except ladder_ops.LadderError as exc:
        raise HTTPException(409, str(exc))
    return {"version": done["version"], "written": done["written"], **_checks()}


@router.post("/plan/regenerate")
def regenerate() -> dict:
    """
    One maintenance pass (pull, roll the horizon) that **never pushes**, whatever
    `push_calendar` says. Getting a new ladder onto the calendar is a separate, deliberate act;
    this only rewrites the stored sessions, and the next scheduled run or an explicit
    `sync_now.py --push` is what carries them out.
    """
    result = maintenance.run_cycle(push_enabled=False)
    return {"summary": maintenance.summarise(result), "errors": result.get("errors", [])}

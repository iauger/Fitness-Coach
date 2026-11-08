"""
The Signals screen: everything the athlete has told the coach, filterable, and theirs to correct.

A signal the extractor got wrong is dismissed, not deleted: the row stays with the reason, so it
can become a known-wrong example in the evaluation set. Dismissal is the only write here, it is
reversible, and a dismissed signal vanishes from the coach's briefing, the watch list and the
check-in status at once, since all of those read the same function.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src import signals
from src.api.ids import RowId
from src.clock import today_local

router = APIRouter(prefix="/api/signals", tags=["signals"])

MAX_ROWS = 300


class Dismissal(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.get("")
def list_signals(kind: str | None = None, topic: str | None = None,
                 min_severity: int = Query(0, ge=0, le=3), since: str | None = None,
                 until: str | None = None, dismissed: bool = False,
                 limit: int = Query(100, ge=1, le=MAX_ROWS)) -> dict:
    """Mentions newest first. `dismissed` includes the ones the athlete has set aside."""
    if kind and kind not in signals.KINDS:
        raise HTTPException(422, f"unknown kind {kind!r}")
    rows = signals.timeline(since=since, until=until, kind=kind, topic=topic,
                            min_severity=min_severity, limit=100000, dismissed=dismissed)
    return {"total": len(rows), "truncated": len(rows) > limit, "rows": rows[:limit],
            "kinds": list(signals.KINDS), "severity_labels": signals.SEVERITY_LABELS,
            "digest": signals.digest(today_local().isoformat())}


@router.post("/{signal_id}/dismiss")
def dismiss(signal_id: RowId, body: Dismissal) -> dict:
    if not signals.dismiss(signal_id, body.reason):
        raise HTTPException(404, f"no live signal {signal_id}")
    return {"id": signal_id, "dismissed": True}


@router.post("/{signal_id}/restore")
def restore(signal_id: RowId) -> dict:
    if not signals.restore(signal_id):
        raise HTTPException(404, f"no dismissed signal {signal_id}")
    return {"id": signal_id, "dismissed": False}

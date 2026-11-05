"""
Push the stored horizon to the intervals.icu calendar.

Reads `prescribed_sessions` — the plan of record — and turns each session into a calendar
event. Nothing is generated here; if the exporter and the generator ever disagree, the stored
rows win, because those are what the athlete was actually asked to do.

**Structured steps ride along as the .zwo we already produce.** The bulk endpoint accepts
`file_contents` plus `filename` for zwo, mrc, erg and fit, and parses it server-side. That makes
a second serialiser — the native intervals.icu text DSL — unnecessary: one workout definition,
already validated by being ridden in Zwift, feeds both targets. The DSL's grammar is not
formally documented anywhere, which would have made it the riskier of the two.

**Identity is tracked locally, not by `upsert`.** intervals.icu documents its `upsert` parameter
as matching `external_id` only for events "created by the same OAuth application", and this
client uses an API key. Whether that counts is undocumented, and the failure mode is silent
duplication of the whole horizon on every push. So each created event's `id` is stored in
`prescribed_sessions.remote_event_id`, and a re-push deletes those ids before creating again.
`external_id` is still sent — it costs nothing and makes the rows identifiable in the web UI —
but nothing depends on the server matching it.
"""

import json
from datetime import date, timedelta

from src.clock import utc_now_iso
from src.planning.constants import plan_name as configured_plan_name
from src.planning.zwo import safe_filename, to_zwo

# intervals.icu rejects a start date carrying a time of day: the forum is explicit that calendar
# dates "must end with T00:00:00".
MIDNIGHT = "T00:00:00"

# Of the documented categories (WORKOUT, RACE_A..C, NOTE, PLAN, HOLIDAY, ...), WORKOUT is the one
# the forum's own worked example uses for a planned session. PLAN is undocumented as to meaning.
CATEGORY = "WORKOUT"

# Strength days have no power target and no .zwo. They still belong on the calendar — strength is
# the habit the whole plan is anchored on, and leaving it off would make the calendar disagree
# with the plan about what the week asks for.
SPORT_BY_ARCHETYPE = {
    "lit": "Ride", "sit": "Ride", "hiit": "Ride", "threshold": "Ride",
    "strength": "WeightTraining",
}


def rideable(session: dict) -> bool:
    return session["archetype"] != "strength"


def event_for(row: dict, zwo: str | None = None) -> dict:
    """
    One `prescribed_sessions` row as an intervals.icu event.

    `moving_time` is seconds. `icu_training_load` is our own TSS estimate — the API recalculates
    load for workouts it can parse, so this is a floor rather than the last word, and it is what
    makes an unparsed strength day show a sensible number instead of nothing.
    """
    event = {
        "start_date_local": f"{row['date']}{MIDNIGHT}",
        "category": CATEGORY,
        "type": SPORT_BY_ARCHETYPE.get(row["archetype"], "Ride"),
        "name": row["prescription"].split("  (")[0].strip(),
        "description": _description(row, dose=zwo is None),
        "moving_time": int(round((row["minutes"] or 0) * 60)),
        "icu_training_load": int(round(row["tss"] or 0)),
        "external_id": row["external_id"],
    }
    if zwo:
        # Server-side parsed into workout_doc, which is also what Garmin sync needs.
        event["filename"] = safe_filename(f"{row['date']}_{row['role']}")
        event["file_contents"] = zwo
    return event


def _description(row: dict, dose: bool = True) -> str:
    """
    What the athlete reads on the calendar: where the session sits in the plan, and — only when
    nothing better is attached — the dose.

    **The dose is omitted whenever a .zwo is attached, and this is not cosmetic.** intervals.icu
    parses `description` as a workout in its own native format. Our prescription strings start
    with things like "8x 10s all-out, 3min recovery", and the parser read that "8x" as a repeat
    count over the *entire* attached workout: a 2720-second session came back as 21760 seconds,
    exactly eight times too long, with a training load of 212 instead of 26. "6x 20s" did the
    same thing six times over. Observed on the live calendar, not theorised.

    So where there is a `workout_doc` to describe the session, the description carries only
    prose the parser cannot mistake for a step list. Strength days keep the full text: they have
    no file attached, their prescription has no leading rep count, and the text is all there is.
    """
    bits = []
    if dose:
        bits.append(row["prescription"])
    where = f"{row['block_name']} wk{row['week_in_block']}"
    if row["volume_tier"] is not None:
        where += f", T{row['volume_tier']}"
    if row["week_type"] and row["week_type"] != "normal":
        where += f" ({row['week_type']} week)"
    bits.append(where)
    bits.append(row["plan_name"])
    return "\n".join(bits)


def pending(conn, today: date, plan_name: str | None = None) -> list[dict]:
    """
    The sessions that should be on the remote calendar: today onward, within the stored plan.

    Past sessions are deliberately excluded. They are the record of what was asked, and pushing
    them would both be pointless and risk overwriting an activity the athlete has already
    logged against that day.
    """
    plan_name = plan_name or configured_plan_name()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM prescribed_sessions WHERE plan_name = ? AND date >= ? ORDER BY date, role",
        (plan_name, today.isoformat()),
    )]


def build_payload(rows: list[dict]) -> list[dict]:
    """Events for every row, with the .zwo attached wherever there is one to attach."""
    from src.planning import archetypes as A

    out = []
    for row in rows:
        zwo = None
        if rideable(row):
            params = json.loads(row["params_json"] or "{}")
            session = _session_from(row, params, A)
            if session is not None:
                # The .zwo's own <description> must be dose-free too, and this is the line
                # the first fix missed. intervals.icu reads the attached file, lifts its
                # description verbatim into the event, and *then* parses the result as a
                # native workout — so the "8x ..." living inside the file repeated the whole
                # 19-step session eight times even after the event description was cleaned.
                # The copy Zwift gets keeps the full prescription; only this one is stripped.
                zwo = to_zwo(session, row["prescription"].split("  (")[0].strip(),
                             _description(row, dose=False))
        out.append(event_for(row, zwo))
    return out


def _session_from(row: dict, params: dict, A):
    """Rebuild the archetype session a stored row came from, so its steps can be re-emitted."""
    builders = {
        "lit": lambda: A.lit(int(row["minutes"]), row["variant"]),
        "sit": lambda: A.sit(row["variant"]),
        "hiit": lambda: A.hiit(row["variant"]),
        "threshold": lambda: A.threshold(row["variant"]),
    }
    builder = builders.get(row["archetype"])
    return builder() if builder else None


def _as_event_id(value) -> int | None:
    """A stored remote id as the integer the API wants, or None if it is unusable."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def reconcile(conn, client, today: date, plan_name: str | None = None,
              horizon_days: int = 60) -> dict:
    """
    Re-link local sessions to remote events that already exist, by `external_id`.

    The push deletes by stored id, so a session that has lost its id cannot clean up the event
    it created — it just creates another one beside it. That is how a scheduled push turns into
    a duplicating one, and it is not hypothetical: a roll that re-inserted its rows dropped the
    ids for 13 of 15 live events in exactly this way.

    `external_id` survives on the remote side regardless, which is what makes repair possible
    without deleting anything. Run before every push: if the local link is intact it changes
    nothing, and if it is not, it restores it instead of duplicating.

    Only ever *adds* a link. A row that already has an id is left alone, so this can never
    repoint a session at some other athlete's event.
    """
    remote = {e.get("external_id"): e.get("id")
              for e in client.get_events(today, today + timedelta(days=horizon_days))
              if e.get("external_id")}
    relinked = 0
    for row in pending(conn, today, plan_name):
        if row["remote_event_id"] or row["external_id"] not in remote:
            continue
        conn.execute("UPDATE prescribed_sessions SET remote_event_id = ? WHERE id = ?",
                     (str(remote[row["external_id"]]), row["id"]))
        relinked += 1
    return {"remote_events": len(remote), "relinked": relinked}


def push(conn, client, today: date, plan_name: str | None = None,
         dry_run: bool = True) -> dict:
    """
    Replace the remote copy of the upcoming horizon.

    Delete-then-create, by ids we recorded. Not an upsert, for the reason in the module
    docstring; and not a range delete, because that removes every WORKOUT in the window
    regardless of who created it — including anything the athlete added themselves.

    `dry_run` defaults to True. This writes to a real calendar, so the caller has to ask for it.
    """
    rows = pending(conn, today, plan_name)
    payload = build_payload(rows)
    # `remote_event_id` is a TEXT column but intervals.icu event ids are integers. Coerced here
    # so the client is handed what the API expects, and a non-numeric value — which should be
    # impossible, but would otherwise raise mid-push with some events already deleted — is
    # skipped rather than crashing the run.
    stale = [i for i in (_as_event_id(r["remote_event_id"]) for r in rows) if i is not None]

    result = {"sessions": len(rows), "to_delete": len(stale), "dry_run": dry_run,
              "payload": payload, "deleted": 0, "created": 0}
    if dry_run or not payload:
        return result

    result["deleted"] = client.delete_events_by_id(stale)
    created = client.create_events(payload)
    result["created"] = len(created)

    # Match responses back to rows by external_id. Positional matching would be one reordering
    # away from stamping the wrong remote id onto a session, and the next push would then
    # delete somebody else's event.
    by_external = {e.get("external_id"): e.get("id") for e in created if e.get("external_id")}
    stamped = 0
    for row in rows:
        remote = by_external.get(row["external_id"])
        if remote is None:
            continue
        conn.execute(
            "UPDATE prescribed_sessions SET remote_event_id = ?, pushed_at = ? WHERE id = ?",
            (str(remote), utc_now_iso(), row["id"]),
        )
        stamped += 1
    result["stamped"] = stamped
    result["unmatched"] = len(created) - stamped
    return result

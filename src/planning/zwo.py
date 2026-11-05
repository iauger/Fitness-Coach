"""
Session -> Zwift `.zwo` XML.

`Session.steps` already carries exactly what the format needs: `seconds` and `pct` (a fraction of
FTP), plus an optional cadence where the cadence *is* the point of the step. Zwift resolves Power
against the FTP configured in Zwift itself, so nothing is baked in here — which is the same
reason the archetypes are expressed in percentages rather than watts.

Every step is emitted as a `<SteadyState>` — unambiguous, and correct for all of the current
archetypes. No `<IntervalsT>` shorthand, no ERG-mode flags.

**This no longer writes to the Zwift folder.** It used to, and that turned out to duplicate
every session: intervals.icu has `zwift_upload_workouts` enabled on this account, so it was
already delivering the same workouts to Zwift, and the athlete saw each one twice. The file
content is still generated — it is what `planning/export.py` attaches as `file_contents` for
intervals.icu to parse into a `workout_doc` — but delivery to the device is intervals.icu's
job, and doing it ourselves as well was work that made things worse.
"""

import re
from xml.sax.saxutils import escape

from src.planning.archetypes import Session

# A whitelist rather than a blacklist of Windows-illegal characters. Zwift uploads imported
# workouts to its cloud so they appear on other devices, and its forums report that names with
# special characters can stop that sync happening. Threshold prescriptions are full of `@`, `%`
# and `/`, so a blacklist of just the Windows-illegal set let exactly the risky ones through.
_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_RUNS = re.compile(r"_{2,}")


def safe_component(name: str) -> str:
    """One path component Zwift will sync and Windows will accept, without an extension."""
    cleaned = _ALLOWED.sub("", name.strip().replace(" ", "_"))
    return _RUNS.sub("_", cleaned).strip("_.")


def safe_filename(name: str) -> str:
    """A filename Zwift will list, sync, and Windows will accept."""
    return (safe_component(name) or "workout") + ".zwo"


def to_zwo(session: Session, name: str, description: str = "") -> str:
    """
    Render one session as a `.zwo` document.

    Steps are emitted in order with no merging: a 30/15 bout becomes alternating 30s and 15s
    blocks, which is the whole reason the archetypes were made to build real steps rather than
    describe themselves in a note string. A flattened version would be unridable.
    """
    lines = [
        '<workout_file>',
        f'    <author>{AUTHOR}</author>',
        f'    <name>{escape(name)}</name>',
        f'    <description>{escape(description)}</description>',
        '    <sportType>bike</sportType>',
        '    <tags/>',
        '    <workout>',
    ]
    for step in session.steps:
        if step.seconds <= 0:
            continue
        attrs = [f'Duration="{int(step.seconds)}"', f'Power="{round(step.pct, 3)}"']
        if step.cadence:
            attrs.append(f'Cadence="{int(step.cadence)}"')
        pace = " ".join(attrs)
        text = escape(step.label) if step.label else ""
        lines.append(f'        <SteadyState {pace}>')
        if text:
            # A textevent at the start of the block is how Zwift shows the step's intent on
            # screen; without it a 30/15 set is a wall of unexplained blocks.
            lines.append(f'            <textevent timeoffset="0" message="{text}"/>')
        lines.append('        </SteadyState>')
    lines += ['    </workout>', '</workout_file>', '']
    return "\n".join(lines)


def session_name(session: Session, prefix: str = "") -> str:
    """A short, sortable name: what it is and how long, so the Zwift list is readable."""
    kind = {"lit": "Endurance", "sit": "Sprints", "hiit": "VO2",
            "threshold": "Threshold"}.get(session.archetype, session.archetype.title())
    variant = session.variant.replace("_", " ")
    body = f"{kind} {int(session.minutes)}min"
    if session.archetype != "lit" or variant != "steady":
        body += f" ({variant})"
    return f"{prefix} {body}".strip()


# Written into every generated file. Zwift no longer reads these from disk -- intervals.icu
# delivers them -- but the tag stays because it identifies the file as ours wherever it
# ends up, and export.py sends this content as `file_contents`.
AUTHOR = "Fitness Coach"
AUTHOR_TAG = f"<author>{AUTHOR}</author>"

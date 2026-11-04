"""
Session -> Zwift `.zwo` XML.

`Session.steps` already carries exactly what the format needs: `seconds` and `pct` (a fraction of
FTP), plus an optional cadence where the cadence *is* the point of the step. Zwift resolves Power
against the FTP configured in Zwift itself, so nothing is baked in here — which is the same
reason the archetypes are expressed in percentages rather than watts.

This is the minimum useful slice of the export path, written ahead of the rest because a
prescription nobody can ride is not worth storing. It does not handle the intervals.icu workout
DSL, the `.zwo` `<IntervalsT>` shorthand, or ERG-mode flags — every step is emitted as a
`<SteadyState>`, which is unambiguous and renders correctly for all of the current archetypes.
"""

import re
from datetime import date
from pathlib import Path
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


# Written into every file we generate, and the thing that makes `supersede` safe: it is how a
# workout of ours is told apart from the 80-odd hand-made ones in the same folder.
AUTHOR = "Fitness Coach"
AUTHOR_TAG = f"<author>{AUTHOR}</author>"


def supersede(out_dir: Path, on: date, keep: set[Path]) -> list[Path]:
    """
    Delete the files we previously wrote for `on`, other than the ones just written.

    A session inside the rolling horizon can change — a tier step, a different variant as the
    rotation turns — and the new prescription lands under a different filename. Left alone the
    folder accumulates one stale workout per revision and Zwift lists every one of them, which
    is worse than listing nothing: the athlete picks from a menu where several entries claim to
    be today's ride.

    Two guards, because this deletes inside a directory the athlete keeps their own workouts in.
    The name must start with the ISO date **and** the file must carry our author tag. A date
    prefix alone is not ours to claim, and `Path.unlink` is not undoable.
    """
    removed = []
    for path in sorted(out_dir.glob(f"{on.isoformat()}_*.zwo")):
        if path in keep:
            continue
        try:
            if AUTHOR_TAG not in path.read_text(encoding="utf-8", errors="replace"):
                continue
        except OSError:
            # Unreadable is not ours to delete. Being conservative here leaves a stale file at
            # worst; being permissive removes something we cannot prove we wrote.
            continue
        path.unlink()
        removed.append(path)
    return removed


# Zwift's workout folder on Windows, newest location first.
#
# Hardcoding this has already failed once. Zwift 1.0.167761, installed 2026-11-02 23:36, moved
# its data directory from Documents to AppData\Local, migrated all 92 .zwo files across, and left
# an _INFO_README.txt in the old folder saying so. A workout written to the old path afterwards
# was never seen, while the pre-migration copy went on being listed — which looked exactly like
# Zwift refusing to pick up an edit, and was nothing of the kind.
#
# Both folders still exist and both still hold the athlete's hand-made workouts, so "the
# directory is there" is no evidence it is the live one. Order is what decides it.
ZWIFT_ROOTS = (
    ("AppData", "Local", "Zwift"),
    # Pre-1.0.167761. A fallback for an older install. The OneDrive mirror of this path is stale
    # from 2021 and is not a candidate at all.
    ("Documents", "Zwift"),
)


def zwift_workouts_dir(athlete_id: str, home: Path | None = None,
                       roots: tuple = ZWIFT_ROOTS) -> Path:
    """
    The workouts folder Zwift actually reads.

    Returns the first candidate that exists, or the newest candidate when none does — so a fresh
    install gets the current location rather than a path Zwift has stopped reading.
    """
    base = home or Path.home()
    candidates = [base.joinpath(*root, "Workouts", athlete_id) for root in roots]
    return next((c for c in candidates if c.is_dir()), candidates[0])


# ── where a workout file lives, and what Zwift calls it ───────────────────────

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def plan_prefix(plan_name: str) -> str:
    """The leading component every folder this generator creates shares."""
    return safe_component(plan_name)


def week_dir(root: Path, plan_name: str, plan_week: int, monday: date) -> Path:
    """
    One folder per plan week, directly under the athlete-id folder.

    **Zwift flattens nesting.** Verified in the app on 2026-11-03: a
    `Sustainable_rolling/W1_Nov_02/` tree showed a single `Sustainable_rolling` tile whose
    contents were the three workouts, with the week folder gone. Community guides claiming
    nested folders appear in the picker are wrong, at least for this build. So the week has to
    live in the one folder name we get:

        Sustainable_rolling_W1_Nov_02/

    Underscores rather than spaces because every workout Zwift itself writes to this folder uses
    them, which is the only evidence available about what it will sync.
    """
    return root / safe_component(
        f"{plan_name} W{plan_week} {monday.strftime('%b')} {monday.day:02d}")


def display_name(session: Session, weekday: int) -> str:
    """
    What Zwift shows in the picker, ordered and labelled by day.

    **The picker sorts alphabetically, not chronologically.** Verified in the app: three
    workouts named "Tue -", "Thu -", "Sat -" listed as Sat, Thu, Tue, and the athlete's 91
    hand-made workouts list as 10x..., 15x..., 1x10..., which is a plain string sort. A bare day
    name therefore never orders correctly, so the weekday index leads.

    The index rather than the date, because a week can straddle a month end — Mon Oct 30 would
    sort after Sat Nov 04 on "30" vs "04", putting the long ride before the week it belongs to.

    ASCII only: the separator is a plain hyphen rather than a middot because filenames with
    special characters are reported to break Zwift's cloud sync, and that is not worth risking
    for punctuation.
    """
    day = weekday % 7
    return session_name(session, prefix=f"{day + 1} {DAY_NAMES[day]} -")


def prune_week_dirs(root: Path, plan_name: str, keep: set[Path]) -> list[Path]:
    """
    Remove week folders that have rolled out of the horizon.

    Without this the picker accumulates a folder per week forever, which defeats the point of
    organising it at all. The .zwo is only a rideable artifact — `prescribed_sessions` holds the
    record of what was asked — so a past week's folder is safe to drop.

    Now that the week folders sit at the top level beside the athlete's own files, the match is
    on this plan's name prefix *and* on every file inside carrying our author tag. A folder
    holding anything else is left in place, empty directories included, because an empty
    directory is not evidence we created it.
    """
    if not root.is_dir():
        return []
    prefix = plan_prefix(plan_name)
    removed = []
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir() or candidate in keep:
            continue
        if not candidate.name.startswith(prefix):
            continue
        files = list(candidate.glob("*"))
        if not files or not all(_is_ours(f) for f in files):
            continue
        for f in files:
            f.unlink()
        candidate.rmdir()
        removed.append(candidate)
    return removed


def _is_ours(path: Path) -> bool:
    if path.is_dir() or path.suffix.lower() != ".zwo":
        return False
    try:
        return AUTHOR_TAG in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

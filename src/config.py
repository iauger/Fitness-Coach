"""
The athlete's configuration, loaded once and validated at the door.

A leaf module: stdlib and PyYAML only, no imports from this project. That is not tidiness, it
is a requirement — `planning/constants.py` already had to become a leaf because
`planning/blocks.py` imports `analysis/derive.py`, so anything `analysis` needs cannot live
downstream of `planning`. Config is needed by both.

**Why dataclasses rather than passing the parsed dict around.** A typo in a YAML file is
inevitable, and a dict lets it travel: `config["plan"]["horzion_weeks"]` raises a `KeyError`
four modules away from the mistake, at whatever time of day that code path first runs. Parsing
into frozen dataclasses turns every typo into one error, at load, naming the key and the
section. That is the entire point of this step, and the reason the loader is strict about
unknown keys rather than ignoring them — a key the loader ignores is a setting the athlete
believes they changed.

**Loading is lazy and memoized, not done at import.** The effect is load-once, which is what
was wanted; the mechanism matters because an import-time load makes the module impossible to
test against anything but the real file, and makes every `import src.config` pay for file I/O
whether or not it reads a value.
"""

import dataclasses
import os
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from pathlib import Path
from typing import Any, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

REPO_ROOT = Path(__file__).parent.parent
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_PATH = Path(os.environ.get("FITNESS_COACH_CONFIG", CONFIG_DIR / "athlete.yaml"))
EXAMPLE_PATH = CONFIG_DIR / "athlete.example.yaml"


class ConfigError(Exception):
    """A configuration file that cannot be used, with a message saying why."""


# ── sections ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Athlete:
    name: str
    timezone: str = "UTC"
    history_start: int | None = None
    primary_sport: str = "cycling"
    # Free text, both of them, and both read by the coach rather than by any logic. `notes`
    # carries life load — small children, shift work, a long commute — and is the one field
    # here that materially changes what the model says, because it is what lets a missed week
    # be read as life rather than as a failure.
    equipment: str = ""
    notes: str = ""
    tools: tuple[str, ...] = ()

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Profile:
    """
    Baseline body and power numbers, used *only* to seed an empty `athlete_profile` table.

    Deliberately not the source of truth: `src/athlete/profile.py` holds dated entries, so FTP
    has a history rather than a value, and `scripts/seed_profile.py` will not write one of these
    over a metric that already has a record. Editing a number here after the first run does
    nothing, which is correct — a new FTP is an event on a date, not a changed setting.
    """
    height_in: float | None = None
    weight_lbs: float | None = None
    ftp: float | None = None


@dataclass(frozen=True)
class Goal:
    description: str
    date: date | None = None
    priority: str = "secondary"


@dataclass(frozen=True)
class Plan:
    name: str
    start_monday: date
    # The plan's identity. Generated from the name on first creation and then **never
    # recomputed** — deriving it at runtime is what made a rename insert a second plan with
    # the stored rows repointed at it, and it is what every external_id on the intervals.icu
    # calendar was built from. Left unset, `plans.resolve` takes the slug of the plan already
    # in the database and only falls back to deriving one when there is no plan at all.
    slug: str | None = None
    sustainable_band_hours: tuple[float, float] = (3.5, 4.0)
    long_ride_ceiling_min: int = 120
    # An availability ceiling, not a target. The ladder's ride-day count is read from what you
    # actually ride; this only stops it climbing past the days you have. Seven is allowed and
    # is a bad idea for most people, which is a judgement for the athlete rather than the code.
    max_ride_days: int = 7
    strength_days: int = 2
    horizon_weeks: int = 3
    goal_date: date | None = None


@dataclass(frozen=True)
class IntervalsIcu:
    push_calendar: bool = True


@dataclass(frozen=True)
class Integrations:
    intervals_icu: IntervalsIcu = IntervalsIcu()


@dataclass(frozen=True)
class Server:
    host: str = "127.0.0.1"
    port: int = 8800


@dataclass(frozen=True)
class Coach:
    enabled: bool = True
    model: str = "claude-haiku-4-5"


@dataclass(frozen=True)
class Config:
    athlete: Athlete
    plan: Plan
    goals: tuple[Goal, ...] = ()
    profile: Profile = Profile()
    integrations: Integrations = Integrations()
    server: Server = Server()
    coach: Coach = Coach()


# ── parsing ───────────────────────────────────────────────────────────────────

def _coerce(value: Any, declared: Any, where: str) -> Any:
    """Turn one YAML value into the declared type, or say precisely what is wrong."""
    origin = get_origin(declared)

    # `X | None` — accept None, otherwise coerce to X.
    if origin is not None and type(None) in get_args(declared):
        if value is None:
            return None
        inner = next(a for a in get_args(declared) if a is not type(None))
        return _coerce(value, inner, where)

    if is_dataclass(declared):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a block of settings, got {type(value).__name__}")
        return _build(declared, value, where)

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
        args = get_args(declared)
        if len(args) == 2 and args[1] is Ellipsis:        # tuple[X, ...]
            return tuple(_coerce(v, args[0], f"{where}[{i}]") for i, v in enumerate(value))
        if len(value) != len(args):
            raise ConfigError(f"{where}: expected {len(args)} values, got {len(value)}")
        return tuple(_coerce(v, a, f"{where}[{i}]") for i, (v, a) in enumerate(zip(value, args)))

    if declared is date:
        if isinstance(value, date):
            return value                                   # YAML parses unquoted dates natively
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ConfigError(f"{where}: {value!r} is not a date (expected YYYY-MM-DD)") from None

    if declared is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false, got {value!r}")
        return value

    if declared in (int, float, str):
        # bool is an int subclass; accepting it here would silently turn `true` into 1.
        if declared is not str and isinstance(value, bool):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        try:
            return declared(value)
        except (TypeError, ValueError):
            raise ConfigError(
                f"{where}: expected {declared.__name__}, got {value!r}") from None

    return value


def _build(cls: type, data: dict, where: str = "") -> Any:
    """Build one dataclass from a dict, rejecting anything it does not declare."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        # Named, and with the alternatives listed: the commonest config mistake is a plausible
        # misspelling, and "unknown key" alone leaves the athlete to guess which.
        raise ConfigError(
            f"{where or cls.__name__.lower()}: unknown setting(s) "
            f"{', '.join(sorted(unknown))}. Known: {', '.join(sorted(known))}")

    kwargs = {}
    for name, f in known.items():
        prefix = f"{where}.{name}" if where else name
        if name in data:
            kwargs[name] = _coerce(data[name], f.type, prefix)
        elif _has_default(f):
            continue                                        # the dataclass supplies it
        else:
            raise ConfigError(f"{prefix}: required setting is missing")
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{where or cls.__name__.lower()}: {exc}") from None


def _has_default(f) -> bool:
    return (f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING)


# ── validation beyond types ───────────────────────────────────────────────────

def _validate(cfg: Config) -> None:
    """
    The checks a type cannot make.

    Each one exists because getting it wrong produces a plausible but incorrect plan rather
    than an error, which is the failure mode this whole project keeps running into.
    """
    try:
        ZoneInfo(cfg.athlete.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"athlete.timezone: {cfg.athlete.timezone!r} is not a known IANA timezone "
            f"(e.g. America/New_York, Europe/London, UTC)") from None

    if cfg.plan.start_monday.weekday() != 0:
        raise ConfigError(
            f"plan.start_monday: {cfg.plan.start_monday} is a "
            f"{cfg.plan.start_monday.strftime('%A')}, not a Monday. Every week boundary in "
            f"this project is Monday-to-Sunday.")

    low, high = cfg.plan.sustainable_band_hours
    if low <= 0 or high <= low:
        raise ConfigError(
            f"plan.sustainable_band_hours: expected [low, high] with 0 < low < high, "
            f"got [{low}, {high}]")

    if cfg.plan.slug is not None:
        # The slug travels into `external_id`s on the remote calendar, so it has to survive a
        # URL and a filename. Rejecting a bad one here beats discovering it as events that do
        # not match the ones already pushed.
        slug = cfg.plan.slug
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
        if not slug or slug.strip("-") != slug or set(slug) - allowed:
            raise ConfigError(
                f"plan.slug: {slug!r} must be lowercase letters, digits and hyphens, not "
                f"starting or ending with a hyphen. Normally you leave this unset and it is "
                f"generated from plan.name once.")

    for name in ("height_in", "weight_lbs", "ftp"):
        value = getattr(cfg.profile, name)
        if value is not None and value <= 0:
            raise ConfigError(f"profile.{name}: {value} is not a usable measurement")

    if not 1 <= cfg.plan.max_ride_days <= 7:
        raise ConfigError(
            f"plan.max_ride_days: {cfg.plan.max_ride_days} is not a number of days in a week")

    if not 0 <= cfg.plan.strength_days <= 5:
        raise ConfigError(
            f"plan.strength_days: {cfg.plan.strength_days} leaves no room for riding. The "
            f"long ride and the hard session are never given over to lifting, so at most 5 "
            f"days can carry one.")

    if cfg.plan.horizon_weeks < 1:
        raise ConfigError(
            f"plan.horizon_weeks: {cfg.plan.horizon_weeks} leaves no dated sessions at all")

    if cfg.plan.long_ride_ceiling_min < 30:
        raise ConfigError(
            f"plan.long_ride_ceiling_min: {cfg.plan.long_ride_ceiling_min} is shorter than "
            f"any session this plan prescribes")

    if not 1 <= cfg.server.port <= 65535:
        raise ConfigError(f"server.port: {cfg.server.port} is not a usable port")

    allowed = {"primary", "secondary", "opt_in"}
    for i, goal in enumerate(cfg.goals):
        if goal.priority not in allowed:
            raise ConfigError(
                f"goals[{i}].priority: {goal.priority!r} is not one of "
                f"{', '.join(sorted(allowed))}")


# ── loading ───────────────────────────────────────────────────────────────────

def parse(text: str) -> Config:
    """Parse and validate YAML text. The path-free core, so tests need no files."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse the configuration file: {exc}") from None
    if data is None:
        raise ConfigError("the configuration file is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"the configuration file should be a block of settings, got {type(data).__name__}")

    cfg = _build(Config, data)
    _validate(cfg)
    return cfg


def load(path: Path | None = None, *, create_if_missing: bool = True) -> Config:
    """
    Read the configuration, writing a starter copy from the example if there is none.

    Generating rather than refusing is deliberate: refusing is clearer to someone who already
    knows this project, and a wall to someone who has just cloned it. The generated file is the
    committed example, which is a working configuration, so the first run gets somewhere.
    """
    path = Path(path) if path else CONFIG_PATH
    if not path.exists():
        if not create_if_missing:
            raise ConfigError(f"no configuration file at {path}")
        if not EXAMPLE_PATH.exists():
            raise ConfigError(
                f"no configuration at {path} and no example at {EXAMPLE_PATH} to copy")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  created {path} from the example — edit it before relying on the plan")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
    try:
        return parse(text)
    except ConfigError as exc:
        # The path is what makes the message actionable when several configs are in play.
        raise ConfigError(f"{path}: {exc}") from None


_cached: Config | None = None


def config() -> Config:
    """The loaded configuration. Read once per process; call `reset()` to reload."""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reset() -> None:
    """Drop the memoized configuration. For tests, and for a future reload endpoint."""
    global _cached
    _cached = None

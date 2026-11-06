"""
The rules a configuration file may not break.

Archetype variants become overridable in this step, which is the first time someone editing a
text file can change what a session actually asks them to ride. Most of that is theirs to
decide — rep counts, bout lengths, how many minutes of threshold work a session accumulates.
A few things are not, and this module is the list.

**Why a separate leaf module.** `config.py` validates at load, because a typo that travels
four modules before raising is the failure this project keeps running into. But `config.py` is
a leaf by requirement — `analysis` and `planning` both need it — so it cannot import
`archetypes` to learn what a safe variant looks like. These rules therefore live somewhere
both can reach: stdlib only, no imports from this project at all, not even `config`.

**Each rule is here because breaking it produces a plausible session that is wrong**, rather
than an error. That is the test for belonging in this file. A rep count of 400 is obviously
silly and the athlete will notice; a threshold target of 1.02 looks almost identical to 0.98
and quietly converts the session into something with a different metabolic response and a
different fatigue cost.

**What is deliberately not here.** At most two hard sessions a week, and strength never being
trimmed, are both enforced in `blocks.py` — by the structure of the weekly pattern and by
`pattern_for` only ever reducing. A config cannot reach either, because the pattern is
generated rather than configured. They are named in the module docstring there, not duplicated
as checks here, because a rule enforced in two places drifts in one of them.

Every built-in variant is validated against these rules in the test suite. A rule our own
library breaks is a wrong rule, not a library to fix.
"""

# ── threshold ────────────────────────────────────────────────────────────────

# The one rule the source is emphatic about: "better to hedge low, say by 10%, than to
# overshoot the target by even 2%", because above and below the transition "a completely
# different metabolic milieu and different fatigue profile develops". A variant that crosses it
# is no longer a threshold session, whatever it is called.
THRESHOLD_MAX_PCT = 1.00

# The source's stated target for a threshold session: 40+ minutes of accumulated work, in bouts
# of 10 minutes or more. Easy to break by trimming reps to shorten a session, which keeps the
# name and loses the point.
THRESHOLD_MIN_WORK_MIN = 40
THRESHOLD_MIN_BOUT_MIN = 10

# ~10:1 work to rest. The source's own diagnostic is that needing rest longer or more often
# means the intensity is too high, so a long rest is a symptom to fix rather than a comfort to
# grant. Eight is the floor rather than ten because the built-in variants sit at 8-10.
THRESHOLD_MIN_WORK_REST_RATIO = 8

# ── HIIT ─────────────────────────────────────────────────────────────────────

# "Bouts longer than 4 minutes" — time in the severe domain is what drives the adaptation, so
# the way to make a session harder is a longer bout, not a higher target.
HIIT_MIN_BOUT_MIN = 4

# Sub-maximal, with "half a rep left in the tank". Above this a bout stops being repeatable for
# the whole session, which is the one thing the source asks of HIIT.
HIIT_MAX_PCT = 1.35

# Rest no longer than the work. 2:1 is what the source asks; the floor here is looser because
# `thirty_fifteen` legitimately rests more between its bouts, being denser inside them.
HIIT_MAX_REST_RATIO = 1.0

# ── SIT ──────────────────────────────────────────────────────────────────────

# "Efforts of 10 to 40 seconds", all-out. Longer and it is not a sprint; shorter and there is
# no meaningful work done.
SIT_MIN_WORK_S = 10
SIT_MAX_WORK_S = 40

# ── LIT ──────────────────────────────────────────────────────────────────────

# The failure mode for endurance work is drifting up into tempo, which converts the session's
# purpose from duration-dependent to intensity-dependent stimulus — and it is the session that
# carries most of the week's volume, so making it cost more is how a sustainable week stops
# being sustainable. The ceiling is well above the 62-66% the library uses: this is a guard
# against turning the easy ride into a hard one, not a narrow band.
LIT_MAX_PCT = 0.80


def _violations_threshold(cfg: dict) -> list[str]:
    out = []
    pct = cfg.get("pct")
    if pct is not None and pct > THRESHOLD_MAX_PCT:
        out.append(
            f"pct {pct} is above FTP. Threshold work must stay at or below "
            f"{THRESHOLD_MAX_PCT:.2f}: either side of that transition the body responds "
            f"differently, and the source this plan follows is emphatic that hedging 10% low "
            f"beats overshooting by 2%.")

    micro = cfg.get("micro") or {}
    for key in ("a_pct", "b_pct"):
        value = micro.get(key)
        if value is not None and value > THRESHOLD_MAX_PCT:
            out.append(
                f"micro.{key} {value} is above FTP. An over-under may roll either side of its "
                f"own mean but never above {THRESHOLD_MAX_PCT:.2f}.")

    work, reps = cfg.get("work_min"), cfg.get("reps")
    if work is not None and work < THRESHOLD_MIN_BOUT_MIN:
        out.append(
            f"work_min {work} is shorter than {THRESHOLD_MIN_BOUT_MIN}min. Threshold bouts are "
            f"10 minutes or more; shorter ones are a different session wearing the name.")
    if work is not None and reps is not None and work * reps < THRESHOLD_MIN_WORK_MIN:
        out.append(
            f"{reps} x {work}min accumulates {work * reps}min of work, under the "
            f"{THRESHOLD_MIN_WORK_MIN}min a threshold session is for.")

    rest = cfg.get("rest_min")
    if work is not None and rest:
        ratio = work / rest
        if ratio < THRESHOLD_MIN_WORK_REST_RATIO:
            out.append(
                f"work:rest is {ratio:.1f}:1, under {THRESHOLD_MIN_WORK_REST_RATIO}:1. Needing "
                f"a longer rest means the target is too high — lower pct rather than "
                f"lengthening the recovery.")
    return out


def _violations_hiit(cfg: dict) -> list[str]:
    out = []
    pct = cfg.get("pct")
    if pct is not None and pct > HIIT_MAX_PCT:
        out.append(
            f"pct {pct} is above {HIIT_MAX_PCT:.2f}. HIIT here is sub-maximal — the aim is the "
            f"highest load sustainable for the whole session, half a rep in hand.")

    work = cfg.get("work_min")
    if work is not None and work < HIIT_MIN_BOUT_MIN:
        out.append(
            f"work_min {work} is under {HIIT_MIN_BOUT_MIN}min. Time in the severe domain is "
            f"what drives the adaptation, so a bout gets longer rather than harder.")

    ratio = cfg.get("rest_ratio")
    if ratio is not None and ratio > HIIT_MAX_REST_RATIO:
        out.append(
            f"rest_ratio {ratio} rests longer than the bout itself; the source asks for 2:1.")
    return out


def _violations_sit(cfg: dict) -> list[str]:
    work = cfg.get("work_s")
    if work is None:
        return []
    if not SIT_MIN_WORK_S <= work <= SIT_MAX_WORK_S:
        return [f"work_s {work} is outside {SIT_MIN_WORK_S}-{SIT_MAX_WORK_S}s. Longer is not a "
                f"sprint and shorter does no meaningful work."]
    return []


def _violations_lit(cfg: dict) -> list[str]:
    pct = cfg.get("pct")
    if pct is not None and pct > LIT_MAX_PCT:
        return [f"pct {pct} is above {LIT_MAX_PCT:.2f}. The easy ride carries most of the "
                f"week's volume; raising it is how a sustainable week stops being one."]
    ramp = cfg.get("ramp")
    if ramp and max(ramp) > LIT_MAX_PCT:
        return [f"ramp finishes at {max(ramp)}, above {LIT_MAX_PCT:.2f}."]
    return []


def _violations_strength(cfg: dict) -> list[str]:
    if not (cfg.get("focus") or "").strip():
        return ["focus is empty; it is the only thing that reaches the athlete."]
    return []


_CHECKS = {
    "threshold": _violations_threshold,
    "hiit": _violations_hiit,
    "sit": _violations_sit,
    "lit": _violations_lit,
    "strength": _violations_strength,
}

ARCHETYPES = tuple(sorted(_CHECKS))


def violations(archetype: str, variant: str, cfg: dict) -> list[str]:
    """
    Every safety rule this variant breaks, each as a sentence naming the rule and why.

    Plural on purpose. Returning the first problem makes fixing a config a sequence of
    round trips, and the rules interact — lowering `pct` to get under FTP can leave the
    work:rest ratio still wrong, and finding that out one run later is the worst version of
    this.
    """
    check = _CHECKS.get(archetype)
    if check is None:
        return [f"unknown archetype {archetype!r}; expected one of {', '.join(ARCHETYPES)}"]
    return [f"archetypes.{archetype}.{variant}: {v}" for v in check(cfg)]


def check_all(overrides: dict, defaults: dict) -> list[str]:
    """
    Validate a whole `archetypes:` block, against each variant *merged over its default*.

    Merged, not checked in isolation, and that is the part worth stating: an override naming
    only `pct` would otherwise be judged without the `work_min` and `rest_min` it will
    actually be ridden with, and a lowered target that leaves the rest ratio broken would
    pass. The merge here has to be the same one `archetypes` applies at use, or this validates
    something nobody rides.
    """
    out: list[str] = []
    for archetype, variants in (overrides or {}).items():
        if archetype not in _CHECKS:
            out.append(f"archetypes.{archetype}: unknown archetype; expected one of "
                       f"{', '.join(ARCHETYPES)}")
            continue
        if not isinstance(variants, dict):
            out.append(f"archetypes.{archetype}: expected a block of named variants")
            continue
        for variant, cfg in variants.items():
            if not isinstance(cfg, dict):
                out.append(f"archetypes.{archetype}.{variant}: expected a block of settings")
                continue
            base = dict((defaults.get(archetype) or {}).get(variant) or {})
            base.update(cfg)
            out += violations(archetype, variant, base)
    return out

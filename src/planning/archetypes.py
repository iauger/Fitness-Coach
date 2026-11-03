"""
Workout archetypes — the parameterised templates a plan is built from.

Five types, after Jem Arnold's Sustainable Training template: low-intensity (LIT), sprint
intervals (SIT), high-intensity intervals (HIIT), threshold, and strength. Each is a small
number of parameters, and the variants below are what TrainerRoad's library actually is once
the names come off — "Spickard +3/+4/+5" is one workout at three doses.

Everything is expressed as a percentage of FTP, never as watts. Watts are resolved at render
time from the tracked FTP for that date, so a ramp test re-renders the whole plan instead of
invalidating it. (FTP was settled at 260 effective 2026-10-01 from a maximal ramp; it had been
uncertain across 231/238/260 for the whole of the preceding block, which is why the indirection
exists and is worth keeping.)

## What comes from the source, and what is ours

The source article is prose, not a spec. Read it and you find **six** numeric prescriptions in
the entire piece. Everything else below is our invention, and the two must not be confused —
an earlier version of this module read as if derived throughout, which made our own guesses
look like they carried the source's authority.

**From the source, do not quietly "improve":**

- **At most two hard sessions a week** — "zero to two structured high-intensity sessions each
  week at most". Enforced in `blocks.py` (`MAX_HARD_PER_WEEK`), not here.
- **Threshold hedges low.** "Better to hedge low, say by 10%, than to overshoot the target by
  even 2%", because above and below the transition "a completely different metabolic milieu and
  different fatigue profile develops". Hence targets at 0.86–0.92 and **nothing above 1.00**.
- **Threshold is ~10:1 work-to-rest**, in bouts of 10+ minutes, accumulating 40+ minutes of
  work. The source's own diagnostic: needing rest longer or more often means the intensity is
  too high. So long rests are a symptom to fix, not a comfort to grant.
- **HIIT bouts longer than 4 minutes, at 2:1 work-to-rest**, and sub-maximal — the aim is the
  highest load sustainable for the *whole* session, "half a rep left in the tank".
- **SIT efforts of 10–40 seconds**, all-out, used to "kick-start the first couple weeks of a
  plan" and explicitly not repeatable indefinitely.
- **A sprint every 20–30 minutes** during a long endurance ride.

**Ours, not the source's** — every threshold and LIT percentage, every SIT rep count and
recovery duration, the warm-up durations, all the variant names, and the specific bout/rep
arithmetic in each `*_VARIANTS` entry. The source gives no percentages of FTP anywhere.
"""

from dataclasses import dataclass, field


def _num(v: float) -> str:
    """2.0 -> '2', 1.5 -> '1.5'. Keeps rest durations readable now that some are fractional."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _dur(seconds: int) -> str:
    return f"{seconds}s" if seconds < 60 else f"{_num(seconds / 60)}min"


def _work_phrase(p: dict) -> str:
    """
    How a work bout is actually ridden.

    `summary()` printed `@ {pct}%` for every variant, which lied about any whose shape is
    governed by something other than a flat target: `over_unders` printed "@ 95%" while
    building 2min/1min alternations, and `thirty_fifteen` printed "@ 112%" for a bout that is
    30s on / 15s off. The prescription string is both what the athlete reads and what gets
    matched to a library workout, so it has to describe what the steps do.
    """
    if "micro" in p:
        m = p["micro"]
        return (f"alternating {_dur(m['a_s'])} @ {int(m['a_pct'] * 100)}% / "
                f"{_dur(m['b_s'])} @ {int(m['b_pct'] * 100)}%")
    if "lead" in p:
        lead = p["lead"]
        return (f"@ {int(p['pct'] * 100)}% after a {_dur(lead['lead_s'])} opener at "
                f"{int(lead['lead_pct'] * 100)}%")
    return f"@ {int(p['pct'] * 100)}%"


def _lit_phrase(p: dict) -> str:
    """Same problem on the endurance variants: a ramp, a cadence drill and a sprint set all
    printed as a single flat percentage."""
    if "ramp" in p:
        low, high = p["ramp"]
        return f"ramping {int(low * 100)}% -> {int(high * 100)}% FTP"
    base = f"@ {int(p['pct'] * 100)}% FTP"
    if "cadences" in p:
        blocks = _dur(p["cadence_block_s"])
        return f"{base}, {blocks} blocks alternating {'/'.join(str(c) for c in p['cadences'])}rpm"
    if "sprint_every_s" in p:
        return (f"{base} + {_dur(p['sprint_s'])} sprints every "
                f"{_dur(p['sprint_every_s'])}")
    return base


@dataclass
class Step:
    """One block of time at one intensity. `pct` is a fraction of FTP."""
    seconds: int
    pct: float
    label: str = ""
    # Optional target cadence. Only set where the cadence *is* the point of the step
    # (cadence drills); otherwise left None so a serializer omits it rather than
    # inventing a number.
    cadence: int | None = None

    @property
    def tss(self) -> float:
        # Standard relationship: TSS = duration_s * IF^2 / 3600 * 100.
        return self.seconds * (self.pct ** 2) / 3600 * 100


@dataclass
class Session:
    archetype: str
    variant: str
    steps: list[Step] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def seconds(self) -> int:
        return sum(s.seconds for s in self.steps)

    @property
    def minutes(self) -> float:
        return round(self.seconds / 60, 0)

    @property
    def tss(self) -> float:
        return round(sum(s.tss for s in self.steps), 0)

    @property
    def intensity_factor(self) -> float:
        """Whole-session IF, back-derived from accumulated TSS."""
        if not self.seconds:
            return 0.0
        return round(((self.tss * 3600) / (self.seconds * 100)) ** 0.5, 2)

    @property
    def name(self) -> str:
        return f"{self.archetype.title()} — {self.variant.replace('_', ' ')}"

    def summary(self) -> str:
        """The one-line prescription, which is what gets matched to a library workout."""
        p = self.params
        core = {
            "lit": lambda: f"{int(self.minutes)}min {_lit_phrase(p)}",
            "sit": lambda: f"{p['reps']}x {p['work_s']}s all-out, {p['rest_min']}min recovery",
            "hiit": lambda: (f"{p['reps']}x {p['work_min']}min {_work_phrase(p)}, "
                             f"{_num(p['rest_min'])}min recovery"),
            "threshold": lambda: (f"{p['reps']}x {p['work_min']}min {_work_phrase(p)}, "
                                  f"{_num(p['rest_min'])}min recovery"),
            "strength": lambda: f"{int(self.minutes)}min — {p.get('focus', 'full body')}",
        }[self.archetype]()
        return f"{core}  ({int(self.minutes)}min, {self.tss:.0f} TSS, IF {self.intensity_factor})"


# ── warm-up / cool-down ───────────────────────────────────────────────────────

def _warmup(minutes: int = 12) -> list[Step]:
    """
    Progressive warm-up, longer the harder the session.

    Arnold treats this as an instrument, not a formality: a standardised protocol you learn the
    feel of, so "about normal / worse / better" becomes a usable daily readiness reading. Keep
    it fixed for that reason — varying it destroys the comparison.
    """
    third = int(minutes * 60 / 3)
    return [
        Step(third, 0.45, "easy spin, stay under the pedals"),
        Step(third, 0.60, "build"),
        Step(third, 0.70, "openers"),
    ]


def _cooldown(minutes: int = 8) -> list[Step]:
    return [Step(minutes * 60, 0.45, "cool-down")]


# ── work-bout shapes ──────────────────────────────────────────────────────────
#
# Several variants used to describe their structure in a `note` string while emitting a single
# flat block — `thirty_fifteen` produced one 9-minute effort at 112% FTP, which is unridable,
# and its stored TSS described a session nobody was ever prescribed. These build the real thing.

def _alternating(total_s: int, a_s: int, a_pct: float, b_s: int, b_pct: float,
                 label: str) -> list[Step]:
    """Repeat a/b until the bout is used up, truncating the final segment rather than overrunning."""
    steps: list[Step] = []
    elapsed = 0
    i = 0
    while elapsed < total_s:
        seg_s, pct = (a_s, a_pct) if i % 2 == 0 else (b_s, b_pct)
        seg_s = min(seg_s, total_s - elapsed)
        if seg_s <= 0:
            break
        steps.append(Step(seg_s, pct, label))
        elapsed += seg_s
        i += 1
    return steps


def _hard_start(total_s: int, lead_s: int, lead_pct: float, pct: float,
                label: str) -> list[Step]:
    """A short supra-target opener, then settle. Deliberately not a separate interval."""
    lead_s = min(lead_s, total_s)
    out = [Step(lead_s, lead_pct, f"{label} — hard start")]
    if total_s - lead_s > 0:
        out.append(Step(total_s - lead_s, pct, label))
    return out


def _ramp(total_s: int, low: float, high: float, segments: int = 4) -> list[Step]:
    """Stepped approximation of a continuous ramp."""
    seg = total_s // segments
    steps = []
    for i in range(segments):
        pct = round(low + (high - low) * i / max(1, segments - 1), 3)
        dur = seg if i < segments - 1 else total_s - seg * (segments - 1)
        steps.append(Step(dur, pct, f"ramp {i + 1}/{segments}"))
    return steps


def _bout_steps(total_s: int, cfg: dict, label: str) -> list[Step]:
    """One work bout, expanded according to whatever shape the variant declares."""
    if "micro" in cfg:
        m = cfg["micro"]
        return _alternating(total_s, m["a_s"], m["a_pct"], m["b_s"], m["b_pct"], label)
    if "lead" in cfg:
        return _hard_start(total_s, cfg["lead"]["lead_s"], cfg["lead"]["lead_pct"],
                           cfg["pct"], label)
    return [Step(total_s, cfg["pct"], label)]


# ── archetypes ────────────────────────────────────────────────────────────────

LIT_VARIANTS = {
    "steady":            {"pct": 0.62},
    "cadence_drills":    {"pct": 0.62, "note": "alternate 5min @ 95rpm / 5min @ 75rpm",
                          "cadence_block_s": 300, "cadences": (95, 75)},
    "endurance_sprints": {"pct": 0.62, "note": "one 15s all-out sprint every 25min",
                          "sprint_every_s": 1500, "sprint_s": 15, "sprint_pct": 1.80},
    "progressive":       {"pct": 0.66, "note": "start 55%, finish 72%",
                          "ramp": (0.55, 0.72)},
}


def lit(minutes: int, variant: str = "steady") -> Session:
    """
    Low-intensity continuous. The foundation, not filler.

    No warm-up block: the ride is its own warm-up. Kept at 60-66% of FTP because the
    failure mode for endurance work is drifting up into tempo, which converts the session's
    purpose from duration-dependent to intensity-dependent stimulus.
    """
    cfg = LIT_VARIANTS[variant]
    total = minutes * 60
    if "ramp" in cfg:
        steps = _ramp(total, *cfg["ramp"])
    elif "cadences" in cfg:
        # Same power throughout; the cadence is the stimulus, so it is carried on the Step
        # rather than left in prose a serializer would drop.
        steps = []
        elapsed, i = 0, 0
        while elapsed < total:
            seg = min(cfg["cadence_block_s"], total - elapsed)
            steps.append(Step(seg, cfg["pct"], "cadence block",
                              cadence=cfg["cadences"][i % len(cfg["cadences"])]))
            elapsed += seg
            i += 1
    elif "sprint_every_s" in cfg:
        steps = []
        elapsed = 0
        while elapsed < total:
            seg = min(cfg["sprint_every_s"], total - elapsed)
            steps.append(Step(seg, cfg["pct"], "steady endurance"))
            elapsed += seg
            if total - elapsed > cfg["sprint_s"] * 4:
                steps.append(Step(cfg["sprint_s"], cfg["sprint_pct"], "all-out sprint"))
                elapsed += cfg["sprint_s"]
    else:
        steps = [Step(total, cfg["pct"], "steady endurance")]
    return Session("lit", variant, steps=steps,
                   params={"minutes": minutes, "pct": cfg["pct"], **cfg})


SIT_VARIANTS = {
    "short":  {"work_s": 10, "reps": 8, "rest_min": 3},
    "medium": {"work_s": 20, "reps": 6, "rest_min": 4},
    "long":   {"work_s": 30, "reps": 4, "rest_min": 5},
    "maximal": {"work_s": 40, "reps": 3, "rest_min": 6},
}

# All-out efforts have no meaningful percentage target — this is a nominal figure used only so
# the session carries a sensible TSS, not a number to ride to.
SIT_NOMINAL_PCT = 1.80


def sit(variant: str = "medium", reps: int | None = None) -> Session:
    cfg = dict(SIT_VARIANTS[variant])
    if reps:
        cfg["reps"] = reps
    steps = _warmup(15)
    for i in range(cfg["reps"]):
        steps.append(Step(cfg["work_s"], SIT_NOMINAL_PCT, f"all-out {i+1}"))
        # Guard that hiit() and threshold() both had and this did not: without it every SIT
        # session carried a full recovery interval immediately before the cool-down, inflating
        # the stored duration and TSS by 3-6 minutes.
        if i < cfg["reps"] - 1:
            steps.append(Step(cfg["rest_min"] * 60, 0.45, "full recovery"))
    steps += _cooldown(8)
    return Session("sit", variant, steps=steps, params=cfg)


HIIT_VARIANTS = {
    # Arnold prefers bouts over 4 minutes: time in the severe domain is what drives the
    # adaptation, so extend duration rather than raising intensity.
    "straight_4":    {"work_min": 4, "reps": 5, "pct": 1.10, "rest_ratio": 0.5},
    "straight_5":    {"work_min": 5, "reps": 4, "pct": 1.08, "rest_ratio": 0.5},
    "straight_6":    {"work_min": 6, "reps": 4, "pct": 1.06, "rest_ratio": 0.5},
    "straight_8":    {"work_min": 8, "reps": 3, "pct": 1.04, "rest_ratio": 0.5},
    "thirty_fifteen": {"work_min": 9, "reps": 3, "pct": 1.12, "rest_ratio": 0.6,
                       "note": "30s on / 15s off within each bout",
                       "micro": {"a_s": 30, "a_pct": 1.12, "b_s": 15, "b_pct": 0.45}},
    "hard_start":    {"work_min": 5, "reps": 4, "pct": 1.08, "rest_ratio": 0.5,
                      "note": "first 30s at 130%, then settle to target",
                      "lead": {"lead_s": 30, "lead_pct": 1.30}},
}


def hiit(variant: str = "straight_5", reps: int | None = None,
         work_min: float | None = None) -> Session:
    cfg = dict(HIIT_VARIANTS[variant])
    if reps:
        cfg["reps"] = reps
    if work_min:
        cfg["work_min"] = work_min
    rest_min = round(cfg["work_min"] * cfg["rest_ratio"], 1)
    cfg["rest_min"] = rest_min
    steps = _warmup(15)
    for i in range(cfg["reps"]):
        steps += _bout_steps(int(cfg["work_min"] * 60), cfg, f"bout {i+1}")
        if i < cfg["reps"] - 1:
            steps.append(Step(int(rest_min * 60), 0.45, "recovery"))
    steps += _cooldown(10)
    return Session("hiit", variant, steps=steps, params=cfg)


THRESHOLD_VARIANTS = {
    # Hedged the full 10% the source asks for, and rested at ~10:1 rather than the 3-4:1 these
    # carried until 2026-11-02. The two changes go together and must stay together: the source's
    # diagnostic is that needing long rests means the intensity is too high, so cutting rests
    # while leaving 92-95% targets would have made these harder, not more faithful.
    #
    # Note this does raise session IF and TSS slightly — 2x20 @90% off 2min rests is a harder
    # session than 2x20 @92% off 5min. That is expected. The protection against too much
    # intensity is the tier-gated hard-session *count* in blocks.py, not softened targets.
    "long_reps":    {"work_min": 20, "reps": 2, "pct": 0.90, "rest_min": 2},
    "medium_reps":  {"work_min": 15, "reps": 3, "pct": 0.90, "rest_min": 1.5},
    "short_reps":   {"work_min": 10, "reps": 4, "pct": 0.92, "rest_min": 1},
    "continuous":   {"work_min": 40, "reps": 1, "pct": 0.86, "rest_min": 0},
    "sweet_spot":   {"work_min": 18, "reps": 3, "pct": 0.88, "rest_min": 2},
    # The one rule the source is emphatic about is not crossing the threshold transition, and
    # this variant used to spend a third of its work time at 103% FTP — straight through it.
    # It now rolls over and under its own mean (0.92) without ever exceeding 0.99. `pct` is the
    # true time-weighted mean of the alternation, so anything reading params["pct"] (e.g.
    # plan_build.py's --watts) gets the least-wrong single number.
    "over_unders":  {"work_min": 15, "reps": 3, "pct": 0.92, "rest_min": 1.5,
                     "micro": {"a_s": 120, "a_pct": 0.88, "b_s": 60, "b_pct": 0.99}},
}


def threshold(variant: str = "long_reps", reps: int | None = None,
              work_min: float | None = None) -> Session:
    cfg = dict(THRESHOLD_VARIANTS[variant])
    if reps:
        cfg["reps"] = reps
    if work_min:
        cfg["work_min"] = work_min
    steps = _warmup(12)
    for i in range(cfg["reps"]):
        steps += _bout_steps(int(cfg["work_min"] * 60), cfg, f"effort {i+1}")
        if i < cfg["reps"] - 1 and cfg["rest_min"]:
            steps.append(Step(int(cfg["rest_min"] * 60), 0.45, "release"))
    steps += _cooldown(8)
    return Session("threshold", variant, steps=steps, params=cfg)


STRENGTH_VARIANTS = {
    "lower": {"focus": "lower body — squat/deadlift pattern, heavy"},
    "upper": {"focus": "upper body + grip, core anti-rotation"},
    "full":  {"focus": "full body, compound lifts"},
}


def strength(minutes: int = 40, variant: str = "full") -> Session:
    """
    Not power-based, so it carries no IF. Present in the plan as a real slot because Arnold is
    emphatic that it belongs year-round, and because its absence from the PMC is exactly why
    this project keeps undercounting its load.
    """
    cfg = dict(STRENGTH_VARIANTS[variant])
    return Session("strength", variant,
                   steps=[Step(minutes * 60, 0.0, cfg["focus"])],
                   params={"minutes": minutes, **cfg})


def variant_names(archetype: str) -> list[str]:
    return sorted({
        "lit": LIT_VARIANTS, "sit": SIT_VARIANTS, "hiit": HIIT_VARIANTS,
        "threshold": THRESHOLD_VARIANTS, "strength": STRENGTH_VARIANTS,
    }[archetype])


def catalogue() -> list[Session]:
    """Every archetype at every variant — the whole library, for inspection."""
    out = []
    for v in variant_names("lit"):
        out.append(lit(90, v))
    for v in variant_names("sit"):
        out.append(sit(v))
    for v in variant_names("hiit"):
        out.append(hiit(v))
    for v in variant_names("threshold"):
        out.append(threshold(v))
    for v in variant_names("strength"):
        out.append(strength(40, v))
    return out

"""
Workout archetypes — the parameterised templates a plan is built from.

Five types, after Jem Arnold's Sustainable Training template: low-intensity (LIT), sprint
intervals (SIT), high-intensity intervals (HIIT), threshold, and strength. Each is a small
number of parameters, and the variants below are what TrainerRoad's library actually is once
the names come off — "Spickard +3/+4/+5" is one workout at three doses.

Everything is expressed as a percentage of FTP, never as watts. Watts are resolved at render
time from the tracked FTP for that date, so a ramp test re-renders the whole plan instead of
invalidating it. That matters here: the tracked number is uncertain (TrainerRoad prescribes
against ~231, athlete_profile says 238, intervals' eFTP says 260) and the plan must not bake
one in.

Two prescriptions carried over from the source and worth not quietly "improving":

- **Threshold hedges low.** Arnold: better to undershoot by 10% than overshoot by 2%, because
  the metabolic response either side of the threshold transition is qualitatively different.
  The threshold targets here sit deliberately below a naive "100% of FTP".
- **HIIT is sub-maximal.** The goal is to extend time in the severe domain, not to maximise any
  single interval — "half a rep left in the tank". Targets are modest for VO2 work on purpose.
"""

from dataclasses import dataclass, field


@dataclass
class Step:
    """One block of time at one intensity. `pct` is a fraction of FTP."""
    seconds: int
    pct: float
    label: str = ""

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
            "lit": lambda: f"{int(self.minutes)}min @ {int(p['pct']*100)}% FTP",
            "sit": lambda: f"{p['reps']}x {p['work_s']}s all-out, {p['rest_min']}min recovery",
            "hiit": lambda: (f"{p['reps']}x {p['work_min']}min @ {int(p['pct']*100)}%, "
                             f"{p['rest_min']}min recovery"),
            "threshold": lambda: (f"{p['reps']}x {p['work_min']}min @ {int(p['pct']*100)}%, "
                                  f"{p['rest_min']}min recovery"),
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


# ── archetypes ────────────────────────────────────────────────────────────────

LIT_VARIANTS = {
    "steady":            {"pct": 0.62},
    "cadence_drills":    {"pct": 0.62, "note": "alternate 5min @ 95rpm / 5min @ 75rpm"},
    "endurance_sprints": {"pct": 0.62, "note": "one 15s all-out sprint every 20-30min"},
    "progressive":       {"pct": 0.66, "note": "start 55%, finish 72%"},
}


def lit(minutes: int, variant: str = "steady") -> Session:
    """
    Low-intensity continuous. The foundation, not filler.

    No warm-up block: the ride is its own warm-up. Kept at 60-66% of FTP because the
    failure mode for endurance work is drifting up into tempo, which converts the session's
    purpose from duration-dependent to intensity-dependent stimulus.
    """
    cfg = LIT_VARIANTS[variant]
    return Session("lit", variant,
                   steps=[Step(minutes * 60, cfg["pct"], cfg.get("note", "steady endurance"))],
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
                       "note": "30s on / 15s off within each bout"},
    "hard_start":    {"work_min": 5, "reps": 4, "pct": 1.08, "rest_ratio": 0.5,
                      "note": "first 30s at 130%, then settle to target"},
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
        steps.append(Step(int(cfg["work_min"] * 60), cfg["pct"], f"bout {i+1}"))
        if i < cfg["reps"] - 1:
            steps.append(Step(int(rest_min * 60), 0.45, "recovery"))
    steps += _cooldown(10)
    return Session("hiit", variant, steps=steps, params=cfg)


THRESHOLD_VARIANTS = {
    # Deliberately hedged low - see the module docstring. These sit under FTP, not on it.
    "long_reps":    {"work_min": 20, "reps": 2, "pct": 0.92, "rest_min": 5},
    "medium_reps":  {"work_min": 15, "reps": 3, "pct": 0.93, "rest_min": 4},
    "short_reps":   {"work_min": 10, "reps": 4, "pct": 0.95, "rest_min": 3},
    "continuous":   {"work_min": 40, "reps": 1, "pct": 0.88, "rest_min": 0},
    "sweet_spot":   {"work_min": 18, "reps": 3, "pct": 0.89, "rest_min": 4},
    "over_unders":  {"work_min": 15, "reps": 3, "pct": 0.95, "rest_min": 5,
                     "note": "alternate 2min @ 92% / 1min @ 103%"},
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
        steps.append(Step(int(cfg["work_min"] * 60), cfg["pct"], f"effort {i+1}"))
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


BUILDERS = {"lit": lit, "sit": sit, "hiit": hiit, "threshold": threshold, "strength": strength}


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

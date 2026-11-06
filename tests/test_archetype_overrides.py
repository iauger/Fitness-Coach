"""
Overriding the workout library, and the rules an override may not break.

This is the first step where editing a text file changes what a session actually asks someone
to ride. Most of that is theirs to decide. A few things are not, and the interesting half of
this step is the refusal rather than the override.

The strongest test here is `test_invariants_hold_when_every_variant_is_overridden`: a config
that replaces the entire library, run through the real generator, with every structural rule
still holding. It is the roadmap's own stated bar for this step, and it is the one that would
catch an override reaching somewhere it should not.
"""

import sqlite3
import textwrap
from datetime import date

import pytest

import src.config as C
from conftest import seed_ladder
from src.db.migrations import migrate
from src.planning import archetypes as A
from src.planning import safety
from src.planning.roller import store_horizon

MINIMAL = "athlete:\n  name: X\nplan:\n  name: P\n  start_monday: 2026-11-02\n"


def with_config(text: str, tmp_path, monkeypatch):
    path = tmp_path / "a.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", path)
    C.reset()
    return path


# ── our own library is the first thing the rules are tested against ──────────

def test_every_built_in_variant_passes_the_safety_rules():
    """
    A rule our own library breaks is a wrong rule, not a library to fix.

    Without this, the rules could be written to any strictness at all and only ever be felt by
    someone else — the defaults would sail past because nothing checked them, and the first
    person to discover a rule was too tight would be a stranger with a config file.
    """
    broken = []
    for archetype, variants in A.DEFAULTS.items():
        for name, cfg in variants.items():
            broken += safety.violations(archetype, name, cfg)
    assert broken == []


def test_the_rules_cover_every_archetype_the_library_has():
    """A new archetype with no rules would be silently unconstrained."""
    assert set(safety.ARCHETYPES) == set(A.DEFAULTS)


# ── merging ───────────────────────────────────────────────────────────────────

def test_an_override_reaches_the_generated_session(tmp_path, monkeypatch):
    with_config(MINIMAL + textwrap.dedent("""\n        archetypes:
          threshold:
            long_reps:
              pct: 0.84
              work_min: 25
        """), tmp_path, monkeypatch)
    session = A.threshold("long_reps")
    assert session.params["pct"] == 0.84
    assert session.params["work_min"] == 25
    assert "25min @ 84%" in session.summary()


def test_an_override_changes_only_the_keys_it_names(tmp_path, monkeypatch):
    """
    Per variant, per key. Replacing the whole variant would make a one-line tweak require
    restating four numbers correctly, which is a worse way to get a session wrong than the one
    it protects against.
    """
    before = dict(A.THRESHOLD_VARIANTS["long_reps"])
    with_config(MINIMAL + textwrap.dedent("""\n        archetypes:
          threshold:
            long_reps:
              pct: 0.88
        """), tmp_path, monkeypatch)
    after = A.variants("threshold")["long_reps"]
    assert after["pct"] == 0.88
    assert after["reps"] == before["reps"]
    assert after["work_min"] == before["work_min"]
    assert after["rest_min"] == before["rest_min"]


def test_an_unnamed_variant_is_untouched(tmp_path, monkeypatch):
    with_config(MINIMAL + textwrap.dedent("""\n        archetypes:
          threshold:
            long_reps:
              pct: 0.88
        """), tmp_path, monkeypatch)
    assert A.variants("threshold")["continuous"] == A.THRESHOLD_VARIANTS["continuous"]


def test_a_new_variant_name_creates_one(tmp_path, monkeypatch):
    """
    What makes the library extensible rather than merely adjustable — and why `check_all`
    validates the merged result: a brand-new variant has no defaults to lean on, so every rule
    has to hold on its own.
    """
    with_config(MINIMAL + textwrap.dedent("""\n        archetypes:
          threshold:
            my_own:
              work_min: 12
              reps: 4
              pct: 0.90
              rest_min: 1.5
        """), tmp_path, monkeypatch)
    assert "my_own" in A.variant_names("threshold")
    assert A.threshold("my_own").params["reps"] == 4


def test_the_defaults_are_not_mutated_by_an_override(tmp_path, monkeypatch):
    """
    `variants()` must copy. Mutating the module dict would make an override permanent for the
    process, so a test that reset the config would keep the previous one's numbers.
    """
    original = dict(A.THRESHOLD_VARIANTS["long_reps"])
    with_config(MINIMAL + textwrap.dedent("""\n        archetypes:
          threshold:
            long_reps:
              pct: 0.84
        """), tmp_path, monkeypatch)
    A.threshold("long_reps")
    C.reset()
    assert A.THRESHOLD_VARIANTS["long_reps"] == original


# ── the rules that are not configurable ──────────────────────────────────────

@pytest.mark.parametrize("override, expected", [
    ("threshold:\n    long_reps:\n      pct: 1.05\n", "above FTP"),
    ("threshold:\n    over_unders:\n      micro:\n        b_pct: 1.06\n", "above FTP"),
    ("threshold:\n    long_reps:\n      rest_min: 8\n", "work:rest"),
    ("threshold:\n    long_reps:\n      work_min: 8\n", "shorter than 10min"),
    ("threshold:\n    long_reps:\n      reps: 1\n", "under the 40min"),
    ("hiit:\n    straight_5:\n      pct: 1.60\n", "sub-maximal"),
    ("hiit:\n    straight_5:\n      work_min: 2\n", "severe domain"),
    ("sit:\n    short:\n      work_s: 90\n", "not a sprint"),
    ("lit:\n    steady:\n      pct: 0.88\n", "sustainable week"),
    ("lit:\n    progressive:\n      ramp: [0.6, 0.95]\n", "ramp finishes"),
    ("strength:\n    full:\n      focus: ''\n", "focus is empty"),
])
def test_an_override_that_breaks_a_rule_is_refused_with_the_rule_named(override, expected):
    with pytest.raises(C.ConfigError, match=expected):
        C.parse(MINIMAL + "archetypes:\n  " + override)


def test_an_unknown_archetype_is_refused_and_lists_the_real_ones():
    with pytest.raises(C.ConfigError, match="unknown archetype"):
        C.parse(MINIMAL + "archetypes:\n  cyclocross:\n    x:\n      pct: 0.9\n")


def test_every_broken_rule_is_reported_at_once():
    """
    The rules interact: lowering `pct` to get under FTP can leave the work:rest ratio wrong,
    and finding that out one run later is the worst version of this.
    """
    with pytest.raises(C.ConfigError) as exc:
        C.parse(MINIMAL + """archetypes:
  threshold:
    broken:
      work_min: 5
      reps: 2
      pct: 1.20
      rest_min: 1
""")
    message = str(exc.value)
    assert "above FTP" in message
    assert "shorter than 10min" in message
    assert "under the 40min" in message


def test_an_override_is_judged_merged_with_its_default_not_alone():
    """
    An override naming only `rest_min` has to be checked against the `work_min` it will
    actually be ridden with. Judged alone there is no ratio to check and it would pass.
    """
    with pytest.raises(C.ConfigError, match="work:rest"):
        C.parse(MINIMAL + "archetypes:\n  threshold:\n    long_reps:\n      rest_min: 9\n")


def test_a_valid_override_of_every_archetype_is_accepted():
    cfg = C.parse(MINIMAL + """archetypes:
  lit:
    steady:
      pct: 0.65
  sit:
    short:
      work_s: 15
  hiit:
    straight_5:
      pct: 1.05
  threshold:
    long_reps:
      pct: 0.88
  strength:
    full:
      focus: full body, kettlebell complexes
""")
    assert set(cfg.archetypes) == {"lit", "sit", "hiit", "threshold", "strength"}


# ── the roadmap's bar for this step ──────────────────────────────────────────

EVERYTHING = """
archetypes:
  lit:
    steady:
      pct: 0.58
    cadence_drills:
      pct: 0.60
    endurance_sprints:
      pct: 0.60
    progressive:
      ramp: [0.52, 0.70]
  sit:
    short:
      work_s: 12
      reps: 10
      rest_min: 3
    medium:
      work_s: 25
      reps: 5
    long:
      work_s: 35
      reps: 4
    maximal:
      work_s: 40
      reps: 3
  hiit:
    straight_4:
      work_min: 5
      pct: 1.06
    straight_5:
      work_min: 6
      pct: 1.04
    straight_6:
      work_min: 7
      pct: 1.02
    straight_8:
      work_min: 9
      pct: 1.00
    thirty_fifteen:
      work_min: 10
      pct: 1.10
    hard_start:
      work_min: 6
      pct: 1.05
  threshold:
    long_reps:
      work_min: 22
      reps: 2
      pct: 0.89
      rest_min: 2
    medium_reps:
      work_min: 14
      reps: 3
      pct: 0.90
      rest_min: 1.5
    short_reps:
      work_min: 10
      reps: 5
      pct: 0.93
      rest_min: 1
    continuous:
      work_min: 45
      reps: 1
      pct: 0.85
      rest_min: 0
    sweet_spot:
      work_min: 20
      reps: 2
      pct: 0.87
      rest_min: 2
    over_unders:
      work_min: 14
      reps: 3
      pct: 0.91
      rest_min: 1.5
  strength:
    lower:
      focus: legs, hinge-dominant
    upper:
      focus: push/pull, overhead
    full:
      focus: complexes
"""


def test_invariants_hold_when_every_variant_is_overridden(tmp_path, monkeypatch):
    """
    The roadmap's own bar for this step, and the test that would catch an override reaching
    somewhere it should not.

    A config replacing the entire library, run through the real generator. Everything
    structural still has to hold: the hard-session ceiling, two lifts a week, no lower body
    beside a long ride, no doubled slot, every session with a real duration. None of those
    come from the library, and that is the point — the library decides what a hard session
    *is*, never how many there are or where they land.
    """
    with_config(MINIMAL + EVERYTHING, tmp_path, monkeypatch)

    path = tmp_path / "o.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn, slug="p", name="P")

    store_horizon(conn, date(2026, 11, 4), "P")
    rows = [dict(r) for r in conn.execute(
        "SELECT date, weekday, role, archetype, variant, minutes, prescription "
        "FROM prescribed_sessions WHERE plan_name = 'P' ORDER BY date")]
    assert rows, "the roller produced nothing"

    from src.analysis.dates import monday_of

    weeks: dict[str, list[dict]] = {}
    for r in rows:
        weeks.setdefault(monday_of(date.fromisoformat(r["date"])).isoformat(), []).append(r)

    for monday, sessions in weeks.items():
        assert sum(1 for s in sessions if s["role"] == "hard") <= 2, monday
        assert sum(1 for s in sessions if s["role"] == "strength") == 2, monday
        longs = {s["weekday"] for s in sessions if s["role"] == "long"}
        for s in sessions:
            if s["archetype"] == "strength" and s["variant"] == "lower":
                assert not longs & {(s["weekday"] - 1) % 7, (s["weekday"] + 1) % 7}, monday
            assert (s["minutes"] or 0) > 0, f"{s['date']} has no duration"
            assert s["prescription"], f"{s['date']} has no prescription text"
        rides = [s for s in sessions if s["role"] != "strength"]
        assert len({s["weekday"] for s in rides}) == len(rides), monday
    conn.close()


def test_the_overridden_library_still_obeys_the_rules_in_the_built_sessions(tmp_path,
                                                                            monkeypatch):
    """
    Validation happens on the config, but what matters is the session that comes out of it.
    `_alternating` and `_ramp` build steps from the parameters, and a parameter that passed
    could still produce a step above FTP if a builder ignored it.
    """
    with_config(MINIMAL + EVERYTHING, tmp_path, monkeypatch)
    for variant in A.variant_names("threshold"):
        steps = A.threshold(variant).steps
        assert max(s.pct for s in steps) <= 1.00, variant
    for variant in A.variant_names("lit"):
        steps = [s for s in A.lit(60, variant).steps if "sprint" not in s.label]
        assert max(s.pct for s in steps) <= safety.LIT_MAX_PCT, variant


def test_an_unknown_archetype_with_no_variants_is_still_refused():
    """
    The case the guard in `check_all` exists for, and the only one it is reachable in.

    `violations` rejects an unknown archetype too, so for a misspelled block that *has*
    variants either check would catch it — which is why a mutant removing the `check_all`
    guard survived the suite. With no variants there is nothing to iterate and nothing to
    reach `violations` at all, so a typo like `threshhold:` with its contents still to be
    filled in would be silently accepted and then silently do nothing.
    """
    with pytest.raises(C.ConfigError, match="unknown archetype"):
        C.parse(MINIMAL + "archetypes:\n  cyclocross: {}\n")


@pytest.mark.parametrize("text, expected", [
    ("archetypes:\n  threshold: [1, 2]\n", "block of named variants"),
    ("archetypes:\n  threshold:\n    long_reps: 0.9\n", "block of settings"),
    ("archetypes: 7\n", "block of settings"),
])
def test_a_malformed_archetypes_block_says_what_shape_it_wanted(text, expected):
    """
    This is the one block the unknown-key check cannot police, since variant names are
    open-ended — so shape errors have to be caught deliberately rather than falling out of
    the dataclass build.
    """
    with pytest.raises(C.ConfigError, match=expected):
        C.parse(MINIMAL + text)


def test_a_plan_with_no_goal_date_can_still_be_stored(tmp_path, monkeypatch):
    """
    Found while testing overrides, and nothing to do with them: `plan.goal_date` is optional
    in config but `training_plan_weeks.plan_end_date` was NOT NULL, so omitting it killed
    `store_horizon` with an IntegrityError several frames from anything mentioning goals.

    Plenty of people train without a dated event, and the rotation deliberately does not taper
    for one anyway. Whoever hit this would have been running the project for the first time.
    """
    with_config(MINIMAL, tmp_path, monkeypatch)
    assert C.config().plan.goal_date is None

    path = tmp_path / "g.db"
    migrate(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    seed_ladder(conn, slug="p", name="P")

    store_horizon(conn, date(2026, 11, 4), "P")
    ends = {r[0] for r in conn.execute(
        "SELECT DISTINCT plan_end_date FROM training_plan_weeks")}
    assert ends == {None}
    assert conn.execute("SELECT COUNT(*) FROM prescribed_sessions").fetchone()[0] > 0
    conn.close()

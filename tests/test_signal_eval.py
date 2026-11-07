"""
How the extractor is scored. The scoring has to be right before its verdict means anything.

Mutants: an extra of an allowed kind counted as a false positive (or an extra of a disallowed kind
forgiven); a milder-than-expected severity-2 signal counted as correct; the bar compared with `>=`
or applied to all severities rather than 2 and 3; two predictions of one kind both matched to a
single expectation; a gold file whose own cases are malformed.
"""

import json

import pytest

from src import signals
from src.coach import signal_eval as ev

GOLD = ev.GOLD_PATH


def p(kind, severity=1, quote="q"):
    return {"kind": kind, "severity": severity, "quote": quote}


def case(expect=(), allow=()):
    return {"id": "c", "expect": [dict(e) for e in expect], "allow": list(allow)}


def e(kind, lo=0, hi=3):
    return {"kind": kind, "min_severity": lo, "max_severity": hi}


# -- matching -----------------------------------------------------------------------------------

def test_an_expected_kind_is_a_hit_and_a_missing_one_is_a_miss():
    r = ev.match(case([e("illness"), e("fatigue")]), [p("illness")])
    assert [h["kind"] for h in r["hits"]] == ["illness"] and [m["kind"] for m in r["misses"]] == ["fatigue"]


def test_an_unexpected_kind_is_a_false_positive():
    assert [x["kind"] for x in ev.match(case([]), [p("burnout", 3)])["false_pos"]] == ["burnout"]


def test_an_allowed_extra_is_neither_a_hit_nor_a_false_positive():
    r = ev.match(case([e("illness")], allow=["fatigue"]), [p("illness"), p("fatigue")])
    assert r["false_pos"] == [] and [x["kind"] for x in r["extras"]] == ["fatigue"]


def test_a_kind_is_only_allowed_if_listed():
    assert ev.match(case([], allow=["fatigue"]), [p("illness")])["false_pos"]


def test_two_predictions_of_one_kind_do_not_both_match_a_single_expectation():
    r = ev.match(case([e("illness")]), [p("illness"), p("illness")])
    assert len(r["hits"]) == 1 and len(r["false_pos"]) == 1


def test_severity_range_is_inclusive_and_flags_too_high():
    r = ev.match(case([e("illness", 1, 2)]), [p("illness", 3)])
    assert r["hits"][0]["in_range"] is False and r["hits"][0]["too_high"]
    for sev in (1, 2):
        assert ev.match(case([e("illness", 1, 2)]), [p("illness", sev)])["hits"][0]["in_range"]
    assert ev.match(case([e("illness", 1, 2)]), [p("illness", 0)])["hits"][0]["in_range"] is False


# -- scoring ------------------------------------------------------------------------------------

def run(cases_and_predictions):
    return ev.score([ev.match(c, preds) for c, preds in cases_and_predictions])


def test_precision_and_recall_by_kind():
    got = run([(case([e("illness")]), [p("illness")]),
               (case([e("illness")]), []),
               (case([]), [p("illness")])])["kinds"]["illness"]
    assert (got["tp"], got["fp"], got["fn"]) == (1, 1, 1)
    assert got["precision"] == 0.5 and got["recall"] == 0.5


def test_a_severity_two_signal_expected_only_as_mild_counts_as_wrong():
    """Mutant: only false positives count. An over-graded true signal reaches the coach just the same."""
    got = run([(case([e("fatigue", 0, 1)]), [p("fatigue", 2)])])["kinds"]["fatigue"]
    assert (got["high_predicted"], got["high_wrong"]) == (1, 1)


def test_a_severity_two_signal_expected_at_two_is_right():
    got = run([(case([e("fatigue", 1, 2)]), [p("fatigue", 2)])])["kinds"]["fatigue"]
    assert (got["high_predicted"], got["high_wrong"]) == (1, 0)


def test_mild_false_positives_do_not_count_against_the_bar():
    """Mutant: the bar applied to every severity. A stray severity-0 mention never reaches the coach."""
    out = run([(case([]), [p("fatigue", 1)] * 5)])
    assert out["kinds"]["fatigue"]["high_predicted"] == 0 and out["passes"]


def test_the_bar_is_ten_percent_and_the_boundary_passes():
    ten = [(case([e("illness", 2, 3)]), [p("illness", 2)])] * 9 + [(case([]), [p("illness", 2)])]
    assert run(ten)["passes"]                                            # exactly 1 in 10
    eleven = ten + [(case([e("illness", 2, 3)]), [p("illness", 2)])] * 0 + [(case([]), [p("illness", 3)])]
    out = run(eleven)
    assert not out["passes"] and out["failing"] == ["illness"]           # 2 in 11


def test_a_kind_with_no_high_severity_predictions_has_no_rate():
    assert run([(case([e("fatigue")]), [p("fatigue", 1)])])["kinds"]["fatigue"]["high_wrong_rate"] is None


# -- the gold file ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cases():
    return ev.load(GOLD, private=False)         # what a fresh clone has


def test_every_gold_case_is_well_formed(cases):
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["text"].strip() and c["origin"] in ("real", "written")
        assert c["source"] in ("conversation", "feel_note", "ride_note")
        assert len(c["today"]) == 10
        for x in c["expect"]:
            assert x["kind"] in signals.KINDS and 0 <= x["min_severity"] <= x["max_severity"] <= 3
        assert set(c["allow"]) <= set(signals.KINDS)
        assert not {x["kind"] for x in c["expect"]} & set(c["allow"])


def test_the_gold_set_covers_every_kind_but_other_and_has_negatives(cases):
    expected = {x["kind"] for c in cases for x in c["expect"]}
    assert set(signals.KINDS) - expected == {"other"}
    assert sum(1 for c in cases if not c["expect"]) >= 8


def test_the_athletes_own_writing_is_merged_in_when_present_and_never_required(tmp_path):
    """Mutant: private cases required (a fresh clone fails) or ignored (the real check never runs)."""
    gold = tmp_path / "signals_gold.json"
    gold.write_text(json.dumps({"cases": [{"id": "a"}]}), encoding="utf-8")
    assert [c["id"] for c in ev.load(gold, private=False)] == ["a"]
    own = tmp_path / "mine.json"
    own.write_text(json.dumps({"cases": [{"id": "b"}]}), encoding="utf-8")
    assert [c["id"] for c in ev.load(gold, private=own)] == ["a", "b"]
    assert [c["id"] for c in ev.load(gold, private=False)] == ["a"]
    assert [c["id"] for c in ev.load(gold, private=tmp_path / "absent.json")] == ["a"]


def test_the_athletes_writing_is_looked_for_under_data_which_is_never_committed():
    import subprocess
    assert ev.PRIVATE_PATH.parent.name == "data"
    ignored = subprocess.run(["git", "check-ignore", "-q", str(ev.PRIVATE_PATH)],
                             cwd=ev.PRIVATE_PATH.parents[1]).returncode
    assert ignored == 0


def test_nothing_in_the_public_gold_file_is_marked_real(cases):
    assert {c["origin"] for c in cases} == {"written"}


def test_the_file_is_valid_json_with_a_cases_list():
    assert isinstance(json.loads(GOLD.read_text(encoding="utf-8"))["cases"], list)

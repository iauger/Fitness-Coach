"""
Scoring the extractor against the gold set.

The model is not deterministic and costs money, so running it is `scripts/eval_signals.py`, on
demand. What happens to its answers is pure and lives here, where it can be tested: each
prediction is matched to an expectation by kind, and what is left over is a false positive (said,
not expected) or a false negative (expected, not said).

A gold case lists what must be found (`expect`: a kind and the severities that are acceptable) and
what is merely tolerated (`allow`: kinds that are reasonable to file but not required, so a
defensible extra is neither rewarded nor punished). Everything else filed is a false positive.

The bar for trusting the extractor to feed the coach: no kind, at severity 2 or 3, is wrong more
than `BAR` of the time. A severity-2 signal reaches the watch list and the coach's briefing, so a
wrong one is a sentence the coach says. "Wrong" is a signal that was not expected at all, or was
expected only as something milder.
"""

import json
from pathlib import Path

GOLD_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "signals_gold.json"
BAR = 0.10
HIGH = 2


def load(path: Path = GOLD_PATH, private: bool = True) -> list[dict]:
    """
    The gold cases, plus the athlete's own writing if `signals_gold.private.json` sits beside them.

    The repository is public and an athlete's notes and conversations are not, so the real cases
    are kept in a git-ignored file and merged in when present. Everything else runs without it.
    """
    path = Path(path)
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    extra = path.with_name("signals_gold.private.json")
    if private and extra.exists():
        cases += json.loads(extra.read_text(encoding="utf-8"))["cases"]
    return cases


def match(case: dict, predicted: list[dict]) -> dict:
    """Compare one case's predictions with its expectations."""
    expected = [dict(e, matched=False) for e in case.get("expect", [])]
    allowed = set(case.get("allow", []))
    hits, extras, false_pos = [], [], []
    for p in predicted:
        e = next((e for e in expected if not e["matched"] and e["kind"] == p["kind"]), None)
        if e:
            e["matched"] = True
            lo, hi = e.get("min_severity", 0), e.get("max_severity", 3)
            hits.append({"kind": p["kind"], "severity": p["severity"], "in_range": lo <= p["severity"] <= hi,
                         "too_high": p["severity"] > hi, "expected_max": hi, "quote": p["quote"]})
        elif p["kind"] in allowed:
            extras.append(p)
        else:
            false_pos.append(p)
    return {"id": case["id"], "hits": hits, "extras": extras, "false_pos": false_pos,
            "misses": [e for e in expected if not e["matched"]]}


def score(results: list[dict]) -> dict:
    """
    Per-kind counts and the bar. `results` are `match` outputs.

    `high_wrong` counts the severity-2+ predictions that were not expected at all or were
    expected as something milder: those are the ones that would reach the coach.
    """
    kinds: dict[str, dict] = {}

    def row(kind):
        return kinds.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0, "severity_off": 0,
                                       "high_predicted": 0, "high_wrong": 0})
    for r in results:
        for h in r["hits"]:
            k = row(h["kind"])
            k["tp"] += 1
            k["severity_off"] += 0 if h["in_range"] else 1
            if h["severity"] >= HIGH:
                k["high_predicted"] += 1
                k["high_wrong"] += int(h["expected_max"] < HIGH)
        for p in r["false_pos"]:
            k = row(p["kind"])
            k["fp"] += 1
            if p["severity"] >= HIGH:
                k["high_predicted"] += 1
                k["high_wrong"] += 1
        for e in r["misses"]:
            row(e["kind"])["fn"] += 1
    for k in kinds.values():
        k["precision"] = k["tp"] / (k["tp"] + k["fp"]) if k["tp"] + k["fp"] else None
        k["recall"] = k["tp"] / (k["tp"] + k["fn"]) if k["tp"] + k["fn"] else None
        k["high_wrong_rate"] = k["high_wrong"] / k["high_predicted"] if k["high_predicted"] else None
    failing = sorted(n for n, k in kinds.items()
                     if k["high_wrong_rate"] is not None and k["high_wrong_rate"] > BAR)
    return {"kinds": dict(sorted(kinds.items())), "failing": failing, "passes": not failing}

"""
Run the signal extractor over the gold set and report how it did.

    python scripts/eval_signals.py                  # tests/fixtures/signals_gold.json
    python scripts/eval_signals.py --gold PATH      # e.g. the draft, before it is frozen
    python scripts/eval_signals.py --cases w-burnout,w-fatigue
    python scripts/eval_signals.py --save out.json  # every raw prediction, for reading

On demand, never in the default test run: it calls the model (a few cents for the whole set) and
a model is not deterministic. It reads and writes no database: the extractor is told there are
no open threads and nothing is filed.

Reports precision and recall by kind, lists every false positive and miss with the quote the model
used, and ends with the bar: no kind may be wrong more than 10% of the time at severity 2 or 3.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main(argv: list[str] | None = None) -> int:
    # Parsed first, so `--help` and a mistyped flag never reach the model or the database.
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--gold", type=Path, help="gold set to use (default: the frozen one)")
    ap.add_argument("--cases", help="comma-separated case ids to run")
    ap.add_argument("--model", help="extractor model (default: the configured one)")
    ap.add_argument("--save", type=Path, help="write every prediction to this JSON file")
    args = ap.parse_args(argv)

    from dotenv import load_dotenv

    from src.coach import extract, signal_eval
    from src.coach import memory

    load_dotenv()
    cases = signal_eval.load(args.gold) if args.gold else signal_eval.load()
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if not cases:
        print("no cases to run")
        return 1

    client = memory._client()
    results, raw = [], []
    for case in cases:
        found = extract.extract(case["text"], case["today"], client, model=args.model, open_threads=[])
        r = signal_eval.match(case, found.signals)
        results.append(r)
        raw.append({"id": case["id"], "signals": found.signals, "rejected": found.rejected,
                    "gist": found.gist, "followups": found.followups})
        flag = "ok " if not (r["false_pos"] or r["misses"] or any(not h["in_range"] for h in r["hits"])) else "!! "
        print(flag + case["id"])

    report = signal_eval.score(results)
    print("\nkind                 tp  fp  fn  sev-off  sev2+ wrong")
    for kind, k in report["kinds"].items():
        high = f"{k['high_wrong']}/{k['high_predicted']}" if k["high_predicted"] else "-"
        print(f"{kind:20} {k['tp']:3} {k['fp']:3} {k['fn']:3} {k['severity_off']:8}  {high}")

    print("\nfalse positives")
    for r in results:
        for p in r["false_pos"]:
            print(f"  {r['id']}: {p['kind']} [{p['severity']}] \"{p['quote']}\"")
    print("misses")
    for r in results:
        for e in r["misses"]:
            print(f"  {r['id']}: expected {e['kind']} {e.get('min_severity', 0)}-{e.get('max_severity', 3)}")
    print("severity outside the expected range")
    for r in results:
        for h in r["hits"]:
            if not h["in_range"]:
                print(f"  {r['id']}: {h['kind']} graded {h['severity']}")
    rejected = [(x["id"], q) for x in raw for q in x["rejected"]]
    print(f"rejected by the guards: {len(rejected)}")
    for case_id, q in rejected:
        print(f"  {case_id}: {q.get('kind')} - {q['why']}")

    if args.save:
        args.save.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print("\nbar (no kind wrong >10% at severity 2+):",
          "PASS" if report["passes"] else "FAIL " + ", ".join(report["failing"]))
    return 0 if report["passes"] else 2


if __name__ == "__main__":
    sys.exit(main())

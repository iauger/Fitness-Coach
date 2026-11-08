"""
Build, check and query the code graph: every module, function, table, route and log event in the
repo and the edges between them, read from the source.

    python scripts/code_graph.py                      # rebuild docs/CODEMAP.md and docs/codegraph/graph.json
    python scripts/code_graph.py --check              # exit 1 if the committed map no longer matches the code
    python scripts/code_graph.py show src.sync        # a module, with what it imports, calls, reads and who uses it
    python scripts/code_graph.py show incremental_sync
    python scripts/code_graph.py show table:signals
    python scripts/code_graph.py show "route:POST /api/chat/send"
    python scripts/code_graph.py show event:sync.done
    python scripts/code_graph.py show src.sync --json # the same, as data

`show` builds the graph fresh (under a second), so it also answers for code that is not committed
yet and includes line numbers, which the committed files leave out so they do not change on every
edit. Read-only apart from the default rebuild, which writes only under docs/.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import cli, codegraph
from src.codegraph_render import render

DOCS = codegraph.ROOT / "docs"
GRAPH_PATH = codegraph.OUT_DIR / "graph.json"
MAP_PATH = DOCS / "CODEMAP.md"


def outputs() -> dict[Path, str]:
    """What the committed files should contain right now, by path."""
    stable = codegraph.stable(codegraph.build())
    return {
        GRAPH_PATH: codegraph.dumps(stable),
        MAP_PATH: render(stable),
    }


def stale() -> list[Path]:
    """Committed files that differ from what the code produces now (a missing file counts)."""
    out = []
    for path, text in outputs().items():
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have is None or have.replace("\r\n", "\n") != text:
            out.append(path)
    return out


def show(query: str, as_json: bool) -> int:
    graph = codegraph.Graph(codegraph.build())
    hits = graph.find(query)
    if not hits:
        print(f"nothing matches {query!r}. Try a module (src.sync), a function name, table:<name>, route:<METHOD /path> or event:<name>.")
        return 1
    if as_json:
        rows = []
        for n in hits[:10]:
            rows.append({"node": n, "out": graph.out.get(n["id"], []), "in": graph.into.get(n["id"], [])})
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    for n in hits[:6]:
        print(graph.describe(n))
        print()
    if len(hits) > 6:
        print(f"{len(hits) - 6} more matches; narrow it: " + ", ".join(h["id"] for h in hits[6:14]))
    return 0


def main(argv: list[str] | None = None) -> int:
    cli.utf8_output()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("command", nargs="?", choices=["build", "show"], default="build")
    ap.add_argument("query", nargs="?", help="for show: a module, function, table:..., route:... or event:... name")
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed map is out of date; write nothing")
    ap.add_argument("--json", action="store_true", help="for show: print data instead of text")
    args = ap.parse_args(argv)

    if args.command == "show":
        if not args.query:
            ap.error("show needs a name to look up")
        return show(args.query, args.json)

    if args.check:
        old = stale()
        if old:
            print("out of date: " + ", ".join(p.relative_to(codegraph.ROOT).as_posix() for p in old))
            print("run: python scripts/code_graph.py")
            return 1
        print("code map is current")
        return 0

    for path, text in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(codegraph.ROOT).as_posix()}  ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

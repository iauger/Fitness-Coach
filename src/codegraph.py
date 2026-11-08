"""
The code graph: every module, symbol, table, route and log event in the repo, and the edges
between them, read from the source rather than written by hand.

It exists to be read by an agent (or a person) who has never seen the repo and does not want to
spend thousands of tokens grepping to find out what calls what. `build()` returns plain dicts;
`scripts/code_graph.py` writes them to `docs/codegraph/` and answers questions about them.

What is a node, and how it is found:

    module    every .py under src/, scripts/ and tests/                      (ast)
    symbol    top-level functions, classes, methods, UPPER_CASE constants    (ast)
    table     every table the migrations produce, with columns and keys      (the migrations,
                                                                              actually run)
    route     every FastAPI route, with its router prefix applied            (decorators)
    event     every `applog` event name                                      (first string arg)
    page      src/web/index.html, the one screen file                        (its fetch calls)
    rule      each `## ` section of CLAUDE.md                                (headings)

and the edges:

    imports   module -> module            weight 1; `lazy` when it is inside a function
    calls     symbol/module -> symbol     best effort: only calls that resolve to a name defined
                                          in this repo (a local def, an imported name, an
                                          imported module's attribute, `self.method`)
    reads / writes   symbol -> table      from SQL in string literals (docstrings excluded)
    serves    symbol -> route
    calls_api page -> route
    emits     symbol -> event
    tests     test module -> module       what the test file imports from src/ and scripts/
    governs   rule -> module              a path named in backticks in that section of CLAUDE.md

**Calls are best effort and say so.** Dynamic dispatch (a tool table keyed by name, FastAPI's
dependency injection, a callback passed as an argument) is invisible to a static pass, so "no
callers found" means *no resolved callers*, not *dead*. Imports, tables, routes and events are
exact.

**Nothing here is read at run time by the app.** It is a map. A test fails when the committed map
no longer matches the code, so it cannot quietly rot.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("src", "scripts", "tests")
OUT_DIR = ROOT / "docs" / "codegraph"
SQL_VERB = re.compile(r"\b(select|insert|update|delete|replace)\b", re.I)
WRITE_RE = re.compile(r"\b(?:insert\s+(?:or\s+\w+\s+)?into|replace\s+into|update|delete\s+from)\s+([a-z_][a-z0-9_]*)", re.I)
READ_RE = re.compile(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", re.I)
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
HTTP_METHODS = {"get", "post", "put", "delete", "patch"}
LOG_LEVELS = {"event", "warning", "error"}


# ---------------------------------------------------------------------------------------------
# reading the source


def _py_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for d in SOURCE_DIRS:
        for p in sorted((root / d).rglob("*.py")):
            if "__pycache__" in p.parts or "fixtures" in p.parts:
                continue
            out.append(p)
    return out


def module_id(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _first_line(doc: str | None) -> str:
    """The first sentence-ish line of a docstring, which is the one-line description."""
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    return (isinstance(parent, ast.Expr) and isinstance(node, ast.Constant) and isinstance(node.value, str))


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        names.append("*")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    return f"{fn.name}({', '.join(names)})"


class _Module:
    """One parsed file: its symbols, import map and the raw facts later passes resolve."""

    def __init__(self, path: Path, root: Path):
        self.path = path
        self.id = module_id(path, root)
        self.rel = path.relative_to(root).as_posix()
        self.source = path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.doc = ast.get_docstring(self.tree)
        self.is_package = path.name == "__init__.py"
        self.symbols: dict[str, dict] = {}          # qualified name -> node dict
        self.imports: list[tuple[str, bool, int]] = []   # (target module or "mod:name", lazy, line)
        self.names: dict[str, str] = {}            # local name -> "module" or "module:symbol"
        self.raw_calls: list[tuple[str, ast.Call]] = []   # (owner id, call node)
        self.raw_refs: list[tuple[str, ast.expr]] = []    # (owner id, a name used without being called)
        self.sql: list[tuple[str, str]] = []       # (owner id, string)
        self.events: list[tuple[str, str]] = []    # (owner id, event name)
        self.routes: list[dict] = []
        self.prefix = ""


def _collect_symbols(m: _Module) -> None:
    def add(qual: str, kind: str, node: ast.AST, **extra) -> None:
        m.symbols[qual] = {
            "id": f"{m.id}:{qual}", "kind": kind, "name": qual, "module": m.id, "path": m.rel,
            "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "public": not qual.split(".")[-1].startswith("_"),
            "doc": _first_line(ast.get_docstring(node)) if hasattr(node, "body") else "",
            **extra,
        }

    for node in m.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, "function", node, signature=_signature(node))
        elif isinstance(node, ast.ClassDef):
            add(node.name, "class", node, bases=[ast.unparse(b) for b in node.bases])
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(f"{node.name}.{sub.name}", "method", sub, signature=_signature(sub))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 1:
                    value = node.value
                    short = value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) <= 80 else None
                    add(t.id, "constant", node, **({"value": short} if short is not None else {}))


def _resolve_import(m: _Module, node: ast.ImportFrom) -> str | None:
    """The absolute dotted module an `ImportFrom` points at, or None if it is not ours."""
    if node.level:
        base = m.id.split(".")
        if not m.is_package:
            base = base[:-1]
        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
        mod = ".".join(base + ([node.module] if node.module else []))
    else:
        mod = node.module or ""
    return mod if mod.split(".")[0] in SOURCE_DIRS else None


def _collect_imports(m: _Module, known: set[str]) -> None:
    class Imports(ast.NodeVisitor):
        depth = 0                                    # > 0 inside a function: a lazy import

        def _fn(self, node) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_FunctionDef = visit_AsyncFunctionDef = _fn

        def visit_Import(self, node: ast.Import) -> None:
            for a in node.names:
                if a.name in known:
                    m.imports.append((a.name, self.depth > 0, node.lineno))
                    if a.asname:
                        m.names[a.asname] = a.name

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            mod = _resolve_import(m, node)
            if mod is None:
                return
            for a in node.names:
                local = a.asname or a.name
                sub = f"{mod}.{a.name}"
                if sub in known:                     # `from src.planning import blocks`
                    m.imports.append((sub, self.depth > 0, node.lineno))
                    m.names[local] = sub
                elif mod in known:                   # `from src.sync import incremental_sync`
                    m.imports.append((mod, self.depth > 0, node.lineno))
                    m.names[local] = f"{mod}:{a.name}"

    Imports().visit(m.tree)


class _Walker(ast.NodeVisitor):
    """Attributes every call, SQL string and event to the function that contains it."""

    def __init__(self, m: _Module):
        self.m = m
        self.stack: list[str] = []     # qualified names, e.g. ["Session", "send"]
        self.parents: list[ast.AST] = []
        self.called: set[int] = set()     # ids of the expressions that are being called

    def owner(self) -> str:
        return f"{self.m.id}:{'.'.join(self.stack)}" if self.stack else self.m.id

    def generic_visit(self, node: ast.AST) -> None:
        self.parents.append(node)
        super().generic_visit(node)
        self.parents.pop()

    def _enter(self, node, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node) -> None:
        # a nested function belongs to the symbol that contains it, so the graph stays flat:
        # only a module-level function or a method of a module-level class owns anything
        top = self.m.symbols.get(self.stack[0], {}).get("kind") if self.stack else None
        if not self.stack or (len(self.stack) == 1 and top == "class"):
            self._enter(node, node.name)
        else:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            parent = self.parents[-1] if self.parents else None
            if not _is_docstring(node, parent) and SQL_VERB.search(node.value):
                self.m.sql.append((self.owner(), node.value))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and id(node) not in self.called:
            self.m.raw_refs.append((self.owner(), node))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and id(node) not in self.called:
            self.m.raw_refs.append((self.owner(), node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.called.add(id(node.func))
        self.m.raw_calls.append((self.owner(), node))
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            name = node.args[0].value
            if "applog" in ast.unparse(node.func) and EVENT_RE.match(name):
                self.m.events.append((self.owner(), name))
        self.generic_visit(node)


def _collect_routes(m: _Module, constants: dict[str, str]) -> None:
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if ast.unparse(node.value.func).endswith("APIRouter"):
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        m.prefix = kw.value.value
    for node in ast.walk(m.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in HTTP_METHODS or not dec.args:
                continue
            arg = dec.args[0]
            path_ = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
            if path_ is None:
                path_ = constants.get(ast.unparse(arg), f"<{ast.unparse(arg)}>")     # e.g. TOKEN_PATH
            base = ast.unparse(dec.func.value)
            prefix = m.prefix if base != "app" else ""
            m.routes.append({"method": dec.func.attr.upper(), "path": prefix + path_, "handler": node.name,
                             "line": node.lineno})


# ---------------------------------------------------------------------------------------------
# the database: the migrations, actually run


def _schema() -> dict[str, dict]:
    """Tables, columns, keys and indexes as the migrations leave them, read from a scratch DB."""
    from src import applog
    from src.db import migrations

    saved = os.environ.get("FITNESS_COACH_LOG_DIR")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        os.environ["FITNESS_COACH_LOG_DIR"] = str(Path(tmp) / "logs")      # migrating logs; keep it out of the real log
        try:
            db = Path(tmp) / "scratch.db"
            migrations.migrate(db)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                tables: dict[str, dict] = {}
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                    name = row["name"]
                    cols = [{"name": c["name"], "type": c["type"], "notnull": bool(c["notnull"]), "pk": bool(c["pk"])}
                            for c in conn.execute(f"PRAGMA table_info({name})")]
                    fks = [{"column": f["from"], "table": f["table"]} for f in conn.execute(f"PRAGMA foreign_key_list({name})")]
                    idx = sorted(i["name"] for i in conn.execute(f"PRAGMA index_list({name})") if not i["name"].startswith("sqlite_"))
                    tables[name] = {"columns": cols, "foreign_keys": fks, "indexes": idx}
                tables["__version__"] = {"user_version": conn.execute("PRAGMA user_version").fetchone()[0]}
                return tables
            finally:
                conn.close()
        finally:
            applog.release()                       # Windows cannot delete a directory holding an open file
            if saved is None:
                os.environ.pop("FITNESS_COACH_LOG_DIR", None)
            else:
                os.environ["FITNESS_COACH_LOG_DIR"] = saved


# ---------------------------------------------------------------------------------------------
# CLAUDE.md and the page


def _rules(root: Path, known_paths: dict[str, str]) -> list[dict]:
    """Each `## ` section of CLAUDE.md, and the repo paths it names in backticks."""
    path = root / "CLAUDE.md"
    if not path.exists():
        return []
    rules, current = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = {"title": line[3:].strip(), "paths": []}
            rules.append(current)
        elif current is not None:
            for token in re.findall(r"`([^`]+)`", line):
                token = token.strip().rstrip("/")
                if token in known_paths and token not in current["paths"]:
                    current["paths"].append(token)
    return rules


def _page_calls(root: Path, routes: list[dict]) -> list[str]:
    """Route keys the screen file reaches, from the /api/ paths written in it.

    By path, not by method: the page builds some requests as `"/api/signals/" + id + "/dismiss"`, so
    a literal ending in a slash matches every route beneath it. That can name a route the page does
    not call; it will not miss one the page spells out.
    """
    page = root / "src" / "web" / "index.html"
    if not page.exists():
        return []
    text = page.read_text(encoding="utf-8")
    literals = {re.sub(r"\$\{[^}]*\}", "{}", m.split("?")[0]) for m in re.findall(r"/api/[A-Za-z0-9_/.${}-]*", text)}
    found = set()
    for r in routes:
        pattern = "^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(r["path"])) + "$"
        for lit in literals:
            if re.match(pattern, lit) or (lit.endswith("/") and r["path"].startswith(lit)):
                found.add(r["key"])
    return sorted(found)


# ---------------------------------------------------------------------------------------------
# the build


def build(root: Path = ROOT) -> dict:
    files = _py_files(root)
    mods = [_Module(p, root) for p in files]
    known = {m.id for m in mods}
    by_id = {m.id: m for m in mods}

    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}

    def edge(src: str, dst: str, kind: str, **extra) -> None:
        key = (src, dst, kind)
        if key in edges:
            edges[key]["weight"] += 1
            if not extra.get("lazy", True):
                edges[key]["lazy"] = False
        else:
            edges[key] = {"src": src, "dst": dst, "kind": kind, "weight": 1, **extra}

    constants: dict[str, str] = {}
    for m in mods:                                  # every module's short string constants, for route paths
        if not m.id.startswith("tests."):
            _collect_symbols(m)
            for q, sym in m.symbols.items():
                if sym["kind"] == "constant" and "value" in sym:
                    constants.setdefault(q, sym["value"])
            m.symbols.clear()

    for m in mods:
        # A test file is a node and says what it imports. Its functions are not nodes: there are
        # more of them than of everything else together, and "which test covers this" is answered
        # by the file.
        _collect_imports(m, known)
        if not m.id.startswith("tests."):
            _collect_symbols(m)
            _collect_routes(m, constants)
            _Walker(m).visit(m.tree)
        layer = m.id.split(".")[0]
        parts = m.id.split(".")
        pkg = m.id if m.is_package else (".".join(parts[:-1]) if len(parts) > 2 else layer)
        nodes[m.id] = {
            "id": m.id, "kind": "module", "name": m.id.split(".")[-1], "path": m.rel, "layer": layer,
            "package": pkg, "doc": _first_line(m.doc), "lines": m.source.count("\n") + 1,
            "public": not m.id.split(".")[-1].startswith("_"),
        }
        for sym in m.symbols.values():
            nodes[sym["id"]] = sym

    # imports -------------------------------------------------------------------------------
    for m in mods:
        for target, lazy, _line in m.imports:
            if target != m.id:
                edge(m.id, target, "imports", lazy=lazy)

    # calls ---------------------------------------------------------------------------------
    for m in mods:
        local_top = {q for q in m.symbols if "." not in q}
        for owner, call in m.raw_calls:
            target = _resolve_call(m, owner, call.func, local_top, by_id)
            if target and target != owner:
                edge(owner, target, "calls")
        # A name used without being called: a function in a dispatch table or passed as a callback,
        # a constant read, a class named in a type. This is what finds the users of `TOOL_FN_MAP[...]`
        # style dispatch that a call edge cannot see.
        for owner, ref in m.raw_refs:
            target = _resolve_call(m, owner, ref, local_top, by_id)
            if target and target != owner:
                edge(owner, target, "refers")

    # tables --------------------------------------------------------------------------------
    schema = _schema()
    version = schema.pop("__version__")["user_version"]
    for name, info in schema.items():
        nodes[f"table:{name}"] = {"id": f"table:{name}", "kind": "table", "name": name, "doc": "",
                                  "columns": info["columns"], "foreign_keys": info["foreign_keys"],
                                  "indexes": info["indexes"]}
        for fk in info["foreign_keys"]:
            edge(f"table:{name}", f"table:{fk['table']}", "references", column=fk["column"])
    for m in mods:
        if m.id.startswith("tests") or m.id == "src.db.migrations":
            continue
        for owner, text in m.sql:
            for t in {x.lower() for x in WRITE_RE.findall(text)} & set(schema):
                edge(owner, f"table:{t}", "writes")
            selects = re.sub(r"\bdelete\s+from\s+\w+", "", text, flags=re.I)     # a DELETE does not read
            for t in {x.lower() for x in READ_RE.findall(selects)} & set(schema):
                edge(owner, f"table:{t}", "reads")

    # events --------------------------------------------------------------------------------
    for m in mods:
        if m.id.startswith("tests"):
            continue
        for owner, name in m.events:
            nodes.setdefault(f"event:{name}", {"id": f"event:{name}", "kind": "event", "name": name, "doc": ""})
            edge(owner, f"event:{name}", "emits")

    # routes --------------------------------------------------------------------------------
    route_list: list[dict] = []
    for m in mods:
        if m.id.startswith("tests"):
            continue
        for r in m.routes:
            key = f"{r['method']} {r['path']}"
            handler = f"{m.id}:{r['handler']}"
            nodes[f"route:{key}"] = {"id": f"route:{key}", "kind": "route", "name": key, "method": r["method"],
                                     "path": r["path"], "module": m.id, "handler": handler,
                                     "guarded": r["method"] != "GET",
                                     "doc": nodes.get(handler, {}).get("doc", "")}
            if handler in nodes:
                edge(handler, f"route:{key}", "serves")
            route_list.append({"method": r["method"], "path": r["path"], "key": f"route:{key}"})
    page_id = "page:src/web/index.html"
    if (root / "src" / "web" / "index.html").exists():
        nodes[page_id] = {"id": page_id, "kind": "page", "name": "index.html", "path": "src/web/index.html",
                          "doc": "The single-page app: every screen is painted from here."}
        for key in _page_calls(root, route_list):
            edge(page_id, key, "calls_api")

    # tests ---------------------------------------------------------------------------------
    for m in mods:
        if m.id.startswith("tests."):
            for target, _lazy, _line in m.imports:
                if not target.startswith("tests"):
                    edge(m.id, target, "tests")

    # rules ---------------------------------------------------------------------------------
    path_to_module = {m.rel: m.id for m in mods}
    for r in _rules(root, path_to_module):
        rid = f"rule:{r['title']}"
        nodes[rid] = {"id": rid, "kind": "rule", "name": r["title"], "path": "CLAUDE.md", "doc": ""}
        for p in r["paths"]:
            edge(rid, path_to_module[p], "governs")

    ordered_nodes = sorted(nodes.values(), key=lambda n: (n["kind"], n["id"]))
    ordered_edges = sorted(edges.values(), key=lambda e: (e["kind"], e["src"], e["dst"]))
    return {
        "meta": {
            "schema_version": version,
            "node_kinds": sorted({n["kind"] for n in ordered_nodes}),
            "edge_kinds": sorted({e["kind"] for e in ordered_edges}),
            "counts": {k: sum(1 for n in ordered_nodes if n["kind"] == k) for k in sorted({n["kind"] for n in ordered_nodes})},
            "edge_counts": {k: sum(1 for e in ordered_edges if e["kind"] == k) for k in sorted({e["kind"] for e in ordered_edges})},
            "note": "calls are best effort (static names only); imports, tables, routes and events are exact",
        },
        "nodes": ordered_nodes,
        "edges": ordered_edges,
    }


def _resolve_call(m: _Module, owner: str, func: ast.expr, local_top: set[str], by_id: dict[str, _Module]) -> str | None:
    """The node id a call lands on, if it lands on something defined in this repo."""
    cls = owner.split(":", 1)[1].split(".")[0] if ":" in owner else None
    if isinstance(func, ast.Name):
        name = func.id
        if name in local_top:
            return f"{m.id}:{name}"
        got = m.names.get(name)
        if got and ":" in got:
            mod, sym = got.split(":", 1)
            return f"{mod}:{sym}" if sym in by_id[mod].symbols else None
        return None
    if isinstance(func, ast.Attribute):
        attr = func.attr
        base = func.value
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls") and cls and f"{cls}.{attr}" in m.symbols:
                return f"{m.id}:{cls}.{attr}"
            got = m.names.get(base.id)
            if got and ":" not in got and got in by_id and attr in by_id[got].symbols:
                return f"{got}:{attr}"
            if got and ":" in got:                          # a class imported by name: Class.method
                mod, sym = got.split(":", 1)
                if f"{sym}.{attr}" in by_id[mod].symbols:
                    return f"{mod}:{sym}.{attr}"
            if base.id in local_top and f"{base.id}.{attr}" in m.symbols:
                return f"{m.id}:{base.id}.{attr}"
    return None


# ---------------------------------------------------------------------------------------------
# queries over a built graph


class Graph:
    def __init__(self, data: dict):
        self.data = data
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.out: dict[str, list[dict]] = defaultdict(list)
        self.into: dict[str, list[dict]] = defaultdict(list)
        for e in data["edges"]:
            self.out[e["src"]].append(e)
            self.into[e["dst"]].append(e)
        self.members: dict[str, list[dict]] = defaultdict(list)       # module id -> its symbols
        for n in data["nodes"]:
            if n["kind"] in ("function", "class", "method", "constant"):
                self.members[n["module"]].append(n)

    def find(self, query: str) -> list[dict]:
        """Exact id first, then exact name, then a unique-looking suffix, then substring."""
        if query in self.nodes:
            return [self.nodes[query]]
        for test in (lambda n: n["name"] == query,
                     lambda n: n["id"].endswith(":" + query) or n["id"].endswith("." + query) or n["id"].endswith(" " + query),
                     lambda n: query.lower() in n["id"].lower()):
            hit = [n for n in self.nodes.values() if n["kind"] != "rule" and test(n)]
            if hit:
                return sorted(hit, key=lambda n: (n["kind"] != "module", n["id"]))
        return []

    def describe(self, node: dict, limit: int = 40) -> str:
        lines = [f"{node['kind']}  {node['id']}"]
        if node.get("path"):
            where = node["path"] + (f":{node['line']}" if node.get("line") else "")
            lines.append(f"  at {where}")
        if node.get("signature"):
            lines.append(f"  {node['signature']}")
        if node.get("doc"):
            lines.append(f"  {node['doc']}")
        if node["kind"] == "table":
            lines.append("  columns: " + ", ".join(c["name"] + ("*" if c["pk"] else "") for c in node["columns"]))
        groups: dict[str, list[str]] = defaultdict(list)
        if node["kind"] == "module":
            groups["defines"] = [m["signature"] if m.get("signature") else m["name"] for m in self.members.get(node["id"], [])]
        for e in self.out.get(node["id"], []):
            groups[f"-> {e['kind']}"].append(self._label(e["dst"], e))
        for e in self.into.get(node["id"], []):
            groups[f"<- {e['kind']}"].append(self._label(e["src"], e))
        for key in sorted(groups):
            items = sorted(set(groups[key]))
            more = f"  (+{len(items) - limit} more)" if len(items) > limit else ""
            lines.append(f"  {key} ({len(items)}): " + ", ".join(items[:limit]) + more)
        return "\n".join(lines)

    @staticmethod
    def _label(node_id: str, e: dict) -> str:
        return node_id + (" (lazy)" if e.get("lazy") else "")


def stable(data: dict) -> dict:
    """The graph without the fields that change on every edit (line numbers, file sizes).

    The freshness test compares these, so adding a blank line to a file does not make the committed
    map "stale"; adding a function, a call, a table or a route does.
    """
    out = json.loads(json.dumps(data))
    for n in out["nodes"]:
        for k in ("line", "end_line", "lines"):
            n.pop(k, None)
    return out


def dumps(data: dict) -> str:
    """The graph as JSON with one node or edge per line: compact, and a diff shows what changed."""
    def one(x) -> str:
        return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    parts = ['{"meta":' + one(data["meta"]) + ',"nodes":[']
    parts.append(",\n".join(one(n) for n in data["nodes"]))
    parts.append('],"edges":[')
    parts.append(",\n".join(one(e) for e in data["edges"]))
    parts.append("]}")
    return "\n".join(parts) + "\n"

"""
The code graph (`src/codegraph.py`, `docs/CODEMAP.md`, `docs/codegraph/graph.json`).

Three kinds of test, because a map can be wrong in three different ways:

* **The extractor, on code written for the purpose.** A tiny repo is built in a temp directory
  and every edge kind is checked there, *including what must not become an edge* (a docstring that
  mentions a table, a call that resolves to nothing, a DELETE that is not a read). The real repo is
  too big to say which of its 700 edges is the wrong one.
* **The real repo against an independent source.** FastAPI knows its own routes; the migrations
  know their own tables. A route the extractor missed shows up as a difference.
* **Freshness.** The committed map must equal what the code produces now. This is the test that
  fails after you add a function and forget to run `python scripts/code_graph.py`, which is the
  point: a map that is not kept current is worse than no map, because it is believed.
"""

import json
import re
import textwrap
from pathlib import Path

import pytest

from src import codegraph
from src.codegraph_render import render

HERE = Path(__file__).resolve().parent.parent


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """A small repo exercising every edge kind, built once."""
    root = tmp_path_factory.mktemp("tiny")
    write(root, "src/__init__.py", "")
    write(root, "src/util.py", '''
        """Helpers."""

        LIMIT = 5
        PATH = "/api/ping"

        def helper(x):
            """Does a thing."""
            return x

        class Box:
            def open(self):
                return self.shut()

            def shut(self):
                return 1
    ''')
    write(root, "src/applog.py", '''
        def event(name, *a, **k): ...
        def warning(name, *a, **k): ...
    ''')
    write(root, "src/work.py", '''
        """Does the work."""
        from src import util, applog
        from src.util import helper as h

        def local():
            return 1

        def run(conn, flag):
            local()                       # a local def
            h(1)                          # an imported name, aliased
            util.helper(2)                # an imported module's attribute
            len([1])                      # a builtin: no edge
            handlers = {"a": local}
            handlers["a"]()               # dynamic dispatch: no edge
            def inner():
                util.helper(3)            # belongs to run, not to a node of its own
            inner()
            conn.execute("SELECT * FROM activities WHERE id = 1")
            conn.execute("INSERT INTO wellness (date) VALUES (?)", (1,))
            conn.execute("DELETE FROM sync_log WHERE id = 1")
            conn.execute("INSERT INTO events SELECT * FROM activities")
            conn.execute("SELECT * FROM not_a_table")
            applog.warning("work.done", n=1)
            (applog.warning if flag else applog.event)("work.either", n=1)
            applog.event("not an event name")

        def docstring_only():
            """Mentions that we update activities and select from wellness; it does neither."""

        def late():
            from src import util as u2    # a function-level import is lazy
            return u2.LIMIT

        class Thing(util.Box):
            def go(self):
                return self.stop()

            def stop(self):
                return local()
    ''')
    write(root, "src/api/__init__.py", '"""The HTTP layer."""\n')
    write(root, "src/api/routes.py", '''
        from fastapi import APIRouter, FastAPI
        from src.util import PATH

        router = APIRouter(prefix="/api/things")
        app = FastAPI()

        @router.get("/{thing_id}")
        def one(thing_id): ...

        @router.post("/{thing_id}/open")
        def open_it(thing_id): ...

        @app.get(PATH)
        def ping(): ...

        @app.get("/")
        def home(): ...
    ''')
    write(root, "src/web/index.html", '''
        <script>
        fetch("/api/ping");
        api("GET", `/api/things/${id}`);
        api("POST", "/api/things/" + id + "/open");
        </script>
    ''')
    write(root, "scripts/go.py", '"""Run it."""\nfrom src import work\nwork.run(None, False)\n')
    write(root, "tests/test_work.py", "from src import work\nfrom src.api import routes\n")
    write(root, "CLAUDE.md", '''
        # Project

        ## Work has one door

        Everything goes through `src/work.py`; `tests/test_work.py` guards it. Not this: `src/nowhere.py`.

        ## A section naming nothing

        Plain words.
    ''')
    return codegraph.build(root)


def edges(data, kind):
    return {(e["src"], e["dst"]) for e in data["edges"] if e["kind"] == kind}


def node(data, node_id):
    return next(n for n in data["nodes"] if n["id"] == node_id)


# --------------------------------------------------------------------------------- extractor


def test_imports_are_module_edges_and_lazy_only_inside_a_function(tiny):
    got = {(e["src"], e["dst"]): e["lazy"] for e in tiny["edges"] if e["kind"] == "imports"}
    assert got[("src.work", "src.util")] is False, "a module-level import is not lazy, even if the same module is also imported lazily"
    assert got[("src.work", "src.applog")] is False
    assert ("scripts.go", "src.work") in got


def test_a_function_level_import_alone_is_marked_lazy(tmp_path):
    write(tmp_path, "src/a.py", "X = 1\n")
    write(tmp_path, "src/b.py", "def f():\n    from src import a\n    return a.X\n")
    data = codegraph.build(tmp_path)
    (edge,) = [e for e in data["edges"] if e["kind"] == "imports" and e["src"] == "src.b"]
    assert edge["dst"] == "src.a" and edge["lazy"] is True


def test_a_module_level_import_wins_over_an_earlier_function_level_one(tmp_path):
    """Order in the file must not decide it: lazy first, hard later is still a hard edge."""
    write(tmp_path, "src/a.py", "X = 1\n")
    write(tmp_path, "src/b.py", "def f():\n    from src import a\n    return a.X\n\nfrom src import a\n")
    data = codegraph.build(tmp_path)
    (edge,) = [e for e in data["edges"] if e["kind"] == "imports" and e["src"] == "src.b"]
    assert edge["lazy"] is False


def test_calls_resolve_only_to_names_defined_in_the_repo(tiny):
    calls = edges(tiny, "calls")
    assert ("src.work:run", "src.work:local") in calls
    assert ("src.work:run", "src.util:helper") in calls            # via `h` and via `util.helper`, one edge
    weight = next(e["weight"] for e in tiny["edges"] if e["kind"] == "calls" and (e["src"], e["dst"]) == ("src.work:run", "src.util:helper"))
    assert weight == 3, "h(1), util.helper(2) and the call inside the nested function all land on the same edge"
    assert not any("len" in dst for _, dst in calls), "a builtin is not a node"
    assert not any(src.endswith(":inner") for src, _ in calls), "a nested function is not its own owner"


def test_a_name_used_without_being_called_is_a_refers_edge_so_dispatch_tables_are_seen(tiny):
    refers = edges(tiny, "refers")
    assert ("src.work:run", "src.work:local") in refers, "`handlers = {'a': local}` uses local without calling it"
    assert ("src.work:late", "src.util:LIMIT") in refers, "reading another module's constant through an alias"
    assert ("src.work:run", "src.util:helper") not in refers, "a call is a calls edge, not also a refers edge"


def test_self_calls_resolve_to_the_same_class_and_methods_are_owned_by_their_class(tiny):
    calls = edges(tiny, "calls")
    assert ("src.util:Box.open", "src.util:Box.shut") in calls
    assert ("src.work:Thing.go", "src.work:Thing.stop") in calls
    assert ("src.work:Thing.stop", "src.work:local") in calls


def test_sql_in_strings_reads_and_writes_known_tables(tiny):
    reads, writes = edges(tiny, "reads"), edges(tiny, "writes")
    assert ("src.work:run", "table:activities") in reads
    assert ("src.work:run", "table:wellness") in writes
    assert ("src.work:run", "table:events") in writes
    assert ("src.work:run", "table:sync_log") in writes
    assert ("src.work:run", "table:wellness") not in reads
    assert ("src.work:run", "table:sync_log") not in reads, "DELETE FROM names a table without reading it"
    assert not any(dst == "table:not_a_table" for _, dst in reads | writes)


def test_a_docstring_that_mentions_a_table_is_not_a_use_of_it(tiny):
    assert not any(src == "src.work:docstring_only" for src, _ in edges(tiny, "reads") | edges(tiny, "writes"))


def test_events_come_from_applog_calls_with_a_dotted_name_including_the_conditional_form(tiny):
    emits = edges(tiny, "emits")
    assert ("src.work:run", "event:work.done") in emits
    assert ("src.work:run", "event:work.either") in emits
    assert not any("not an event name" in dst for _, dst in emits)


def test_routes_get_their_router_prefix_and_a_constant_path_is_resolved(tiny):
    names = {n["name"] for n in tiny["nodes"] if n["kind"] == "route"}
    assert names == {"GET /api/things/{thing_id}", "POST /api/things/{thing_id}/open", "GET /api/ping", "GET /"}
    assert node(tiny, "route:POST /api/things/{thing_id}/open")["guarded"] is True
    assert node(tiny, "route:GET /api/ping")["guarded"] is False
    assert ("src.api.routes:one", "route:GET /api/things/{thing_id}") in edges(tiny, "serves")


def test_the_page_reaches_routes_by_exact_path_template_literal_and_concatenated_prefix(tiny):
    reached = {dst for _, dst in edges(tiny, "calls_api")}
    assert "route:GET /api/ping" in reached
    assert "route:GET /api/things/{thing_id}" in reached
    assert "route:POST /api/things/{thing_id}/open" in reached
    assert "route:GET /" not in reached


def test_a_test_file_is_a_node_with_tests_edges_but_its_functions_are_not_nodes(tiny):
    assert ("tests.test_work", "src.work") in edges(tiny, "tests")
    assert ("tests.test_work", "src.api.routes") in edges(tiny, "tests")
    assert not any(n["id"].startswith("tests.test_work:") for n in tiny["nodes"])


def test_a_claude_md_section_governs_the_files_it_names_and_only_real_ones(tiny):
    governs = edges(tiny, "governs")
    assert ("rule:Work has one door", "src.work") in governs
    assert ("rule:Work has one door", "tests.test_work") in governs
    assert not any("nowhere" in dst for _, dst in governs)


def test_symbols_carry_kind_signature_and_privacy(tiny):
    helper = node(tiny, "src.util:helper")
    assert helper["kind"] == "function" and helper["signature"] == "helper(x)" and helper["doc"] == "Does a thing."
    assert node(tiny, "src.util:Box.open")["kind"] == "method"
    assert node(tiny, "src.util:LIMIT")["kind"] == "constant"
    assert node(tiny, "src.work:local")["public"] is True


def test_packages_are_named_by_their_directory(tiny):
    assert node(tiny, "src.api.routes")["package"] == "src.api"
    assert node(tiny, "src.work")["package"] == "src"
    assert node(tiny, "scripts.go")["package"] == "scripts"


def test_stable_strips_line_numbers_so_an_edit_that_moves_code_does_not_stale_the_map(tiny):
    flat = codegraph.stable(tiny)
    assert all("line" not in n and "lines" not in n and "end_line" not in n for n in flat["nodes"])
    assert any("line" in n for n in tiny["nodes"])


def test_find_prefers_exact_then_name_then_suffix(tiny):
    g = codegraph.Graph(tiny)
    assert [n["id"] for n in g.find("src.work:run")] == ["src.work:run"]
    assert [n["id"] for n in g.find("helper")][0] == "src.util:helper"
    assert g.find("zzz-nothing") == []
    text = g.describe(g.find("src.work")[0])
    assert "imports" in text and "defines" in text and "run(conn, flag)" in text


def test_the_json_writer_is_valid_json_one_record_per_line(tiny):
    text = codegraph.dumps(codegraph.stable(tiny))
    back = json.loads(text)
    assert len(back["nodes"]) == len(tiny["nodes"]) and len(back["edges"]) == len(tiny["edges"])
    assert text.count("\n") > len(tiny["nodes"]), "one record per line, so a diff reads as a diff"


# ----------------------------------------------------------------------------------- the repo


@pytest.fixture(scope="module")
def real():
    return codegraph.build()


def test_every_fastapi_route_is_in_the_graph_and_nothing_else_is():
    """FastAPI is the oracle: the graph's routes come from decorators, its own list from the running app."""
    from src.api import guard
    from src.api.app import app

    ours = {n["name"] for n in codegraph.build()["nodes"] if n["kind"] == "route"}
    theirs = set()
    for r in guard.routes(app):
        if r.path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        for method in getattr(r, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            theirs.add(f"{method} {r.path}")
    assert ours == theirs, f"only in graph: {sorted(ours - theirs)}; only in FastAPI: {sorted(theirs - ours)}"


def test_every_table_the_migrations_create_is_a_node_with_its_columns(real):
    from src.db import migrations

    tables = {n["name"]: n for n in real["nodes"] if n["kind"] == "table"}
    assert {"activities", "plan_progression", "signals", "sync_log"} <= set(tables)
    assert any(c["name"] == "id" and c["pk"] for c in tables["plan_progression"]["columns"])
    assert real["meta"]["schema_version"] == migrations.LATEST_VERSION


def test_a_function_that_writes_a_table_is_found_where_the_code_does_it(real):
    writes = {(e["src"], e["dst"]) for e in real["edges"] if e["kind"] == "writes"}
    assert ("src.planning.progression_store:record_week", "table:plan_progression") in writes


def test_every_module_in_the_tree_is_a_node_and_nothing_imports_a_module_that_is_not(real):
    ids = {n["id"] for n in real["nodes"] if n["kind"] == "module"}
    on_disk = {codegraph.module_id(p, HERE) for p in codegraph._py_files(HERE)}
    assert ids == on_disk
    for e in real["edges"]:
        if e["kind"] == "imports":
            assert e["dst"] in ids


def test_building_twice_gives_the_same_bytes():
    a = codegraph.dumps(codegraph.stable(codegraph.build()))
    b = codegraph.dumps(codegraph.stable(codegraph.build()))
    assert a == b


def test_building_the_graph_leaves_the_application_log_alone(tmp_path, monkeypatch):
    """Running the migrations to read the schema logs `db.migrated`; that must land in a scratch log, not yours."""
    monkeypatch.setenv("FITNESS_COACH_LOG_DIR", str(tmp_path / "mine"))
    codegraph.build()
    assert not (tmp_path / "mine").exists() or not list((tmp_path / "mine").glob("*.jsonl"))
    assert codegraph.os.environ["FITNESS_COACH_LOG_DIR"] == str(tmp_path / "mine"), "and the setting is put back"


def test_the_committed_map_is_current():
    """Fails after you change the structure and do not regenerate. The fix is the command in the message."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("code_graph_script", HERE / "scripts" / "code_graph.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    old = script.stale()
    assert not old, (
        "the code map is out of date: " + ", ".join(p.name for p in old)
        + "\nrun:  .venv/Scripts/python.exe scripts/code_graph.py   and commit docs/"
    )


def test_the_rendered_map_has_no_line_numbers_to_go_stale(real):
    text = render(codegraph.stable(real))
    assert not re.search(r"\.py:\d+", text), "a path:line reference would change on every edit"

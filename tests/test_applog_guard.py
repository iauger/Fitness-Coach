"""
Two guards on how the log is used, both greps over the source because both behaviours look fine in
isolation.

1. **A broad `except` in a module that runs unattended must say so.** The sync, the maintenance loop,
   the coach turn and the API each had handlers that swallowed or returned a failure, and that is how
   a whole class of problems went unseen until someone read the code. A handler that catches
   `Exception` must log it (`applog.`), re-raise, or carry a comment `# not logged:` giving the reason.

2. **No call site passes a field the logger would refuse.** The logger drops words at run time, but a
   call that tries to log a note is a mistake to catch in review, not to rely on a filter for.
"""

import ast
from pathlib import Path

import pytest

from src import applog

ROOT = Path(__file__).parent.parent

UNATTENDED = [
    "src/sync.py", "src/maintenance.py", "src/coach/stream.py", "src/coach/memory.py",
    "src/coach/tools.py", "src/coach/extract.py", "src/coach/session.py", "src/api/app.py",
    "src/api/chat.py", "src/api/jobs.py", "src/history_fetch.py", "src/db/migrations.py",
    "src/analysis/report.py", "src/clock.py", "src/applog.py", "src/support.py",
]


def broad_handlers(path: Path):
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler):
            kind = node.type
            if kind is None or (isinstance(kind, ast.Name) and kind.id in ("Exception", "BaseException")):
                yield node, source


def says_something(node: ast.ExceptHandler, source: str) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Raise):
            return True
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and isinstance(inner.func.value, ast.Name) and inner.func.value.id == "applog"):
            return True
    return "# not logged" in "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])


@pytest.mark.parametrize("module", UNATTENDED)
def test_every_broad_handler_logs_re_raises_or_says_why_not(module):
    silent = [f"{module}:{node.lineno}" for node, source in broad_handlers(ROOT / module)
              if not says_something(node, source)]
    assert silent == [], (
        f"a handler that catches Exception and records nothing: {silent}. Call applog.warning/error "
        f"with the exception, re-raise it, or add `# not logged: <why>` on the except line.")


def test_the_guard_finds_handlers_at_all():
    """Mutant: a walker that matches nothing, which would make the guard above pass for ever."""
    assert sum(1 for m in UNATTENDED for _ in broad_handlers(ROOT / m)) >= 25


def test_a_silent_handler_would_be_caught(tmp_path):
    bad = tmp_path / "m.py"
    bad.write_text("try:\n    1\nexcept Exception:\n    pass\n", encoding="utf-8")
    ((node, source),) = list(broad_handlers(bad))
    assert not says_something(node, source)
    ok = tmp_path / "n.py"
    ok.write_text("try:\n    1\nexcept Exception:   # not logged: it is the probe\n    pass\n", encoding="utf-8")
    ((node, source),) = list(broad_handlers(ok))
    assert says_something(node, source)


def test_no_applog_call_passes_a_field_the_logger_drops():
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "applog"
                    and node.func.attr in ("event", "warning", "error")):
                offenders += [f"{path.relative_to(ROOT)}:{node.lineno} passes {kw.arg}="
                              for kw in node.keywords if kw.arg in applog.DENY]
    assert offenders == []

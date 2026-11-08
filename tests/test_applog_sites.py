"""
The places that write to the application log, and what they leave out of it.

The coach turn is the one that matters most to get right: it is the usage data (tokens, tools, whether
a button was offered) and also the place a question or a reply could most easily leak. These tests
run the real `stream_turn` against a fake client, which no test did before, and read the file back.

Mutants: a turn that logs nothing, or twice; token counts that are not summed across iterations; a
turn that is cut off, fails, is abandoned or runs away logged as a success; the question or the
answer present in the file; a sync failure or a skipped record that goes unrecorded; a migration that
is not noted; an unhandled API exception that leaves no trace; an extractor result without its counts.
"""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src import applog
from src import sync
from src.api import setup
from src.api.app import app
from src.coach import extract, stream, tools
from src.db.migrations import MIGRATIONS, migrate
from test_migration_backup import make_old
from test_sync import ride, world  # noqa: F401  (a fixture)

QUESTION = "Why does my SECRETQUESTION knee hurt?"
ANSWER = "Your SECRETANSWER tendon is overloaded."


def events(name=None):
    rows = applog.read()
    return [r for r in rows if name is None or r["event"] == name]


def raw_log() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in applog.files())


# -- a fake model, enough to drive stream_turn ----------------------------------------------------

class Block:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class Msg:
    def __init__(self, stop_reason, content, tokens=(100, 20)):
        self.stop_reason, self.content = stop_reason, content
        self.usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])


class Stream:
    def __init__(self, texts, msg):
        self.text_stream, self._msg = iter(texts), msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._msg


class Client:
    def __init__(self, *turns, fail=None):
        self.turns, self.fail = list(turns), fail
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        if self.fail:
            raise self.fail
        texts, msg = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        return Stream(texts, msg)


def tool_turn(name="suggest_change", tokens=(100, 20)):
    return ([], Msg("tool_use", [Block("tool_use", name=name, input={"setting": "x"}, id="t1")], tokens))


def text_turn(stop="end_turn", tokens=(300, 40)):
    return ([ANSWER], Msg(stop, [Block("text", text=ANSWER)], tokens))


def run(client, **kw):
    return list(stream.stream_turn(client, "test-model", "system", [{"role": "user", "content": QUESTION}],
                                   100, tools=[], **kw))


@pytest.fixture(autouse=False)
def shown(monkeypatch):
    monkeypatch.setattr(stream, "execute_tool", lambda name, inp: '{"status": "shown"}')


# -- the coach turn --------------------------------------------------------------------------------

def test_a_completed_turn_is_one_event_with_summed_tokens_tools_and_whether_a_button_was_offered(shown):
    run(Client(tool_turn(tokens=(100, 20)), text_turn(tokens=(300, 40))))
    (row,) = events("coach.turn")
    assert row["level"] == "info" and row["ended"] == "done" and row["model"] == "test-model"
    assert (row["iterations"], row["input_tokens"], row["output_tokens"]) == (2, 400, 60)
    assert row["tools"] == ["suggest_change"] and row["suggested"] is True
    assert isinstance(row["ms"], int)


def test_a_turn_that_offers_no_button_says_so(monkeypatch):
    monkeypatch.setattr(stream, "execute_tool", lambda name, inp: '{"error": "no"}')
    run(Client(tool_turn(), text_turn()))
    assert events("coach.turn")[0]["suggested"] is False


def test_the_question_and_the_answer_never_reach_the_file(shown):
    """Mutant: any field carrying the conversation. This is the leak that matters."""
    run(Client(tool_turn(), text_turn()))
    text = raw_log()
    assert "SECRETQUESTION" not in text and "SECRETANSWER" not in text


def test_a_turn_cut_off_at_max_tokens_is_a_warning_not_a_success():
    run(Client(text_turn(stop="max_tokens")))
    (row,) = events("coach.turn")
    assert row["level"] == "warning" and row["ended"] == "max_tokens"


def test_a_turn_that_fails_is_an_error_with_the_exception_type_and_place():
    run(Client(fail=RuntimeError("the api is down")))
    (row,) = events("coach.turn")
    assert row["level"] == "error" and row["ended"] == "error"
    assert row["exc_type"] == "RuntimeError" and row["at"]


def test_a_turn_the_consumer_walks_away_from_is_recorded_as_abandoned(shown):
    gen = stream.stream_turn(Client(tool_turn(), text_turn()), "m", "s", [], 100, tools=[])
    next(gen)
    gen.close()
    (row,) = events("coach.turn")
    assert row["level"] == "warning" and row["ended"] == "abandoned"


def test_a_runaway_turn_is_recorded_as_hitting_the_iteration_limit(shown, monkeypatch):
    monkeypatch.setattr(stream, "MAX_ITERATIONS", 3)
    run(Client(tool_turn()))
    (row,) = events("coach.turn")
    assert row["ended"] == "iteration_limit" and row["iterations"] == 3 and row["level"] == "warning"


def test_exactly_one_event_per_turn(shown):
    run(Client(tool_turn(), tool_turn(), text_turn()))
    assert len(events("coach.turn")) == 1


# -- a tool that raises ----------------------------------------------------------------------------

def test_a_tool_that_raises_is_logged_by_name_and_not_by_its_input(monkeypatch):
    monkeypatch.setitem(tools.TOOL_FN_MAP, "boom", lambda **kw: (_ for _ in ()).throw(ValueError("it broke")))
    out = json.loads(tools.execute_tool("boom", {"note": "SECRETNOTE"}))
    assert out == {"error": "it broke"}
    (row,) = events("coach.tool_error")
    assert row["tool"] == "boom" and row["exc_type"] == "ValueError"
    assert "SECRETNOTE" not in raw_log()


# -- sync --------------------------------------------------------------------------------------------

def test_a_failed_activities_fetch_is_an_error_and_the_sync_summary_is_a_warning(world):
    world.client(fail_activities=True)
    sync.incremental_sync(silent=True)
    assert [r["event"] for r in events() if r["level"] == "error"] == ["sync.activities_failed"]
    (done,) = events("sync.done")
    assert done["level"] == "warning" and done["problems"] >= 1 and done["recorded_decisions"] is False


def test_a_clean_sync_is_one_info_line_with_counts(world):
    world.client(activities=[ride("a1")])
    sync.incremental_sync(silent=True)
    (done,) = events("sync.done")
    assert done["level"] == "info" and done["activities"] == 1 and done["problems"] == 0
    assert done["recorded_decisions"] is True
    assert not [r for r in events() if r["level"] in ("warning", "error")]


def test_a_skipped_record_is_a_warning_with_a_count_and_no_ids(world):
    world.client(activities=[ride("a1"), {"id": "bad", "start_date_local": None}])
    sync.incremental_sync(silent=True)
    (row,) = events("sync.records_skipped")
    assert row["level"] == "warning" and row["count"] == 1 and "bad" not in json.dumps(row)


# -- migrations --------------------------------------------------------------------------------------

def test_a_first_migration_is_noted_with_the_versions_it_crossed(tmp_path):
    migrate(tmp_path / "t.db")
    (row,) = events("db.migrated")
    assert (row["from_version"], row["to_version"], row["steps"]) == (0, MIGRATIONS[-1][0], len(MIGRATIONS))
    assert row["backed_up"] is False


def test_a_migration_of_a_database_with_history_notes_that_it_was_backed_up(tmp_path):
    path = tmp_path / "t.db"
    make_old(path)
    migrate(path)
    assert events("db.migrated")[-1]["backed_up"] is True


def test_a_migration_that_fails_is_an_error_and_still_raises(tmp_path, monkeypatch):
    from src.db import migrations

    def boom(conn):
        raise RuntimeError("step blew up")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "boom", boom)])
    with pytest.raises(RuntimeError):
        migrate(tmp_path / "t.db")
    (row,) = events("db.migration_failed")
    assert row["version"] == 1 and row["exc_type"] == "RuntimeError"


# -- the API ----------------------------------------------------------------------------------------

def test_an_unhandled_exception_in_a_route_is_logged_and_answered_with_a_plain_500(monkeypatch):
    def boom():
        raise RuntimeError("secret detail for i123456")

    monkeypatch.setattr(setup, "current", boom)
    client = TestClient(app, base_url="http://localhost:8800", raise_server_exceptions=False)
    r = client.get("/api/settings")
    assert r.status_code == 500 and "secret detail" not in r.text and "recorded" in r.text
    (row,) = events("api.unhandled")
    assert (row["method"], row["route"], row["exc_type"]) == ("GET", "/api/settings", "RuntimeError")
    assert "i123456" not in raw_log()


# -- the extractor ------------------------------------------------------------------------------------

def test_an_extraction_is_logged_as_counts_with_what_the_quote_guard_rejected():
    text = "My left knee has been aching on the long ride."
    raw = {"signals": [{"kind": "injury_pain", "topic": "left knee", "severity": 2,
                        "quote": "left knee has been aching"},
                       {"kind": "poor_sleep", "topic": "sleep", "severity": 2, "quote": "never said this"}],
           "followups": [], "gist": "SECRETGIST"}
    reply = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=extract.TOOL, input=raw)],
                            usage=SimpleNamespace(input_tokens=500, output_tokens=60))
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply))
    extract.extract(text, "2026-11-10", client, model="m", open_threads=[])
    (row,) = events("extract.done")
    assert (row["signals"], row["rejected"], row["input_tokens"], row["output_tokens"]) == (1, 1, 500, 60)
    assert "SECRETGIST" not in raw_log() and "left knee" not in raw_log()


# -- the maintenance wrapper ----------------------------------------------------------------------

def test_a_maintenance_cycle_is_one_info_line_when_nothing_went_wrong(monkeypatch):
    from src import maintenance
    monkeypatch.setattr(maintenance, "_cycle", lambda *a, **k: {
        "errors": [], "push": {"pushed": True, "created": 4, "deleted": 4, "orphans_removed": 2}})
    maintenance.run_cycle()
    (row,) = events("maintenance.cycle")
    assert row["level"] == "info" and row["problems"] == 0
    assert (row["pushed"], row["created"], row["deleted"], row["orphans_removed"]) == (True, 4, 4, 2)
    assert isinstance(row["ms"], int)


def test_a_maintenance_cycle_with_errors_is_a_warning_with_their_count(monkeypatch):
    """Mutant: errors logged at info. The unattended loop is where a problem must not look like a success."""
    from src import maintenance
    monkeypatch.setattr(maintenance, "_cycle", lambda *a, **k: {"errors": ["push: down", "cycle: x"]})
    maintenance.run_cycle()
    (row,) = events("maintenance.cycle")
    assert row["level"] == "warning" and row["problems"] == 2


def test_a_cycle_that_finds_another_running_says_so_and_is_not_an_error(monkeypatch):
    from src import maintenance
    monkeypatch.setattr(maintenance, "_cycle", lambda *a, **k: {"errors": [], "skipped": "lock held"})
    maintenance.run_cycle()
    (row,) = events("maintenance.skipped")
    assert row["level"] == "info" and not events("maintenance.cycle")

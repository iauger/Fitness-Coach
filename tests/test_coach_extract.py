"""
The extractor's guards. The model is never called here; its answer is faked, because what is
under test is what the code does with a wrong or invented answer.

Mutants these exist to catch: a quote that need not appear in the text (the invented-signal
defence); a quote check that is case- or whitespace-sensitive (real signals dropped); a bad
severity repaired instead of refused; a future date kept as the day observed; a malformed date
that sinks the whole signal; a gist or follow-up list left unbounded; an extractor that files the
same source twice.
"""

import functools
from types import SimpleNamespace

import pytest

from src import signals
from src.coach import extract
from src.db import schema
from src.db.migrations import migrate

TODAY = "2026-11-10"
TEXT = "My left knee has been aching on the long ride.\n\nSlept badly all week, honestly."


def item(**kw):
    base = {"kind": "injury_pain", "topic": "left knee", "severity": 2,
            "quote": "left knee has been aching"}
    return {**base, **kw}


def parse(*items, **extra):
    return extract.parse({"signals": list(items), "followups": [], "gist": "g", **extra}, TEXT, TODAY)


# -- the quote guard ----------------------------------------------------------------------------

def test_a_quote_in_the_text_is_accepted():
    assert [s["kind"] for s in parse(item()).signals] == ["injury_pain"]


def test_a_quote_not_in_the_text_is_rejected_and_reported():
    """Mutant: no quote check. The model's inventions would be filed as the athlete's words."""
    got = parse(item(quote="knee is destroyed"))
    assert got.signals == [] and got.rejected[0]["why"] == "quote is not in the text"


def test_the_quote_check_ignores_case_whitespace_and_typographic_punctuation():
    assert extract.quoted("LEFT   knee\nhas been aching", TEXT)
    assert extract.quoted("it’s fine", "Honestly it's fine, really")
    assert extract.quoted("aching on the long ride.", TEXT)           # a trailing full stop is not the claim


def test_a_quote_that_is_too_short_to_mean_anything_is_rejected():
    assert not extract.quoted("my", TEXT) and not extract.quoted("", TEXT) and not extract.quoted("...", TEXT)


def test_a_quote_stitched_from_two_places_is_rejected():
    assert not extract.quoted("knee has been aching all week", TEXT)


# -- the rest of the guards ---------------------------------------------------------------------

def test_an_unknown_kind_becomes_other_and_is_kept():
    assert parse(item(kind="sore_toes")).signals[0]["kind"] == "other"


@pytest.mark.parametrize("bad", [4, -1, "high", None, 1.5])
def test_a_bad_severity_is_refused_not_repaired(bad):
    """Mutant: clamp. A model that says 7 has not told us it means 3."""
    got = parse(item(severity=bad))
    assert got.signals == [] and len(got.rejected) == 1


def test_a_future_date_is_a_plan_not_a_sighting():
    got = parse(item(kind="travel", quote="left knee has been aching", on_date="2026-11-20")).signals[0]
    assert got["on_date"] == TODAY and got["start_date"] == "2026-11-20"


def test_a_stated_start_date_is_not_overwritten_by_a_future_on_date():
    got = parse(item(on_date="2026-11-20", start_date="2026-11-18")).signals[0]
    assert got["start_date"] == "2026-11-18"


def test_a_past_date_is_kept_as_the_day_the_signal_is_about():
    assert parse(item(on_date="2026-11-03")).signals[0]["on_date"] == "2026-11-03"


def test_a_malformed_date_loses_the_date_not_the_signal():
    assert parse(item(on_date="last tuesday")).signals[0]["on_date"] == TODAY


def test_an_episode_ending_before_it_starts_is_rejected():
    got = parse(item(start_date="2026-11-05", end_date="2026-11-01"))
    assert got.signals == [] and "end_date" in got.rejected[0]["why"]


def test_state_defaults_to_new_and_a_bad_state_is_rejected():
    assert parse(item()).signals[0]["state"] == "new"
    assert parse(item(state="improving")).signals[0]["state"] == "improving"
    assert parse(item(state="kind of ok")).signals == []


def test_the_gist_is_cut_to_forty_words():
    got = parse(gist=" ".join(f"w{i}" for i in range(80)))
    assert got.gist.split()[0] == "w0" and len(got.gist.split()) == extract.GIST_WORDS


def test_follow_ups_are_bounded_in_number_and_length_and_blanks_dropped():
    many = [" ".join(["x"] * 60)] + ["b", "c", "d", "e", "  "]
    got = parse(followups=many)
    assert len(got.followups) == extract.MAX_FOLLOWUPS
    assert len(got.followups[0].split()) == extract.FOLLOWUP_WORDS


def test_the_same_thing_said_twice_is_filed_once_at_the_higher_severity():
    """Mutant: no de-duplication. One fever, two quotes, became two level-3 signals and a false alarm."""
    got = parse(item(kind="illness", topic="fever", severity=2, quote="left knee has been aching"),
                item(kind="illness", topic="fever", severity=3, quote="Slept badly all week"))
    assert [(s["kind"], s["severity"], s["quote"]) for s in got.signals] == [
        ("illness", 3, "left knee has been aching")]


def test_the_same_kind_with_different_topics_is_not_merged():
    got = parse(item(topic="left knee"), item(topic="right hip", quote="Slept badly all week"))
    assert len(got.signals) == 2


def test_garbage_in_the_list_is_skipped():
    assert parse("not a dict", item()).signals[0]["kind"] == "injury_pain"


def test_an_empty_filing_is_a_normal_outcome():
    got = extract.parse({"signals": [], "followups": [], "gist": ""}, TEXT, TODAY)
    assert (got.signals, got.followups, got.gist) == ([], [], "")


# -- the call -----------------------------------------------------------------------------------

class Client:
    def __init__(self, content):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.content = content

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=self.content)


def tool(raw):
    return [SimpleNamespace(type="tool_use", name="record_signals", input=raw)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "x.db"
    migrate(path)
    for name in ("read_connection", "write_connection"):
        monkeypatch.setattr(signals, name, functools.partial(getattr(schema, name), path))
    return path


def test_the_call_is_forced_to_the_one_tool_and_carries_the_day_and_the_text(db):
    client = Client(tool({"signals": [item()], "followups": [], "gist": "knee"}))
    got = extract.extract(TEXT, TODAY, client, model="m")
    (call,) = client.calls
    assert call["tool_choice"] == {"type": "tool", "name": "record_signals"} and call["model"] == "m"
    body = call["messages"][0]["content"]
    assert f"Today is {TODAY}." in body and TEXT in body and "No threads are open." in body
    assert got.signals[0]["kind"] == "injury_pain"


def test_open_threads_are_shown_so_the_model_can_continue_one(db):
    signals.record("injury_pain", topic="left knee", severity=2, on_date="2026-11-05")
    client = Client(tool({"signals": [], "followups": [], "gist": ""}))
    extract.extract(TEXT, TODAY, client, model="m")
    assert "injury_pain / left knee: new, last mentioned 2026-11-05" in client.calls[0]["messages"][0]["content"]


def test_a_reply_without_the_tool_call_is_an_error_not_an_empty_filing(db):
    """Mutant: treat it as empty. A failed call would be recorded as "nothing to file" and never retried."""
    with pytest.raises(RuntimeError):
        extract.extract(TEXT, TODAY, Client([SimpleNamespace(type="text", text="hello")]), model="m")


# -- filing -------------------------------------------------------------------------------------

def test_signals_are_filed_with_their_source_and_the_extractor_version(db):
    n = extract.file_signals(parse(item(), item(kind="poor_sleep", topic="", quote="Slept badly all week")),
                             source="conversation", source_ref="abc", observed_at="2026-11-10T08:00:00")
    got = [(s["kind"], s["source"], s["source_ref"], s["extractor_version"], s["observed_at"])
           for s in signals.timeline()]
    assert n == 2 and sorted(got) == [
        ("injury_pain", "conversation", "abc", extract.VERSION, "2026-11-10T08:00:00"),
        ("poor_sleep", "conversation", "abc", extract.VERSION, "2026-11-10T08:00:00")]


def test_a_source_already_filed_is_not_filed_again(db):
    """Mutant: no `recorded` check. A retry after a failed note write would double every signal."""
    found = parse(item())
    assert extract.file_signals(found, source="conversation", source_ref="abc") == 1
    assert extract.file_signals(parse(item()), source="conversation", source_ref="abc") == 0
    assert len(signals.timeline()) == 1


def test_a_dismissed_signal_still_counts_as_filed(db):
    """Mutant: `recorded` ignoring dismissed rows. A signal the athlete deleted would come back on retry."""
    extract.file_signals(parse(item()), source="conversation", source_ref="abc")
    signals.dismiss(signals.timeline()[0]["id"], "wrong")
    assert extract.file_signals(parse(item()), source="conversation", source_ref="abc") == 0
    assert signals.timeline() == []

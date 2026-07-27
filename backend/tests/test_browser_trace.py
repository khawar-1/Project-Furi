"""
Per-run browse trace (app/browser/trace.py) — 2026-07-26.

WHY IT EXISTS. Four live browser tasks failed in one afternoon and every
diagnosis had to be reconstructed by reading source, because the record held one
line per step:

    browse step 1: 'Buy Yonex badminton racket Online at Bes' (158 elements)
        -> {'action': 'extract', 'fields': ['name', 'price']}

That says what was DECIDED. Not what resulted, not how long it took, not which
channel decided it, not whether the extract returned anything. So the log could
not tell "the page is broken" from "our reader is blind" — and the run reported
the first when the truth was the second.

What these tests pin is therefore not "a file appears". It is that the trace
records the fields that were missing, that it closes on EVERY exit path (the run
that most needs a trace is the one that failed), and above all that it can never
affect the run it is watching.
"""
import json

import pytest

from app.agents import browser_loop  # the shim → app.browser.loop
from app.browser import trace as browse_trace

from tests.test_browser_loop import (
    FakeProvider,
    FakeSession,
    ScriptedPage,
    _el,
    _page,
)

run_browse = browser_loop.run_browse


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    """A directory this test can read back. The autouse _hermetic_browse_trace
    fixture already keeps the suite out of the real ~/.jarvis; this pins a path
    the assertions can open."""
    target = tmp_path / "traces"
    monkeypatch.setattr(browse_trace, "TRACE_DIR", target)
    return target


def _events(trace_dir):
    files = sorted(trace_dir.glob("*.jsonl"))
    assert files, "no trace file was written"
    return [json.loads(line) for line in files[-1].read_text("utf-8").splitlines()]


# ------------------------------------------------------------ it records enough
async def test_a_run_records_what_was_seen_decided_and_what_resulted(trace_dir):
    page = ScriptedPage([
        _page([_el(1, role="link", name="Target", href="/t")], url="https://s.test/", title="Home"),
        _page([_el(1, name="Done")], url="https://s.test/t", title="Target"),
    ])
    provider = FakeProvider([
        '{"action":"click","index":1}',
        '{"action":"done","reason":"opened"}',
    ])

    outcome = await run_browse(FakeSession(page), "open the target", provider)
    assert outcome.success is True

    events = _events(trace_dir)
    assert events[0]["event"] == "start"
    assert events[0]["goal"] == "open the target"
    assert events[0]["mode"] == "read"

    steps = [e for e in events if e["event"] == "step"]
    decisions = [e for e in steps if "action" in e]
    assert decisions, "no decision was recorded"
    # WHAT WAS SEEN — the three numbers whose absence made the live failure
    # unreadable: which page, how many elements, how much text.
    assert decisions[0]["url"] == "https://s.test/"
    assert decisions[0]["elements"] == 1
    assert "text_len" in decisions[0]
    # WHICH CHANNEL decided. "vision was stalling every step on cooling keys" is
    # invisible without this field — and that cost a whole live session.
    assert decisions[0]["source"] == "dom"
    # WHAT RESULTED — recorded separately, because the old log stopped at the
    # decision and a decision is not an outcome.
    assert any(e.get("result") == "ok" for e in steps)

    finish = events[-1]
    assert finish["event"] == "finish"
    assert finish["success"] is True
    assert finish["llm_calls"] == 2
    assert isinstance(finish["ms"], int)


async def test_an_extract_records_how_many_records_it_actually_got(trace_dir):
    """The single most useful missing number: on 2026-07-26 three extracts
    returned zero records and the record said nothing about it either way."""
    page = ScriptedPage([{
        "url": "https://shop.test/", "title": "Shop",
        "elements": [
            _el(1, role="item", name="Widget One Deluxe Rs. 100"),
            _el(2, role="item", name="Widget Two Deluxe Rs. 200"),
        ],
        "total": 2, "text": "shop prose",
    }])
    provider = FakeProvider([
        '{"action":"extract","fields":["name","price"]}',
        '{"action":"done","reason":"got them"}',
    ])

    await run_browse(FakeSession(page), "extract the widgets", provider)

    events = _events(trace_dir)
    extracted = [e for e in events if e.get("result") == "extracted"]
    assert extracted and extracted[0]["records"] == 2
    assert events[-1]["records"] == 2


async def test_a_fruitless_read_records_the_reason_not_just_the_failure(trace_dir):
    page = ScriptedPage([{
        "url": "https://empty.test/", "title": "Empty",
        "elements": [_el(1, name="A"), _el(2, name="B")],
        "total": 2, "text": "nothing structured here at all",
    }])
    provider = FakeProvider([
        '{"action":"extract","fields":["name"]}', "[]",
        '{"action":"extract","fields":["price"]}', "[]",
        '{"action":"extract","fields":["rating"]}', "[]",
    ])

    await run_browse(FakeSession(page), "extract the items", provider)

    events = _events(trace_dir)
    empties = [e for e in events if e.get("result") == "extracted-nothing"]
    assert empties, "an empty read left no record"
    assert "read this page" in empties[0]["note"]
    assert events[-1]["success"] is False
    assert "element list" in events[-1]["error"]


# ------------------------------------------------- it closes on EVERY exit path
async def test_a_failed_run_is_traced_too(trace_dir):
    """The run that most needs a trace is the one that failed. `finish` lives in
    _outcome — the ONE funnel every return path takes — rather than at the happy
    ending, so a hand-off, a budget exhaustion and a deadline all close it."""
    page = ScriptedPage([_page([_el(1, name="nothing useful")])])
    provider = FakeProvider(["not json at all"] * 3)

    outcome = await run_browse(FakeSession(page), "do the impossible", provider)

    assert outcome.success is False
    finish = _events(trace_dir)[-1]
    assert finish["event"] == "finish"
    assert finish["success"] is False
    assert finish["error"]


async def test_the_action_cap_still_closes_the_trace(trace_dir):
    page = ScriptedPage([_page([_el(1, role="link", name="loop", href="/a")])])
    provider = FakeProvider(['{"action":"scroll","direction":"down"}'] * 10)

    await run_browse(FakeSession(page), "wander", provider, max_actions=3)

    finish = _events(trace_dir)[-1]
    assert finish["event"] == "finish"
    assert finish["steps"] == 3


# ------------------------------------------- it can never affect what it watches
async def test_a_broken_trace_directory_does_not_break_the_run(tmp_path, monkeypatch):
    """A file where the directory should be — mkdir raises. The run must be
    completely unaffected: an observer that can fail the thing it observes is
    worse than no observer."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(browse_trace, "TRACE_DIR", blocker / "sub")

    page = ScriptedPage([_page([_el(1, name="ok")])])
    provider = FakeProvider(['{"action":"done","reason":"fine"}'])

    outcome = await run_browse(FakeSession(page), "still works", provider)
    assert outcome.success is True


async def test_tracing_off_entirely_is_supported(monkeypatch):
    monkeypatch.setattr(browse_trace, "TRACE_DIR", None)
    page = ScriptedPage([_page([_el(1, name="ok")])])
    provider = FakeProvider(['{"action":"done","reason":"fine"}'])

    outcome = await run_browse(FakeSession(page), "no trace", provider)
    assert outcome.success is True


async def test_the_trace_does_not_read_the_clock_the_deadline_uses(monkeypatch):
    """REGRESSION, and it is the reason this rule is written down. The loop's
    wall-clock-deadline test fakes `time.monotonic` to fire on its second call.
    The trace originally timed itself with the same clock, so merely asking for a
    timestamp CONSUMED the run's deadline and the run sailed past it — the trace
    changed the behaviour of the run it was watching."""
    calls = {"n": 0}
    import time as _time

    real = _time.monotonic

    def counting_monotonic():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(_time, "monotonic", counting_monotonic)
    trace = browse_trace.BrowseTrace("goal", commit=False)
    trace.mark_step_start()
    trace.step(index=0, result="ok")
    trace.finish(success=True, steps=1)
    assert calls["n"] == 0, "the trace read the loop's deadline clock"


# ------------------------------------------------------------------- bounds
def test_lines_per_run_are_bounded(trace_dir):
    trace = browse_trace.BrowseTrace("bounded", commit=False)
    for i in range(browse_trace._MAX_LINES + 50):
        trace.step(index=i, result="ok")
    lines = trace.path.read_text("utf-8").splitlines()
    assert len(lines) == browse_trace._MAX_LINES


def test_long_strings_are_clipped(trace_dir):
    trace = browse_trace.BrowseTrace("x" * 5000, commit=False)
    start = json.loads(trace.path.read_text("utf-8").splitlines()[0])
    assert len(start["goal"]) <= browse_trace._MAX_STR + 3


def test_old_traces_are_swept(trace_dir, monkeypatch):
    """An agent loop that spins must not fill a disk."""
    monkeypatch.setattr(browse_trace, "_KEEP_FILES", 3)
    for i in range(8):
        browse_trace.BrowseTrace(f"run {i}", commit=False)
    assert len(list(trace_dir.glob("*.jsonl"))) <= 4      # 3 kept + the newest


def test_commit_mode_is_recorded(trace_dir):
    trace = browse_trace.BrowseTrace("apply to the job", commit=True)
    start = json.loads(trace.path.read_text("utf-8").splitlines()[0])
    assert start["mode"] == "commit"

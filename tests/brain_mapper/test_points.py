"""The points ledger and the rule that points move only between blocks.

Real pty device, real websocket client, real files; the short flow has 2 s
blocks. Nothing here waits for a fixed time to prove a negative: it reads the
sequence of messages a client received and checks the order.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from brain_mapper import flow
import brain_mapper.engine as engine_module
from brain_mapper.engine import CommandRefused, Session
from brain_mapper.points import Ledger, PointsError
from brain_mapper.server import Client, Hub, Settings
from brain_mapper.source import LiveSource
from test_engine import (
    SHORT,
    accept,
    events_of,
    markers_of,
    next_state,
    open_ws,
    reconnect,
    run_until,
    send,
    start_block,
    to_step,
    session_folder_of,
)

BLOCKS_IN_FLOW = 14  # 2 + 2 + 2 + 2 + 2 + 1 + 1 + 1 + 1 (2-back, flanker, spatial: 2 blocks since 2026-11-25; were 3, 17 in all)


def steps_by_id():
    return {s.id: s for s in flow.default_flow()}


def lines_of(path: Path):
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.fixture
def live(pty_device, tmp_path):
    """A session on a pty ``LiveSource`` with the short flow and a ledger."""
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    ledger = Ledger(folder / "points.jsonl")
    session = Session(flow.load(SHORT), source, ledger, folder=folder)
    yield session, ledger, source, folder
    session.close()
    ledger.close()
    source.stop()


# -- the ledger on its own ---------------------------------------------------


def test_a_block_is_worth_100_points_in_both_flows():
    for steps in (flow.default_flow(), flow.load(SHORT)):
        blocks = [b for s in steps for b in s.blocks]
        assert len(blocks) == BLOCKS_IN_FLOW
        assert {b.points for b in blocks} == {100}


@pytest.mark.parametrize("step_id", ["participant", "device", "calibration", "summary"])
def test_the_ledger_refuses_an_award_for_a_setup_or_summary_step(step_id, tmp_path):
    ledger = Ledger(tmp_path / "points.jsonl")
    step = steps_by_id()[step_id]
    block = flow.Block("fake", 1.0, 5)
    with pytest.raises(PointsError, match="earns no points"):
        ledger.award(step, block, "block complete", 100)
    assert ledger.total == 0 and ledger.awards == []
    assert (tmp_path / "points.jsonl").read_text() == ""
    ledger.close()


def test_the_ledger_writes_one_line_per_award_and_survives_a_reopen(tmp_path):
    path = tmp_path / "points.jsonl"
    ledger = Ledger(path)
    words = steps_by_id()["words"]
    assert ledger.award(words, words.blocks[0], "block complete", 100) == 100
    assert ledger.award(words, words.blocks[1], "block complete", 100) == 200
    ledger.close()
    lines = lines_of(path)
    assert [x["seq"] for x in lines] == [1, 2]
    assert [x["total"] for x in lines] == [100, 200]
    assert set(lines[0]) == {
        "seq", "step", "block", "reason", "amount", "total", "unix_time", "sample",
    }  # fmt: skip
    assert (lines[0]["step"], lines[0]["block"], lines[0]["reason"]) == (
        "words",
        "list1",
        "block complete",
    )
    assert lines[0]["sample"] is None  # no source attached
    reopened = Ledger(path)
    assert reopened.total == lines[-1]["total"] == 200
    assert reopened.awards == lines
    nback = steps_by_id()["nback"]
    assert reopened.award(nback, nback.blocks[0], "block complete", 100) == 300
    reopened.close()
    assert [x["seq"] for x in lines_of(path)] == [1, 2, 3]


@pytest.mark.parametrize("amount", [-1, 2.5, "100", None, True])
def test_the_ledger_refuses_an_amount_that_is_not_a_whole_number_of_at_least_0(amount, tmp_path):
    ledger = Ledger(tmp_path / "points.jsonl")
    words = steps_by_id()["words"]
    with pytest.raises(PointsError, match="amount"):
        ledger.award(words, words.blocks[0], "block complete", amount)
    assert ledger.total == 0
    ledger.close()


def test_a_ledger_file_that_does_not_add_up_is_refused(tmp_path):
    path = tmp_path / "points.jsonl"
    path.write_text(
        '{"seq":1,"step":"words","block":"list1","reason":"r","amount":100,"total":100}\n'
        '{"seq":2,"step":"words","block":"list2","reason":"r","amount":100,"total":999}\n'
    )
    with pytest.raises(PointsError, match="line 2"):
        Ledger(path)


# -- the ledger under the engine --------------------------------------------


@pytest.mark.parametrize("phase", ["running", "paused"])
def test_an_award_during_a_running_or_paused_block_raises(phase, live):
    session, ledger, _, folder = live
    start_block(session, "words")
    if phase == "paused":
        session.pause("fullscreen_lost")
    assert session.state()["phase"] == phase
    step = session.current
    with pytest.raises(PointsError, match=f"no points while the block is {phase}"):
        ledger.award(step, step.blocks[0], "block complete", 100)
    assert ledger.total == 0
    assert (folder / "points.jsonl").read_text() == ""
    assert session.state()["points_total"] == 0


@pytest.mark.parametrize("step_id", ["participant", "device", "calibration"])
def test_an_award_during_the_setup_steps_raises_even_though_the_phase_allows_it(step_id, live):
    session, ledger, _, _ = live
    to_step(session, step_id)
    assert session.state()["phase"] == "waiting"
    step = session.current
    with pytest.raises(PointsError, match="earns no points"):
        ledger.award(step, flow.Block("b", 1.0, 100), "block complete", 100)
    assert ledger.total == 0


def test_the_engine_awards_100_when_a_block_ends_and_the_total_moves_only_then(live):
    session, ledger, _, folder = live
    states = []
    session.on_change = states.append
    totals = []
    # The first block of each round is paused, which ends it: it
    # earns nothing. The second runs to its end and earns its points.
    for step_id, blocks in (("words", 2), ("perception", 2)):
        to_step(session, step_id)
        for index in range(blocks):
            run_until(session, lambda s: s["phase"] == "waiting" or s["phase"] == "between")
            session.advance()
            earned = session.state()["points_total"]
            time.sleep(0.5)
            if index == 0:
                session.pause("fullscreen_lost")
                assert session.state()["points_total"] == earned
                session.resume()
            state = run_until(session, lambda s: s["phase"] == "between")
            totals.append(state["points_total"])
    assert totals == [0, 100, 100, 200]
    assert ledger.total == 200
    lines = lines_of(folder / "points.jsonl")
    assert [x["total"] for x in lines] == [100, 200]
    assert [x["amount"] for x in lines] == [100] * 2
    assert [x["reason"] for x in lines] == ["block complete"] * 2
    assert [(x["step"], x["block"]) for x in lines] == [
        ("words", "list2"),
        ("perception", "block2"),
    ]
    # The total in a state changes only on the state whose phase is between.
    previous = 0
    for state in states:
        if state["points_total"] != previous:
            assert state["phase"] == "between", state
            previous = state["points_total"]
    # Progress moves only then too.
    before = None
    for state in states:
        if before is not None and state["step"] == before["step"]:
            if state["blocks_done"] != before["blocks_done"]:
                # ... or at the pause that ended a block (it counts as played from then on).
                assert state["phase"] == "between" or state["interrupted"], state
        before = state


def test_the_ledger_samples_and_times_follow_the_block_end_marker(live):
    session, ledger, _, folder = live
    to_step(session, "words")
    run_until(session, lambda s: s["phase"] == "waiting")
    before_start = time.time()  # earlier than the engine's own start of the block
    session.advance()
    run_until(session, lambda s: s["phase"] == "between")
    (line,) = lines_of(folder / "points.jsonl")
    events = events_of(folder)
    end = next(e for e in events if e["name"] == "block_end")
    assert end["payload"]["points"] == 100
    # The block ran its 2 s before the award was written, and the award follows the event.
    assert line["unix_time"] >= before_start + 2.0
    assert line["unix_time"] >= end["unix_time"]
    assert line["unix_time"] - end["unix_time"] < 0.1
    # The sample is the marker's own sample, exactly.
    assert line["sample"] == markers_label(folder, f"words/block_end#{end['seq']}")


def markers_label(folder, label):
    (marker,) = [m for m in markers_of(folder / "eeg") if m["label"] == label]
    return marker["sample"]


def delivered(session):
    """Everything the session hands out, in order: states and points messages."""
    seen = []
    session.on_change = lambda state: seen.append(("state", state["phase"], state))
    session.on_points = lambda message: seen.append(("points", message["total"], message))
    return seen


def test_a_source_that_ends_in_the_tick_that_ends_the_block_still_delivers_between_first(
    live, pty_device
):
    session, ledger, source, folder = live
    start_block(session, "words")
    seen = delivered(session)
    deadline = time.monotonic() + 10
    while session.state()["remaining"] > 0.0:  # no tick: only the engine's clock runs
        assert time.monotonic() < deadline
        time.sleep(0.01)
    # The source ends (stopped: since 2026-11-25 a lost live link pauses instead of ending).
    source.stop()
    deadline = time.monotonic() + 10
    while source.ended is None:
        assert time.monotonic() < deadline, "the source did not end"
        time.sleep(0.01)
    assert seen == []  # nothing has ticked: the block's end and the source's end meet now
    session.tick()
    assert [(x[0], x[1]) for x in seen] == [
        ("state", "between"),
        ("points", 100),
        ("state", "done"),
    ]
    assert seen[0][2]["points_total"] == 100 and seen[0][2]["blocks_done"] == 1
    assert seen[2][2]["rev"] > seen[0][2]["rev"]
    assert ledger.total == 100


class InterleavingClock:
    """The real clock (``time.monotonic``), passed to the engine as its ``clock``.

    Once armed, the first time the engine reads it after the block's time is up (that
    read is inside the tick that ends the block) it starts an advance on another thread
    and waits until that thread is about to call ``advance``: the advance is requested
    in the middle of the command that ends the block. No sleep, nothing replaced.
    """

    def __init__(self, seconds):
        self.seconds = seconds
        self.start = None
        self.armed = True
        self.threads = []
        self.session = None
        self.waiting_for = None  # the thread whose read is the start of the next block

    def __call__(self):
        now = time.monotonic()
        if self.start is None:
            # The engine reads the clock when a block starts: that read, by the thread
            # that starts it, is the engine's own start of the block.
            if self.waiting_for is None or threading.current_thread() is self.waiting_for:
                self.start = now
                self.waiting_for = None
        elif self.armed and now - self.start >= self.seconds:
            self.start = None
            ready = threading.Event()

            def advance():
                ready.set()
                try:
                    self.session.advance()
                except CommandRefused:
                    pass  # another advance got there first

            thread = threading.Thread(target=advance, name="advancer")
            self.threads.append(thread)
            self.waiting_for = thread
            thread.start()
            assert ready.wait(10)
        return now


def interleaving_session(source, folder, count):
    steps = [
        # A timed placeholder (module None): a task drawn by a page module may run TASK_GRACE_S
        # past its time, which these block ends are not about.
        flow.Step("w", "task", "W", [flow.Block(f"b{i}", 0.05, 100) for i in range(count)], module=None),
        flow.Step("summary", "summary", "Summary"),
    ]
    clock = InterleavingClock(0.05)
    ledger = Ledger(folder / "interleaving-points.jsonl")
    session = Session(steps, source, ledger, clock, folder=folder / "interleaving")
    clock.session = session
    return session, ledger, clock


def check_hand_out(seen, count):
    points = [x for x in seen if x[0] == "points"]
    assert [x[1] for x in points] == [100 * (i + 1) for i in range(count)]
    total = 0
    for i, item in enumerate(seen):
        if item[0] == "state":
            if item[2]["points_total"] != total:
                assert item[1] == "between", (i, [(x[0], x[1]) for x in seen[: i + 1]])
                total = item[2]["points_total"]
        else:
            # Every points message comes straight after the between state that has its total.
            before = seen[i - 1]
            assert (before[0], before[1], before[2]["points_total"]) == ("state", "between", item[1])


def test_an_advance_requested_inside_the_command_that_ends_a_block_waits_for_the_points(live):
    """Deterministic: one block, the advance is requested exactly inside the tick."""
    _, _, source, folder = live
    session, ledger, clock = interleaving_session(source, folder, 1)
    try:
        clock.armed = False
        session.advance()  # starts the block; the clock noted the start
        seen = delivered(session)
        while time.monotonic() - clock.start < 0.06:
            pass  # the block's time is up; nothing has ticked
        clock.armed = True
        session.tick()
        for thread in clock.threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
    finally:
        session.close()
        ledger.close()
    assert len(clock.threads) == 1
    assert [(x[0], x[1]) for x in seen] == [
        ("state", "between"),
        ("points", 100),
        ("state", "waiting"),  # the advance left the last block for the summary step
    ]
    assert seen[0][2]["points_total"] == seen[2][2]["points_total"] == 100


def test_advances_landing_while_blocks_end_never_let_a_total_change_outside_between(live):
    """The same interleaving at 30 block ends in a row."""
    _, _, source, folder = live
    count = 30
    session, ledger, clock = interleaving_session(source, folder, count)
    seen = delivered(session)
    try:
        session.advance()  # start the first block
        deadline = time.monotonic() + 60
        while not any(x[0] == "state" and x[2]["step"] == "summary" for x in seen):
            assert time.monotonic() < deadline, "the session did not reach the summary"
            session.tick()
        for thread in clock.threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
    finally:
        session.close()
        ledger.close()
    assert len(clock.threads) == count
    check_hand_out(seen, count)


def test_a_failed_marker_at_the_end_of_a_block_takes_the_award_back(live):
    """Atomic: the ledger, the events and the state all stay as they were."""
    session, ledger, source, folder = live
    start_block(session, "words")
    lines = events_of(folder)
    source.stop_recording()
    time.sleep(2.1)
    with pytest.raises(RuntimeError, match="not recording"):
        session.tick()
    assert ledger.total == 0 and ledger.awards == []
    assert (folder / "points.jsonl").read_text() == ""
    state = session.state()
    assert (state["phase"], state["points_total"], state["blocks_done"]) == ("running", 0, 0)
    assert events_of(folder) == lines


def test_a_ledger_that_cannot_take_a_line_back_out_says_it_is_unusable(tmp_path):
    """A real failure: the file is closed, so neither the write nor the truncate can happen."""
    ledger = Ledger(tmp_path / "points.jsonl")
    words = steps_by_id()["words"]
    assert ledger.award(words, words.blocks[0], "block complete", 100) == 100
    ledger._file.close()
    with pytest.raises(PointsError, match="unusable"):
        ledger.award(words, words.blocks[1], "block complete", 100)
    # Memory and file still agree (one line, total 100), and nothing more can be awarded.
    assert ledger.total == 100 and len(ledger.awards) == 1
    assert [x["total"] for x in lines_of(tmp_path / "points.jsonl")] == [100]
    with pytest.raises(PointsError, match="unusable"):
        ledger.award(words, words.blocks[1], "block complete", 100)
    assert Ledger(tmp_path / "points.jsonl").total == 100


def test_a_block_end_whose_ledger_write_fails_ends_the_session_once_with_a_reason(live):
    """A real failure (the ledger file is closed): no retry, one marker, a recorded reason."""
    session, ledger, source, folder = live
    start_block(session, "words")
    ledger.close()
    deadline = time.monotonic() + 10
    while session.state()["remaining"] > 0.0:  # only the engine's clock runs
        assert time.monotonic() < deadline
    session.tick()
    state = session.state()
    assert state["phase"] == "done" and state["ended_early"] is True
    assert state["failure"].startswith("points ledger failed")
    assert (state["points_total"], state["blocks_done"]) == (0, 0)
    events = events_of(folder)
    assert events[-1]["name"] == "ended"
    # The block_end line was taken back out of events.jsonl: the block did not end on record.
    assert [e for e in events if e["name"] == "block_end"] == []
    assert events[-1]["payload"] == {"reason": state["failure"], "ended_early": True}
    assert not source.recording  # closed normally
    markers = markers_of(folder / "eeg")
    assert len([m for m in markers if m["label"].startswith("words/block_end#")]) == 1
    # Further ticks write nothing more.
    for _ in range(5):
        session.tick()
    assert events_of(folder) == events
    assert markers_of(folder / "eeg") == markers
    assert session.state()["rev"] == state["rev"]
    assert lines_of(folder / "points.jsonl") == []


@pytest.fixture
def append_only_events(pty_device, tmp_path, request):
    """A session whose ``events.jsonl`` takes appended lines but cannot be truncated.

    On macOS the file has the user append-only flag (``chflags uappnd``): a write
    succeeds and ``ftruncate`` fails with EPERM, a real failure that reaches the engine's
    "line cannot be taken back out" branch. Elsewhere the file is ``/dev/null``, on which
    ``ftruncate`` fails with EINVAL on Linux (not run on this machine; if it does not
    fail there, the test fails rather than passing for the wrong reason).
    """
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    folder.mkdir()
    events = folder / "events.jsonl"
    if sys.platform == "darwin":
        events.touch()
        # Registered before the flag is set, so that no failure can leave an undeletable file.
        request.addfinalizer(
            lambda: subprocess.run(["chflags", "nouappnd", str(events)], check=False)
        )
        subprocess.run(["chflags", "uappnd", str(events)], check=True)
    else:
        events.symlink_to("/dev/null")
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    ledger = Ledger(folder / "points.jsonl")
    session = Session(flow.load(SHORT), source, ledger, folder=folder)
    yield session, ledger, source, folder
    session.close()
    ledger.close()
    source.stop()


def test_a_line_that_cannot_be_taken_back_makes_the_next_tick_end_the_session(append_only_events):
    session, _, source, folder = append_only_events
    start_block(session, "words")
    source.stop_recording()  # the next marker fails, so the line has to be taken back
    with pytest.raises(engine_module.EventLogFailed, match="could not be taken back"):
        session.pause("fullscreen_lost")
    with pytest.raises(engine_module.EventLogFailed, match="the event log is unusable"):
        session.pause("fullscreen_lost")
    assert session.state()["phase"] == "running"  # a failing command changes no state
    session.tick()
    state = session.state()
    assert state["phase"] == "done" and state["ended_early"] is True
    assert state["failure"].startswith("event log failed")
    rev = state["rev"]
    session.tick()
    session.tick()
    assert session.state()["rev"] == rev


def test_a_ledger_failure_and_an_event_log_failure_together_end_the_session_once(
    append_only_events,
):
    session, ledger, source, folder = append_only_events
    start_block(session, "words")
    ledger.close()
    deadline = time.monotonic() + 10
    while session.state()["remaining"] > 0.0:  # only the engine's clock runs
        assert time.monotonic() < deadline
    session.tick()  # marker put, ledger write fails, the events line cannot be taken back
    state = session.state()
    assert state["phase"] == "done" and state["ended_early"] is True
    assert state["failure"].startswith("event log failed")
    assert "points ledger failed" in state["failure"]
    assert not source.recording
    markers = markers_of(folder / "eeg")
    assert len([m for m in markers if m["label"].startswith("words/block_end#")]) == 1
    for _ in range(5):
        session.tick()
    assert markers_of(folder / "eeg") == markers
    assert session.state()["rev"] == state["rev"]


def test_an_event_log_that_cannot_take_a_line_back_out_fails_clearly_and_marks_nothing(live):
    """A real failure: ``events.jsonl`` is closed under the session."""
    session, ledger, source, folder = live
    start_block(session, "words")
    before = len(markers_of(folder / "eeg"))
    session.close()
    for _ in range(3):
        with pytest.raises(engine_module.EventLogFailed, match="the event log is unusable"):
            session.pause("fullscreen_lost")
    assert len(markers_of(folder / "eeg")) == before  # nothing was marked by any attempt
    assert session.state()["phase"] == "running"


def test_state_carries_the_total_and_kind_so_a_page_can_hide_both_in_rest(live):
    session, _, _, folder = live
    for step_id in ("rest_closed", "rest_open"):
        start_block(session, step_id)
        state = session.state()
        assert state["kind"] == "rest"
        assert "points_total" in state and "blocks_done" in state and "blocks_total" in state
        run_until(session, lambda s: s["phase"] == "between")
        # the five task steps and the video (11 blocks) came first, then the rest blocks
        expected = 1200 if step_id == "rest_closed" else 1300  # were 1500 and 1600 with 3 blocks of 2-back, flanker, spatial
        assert session.state()["points_total"] == expected
        assert lines_of(folder / "points.jsonl")[-1]["total"] == expected


def test_state_carries_the_last_award_only_while_between_blocks(live):
    session, ledger, _, folder = live
    to_step(session, "words")
    run_until(session, lambda s: s["phase"] == "waiting")
    assert session.state()["award"] is None
    session.advance()
    assert session.state()["award"] is None  # nothing is awarded while a block runs
    state = run_until(session, lambda s: s["phase"] == "between")
    assert state["award"] == {"step": "words", "block": "list1", "points": 100, "interrupted": False}
    # The same award is on disk: what the page is told is what was recorded.
    last = lines_of(folder / "points.jsonl")[-1]
    assert (last["step"], last["block"], last["amount"]) == ("words", "list1", 100)
    assert state["points_total"] == last["total"] == ledger.total
    session.advance()
    assert session.state()["award"] is None
    state = run_until(session, lambda s: s["phase"] == "between")
    assert state["award"] == {"step": "words", "block": "list2", "points": 100, "interrupted": False}


def test_a_session_without_a_ledger_reports_no_award(pty_device, tmp_path):
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    session = Session(flow.load(SHORT), source, None, folder=folder)
    try:
        to_step(session, "words")
        run_until(session, lambda s: s["phase"] == "waiting")
        session.advance()
        state = run_until(session, lambda s: s["phase"] == "between")
        assert state["award"] == {"step": "words", "block": "list1", "points": 0, "interrupted": False}
        assert state["points_total"] == 0
    finally:
        session.close()
        source.stop()


# -- over the websocket ------------------------------------------------------


def read_session(ws, until_step_done, deadline_s=120.0):
    """Messages in arrival order, driving the session to its end from the page's side."""
    messages = []
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv(timeout=5))
        except TimeoutError:
            continue
        messages.append(message)
        if message["t"] == "state":
            if until_step_done(message):
                return messages
            if message["phase"] in ("waiting", "between") and message["step"] != "summary":
                send(ws, cmd="advance")
            # Only the video block is paused: a pause holds it (a task block it would end, with no
            # points; see test_pause_next_block.py).
            elif message["phase"] == "running" and message["kind"] == "video" and message["remaining"] < 1.5 and not any(
                m["t"] == "state" and m["phase"] == "paused" and m["block"] == message["block"]
                and m["step"] == message["step"]
                for m in messages
            ):
                send(ws, cmd="pause", reason="fullscreen_lost")
            elif message["phase"] == "paused":
                send(ws, cmd="resume")
    raise AssertionError("the session did not finish in time")


def test_points_messages_arrive_only_between_blocks_and_the_ledger_equals_them(
    app, pty_device
):
    """Review focus 2: the whole short flow, through a real websocket client."""
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        messages = read_session(
            ws, lambda m: m["step"] == "summary" and m["phase"] == "waiting"
        )
    # The summary step comes after the last block's points message, so all of them are in.
    sent = [m for m in messages if m["t"] == "points"]
    assert len(sent) == BLOCKS_IN_FLOW
    assert [m["total"] for m in sent] == [100 * (i + 1) for i in range(BLOCKS_IN_FLOW)]
    assert all(set(m) == {"t", "total", "gained", "block", "reason"} for m in sent)
    assert {m["gained"] for m in sent} == {100}
    # Every points message comes while the last state said "between", and in the
    # state before it the total was still the old one.
    last_state = None
    total_in_state = 0
    pauses = 0
    for message in messages:
        if message["t"] == "state":
            if message["phase"] == "paused":
                pauses += 1
            if message["points_total"] != total_in_state:
                assert message["phase"] == "between", message
                total_in_state = message["points_total"]
            last_state = message
        elif message["t"] == "points":
            assert last_state is not None and last_state["phase"] == "between", last_state
            assert last_state["points_total"] == message["total"]
            assert last_state["block"] == message["block"]
    assert pauses >= 1  # the video block was paused, and no points came of it
    # The ledger on disk equals what the client received, line for line.
    lines = lines_of(session_folder_of(a) / "points.jsonl")
    assert len(lines) == len(sent)
    for line, message in zip(lines, sent, strict=True):
        assert (line["total"], line["amount"], line["block"], line["reason"]) == (
            message["total"],
            message["gained"],
            message["block"],
            message["reason"],
        )
    # Nothing was awarded for the setup steps or the summary.
    assert {x["step"] for x in lines} == {
        "words", "perception", "nback", "flanker", "spatial", "video", "rest_closed", "rest_open", "recall",
    }  # fmt: skip


def test_a_page_that_connects_mid_session_is_told_the_total_in_its_first_state(
    app, pty_device
):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    ws = open_ws(a, page="page-1")
    next_state(ws)
    for _ in range(4):
        send(ws, cmd="advance")
    next_state(ws, predicate=lambda m: m["phase"] == "running")
    done = next_state(ws, predicate=lambda m: m["phase"] == "between")
    assert done["points_total"] == 100
    reloaded, first = reconnect(a, "page-1")
    with reloaded:
        assert first["points_total"] == 100
        assert first["phase"] == "between"


# -- a replaced socket may not send commands --------------------------------


def test_a_replaced_participant_socket_that_has_not_closed_cannot_send_commands(
    pty_device, tmp_path
):
    """On a pty device, which stays connected for as long as the test needs."""
    import asyncio

    settings = Settings(
        data_dir=tmp_path, port=pty_device.path, replay=None, flow=SHORT, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    loop = asyncio.new_event_loop()
    try:
        # nothing is recorded before a label is accepted; this is the call the route makes
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")
        assert hub.session is not None
        old = Client(loop, "participant", "page-1")
        new = Client(loop, "participant", "page-1")
        assert hub.connect(old)
        assert hub.connect(new)  # the same page again: it replaces ``old``
        before = hub.session.state()
        hub.command(old, {"cmd": "advance"})
        hub.command(old, {"cmd": "event", "name": "sneaky", "payload": {}, "browser_time": 1.0,
                         "time_origin": 1.0})  # fmt: skip
        after = hub.session.state()
        assert (after["step"], after["rev"]) == (before["step"], before["rev"]) == (
            "participant",
            before["rev"],
        )
        assert "sneaky" not in [e["name"] for e in events_of(hub.folder)]
        # ... and the replaced page is told, once per command, that it was not run
        told = []
        while not old.messages.empty():
            text = old.messages.get_nowait()[0]
            if text is not None:
                told.append(json.loads(text))
        refusals = [m for m in told if m["t"] == "error" and m.get("kind") == "replaced"]
        assert len(refusals) == 2 and "was replaced" in refusals[0]["msg"]
        # The current participant still can.
        hub.command(new, {"cmd": "advance"})
        assert hub.session.state()["step"] == "device"
    finally:
        hub.stop()
        loop.close()


def test_a_client_that_connects_after_a_ledger_failure_is_told_the_session_ended_and_why(
    pty_device, tmp_path
):
    import asyncio

    settings = Settings(
        data_dir=tmp_path, port=pty_device.path, replay=None, flow=SHORT, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    loop = asyncio.new_event_loop()
    try:
        # nothing is recorded before a label is accepted; this is the call the route makes
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")
        assert hub.session is not None
        page = Client(loop, "participant", "page-1")
        assert hub.connect(page)
        hub.ledger._file.close()  # a real failure: the ledger file is gone from under it
        for _ in range(4):  # participant, device, calibration -> words; start the block
            hub.command(page, {"cmd": "advance"})
        deadline = time.monotonic() + 20
        while hub.session.state()["phase"] != "done":
            assert time.monotonic() < deadline, "the session did not end"
            time.sleep(0.05)
        late = Client(loop, "participant", "page-2")
        hub.disconnect(page)
        assert hub.connect(late)
        states = []
        while not late.messages.empty():
            text, _ = late.messages.get_nowait()
            message = json.loads(text)
            if message["t"] == "state":
                states.append(message)
        assert len(states) == 1
        assert states[0]["phase"] == "done" and states[0]["ended_early"] is True
        assert states[0]["failure"].startswith("points ledger failed")
        events = events_of(hub.folder)
        assert [e["name"] for e in events].count("ended") == 1
        all_markers = markers_of(hub.folder / "eeg")
        assert len([m for m in all_markers if m["label"].startswith("words/block_end#")]) == 1
        # The hub's ticker keeps ticking too; more ticks must not add a marker or an event.
        for _ in range(10):
            hub.session.tick()
        assert markers_of(hub.folder / "eeg") == all_markers
        assert events_of(hub.folder) == events
    finally:
        hub.stop()
        loop.close()

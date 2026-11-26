"""Skipping a round, playing one round, stopping early, and continuing an earlier session.

Everything is real: a pseudo-terminal device as the headset, a recording made from it, the
points ledger on disk, ``brain-mapper`` as a subprocess for the routes and the websocket,
files in a temporary folder. The labels are made up (``P001``, ``P002``).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from brain_mapper import flow, progress
from brain_mapper.engine import CommandRefused, Session
from brain_mapper.points import Ledger, PointsError, read_awards
from brain_mapper.source import LiveSource
from pty_device import PtyDevice, config_reply, onhead_stream
from test_engine import (
    SHORT,
    events_of,
    markers_of,
    next_state,
    open_ws,
    run_until,
    send,
    start_block,
    to_step,
)
from test_participants import request, session_folder, stop
from test_shell_browser import inprocess  # noqa: F401  (a fixture)


# -- a session on a pty device with a points ledger ----------------------------------------


class Rig:
    def __init__(self, session, source, folder, rec, ledger, states, points):
        self.session, self.source, self.folder, self.rec, self.ledger = (
            session,
            source,
            folder,
            rec,
            ledger,
        )
        self.states, self.points = states, points

    def events(self):
        return events_of(self.folder)

    def markers(self):
        return markers_of(self.rec)

    def ledger_lines(self):
        path = self.folder / "points.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()]

    def progress(self):
        return json.loads((self.folder / "progress.json").read_text())


def make_rig(pty_device, tmp_path, clock=time.monotonic, steps=None, carried=None, name="session"):
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / name
    rec = folder / "eeg"
    source.start_recording(rec, "P001", "uuid-1")
    ledger = Ledger(folder / "points.jsonl", carried=carried)
    session = Session(
        steps or flow.load(SHORT),
        source,
        ledger,
        clock,
        folder=folder,
        meta={"session": name, "uuid": "uuid-1", "seed": 7, "continues": None},
    )
    states, points = [], []
    session.on_change = states.append
    session.on_points = points.append
    return Rig(session, source, folder, rec, ledger, states, points)


@pytest.fixture
def rig(pty_device, tmp_path):
    made = make_rig(pty_device, tmp_path)
    yield made
    made.session.close()
    made.ledger.close()
    made.source.stop()


def assert_consistent(rig: Rig) -> None:
    """Events, markers, ledger and progress.json tell the same story as the state."""
    rig.session.tick()
    state = rig.session.state()
    events = rig.events()
    assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})
    marks = [m for m in rig.markers()]
    labelled = [e for e in events if e["payload"].get("marked", True) is not False]
    # every marker is `<step>/<name>#<seq>` of exactly one event, in order
    assert [m["label"] for m in marks] == [f"{e['step']}/{e['name']}#{e['seq']}" for e in labelled]
    # a ledger line for every block that ended with points (not one a pause ended), and nothing else
    ended = [e for e in events if e["name"] == "block_end" and e["payload"].get("ended_by") != "interrupted"]
    lines = [x for x in rig.ledger_lines() if x["reason"] != "carried"]
    assert [(x["step"], x["block"]) for x in lines] == [(e["step"], e["block"]) for e in ended]
    last = rig.ledger_lines()
    assert (last[-1]["total"] if last else 0) == state["points_total"]
    saved = rig.progress()
    assert saved["points_total"] == state["points_total"]
    assert {s["id"]: s["outcome"] for s in saved["steps"]} == state["outcomes"]
    assert saved["plan"] == state["plan"]


# -- flow.select -------------------------------------------------------------------------------


def ids(steps):
    return [s.id for s in steps]


def test_select_only_keeps_the_setup_steps_that_round_and_the_summary():
    chosen = flow.select(flow.load(SHORT), only="nback")
    assert ids(chosen) == ["participant", "device", "calibration", "nback", "summary"]


def test_select_start_at_keeps_that_round_and_every_round_after_it():
    chosen = flow.select(flow.load(SHORT), start_at="video")
    assert ids(chosen) == [
        "participant", "device", "calibration", "video", "rest_closed", "rest_open", "recall", "summary",
    ]  # fmt: skip


def test_select_with_no_choice_keeps_everything():
    steps = flow.load(SHORT)
    assert flow.select(steps) == steps and flow.select(steps) is not steps


def test_select_recall_alone_is_a_flow_choice_the_server_checks_separately():
    assert ids(flow.select(flow.load(SHORT), only="recall"))[-2:] == ["recall", "summary"]


@pytest.mark.parametrize("bad", ["nope", "", "device", "summary", "participant", 5])
@pytest.mark.parametrize("name", ["only", "start_at"])
def test_select_an_unknown_id_raises_naming_the_rounds(name, bad):
    with pytest.raises(ValueError, match="words, perception, nback") as raised:
        flow.select(flow.load(SHORT), **{name: bad})
    assert name in str(raised.value)


def test_select_only_together_with_start_at_raises():
    with pytest.raises(ValueError, match="not both"):
        flow.select(flow.load(SHORT), only="words", start_at="nback")


# -- skip --------------------------------------------------------------------------------------


def test_skip_in_the_middle_of_a_block_ends_it_at_once_earns_nothing_and_goes_to_the_next_round(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["remaining"] < 1.7)
    session.skip()
    state = session.state()
    assert (state["step"], state["phase"], state["blocks_done"]) == ("perception", "waiting", 0)
    assert state["points_total"] == 0 and state["award"] is None
    assert state["outcomes"]["words"] == "skipped" and state["outcomes"]["perception"] == "running"
    events = rig.events()
    skipped = [e for e in events if e["name"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["step"] == "words"
    assert skipped[0]["payload"]["block"] == "list1"
    assert 0.2 < skipped[0]["payload"]["seconds"] < 1.9
    assert [e["name"] for e in events if e["step"] == "words"] == ["step_start", "block_start", "skipped"]
    assert events[-1]["name"] == "step_start" and events[-1]["step"] == "perception"
    assert not any(e["name"] == "block_end" for e in events)
    assert rig.ledger_lines() == [] and rig.points == []
    assert f"words/skipped#{skipped[0]['seq']}" in [m["label"] for m in rig.markers()]
    assert_consistent(rig)


def test_the_skipped_marker_is_saved_once_with_the_round_and_the_block_it_was_pressed_in(rig):
    session = rig.session
    start_block(session, "perception")
    session.skip()
    labels = [m["label"] for m in rig.markers()]
    assert len([x for x in labels if "/skipped#" in x]) == 1
    assert [x for x in labels if "/skipped#" in x][0].startswith("perception/skipped#")


def test_skip_after_a_finished_block_keeps_that_blocks_points_and_adds_none(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["phase"] == "between")
    assert session.state()["points_total"] == 100 and len(rig.points) == 1
    session.advance()  # block 2 starts
    run_until(session, lambda s: s["remaining"] < 1.5)
    session.skip()
    assert session.state()["points_total"] == 100
    assert len(rig.ledger_lines()) == 1 and len(rig.points) == 1
    assert all(s["points_total"] in (0, 100) for s in rig.states)
    assert_consistent(rig)


def test_skip_on_the_between_screen_skips_the_blocks_still_to_play(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["phase"] == "between")
    session.skip()
    state = session.state()
    assert (state["step"], state["phase"]) == ("perception", "waiting")
    skipped = [e for e in rig.events() if e["name"] == "skipped"]
    assert skipped[0]["payload"]["block"] is None and skipped[0]["payload"]["seconds"] == 0.0
    assert skipped[0]["payload"]["blocks_done"] == 1
    assert state["points_total"] == 100 and len(rig.ledger_lines()) == 1
    assert_consistent(rig)


def test_skip_before_a_round_starts_skips_it_unplayed(rig):
    to_step(rig.session, "nback")
    run_until(rig.session, lambda s: s["phase"] == "waiting")
    rig.session.skip()
    assert rig.session.state()["step"] == "flanker"
    assert rig.session.state()["outcomes"]["nback"] == "skipped"
    assert_consistent(rig)


@pytest.mark.parametrize("at", ["participant", "device"])
def test_skip_on_participant_and_device_is_refused_and_changes_nothing(rig, at):
    session = rig.session
    to_step(session, at)
    before = (session.state(), len(rig.events()), len(rig.markers()))
    with pytest.raises(CommandRefused, match="cannot be skipped"):
        session.skip()
    assert (session.state(), len(rig.events()), len(rig.markers())) == before
    assert_consistent(rig)


def test_skip_on_the_summary_is_refused_and_changes_nothing(rig):
    session = rig.session
    to_step(session, "summary")
    run_until(session, lambda s: s["phase"] == "waiting")
    before = (session.state(), len(rig.events()), len(rig.markers()))
    with pytest.raises(CommandRefused, match="summary cannot be skipped"):
        session.skip()
    assert (session.state(), len(rig.events()), len(rig.markers())) == before


def test_calibration_can_be_skipped(rig):
    to_step(rig.session, "calibration")
    rig.session.skip()
    state = rig.session.state()
    assert state["step"] == "words" and state["outcomes"]["calibration"] == "skipped"
    assert [e["name"] for e in rig.events() if e["step"] == "calibration"] == ["step_start", "skipped"]


def test_skip_names_the_round_so_a_second_press_cannot_skip_another_round(rig):
    session = rig.session
    start_block(session, "words")
    session.skip("words")
    with pytest.raises(CommandRefused, match="no longer the current step"):
        session.skip("words")
    state = session.state()
    assert state["step"] == "perception" and state["outcomes"]["perception"] == "running"
    assert len([e for e in rig.events() if e["name"] == "skipped"]) == 1
    assert_consistent(rig)


def test_skip_while_paused_is_allowed_and_the_next_round_starts_unpaused(rig):
    session = rig.session
    start_block(session, "words")
    session.pause("fullscreen_lost")
    assert session.state()["phase"] == "paused" and session.state()["reason"] == "fullscreen_lost"
    session.skip()
    state = session.state()
    assert (state["step"], state["phase"], state["reason"]) == ("perception", "waiting", None)
    skipped = [e for e in rig.events() if e["name"] == "skipped"][0]
    # The pause had ended list1 already (and it counts as played, since 2026-11-25): no block was
    # in progress at the skip, and the round is partly played.
    assert skipped["payload"]["block"] is None and skipped["payload"]["blocks_done"] == 1
    assert state["outcomes"]["words"] == "partial"
    session.advance()
    assert session.state()["phase"] == "running"
    assert_consistent(rig)


def test_a_skip_whose_next_step_cannot_be_entered_ends_the_session_with_the_reason_and_the_skip_stays(
    tmp_path,
):
    """A real cause, in a subprocess: the file size limit (RLIMIT_FSIZE) is set by
    ``fsize_driver.py`` on itself only, never in this process, just above the size of
    ``events.jsonl`` plus its "skipped" line, so the "step_start" line that follows is refused.

    The narrowest real cause: ``events.jsonl`` is made by far the largest file of the session, so
    no other file (raw.bin, the BDF file, the markers, progress.json) reaches the limit, and
    only the event log write fails. (With a limit below the other files, the recording loses
    data too: the BDF file is cut off at the limit and closing the recording raises.)"""
    import subprocess
    import sys

    import open_cognionics as oc

    device = PtyDevice(onhead_stream(), config_reply(), bytes_per_second=7_500)
    try:
        done = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "fsize_driver.py"), device.path, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        device.hang_up()
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["phase"] == "done" and result["ended_early"] is True
    assert result["failure"].startswith("skipping could not be completed")
    assert result["step"] == "words" and result["outcomes"]["words"] == "skipped"
    # events.jsonl is the file that reached the limit: the "skipped" line fitted, the line after
    # it would not have (so it was refused and cut back to a whole line), and no other file of
    # the session was anywhere near the limit
    sizes, limit = result["after"], result["limit"]
    assert result["before"]["events.jsonl"] < sizes["events.jsonl"] <= limit
    assert limit < sizes["events.jsonl"] + result["next_len"]
    assert max(v for k, v in sizes.items() if k != "events.jsonl") < limit
    folder = tmp_path / "limited"
    # the event log: whole lines only, the skip is the last one, nothing after it
    events = events_of(folder)
    assert events[-1]["name"] == "skipped" and [e["name"] for e in events].count("skipped") == 1
    # progress.json says what happened
    saved = json.loads((folder / "progress.json").read_text())
    assert saved["status"] == "failed"
    assert {x["id"]: x["outcome"] for x in saved["steps"]}["words"] == "skipped"
    assert progress.read(folder).next_step == "perception"
    # the recording: written on every call and then closed by the failure, as docs/recordings.md says
    rec = folder / "eeg"
    assert json.loads((rec / "meta.json").read_text())["finished"] is True
    assert result["after"]["eeg/raw.bin"] >= result["before"]["eeg/raw.bin"] > 0
    raw = (rec / "raw.bin").read_bytes()
    recorded = oc.read_recording(rec)
    assert recorded.data.shape[0] > 0 and np.isfinite(recorded.data).all()
    assert recorded.data.shape[0] <= len(raw) // 75
    assert recorded.data.shape[0] >= len(raw) // 75 - 1  # every whole packet read is in the recording
    labels = [m.label for m in recorded.markers]
    skipped = events[-1]
    assert f"words/skipped#{skipped['seq']}" in labels  # the marker that cannot be taken out is there
    assert not any(x.startswith("perception/step_start#") for x in labels)  # nothing of the step that failed


def test_the_last_round_skipped_goes_to_the_summary_and_the_summary_can_be_finished(rig):
    to_step(rig.session, "recall")
    run_until(rig.session, lambda s: s["phase"] == "waiting")
    rig.session.skip()
    assert rig.session.state()["step"] == "summary"
    run_until(rig.session, lambda s: s["phase"] == "waiting")
    rig.session.advance()
    state = rig.session.state()
    assert state["phase"] == "done" and state["stopped"] is False and state["failure"] is None
    assert rig.progress()["status"] == "finished"
    assert state["outcomes"]["recall"] == "skipped" and state["outcomes"]["summary"] == "finished"


def test_skip_in_the_tick_that_ends_a_block_leaves_the_files_consistent_either_way(pty_device, tmp_path):
    """Time is up at the same moment the person presses Skip; both orders are consistent."""
    for tick_first in (True, False):
        now = [0.0]
        made = make_rig(pty_device, tmp_path, clock=lambda: now[0], name=f"race-{tick_first}")
        try:
            session = made.session
            while session.state()["step"] != "words":
                session.advance()
            session.advance()
            assert session.state()["phase"] == "running"
            now[0] = 2.0  # the block's time is up, but no tick has seen it yet
            if tick_first:
                session.tick()
                assert session.state()["phase"] == "between"
            session.skip()
            state = session.state()
            assert state["step"] == "perception"
            names = [e["name"] for e in made.events()]
            assert names.count("skipped") == 1
            # with the tick first the block did finish and earned its points; without it, not
            assert names.count("block_end") == (1 if tick_first else 0)
            assert state["points_total"] == (100 if tick_first else 0)
            assert len(made.ledger_lines()) == (1 if tick_first else 0)
            assert_consistent(made)
        finally:
            made.session.close()
            made.ledger.close()
            made.source.stop()


def test_skip_and_the_ticker_racing_on_real_threads_leave_the_files_consistent(rig):
    session = rig.session
    start_block(session, "words")
    stopped = threading.Event()

    def ticker():
        while not stopped.is_set():
            session.tick()

    thread = threading.Thread(target=ticker)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while session.state()["remaining"] and session.state()["remaining"] > 0.003:
            assert time.monotonic() < deadline
        try:
            session.skip()
        except CommandRefused:
            pass  # the tick moved the session first; nothing may be left half done
    finally:
        stopped.set()
        thread.join()
    assert_consistent(rig)
    # Changed from ("skipped", "running"): when the tick ended the block just before the press,
    # the round had a finished block and is now "partial" rather than "skipped".
    outcome = rig.session.state()["outcomes"]["words"]
    assert outcome in ("skipped", "partial", "running")
    ended = [e for e in rig.events() if e["name"] == "block_end"]
    assert (outcome == "partial") == (len(ended) == 1)


# -- stop --------------------------------------------------------------------------------------


def test_stop_saves_one_stopped_marker_closes_the_recording_and_is_a_normal_ending(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["remaining"] < 1.7)
    session.stop()
    state = session.state()
    assert state["phase"] == "done" and state["stopped"] is True
    assert state["failure"] is None and state["ended_early"] is False
    stops = [e for e in rig.events() if e["name"] == "stopped"]
    assert len(stops) == 1 and stops[0]["step"] == "words" and stops[0]["payload"]["block"] == "list1"
    assert 0.2 < stops[0]["payload"]["seconds"] < 1.9
    assert [m["label"] for m in rig.markers()].count(f"words/stopped#{stops[0]['seq']}") == 1
    with pytest.raises(RuntimeError, match="not recording"):
        rig.source.mark("after the stop")  # the recording is closed
    assert rig.ledger_lines() == [] and rig.points == []
    assert state["points_total"] == 0
    assert_consistent(rig)
    saved = rig.progress()
    assert saved["status"] == "stopped"
    assert {s["id"]: s["outcome"] for s in saved["steps"]}["words"] == "not_reached"


def test_stop_between_blocks_keeps_the_points_already_earned(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["phase"] == "between")
    session.stop()
    assert session.state()["points_total"] == 100 and len(rig.points) == 1
    assert len(rig.ledger_lines()) == 1
    assert_consistent(rig)


def test_stop_while_paused_and_before_a_round_starts(rig):
    session = rig.session
    start_block(session, "words")
    session.pause("fullscreen_lost")
    session.stop()
    assert session.state()["stopped"] is True and session.state()["reason"] is None
    assert_consistent(rig)


def test_stop_from_calibration_is_possible_and_marks_no_round_reached(rig):
    to_step(rig.session, "calibration")
    rig.session.stop()
    saved = rig.progress()
    assert saved["status"] == "stopped"
    outcomes = {s["id"]: s["outcome"] for s in saved["steps"]}
    assert outcomes["participant"] == outcomes["device"] == "finished"
    assert outcomes["calibration"] == "not_reached" and outcomes["words"] == "not_reached"


def test_stop_before_the_participant_step_is_done_is_refused_and_changes_nothing(rig):
    before = (rig.session.state(), len(rig.events()), len(rig.markers()))
    with pytest.raises(CommandRefused, match="participant ID"):
        rig.session.stop()
    assert (rig.session.state(), len(rig.events()), len(rig.markers())) == before


def test_after_a_stop_every_command_is_refused_and_nothing_more_is_written(rig):
    session = rig.session
    start_block(session, "words")
    session.stop()
    events, marks, state = len(rig.events()), len(rig.markers()), session.state()
    for command in (
        session.advance,
        lambda: session.pause("x"),
        session.resume,
        session.skip,
        session.stop,
        lambda: session.event("name", {}, 1.0, 2.0),
    ):
        with pytest.raises(CommandRefused):
            command()
    session.tick()
    assert (len(rig.events()), len(rig.markers()), session.state()) == (events, marks, state)
    assert len([e for e in rig.events() if e["name"] == "stopped"]) == 1


def test_stop_and_skip_after_a_finished_session_are_refused(rig):
    session = rig.session
    while True:
        state = run_until(session, lambda s: s["phase"] in ("waiting", "between", "done"))
        if state["phase"] == "done":
            break
        session.advance()
    for command in (session.stop, session.skip):
        with pytest.raises(CommandRefused, match="over"):
            command()
    assert session.state()["stopped"] is False and rig.progress()["status"] == "finished"
    assert all(v == "finished" for v in session.state()["outcomes"].values())


def test_stop_after_the_source_ended_is_refused(pty_device, tmp_path):
    made = make_rig(pty_device, tmp_path)
    try:
        start_block(made.session, "words")
        # The source ends (stopped: since 2026-11-25 a lost live link pauses instead of ending).
        made.source.stop()
        run_until(made.session, lambda s: s["phase"] == "done", timeout=20)
        assert made.session.state()["ended_early"] is True
        with pytest.raises(CommandRefused):
            made.session.stop()
        with pytest.raises(CommandRefused):
            made.session.skip()
        assert made.progress()["status"] == "ended_early"
    finally:
        made.session.close()
        made.ledger.close()
        made.source.stop()


# -- state and progress.json -------------------------------------------------------------------


def test_state_carries_the_plan_stopped_and_the_outcome_of_every_step(rig):
    state = rig.session.state()
    assert state["plan"] == ids(flow.load(SHORT)) and state["stopped"] is False
    assert state["outcomes"]["participant"] == "running"
    assert all(v == "not_reached" for k, v in state["outcomes"].items() if k != "participant")
    rig.session.advance()
    assert rig.session.state()["outcomes"]["participant"] == "finished"
    assert rig.session.state()["outcomes"]["device"] == "running"


def test_progress_json_is_saved_when_the_session_begins_and_at_every_step_change(rig):
    first = rig.progress()
    assert first["version"] == 1 and first["status"] == "running"
    assert first["plan"] == ids(flow.load(SHORT)) and first["points_total"] == 0
    assert first["seed"] == 7 and first["continues"] is None and first["uuid"] == "uuid-1"
    assert [s["outcome"] for s in first["steps"]][:2] == ["running", "not_reached"]
    rig.session.advance()
    now = rig.progress()
    assert [s["outcome"] for s in now["steps"]][:3] == ["finished", "running", "not_reached"]
    assert [p.name for p in rig.folder.iterdir() if p.name.startswith(".")] == []  # no temp file left


def test_progress_json_is_written_at_boundaries_and_not_for_every_event_of_a_block(rig):
    """Hundreds of page events in a block do not each fsync and rename the file."""
    session = rig.session
    start_block(session, "words")
    path = rig.folder / "progress.json"
    before = (path.stat().st_ino, rig.progress()["updated_unix"])
    for i in range(300):
        session.event("word_shown", {"i": i}, 1.0 + i, 1.0)
    assert len([e for e in rig.events() if e["name"] == "word_shown"]) == 300
    assert (path.stat().st_ino, rig.progress()["updated_unix"]) == before  # not written once
    finish_block(session)  # the block ends: the total changes, so the file is written
    saved = rig.progress()
    assert saved["updated_unix"] > before[1] and saved["points_total"] == 100
    session.stop()
    assert rig.progress()["status"] == "stopped"
    assert_consistent(rig)


def test_progress_json_holds_each_rounds_outcome_after_a_stop(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["phase"] == "between")
    session.advance()
    run_until(session, lambda s: s["phase"] == "between")
    session.advance()  # perception, waiting
    session.skip()  # nback waiting
    start_block(session, "nback")
    session.stop()
    saved = rig.progress()
    outcomes = {s["id"]: s["outcome"] for s in saved["steps"]}
    assert outcomes["participant"] == outcomes["calibration"] == "finished"
    assert outcomes["words"] == "finished" and outcomes["perception"] == "skipped"
    assert outcomes["nback"] == "not_reached" and outcomes["summary"] == "not_reached"
    assert saved["status"] == "stopped" and saved["points_total"] == 200
    assert not any(v == "running" for v in outcomes.values())


def test_progress_reading_gives_next_step_and_skipped_rounds_stay_skipped(rig):
    session = rig.session
    to_step(session, "words")
    run_until(session, lambda s: s["phase"] == "waiting")
    session.skip()  # words skipped, perception waiting
    session.stop()
    found = progress.read(rig.folder)
    assert found.next_step == "perception" and found.next_title == "Perception"
    assert found.rounds_from_next()[0] == "perception" and "words" not in found.rounds_from_next()
    assert found.outcomes["words"] == "skipped" and found.finished_rounds() == set()


def finish_block(session):
    run_until(session, lambda s: s["phase"] == "between")


def test_skip_on_the_between_screen_after_a_rounds_last_block_leaves_the_round_finished(rig):
    session = rig.session
    start_block(session, "words")
    finish_block(session)
    session.advance()
    finish_block(session)
    assert session.state()["blocks_done"] == session.state()["blocks_total"] == 2
    session.skip()
    state = session.state()
    assert state["outcomes"]["words"] == "finished" and state["step"] == "perception"
    assert state["blocks_by_step"]["words"] == 2 and state["points_total"] == 200
    assert_consistent(rig)


def test_stop_on_the_between_screen_after_a_rounds_last_block_leaves_the_round_finished(rig):
    session = rig.session
    start_block(session, "words")
    finish_block(session)
    session.advance()
    finish_block(session)
    session.stop()
    assert session.state()["outcomes"]["words"] == "finished"
    assert progress.read(rig.folder).next_step == "perception"
    assert_consistent(rig)


def test_skip_after_one_finished_block_leaves_the_round_partial_and_counts_that_block(rig):
    session = rig.session
    start_block(session, "words")
    finish_block(session)
    session.skip()
    state = session.state()
    assert state["outcomes"]["words"] == "partial" and state["blocks_by_step"]["words"] == 1
    session.stop()
    saved = progress.read(rig.folder)
    assert saved.outcomes["words"] == "partial" and saved.played_anything
    assert saved.finished_rounds() == {"words"} and "words" not in saved.rounds_from_next()
    assert_consistent(rig)


def test_stop_in_the_second_block_after_one_finished_block_leaves_the_round_partial(rig):
    session = rig.session
    start_block(session, "words")
    finish_block(session)
    session.advance()
    run_until(session, lambda s: s["remaining"] < 1.7)
    session.stop()
    state = session.state()
    assert state["outcomes"]["words"] == "partial" and state["blocks_by_step"]["words"] == 1
    assert state["points_total"] == 100
    found = progress.read(rig.folder)
    assert found.next_step == "perception" and found.finished_rounds() == {"words"}
    assert found.outcomes["words"] == "partial" and "words" not in found.rounds_from_next()
    assert_consistent(rig)


def test_stop_in_the_first_block_of_a_round_leaves_it_not_reached_so_continuing_starts_it(rig):
    session = rig.session
    start_block(session, "words")
    run_until(session, lambda s: s["remaining"] < 1.7)
    session.stop()
    state = session.state()
    assert state["outcomes"]["words"] == "not_reached" and state["blocks_by_step"]["words"] == 0
    assert progress.read(rig.folder).next_step == "words"
    assert_consistent(rig)


# -- the ledger: carried line ------------------------------------------------------------------


def test_a_ledger_that_continues_a_session_starts_with_a_carried_line(tmp_path):
    path = tmp_path / "points.jsonl"
    ledger = Ledger(path, carried=(300, "P001_20261125_101010"))
    try:
        assert ledger.total == 300
        first = json.loads(path.read_text().splitlines()[0])
        assert first["seq"] == 1 and first["reason"] == "carried" and first["amount"] == 0
        assert first["total"] == 300 and first["carried_from"] == "P001_20261125_101010"
        assert first["step"] is None and first["block"] is None
    finally:
        ledger.close()
    reopened = Ledger(path, carried=(300, "P001_20261125_101010"))
    try:
        assert reopened.total == 300 and len(reopened.awards) == 1
    finally:
        reopened.close()
    assert [a["total"] for a in read_awards(path)] == [300]


def test_the_carried_line_is_accepted_as_the_first_line_only_and_a_file_that_does_not_add_up_is_refused(tmp_path):
    good = {"seq": 1, "step": None, "block": None, "reason": "carried", "amount": 0, "total": 200}
    award = {"seq": 2, "step": "words", "block": "list1", "reason": "block complete", "amount": 100, "total": 300}
    path = tmp_path / "a.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in (good, award)) + "\n")
    assert [a["total"] for a in read_awards(path)] == [200, 300]
    cases = {
        "total after carried is wrong": [good, {**award, "total": 301}],
        "carried with a gain": [{**good, "amount": 50}],
        "carried negative": [{**good, "total": -1}],
        "carried second": [{**award, "seq": 1, "total": 100}, {**good, "seq": 2}],
        "carried with a fractional total": [{**good, "total": 1.5}],
        "first line not adding up": [{**award, "seq": 1, "total": 150}],
        "seq not counting": [good, {**award, "seq": 5}],
    }
    for name, lines in cases.items():
        bad = tmp_path / "bad.jsonl"
        bad.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        with pytest.raises(PointsError):
            read_awards(bad)
            raise AssertionError(f"{name} was accepted")
        with pytest.raises(PointsError):
            Ledger(bad)


def test_a_ledger_that_exists_without_the_carried_line_is_refused_when_one_is_asked_for(tmp_path):
    path = tmp_path / "points.jsonl"
    ledger = Ledger(path)
    ledger.close()
    path.write_text(json.dumps({"seq": 1, "step": "words", "block": "list1", "reason": "x", "amount": 5, "total": 5}) + "\n")
    with pytest.raises(PointsError, match="does not start with the line carried"):
        Ledger(path, carried=(5, "P001_x"))


def test_a_continued_ledger_adds_awards_on_top_of_the_carried_total(pty_device, tmp_path):
    made = make_rig(pty_device, tmp_path, carried=(200, "P001_20261125_101010"), name="cont")
    try:
        assert made.session.state()["points_total"] == 200 and made.points == []
        start_block(made.session, "words")
        run_until(made.session, lambda s: s["phase"] == "between")
        assert made.session.state()["points_total"] == 300
        assert made.points[0]["total"] == 300 and made.points[0]["gained"] == 100
        lines = made.ledger_lines()
        assert [(x["reason"], x["total"]) for x in lines] == [("carried", 200), ("block complete", 300)]
        made.session.skip()
        assert made.session.state()["points_total"] == 300
        assert_consistent(made)
    finally:
        made.session.close()
        made.ledger.close()
        made.source.stop()


# -- progress.scan / latest on prepared folders ---------------------------------------------------


def write_session(data: Path, name: str, uuid: str, started: float, steps, total=0, status="stopped", ledger=True, **extra):
    """A session folder like the engine leaves it (made by hand so that old folders can be shaped)."""
    folder = data / "sessions" / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(
        json.dumps({"session": name, "label": "PXXX", "uuid": uuid, "started_unix": started, "source": "live"})
    )
    progress.write(
        folder,
        {
            "session": name,
            "uuid": uuid,
            "continues": extra.get("continues"),
            "seed": extra.get("seed", 5),
            "status": status,
            "plan": [s[0] for s in steps],
            "steps": [{"id": i, "kind": k, "title": i.title(), "outcome": o} for i, k, o in steps],
            "points_total": total,
        },
    )
    if ledger:
        lines = []
        if total:
            lines.append(json.dumps({"seq": 1, "step": "words", "block": "list1", "reason": "block complete", "amount": total, "total": total}))
        (folder / "points.jsonl").write_text("".join(x + "\n" for x in lines))
    return folder


STEPS = [
    ("participant", "setup", "finished"),
    ("words", "task", "finished"),
    ("nback", "task", "not_reached"),
    ("summary", "summary", "not_reached"),
]


def test_latest_returns_the_next_round_of_the_newest_session_of_that_participant(tmp_path):
    write_session(tmp_path, "A_1", "u-a", 100.0, STEPS, total=100)
    found = progress.latest(tmp_path, "u-a")
    assert found is not None and found.session == "A_1" and found.next_step == "nback"
    assert found.points_total == 100 and found.seed == 5


def test_latest_is_none_after_a_finished_session_and_for_an_unknown_participant(tmp_path):
    done = [(i, k, "finished") for i, k, _ in STEPS]
    write_session(tmp_path, "A_1", "u-a", 100.0, done, status="finished", total=100)
    assert progress.latest(tmp_path, "u-a") is None
    assert progress.latest(tmp_path, "u-nobody") is None
    assert progress.latest(tmp_path / "no-such-folder", "u-a") is None


def test_latest_ignores_other_participants_whatever_their_folders_are_called(tmp_path):
    write_session(tmp_path, "P001_x", "u-b", 100.0, STEPS)
    write_session(tmp_path, "P002_y", "u-a", 50.0, STEPS)
    assert progress.latest(tmp_path, "u-a").session == "P002_y"  # the label in the name decides nothing
    assert progress.latest(tmp_path, "u-b").session == "P001_x"


def test_only_the_newest_session_is_offered_by_start_time_not_by_name(tmp_path):
    write_session(tmp_path, "Z_old", "u-a", 10.0, STEPS)
    write_session(tmp_path, "A_new", "u-a", 20.0, STEPS)
    assert progress.latest(tmp_path, "u-a").session == "A_new"


def test_a_session_already_continued_is_not_offered_again(tmp_path):
    write_session(tmp_path, "A_1", "u-a", 10.0, STEPS)
    done = [(i, k, "finished") for i, k, _ in STEPS]
    write_session(tmp_path, "A_2", "u-a", 20.0, done, status="finished", continues="A_1")
    assert progress.latest(tmp_path, "u-a") is None


def test_finished_rounds_come_from_every_readable_session_of_the_participant(tmp_path):
    write_session(tmp_path, "A_1", "u-a", 10.0, STEPS)
    write_session(tmp_path, "A_2", "u-a", 20.0, [("words", "task", "skipped"), ("nback", "task", "not_reached"), ("summary", "summary", "not_reached")])
    found = progress.scan(tmp_path, "u-a")
    assert found.finished_rounds == {"words"}
    assert progress.scan(tmp_path, "u-b").finished_rounds == set()


DAMAGES = {
    "not json": lambda f: (f / "progress.json").write_text("{not json"),
    "binary": lambda f: (f / "progress.json").write_bytes(b"\xff\xfe\x00\x01"),
    "empty": lambda f: (f / "progress.json").write_text(""),
    "a list": lambda f: (f / "progress.json").write_text("[]"),
    "no steps": lambda f: (f / "progress.json").write_text(json.dumps({"version": 1, "status": "stopped", "session": f.name, "steps": [], "points_total": 0})),
    "bad outcome": lambda f: (f / "progress.json").write_text(
        json.dumps({**json.loads((f / "progress.json").read_text()), "steps": [{"id": "a", "kind": "task", "title": "A", "outcome": "maybe"}]})
    ),
    "another version": lambda f: (f / "progress.json").write_text(json.dumps({**json.loads((f / "progress.json").read_text()), "version": 9})),
    "missing": lambda f: (f / "progress.json").unlink(),
    "ledger says more": lambda f: (f / "points.jsonl").write_text(json.dumps({"seq": 1, "step": "words", "block": "list1", "reason": "r", "amount": 900, "total": 900}) + "\n"),
    "ledger does not add up": lambda f: (f / "points.jsonl").write_text('{"seq": 1, "amount": 5, "total": 7, "reason": "r"}\n'),
    "ledger missing": lambda f: (f / "points.jsonl").unlink(),
    "another session's progress": lambda f: (f / "progress.json").write_text(json.dumps({**json.loads((f / "progress.json").read_text()), "session": "other"})),
}


@pytest.mark.parametrize("name", list(DAMAGES))
def test_a_damaged_or_missing_record_is_reported_in_words_treated_as_no_earlier_session_and_not_touched(tmp_path, name):
    folder = write_session(tmp_path, "A_1", "u-a", 10.0, STEPS, total=100)
    DAMAGES[name](folder)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    found = progress.scan(tmp_path, "u-a")
    assert found.latest is None
    assert len(found.problems) == 1 and found.own_problem == found.problems[0]
    assert "treated as no earlier session" in found.own_problem and "has not been changed" in found.own_problem
    assert str(folder) in found.own_problem
    assert progress.latest(tmp_path, "u-a") is None
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before  # never overwritten


@pytest.mark.parametrize(
    "text", ["{not json", "", "[]", json.dumps({"session": "B_1"}), json.dumps({"session": "elsewhere", "uuid": "u-a", "started_unix": 1})]
)
def test_a_damaged_session_json_in_an_old_folder_is_reported_and_other_sessions_are_still_found(tmp_path, text):
    odd = tmp_path / "sessions" / "B_1"
    odd.mkdir(parents=True)
    (odd / "session.json").write_text(text)
    (odd / "progress.json").write_text("{}")
    write_session(tmp_path, "A_1", "u-a", 10.0, STEPS)
    found = progress.scan(tmp_path, "u-a")
    assert found.latest is not None and found.latest.session == "A_1"
    assert len(found.problems) == 1 and "B_1" in found.problems[0] and "left alone" in found.problems[0]
    assert found.own_problem is None  # whose folder it is cannot be told
    assert (odd / "session.json").read_text() == text


def test_a_folder_without_any_files_and_a_stray_file_do_not_crash_the_scan(tmp_path):
    (tmp_path / "sessions" / "empty").mkdir(parents=True)
    (tmp_path / "sessions" / "stray.txt").write_text("x")
    write_session(tmp_path, "A_1", "u-a", 10.0, STEPS)
    found = progress.scan(tmp_path, "u-a")
    assert found.latest.session == "A_1" and len(found.problems) == 1


def test_a_damaged_newest_session_is_not_replaced_by_an_older_one(tmp_path):
    write_session(tmp_path, "A_old", "u-a", 10.0, STEPS)
    newest = write_session(tmp_path, "A_new", "u-a", 20.0, STEPS)
    (newest / "progress.json").write_text("{broken")
    found = progress.scan(tmp_path, "u-a")
    assert found.latest is None and found.own_problem is not None


# -- the routes and the websocket ----------------------------------------------------------------


@pytest.fixture
def device():
    """``device()`` makes another pty device; all are hung up at the end."""
    made: list[PtyDevice] = []

    def new() -> PtyDevice:
        dev = PtyDevice(onhead_stream(), config_reply())
        made.append(dev)
        return dev

    yield new
    for dev in made:
        dev.hang_up()


def post(a, body):
    return request(a, "POST", "/api/participant", body)


def states_until(ws, predicate, timeout=20.0):
    return next_state(ws, timeout=timeout, predicate=predicate)


def drive_to(ws, step, phase="waiting"):
    """Sends advance until the participant's state shows ``step`` in ``phase``."""
    deadline = time.monotonic() + 40
    state = next_state(ws)
    while not (state["step"] == step and state["phase"] == phase):
        assert time.monotonic() < deadline, state
        if state["phase"] in ("waiting", "between"):
            send(ws, cmd="advance")
        state = next_state(ws, predicate=lambda m, s=state: m["rev"] > s["rev"])
    return state


def run_a_session_and_stop_in_nback(a):
    """P001: words finished (200 points), perception skipped, stopped in the first block of nback."""
    assert post(a, {"label": "P001"})[0] == 200
    with open_ws(a) as ws:
        drive_to(ws, "words")
        send(ws, cmd="advance")  # block 1
        next_state(ws, predicate=lambda m: m["phase"] == "between")
        send(ws, cmd="advance")  # block 2
        next_state(ws, predicate=lambda m: m["phase"] == "between" and m["blocks_done"] == 2)
        send(ws, cmd="advance")  # perception, waiting
        state = next_state(ws, predicate=lambda m: m["step"] == "perception")
        assert state["points_total"] == 200
        send(ws, cmd="skip", step="perception")
        next_state(ws, predicate=lambda m: m["step"] == "nback")
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "running")
        send(ws, cmd="stop")
        final = next_state(ws, predicate=lambda m: m["stopped"])
    assert final["points_total"] == 200 and final["failure"] is None
    folder = session_folder(a)
    stop(a)
    return folder


def next_error(ws, timeout=10.0):
    """The next ``error`` message (states and other messages are passed over)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(ws.recv(timeout=timeout))
        if message["t"] == "error":
            return message
    raise AssertionError("no error message")


def test_skip_and_stop_over_the_websocket_follow_the_participant_rules(app, device):
    a = app(port=device().path, flow=SHORT)
    # before a label there is no session: refused, and said so
    with open_ws(a) as ws:
        next_state(ws)
        for command in ({"cmd": "skip"}, {"cmd": "stop"}):
            send(ws, **command)
            error = next_error(ws)
            assert error["kind"] == "refused" and "participant ID first" in error["msg"]
    assert post(a, {"label": "P001"})[0] == 200
    folder = session_folder(a)
    with open_ws(a) as participant, open_ws(a, "onlooker") as onlooker:
        drive_to(participant, "words")
        events = len(events_of(folder))
        send(onlooker, cmd="skip")  # a read-only client: ignored, no answer
        send(onlooker, cmd="stop")
        send(participant, cmd="skip", step="device")  # a stale press
        error = next_error(participant)
        assert error["kind"] == "refused" and "no longer the current step" in error["msg"]
        assert len(events_of(folder)) == events
        send(participant, cmd="choose", only="nback")  # the old command is gone
        error = next_error(participant)
        assert error["kind"] == "bad_command" and "choose" in error["msg"]
        send(participant, cmd="skip", step=5)
        assert next_error(participant)["kind"] == "bad_command"
        assert len(events_of(folder)) == events  # nothing the onlooker or the refusals sent was saved
        send(participant, cmd="skip", step="words")
        moved = next_state(participant, predicate=lambda m: m["step"] == "perception")
        assert moved["outcomes"]["words"] == "skipped" and moved["stopped"] is False


def test_a_stop_over_the_websocket_ends_the_session_and_progress_json_is_final(app, device):
    a = app(port=device().path, flow=SHORT)
    folder = run_a_session_and_stop_in_nback(a)
    saved = json.loads((folder / "progress.json").read_text())
    outcomes = {s["id"]: s["outcome"] for s in saved["steps"]}
    assert saved["status"] == "stopped" and saved["points_total"] == 200
    assert outcomes["words"] == "finished" and outcomes["perception"] == "skipped"
    assert outcomes["nback"] == "not_reached" and not any(v == "running" for v in outcomes.values())
    events = events_of(folder)
    assert [e["name"] for e in events].count("stopped") == 1 and events[-1]["name"] == "stopped"
    labels = [m["label"] for m in markers_of(folder / "eeg")]
    assert labels[-1].startswith("nback/stopped#") and any("/skipped#" in x for x in labels)
    assert [json.loads(x)["total"] for x in (folder / "points.jsonl").read_text().splitlines()] == [100, 200]
    meta = json.loads((folder / "eeg" / "meta.json").read_text())
    assert meta["participant"] == "P001"


def test_continuing_starts_a_new_session_that_carries_the_total_and_the_seed(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    seed = json.loads((earlier / "progress.json").read_text())["seed"]
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer == {
        "next_step": "nback",
        "next_title": "2-back",
        "points_total": 200,
        "session": earlier.name,
        "words_done": True,
        "note": None,
    }
    status, body = post(second, {"label": "P001", "continues": earlier.name})
    assert status == 200 and body["label"] == "P001"
    folders = sorted(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir())
    new = next(f for f in folders if f.name != earlier.name)
    assert json.loads((new / "session.json").read_text())["continues"] == earlier.name
    saved = json.loads((new / "progress.json").read_text())
    assert saved["continues"] == earlier.name and saved["seed"] == seed and saved["points_total"] == 200
    assert saved["plan"] == [
        "participant", "device", "calibration", "nback", "flanker", "spatial",
        "video", "rest_closed", "rest_open", "recall", "summary",
    ]  # fmt: skip
    first_line = json.loads((new / "points.jsonl").read_text().splitlines()[0])
    assert first_line["reason"] == "carried" and first_line["total"] == 200 and first_line["amount"] == 0
    assert first_line["carried_from"] == earlier.name
    # the earlier folder is untouched
    assert json.loads((earlier / "progress.json").read_text())["status"] == "stopped"
    with open_ws(second) as ws:
        state = drive_to(ws, "nback")
        assert state["points_total"] == 200 and state["plan"] == saved["plan"]
        send(ws, cmd="advance")
        messages = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = json.loads(ws.recv(timeout=10))
            messages.append(message)
            if message["t"] == "points":
                break
        points = messages[-1]
        assert points["t"] == "points" and points["total"] == 300 and points["gained"] == 100
        # no points message came before the block ended, and the total did not move
        assert all(m["points_total"] == 200 for m in messages if m["t"] == "state" and m["phase"] == "running")
    assert [json.loads(x)["total"] for x in (new / "points.jsonl").read_text().splitlines()] == [200, 300]
    assert json.loads((new / "progress.json").read_text())["points_total"] == 300


def test_a_session_can_be_continued_once_the_newest_one_is_the_one_asked_for(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    assert post(second, {"label": "P001", "continues": earlier.name})[0] == 200
    middle = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    stop(second)  # stopped by the end of the process: its record says ended_early, and it can be continued
    third = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, body = post(third, {"label": "P001", "continues": earlier.name})
    assert status == 409 and body["code"] == "not_continuable"  # superseded by the newer one
    folders = [p for p in (third.data_dir / "sessions").iterdir() if p.is_dir()]
    assert len(folders) == 2
    status, _ = post(third, {"label": "P001", "continues": middle.name})
    assert status == 200


def stop_in_the_second_block_of_words(a):
    """P001: the first words block finished (100 points), stopped in the second block."""
    assert post(a, {"label": "P001"})[0] == 200
    with open_ws(a) as ws:
        drive_to(ws, "words")
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "between")
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "running" and m["blocks_done"] == 1)
        send(ws, cmd="stop")
        final = next_state(ws, predicate=lambda m: m["stopped"])
    assert final["outcomes"]["words"] == "partial" and final["points_total"] == 100
    folder = newest_folder(a)
    stop(a)
    return folder


def test_continuing_after_a_stop_in_the_second_block_starts_at_the_next_round_and_never_pays_twice(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = stop_in_the_second_block_of_words(first)
    saved = json.loads((earlier / "progress.json").read_text())
    assert {s["id"]: s["outcome"] for s in saved["steps"]}["words"] == "partial"
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["next_step"] == "perception" and offer["points_total"] == 100
    assert offer["words_done"] is True  # a partly played words round counts as studied lists
    assert post(second, {"label": "P001", "continues": earlier.name})[0] == 200
    new = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    plan = json.loads((new / "progress.json").read_text())["plan"]
    assert "words" not in plan and plan[plan.index("calibration") + 1] == "perception"
    with open_ws(second) as ws:
        state = drive_to(ws, "perception")
        assert state["points_total"] == 100
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "between")
    totals = [json.loads(x)["total"] for x in (new / "points.jsonl").read_text().splitlines()]
    assert totals[0] == 100 and totals[1] == 200  # the carried line, then one award, not a second one for words
    assert [e["step"] for e in events_of(new) if e["name"] == "block_start"] == ["perception"]


def test_continuing_after_a_stop_in_the_first_block_starts_that_round_again(app, device):
    first = app(port=device().path, flow=SHORT)
    assert post(first, {"label": "P001"})[0] == 200
    with open_ws(first) as ws:
        drive_to(ws, "words")
        send(ws, cmd="skip", step="words")  # a skipped round counts as played
        drive_to(ws, "perception")
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "running")
        send(ws, cmd="stop")
        final = next_state(ws, predicate=lambda m: m["stopped"])
    assert final["outcomes"]["perception"] == "not_reached" and final["outcomes"]["words"] == "skipped"
    earlier = session_folder(first)
    stop(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    offer = request(second, "POST", "/api/progress", {"participant": "P001"})[1]
    assert offer["next_step"] == "perception" and offer["words_done"] is False
    assert post(second, {"label": "P001", "continues": earlier.name})[0] == 200
    new = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    plan = json.loads((new / "progress.json").read_text())["plan"]
    assert "words" not in plan and plan[plan.index("calibration") + 1] == "perception"
    # the first round is in fact entered, and its first block runs
    with open_ws(second) as ws:
        state = drive_to(ws, "perception")
        assert state["step"] == "perception" and state["plan"] == plan
        send(ws, cmd="advance")
        running = next_state(ws, predicate=lambda m: m["phase"] == "running")
        assert running["step"] == "perception" and running["block"] is not None
    starts = [e for e in events_of(new) if e["name"] == "block_start"]
    assert starts and starts[0]["step"] == "perception"


def test_a_block_ended_by_a_pause_then_a_stop_counts_as_played_when_the_session_is_continued(app, device):
    """Pause in a block, then Stop, then continue that session -> the
    paused block counts as played (only pause-interrupted blocks count as played; a
    block cut by the session's end is not, see the test above)."""
    first = app(port=device().path, flow=SHORT)
    assert post(first, {"label": "P001"})[0] == 200
    with open_ws(first) as ws:
        drive_to(ws, "words")
        send(ws, cmd="skip", step="words")  # a skipped round counts as played
        drive_to(ws, "perception")
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "running")
        send(ws, cmd="pause", reason="hand")
        paused = next_state(ws, predicate=lambda m: m["phase"] == "paused")
        assert paused["interrupted"] == "block1"
        send(ws, cmd="stop")
        final = next_state(ws, predicate=lambda m: m["stopped"])
    assert final["outcomes"]["perception"] == "partial" and final["blocks_by_step"]["perception"] == 1
    earlier = session_folder(first)
    ends = [e for e in events_of(earlier) if e["name"] == "block_end"]
    assert [(e["step"], e["block"], e["payload"]["ended_by"]) for e in ends] == [("perception", "block1", "interrupted")]
    saved = json.loads((earlier / "progress.json").read_text())
    assert {s["id"]: s["outcome"] for s in saved["steps"]}["perception"] == "partial"
    stop(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    offer = request(second, "POST", "/api/progress", {"participant": "P001"})[1]
    assert offer["next_step"] == "nback"  # perception is not played again
    assert post(second, {"label": "P001", "continues": earlier.name})[0] == 200
    new = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    plan = json.loads((new / "progress.json").read_text())["plan"]
    assert "perception" not in plan and plan[plan.index("calibration") + 1] == "nback"
    with open_ws(second) as ws:
        state = drive_to(ws, "nback")
        assert state["step"] == "nback"
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["phase"] == "running")
    starts = [e for e in events_of(new) if e["name"] == "block_start"]
    assert starts and starts[0]["step"] == "nback"


def play_one_round_and_stop(a, step, blocks=1):
    """P001 plays ``step`` alone: ``blocks`` blocks are finished, then the session is stopped."""
    assert post(a, {"label": "P001", "only": step})[0] == 200
    with open_ws(a) as ws:
        drive_to(ws, step)
        for n in range(1, blocks + 1):
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m, n=n: m["phase"] == "between" and m["blocks_done"] == n)
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["stopped"])
    folder = newest_folder(a)
    stop(a)
    return folder


def newest_folder(a) -> Path:
    """The newest session folder of the data folder (by the start time in session.json)."""
    found = [p for p in (a.data_dir / "sessions").iterdir() if p.is_dir()]
    return max(found, key=lambda p: json.loads((p / "session.json").read_text())["started_unix"])


def test_a_one_round_session_stands_apart_and_never_blocks_continuing_the_whole_route_session(app, device):
    first = app(port=device().path, flow=SHORT)
    whole = stop_in_the_second_block_of_words(first)  # the whole route, 100 points, stopped
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    alone = play_one_round_and_stop(second, "nback")  # newer than the first, 100 points of its own
    # recorded in both files, with its own ledger starting at 0 (no carried line)
    assert json.loads((alone / "session.json").read_text())["only"] == "nback"
    saved = json.loads((alone / "progress.json").read_text())
    assert saved["only"] == "nback" and saved["continues"] is None and saved["points_total"] == 100
    assert [json.loads(x)["reason"] for x in (alone / "points.jsonl").read_text().splitlines()] == ["block complete"]
    assert "only" not in json.loads((whole / "session.json").read_text())
    third = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(third, "POST", "/api/progress", {"participant": "P001"})
    # the offer is still the whole-route session, with its own total
    assert status == 200 and offer["session"] == whole.name and offer["points_total"] == 100
    assert offer["next_step"] == "perception"
    assert post(third, {"label": "P001", "continues": whole.name})[0] == 200
    new = next(p for p in (third.data_dir / "sessions").iterdir() if p.is_dir() and p.name not in (whole.name, alone.name))
    assert json.loads((new / "progress.json").read_text())["continues"] == whole.name
    lines = [json.loads(x) for x in (new / "points.jsonl").read_text().splitlines()]
    assert lines[0]["reason"] == "carried" and lines[0]["total"] == 100 and lines[0]["carried_from"] == whole.name
    assert "only" not in json.loads((new / "session.json").read_text())


def test_a_continues_naming_a_one_round_session_is_refused_and_creates_nothing(app, device):
    first = app(port=device().path, flow=SHORT)
    alone = play_one_round_and_stop(first, "nback")
    assert json.loads((alone / "session.json").read_text())["only"] == "nback"
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    before = sorted(p.name for p in (second.data_dir / "sessions").iterdir())
    participants = (second.data_dir / "participants.json").read_text()
    status, body = post(second, {"label": "P001", "continues": alone.name})
    assert status == 409 and body["code"] == "not_continuable"
    assert alone.name not in json.dumps(body)
    assert sorted(p.name for p in (second.data_dir / "sessions").iterdir()) == before
    assert (second.data_dir / "participants.json").read_text() == participants
    # nothing was started: the label is still free to start a session of its own
    assert post(second, {"label": "P001"})[0] == 200


def test_a_session_that_played_one_round_alone_is_not_offered_when_it_is_the_only_one(app, device):
    first = app(port=device().path, flow=SHORT)
    play_one_round_and_stop(first, "nback")
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["next_step"] is None and offer["session"] is None and offer["points_total"] == 0


def test_a_words_round_played_alone_counts_as_lists_studied_for_recall_alone(app, device):
    first = app(port=device().path, flow=SHORT)
    play_one_round_and_stop(first, "words", blocks=2)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    offer = request(second, "POST", "/api/progress", {"participant": "P001"})[1]
    assert offer["words_done"] is True and offer["next_step"] is None
    assert post(second, {"label": "P001", "only": "recall"})[0] == 200
    stop(second)
    # and one block of words played alone (partial) counts too
    third = app(port=device().path, flow=SHORT)
    play_one_round_and_stop(third, "words", blocks=1)
    fourth = app(port=device().path, flow=SHORT, data_dir=third.data_dir)
    assert request(fourth, "POST", "/api/progress", {"participant": "P001"})[1]["words_done"] is True


def test_continuing_never_mixes_participants(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    before = sorted(p.name for p in (second.data_dir / "sessions").iterdir())
    participants = (second.data_dir / "participants.json").read_text()
    # another label is not offered the session ...
    status, offer = request(second, "POST", "/api/progress", {"participant": "P002"})
    assert status == 200 and offer == {
        "next_step": None, "next_title": None, "points_total": 0, "session": None, "words_done": False, "note": None,
    }  # fmt: skip
    assert earlier.name not in json.dumps(offer)
    # ... and cannot ask for it by name either
    status, body = post(second, {"label": "P002", "continues": earlier.name})
    assert status == 409 and body["code"] == "not_continuable"
    assert earlier.name not in json.dumps(body) and "P001" not in json.dumps(body)
    for name in ("../x", "", "nope", str(earlier), "sessions/" + earlier.name, "P001_20000101_000000"):
        status, body = post(second, {"label": "P002", "continues": name})
        assert status in (400, 409) and body["code"] in ("not_continuable", "bad_body"), name
    assert sorted(p.name for p in (second.data_dir / "sessions").iterdir()) == before
    assert (second.data_dir / "participants.json").read_text() == participants
    assert request(second, "GET", "/api/participants")[1] == {"labels": ["P001"]}
    # and the same label that is new in this data folder cannot take it either
    assert post(second, {"label": "P002"})[0] == 200
    folder = next(p for p in (second.data_dir / "sessions").iterdir() if p.name not in before)
    assert "continues" not in json.loads((folder / "session.json").read_text())
    assert json.loads((folder / "progress.json").read_text())["continues"] is None
    assert (folder / "points.jsonl").read_text() == ""  # nothing carried into another participant's ledger


def test_a_label_that_differs_only_by_case_is_another_participant_and_is_not_offered_the_session(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    assert request(second, "POST", "/api/progress", {"participant": "p001"})[1]["next_step"] is None
    assert post(second, {"label": "p001", "continues": earlier.name})[1]["code"] == "not_continuable"


def test_the_progress_route_answers_the_same_for_an_unknown_label_and_one_without_sessions(app, device):
    a = app(port=device().path, flow=SHORT)
    same = {"next_step": None, "next_title": None, "points_total": 0, "session": None, "words_done": False, "note": None}
    assert request(a, "POST", "/api/progress", {"participant": "P009"}) == (200, same)
    assert post(a, {"label": "P001"})[0] == 200
    assert request(a, "POST", "/api/progress", {"participant": "P009"}) == (200, same)
    # the label whose session is running right now is answered like one with no earlier session
    assert request(a, "POST", "/api/progress", {"participant": "P001"}) == (200, same)


@pytest.mark.parametrize("label", ["", "a b", "../x", "a" * 33])
def test_the_progress_route_refuses_a_bad_label_with_a_code(app, device, label):
    a = app(port=device().path, flow=SHORT)
    status, body = request(a, "POST", "/api/progress", {"participant": label})
    assert status == 400 and body["code"] == "invalid_label"


@pytest.mark.parametrize("path", ["/api/progress", "/api/progress?participant=P001"])
def test_the_progress_route_has_no_get_so_a_label_is_never_part_of_a_url(app, device, path):
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001"})[0] == 200
    status, body = request(a, "GET", path)
    assert status == 405  # the answer names no participant and no session
    assert b"P001" not in (body if isinstance(body, bytes) else json.dumps(body).encode())


def test_the_answer_for_an_unknown_label_equals_the_answer_for_a_known_label_without_an_earlier_session(app, device):
    first = app(port=device().path, flow=SHORT)
    run_a_session_and_stop_in_nback(first)  # P001 has a session to continue
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    assert post(second, {"label": "P002"})[0] == 200  # known now, and nothing played
    stop(second)
    third = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    unknown = request(third, "POST", "/api/progress", {"participant": "P009"})
    known = request(third, "POST", "/api/progress", {"participant": "P002"})
    assert unknown == known  # status and the whole body: nothing tells whether the label exists
    assert unknown[0] == 200 and unknown[1]["next_step"] is None


def test_the_progress_post_gives_the_same_answer_with_the_label_in_the_body(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    expected = {
        "next_step": "nback",
        "next_title": "2-back",
        "points_total": 200,
        "session": earlier.name,
        "words_done": True,
        "note": None,
    }
    assert request(second, "POST", "/api/progress", {"participant": "P001"}) == (200, expected)
    assert request(second, "POST", "/api/progress", {"participant": "P001"})[1]["session"] == earlier.name
    assert request(second, "POST", "/api/progress", {"nobody": 1})[1]["code"] == "bad_body"
    assert request(second, "POST", "/api/progress", raw=b"not json")[1]["code"] == "bad_body"
    assert request(second, "POST", "/api/progress", {"participant": "a b"})[1]["code"] == "invalid_label"


def test_the_progress_route_checks_host_and_origin(app, device):
    a = app(port=device().path, flow=SHORT)
    assert request(a, "POST", "/api/progress", {"participant": "P001"}, host="example.com")[0] == 403
    assert request(a, "POST", "/api/progress", {"participant": "P001"}, host="127.0.0.1:1")[0] == 403
    assert request(a, "POST", "/api/progress", {"participant": "P001"}, origin="http://example.com")[0] == 403
    assert request(a, "POST", "/api/progress", {"participant": "P001"}, origin=None)[0] == 403
    assert request(a, "POST", "/api/progress", {"participant": "P001"})[0] == 200


def test_a_damaged_record_is_explained_to_the_page_and_nothing_is_overwritten_or_crashes(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    (earlier / "progress.json").write_text("{this is not json")
    before = (earlier / "progress.json").read_bytes()
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["next_step"] is None and offer["session"] is None
    assert "progress.json is not valid JSON" in offer["note"] and "no earlier session" in offer["note"]
    status, body = post(second, {"label": "P001", "continues": earlier.name})
    assert status == 409 and body["code"] == "not_continuable"
    assert post(second, {"label": "P001"})[0] == 200  # starting from the beginning still works
    assert (earlier / "progress.json").read_bytes() == before


def test_a_session_folder_from_before_progress_files_is_treated_as_no_earlier_session(app, device):
    first = app(port=device().path, flow=SHORT)
    earlier = run_a_session_and_stop_in_nback(first)
    (earlier / "progress.json").unlink()  # what an older version left
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["next_step"] is None and "is missing" in offer["note"]
    assert not (earlier / "progress.json").exists()


def test_a_damaged_session_json_elsewhere_is_logged_without_a_label_and_does_not_stop_the_route(
    inprocess,  # noqa: F811  (the fixture imported from test_shell_browser)
    tmp_path,
    caplog,
):
    import logging

    data = tmp_path / "data"
    ident = "00000000-0000-4000-8000-00000000000a"
    odd = data / "sessions" / "P777_20200101_000000"
    odd.mkdir(parents=True)
    (odd / "session.json").write_text("{broken")
    (data / "participants.json").write_text(
        json.dumps({"version": 1, "participants": [{"label": "P001", "uuid": ident}]})
    )
    write_session(data, "P001_20260102_000000", ident, 20.0, STEPS, total=100)
    server = inprocess(SHORT)
    caplog.set_level(logging.WARNING, logger="brain_mapper")
    a = type("A", (), {"host_port": ("127.0.0.1", server.port)})()
    status, offer = request(a, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["session"] == "P001_20260102_000000" and offer["note"] is None
    assert offer["next_step"] == "nback" and offer["points_total"] == 100
    assert (odd / "session.json").read_text() == "{broken"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not be read and were left alone" in text
    assert "P777" not in text and "P001" not in text


# -- choosing rounds with the label ---------------------------------------------------------------


def test_a_label_with_only_plays_setup_that_round_and_the_summary(app, device):
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "only": "spatial"})[0] == 200
    with open_ws(a) as ws:
        state = next_state(ws)
    assert state["plan"] == ["participant", "device", "calibration", "spatial", "summary"]
    folder = session_folder(a)
    saved = json.loads((folder / "progress.json").read_text())
    assert saved["plan"] == state["plan"] and saved["continues"] is None
    assert [s["id"] for s in saved["steps"]] == state["plan"]


def test_a_label_with_start_at_plays_that_round_and_those_after_it(app, device):
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "start_at": "rest_closed"})[0] == 200
    with open_ws(a) as ws:
        state = next_state(ws)
    assert state["plan"] == ["participant", "device", "calibration", "rest_closed", "rest_open", "recall", "summary"]


@pytest.mark.parametrize(
    "extra, code",
    [
        ({"only": "nope"}, "unknown_step"),
        ({"start_at": "nope"}, "unknown_step"),
        ({"only": "device"}, "unknown_step"),
        ({"only": "summary"}, "unknown_step"),
        ({"only": ""}, "unknown_step"),
        ({"only": "words", "start_at": "nback"}, "bad_selection"),
        ({"continues": "x", "only": "words"}, "bad_selection"),
        ({"continues": "x", "start_at": "words"}, "bad_selection"),
        ({"only": 5}, "bad_body"),
        ({"start_at": ["words"]}, "bad_body"),
        ({"continues": 7}, "bad_body"),
        ({"only": "recall"}, "recall_needs_words"),
        ({"start_at": "recall"}, "recall_needs_words"),
        ({"continues": "P001_20000101_000000"}, "not_continuable"),
    ],
)
def test_a_choice_that_cannot_be_played_is_refused_in_words_and_nothing_is_created(app, device, extra, code):
    a = app(port=device().path, flow=SHORT)
    status, body = post(a, {"label": "P001", **extra})
    assert status in (400, 409) and body["code"] == code and body["error"]
    assert not (a.data_dir / "sessions").exists() or not any((a.data_dir / "sessions").iterdir())
    assert not (a.data_dir / "participants.json").exists()
    assert post(a, {"label": "P001"})[0] == 200  # and the label can still be accepted afterwards


def test_the_unknown_round_is_named_with_the_rounds_in_the_answer(app, device):
    a = app(port=device().path, flow=SHORT)
    _, body = post(a, {"label": "P001", "only": "nope"})
    assert "'nope'" in body["error"] and "words, perception, nback" in body["error"]


def test_recall_alone_is_accepted_once_the_participant_finished_the_words(app, device):
    first = app(port=device().path, flow=SHORT)
    run_a_session_and_stop_in_nback(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    assert request(second, "POST", "/api/progress", {"participant": "P001"})[1]["words_done"] is True
    assert post(second, {"label": "P001", "only": "recall"})[0] == 200
    with open_ws(second) as ws:
        assert next_state(ws)["plan"] == ["participant", "device", "calibration", "recall", "summary"]
    # another participant has not studied words
    stop(second)
    third = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, body = post(third, {"label": "P002", "only": "recall"})
    assert status == 409 and body["code"] == "recall_needs_words"


def test_a_label_accepted_again_with_another_choice_is_refused_and_with_the_same_choice_is_not(app, device):
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "only": "words"})[0] == 200
    assert post(a, {"label": "P001", "only": "words"})[0] == 200
    status, body = post(a, {"label": "P001"})
    assert status == 409 and body["code"] == "already_set"
    status, body = post(a, {"label": "P001", "only": "nback"})
    assert status == 409 and body["code"] == "already_set"
    assert len([p for p in (a.data_dir / "sessions").iterdir()]) == 1


def test_a_label_post_without_a_choice_gives_the_same_answer_as_before(app, device):
    a = app(port=device().path, flow=SHORT)
    status, body = post(a, {"label": "P001"})
    assert status == 200 and set(body) == {"label", "uuid"}
    with open_ws(a) as ws:
        state = next_state(ws)
    assert state["plan"] == [s.id for s in flow.load(SHORT)] and state["stopped"] is False
    stop(a)
    assert a.process.returncode is not None


def test_a_session_started_with_a_choice_leaves_the_source_thread_and_the_link_running(app, device):
    """The accepted choice must not get in the way of what the server does for every chunk."""
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "only": "nback"})[0] == 200
    with open_ws(a) as ws:
        deadline = time.monotonic() + 3
        kinds = []
        while time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv(timeout=0.5))
            except TimeoutError:
                continue
            kinds.append((message["t"], message.get("state") or message.get("kind")))
        assert ("link", "ended") not in kinds and not [k for k in kinds if k[0] == "error"], kinds
    with open_ws(a) as ws:
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        # Trace data comes only from the source thread (states come from the ticker whatever
        # that thread does), so a "d" message is what shows that the thread is alive.
        deadline = time.monotonic() + 10
        data = None
        while data is None and time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv(timeout=1))
            except TimeoutError:
                continue
            assert not (message["t"] == "link" and message["state"] == "ended"), message
            if message["t"] == "d":
                data = message
        assert data is not None, "no trace data arrived in 10 s"
        assert len(data["x"]) > 0 and len(data["x"][0]) == 20


NOTHING_PLAYED = [(i, k, "not_reached") for i, k, _ in STEPS]


def test_a_session_in_which_nothing_was_played_is_not_offered_but_a_skipped_round_or_points_are_enough(tmp_path):
    write_session(tmp_path, "A_1", "u-a", 10.0, NOTHING_PLAYED, status="stopped")
    found = progress.scan(tmp_path, "u-a")
    assert found.latest is None and found.problems == [] and found.own_problem is None
    write_session(tmp_path, "A_2", "u-a", 20.0, [("words", "task", "skipped"), ("nback", "task", "not_reached")], status="stopped")
    assert progress.latest(tmp_path, "u-a").session == "A_2"  # a skipped round is something to carry on from
    write_session(tmp_path, "A_3", "u-a", 30.0, NOTHING_PLAYED, status="stopped", total=300)
    assert progress.latest(tmp_path, "u-a").session == "A_3"  # carried points are too


def test_a_session_that_got_no_further_than_the_setup_is_not_offered_and_cannot_be_continued(app, device):
    first = app(port=device().path, flow=SHORT)
    assert post(first, {"label": "P001"})[0] == 200
    earlier = session_folder(first)
    stop(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    offer = request(second, "POST", "/api/progress", {"participant": "P001"})[1]
    assert offer["next_step"] is None and offer["session"] is None and offer["note"] is None
    status, body = post(second, {"label": "P001", "continues": earlier.name})
    assert status == 409 and body["code"] == "not_continuable"

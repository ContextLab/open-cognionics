"""A pause ends the running block and the session goes on with the next one (resuming
a task part-way after a break would be a different task); the page may end
a block early with ``block_finished``; a task block may run up to ``TASK_GRACE_S`` past its time; the
headset check of the paused screen; video points.

The engine runs on a real simulated headset (a pseudo-terminal) and the real clock; flow files are
written for each test. Nothing is mocked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from test_engine import events_of, run_until

from brain_mapper import engine, flow, progress, scores
from brain_mapper.engine import CommandRefused, Session
from brain_mapper.source import LiveSource


def write_flow(tmp_path: Path, steps: list[dict]) -> Path:
    path = tmp_path / "flow.json"
    path.write_text(json.dumps(steps))
    return path


def setup_steps() -> list[dict]:
    return [{"id": "participant", "kind": "setup", "title": "Participant", "blocks": []}]


def blocks(n: int, seconds: float) -> list[dict]:
    return [{"id": f"block{i + 1}", "seconds": seconds, "points": 100} for i in range(n)]


SUMMARY = {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []}


@pytest.fixture
def rig(pty_device, tmp_path):
    """``make(steps)`` gives a session on a live pty source with those steps, recording."""
    made = []

    def make(steps: list[dict]) -> tuple[Session, Path]:
        source = LiveSource(pty_device.path)
        source.open()
        source.subscribe(4096)
        folder = tmp_path / f"session{len(made)}"
        source.start_recording(folder / "eeg", "P001", "uuid-1")
        session = Session(flow.load(write_flow(tmp_path, steps)), source, None, folder=folder)
        made.append((session, source))
        return session, folder

    yield make
    for session, source in made:
        session.close()
        source.stop()


def start(session: Session, step: str) -> None:
    while session.state()["step"] != step:
        run_until(session, lambda s: s["phase"] in ("waiting", "between"))
        session.advance()
    run_until(session, lambda s: s["phase"] == "waiting")
    session.advance()
    assert session.state()["phase"] == "running"


def names(folder: Path) -> list[str]:
    return [e["name"] for e in events_of(folder)]


# -- 1. a pause ends the running block ------------------------------------------------------------


def test_a_pause_in_a_task_block_ends_it_and_resume_goes_to_the_between_screen_then_the_next_block(rig):
    session, folder = rig([*setup_steps(), {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(2, 30)}, SUMMARY])
    start(session, "flanker")
    time.sleep(0.3)
    session.pause("fullscreen_lost")
    state = session.state()
    assert state["phase"] == "paused" and state["interrupted"] == "block1" and state["remaining"] is None
    assert state["blocks_done"] == 1 and state["interrupted_blocks"] == {"flanker": ["block1"]}
    end = [e for e in events_of(folder) if e["name"] == "block_end"][-1]
    assert end["payload"]["ended_by"] == "interrupted" and end["payload"]["points"] == 0
    assert end["payload"]["reason"] == "fullscreen_lost" and 0.25 <= end["payload"]["seconds"] < 5
    session.resume()
    state = session.state()
    assert state["phase"] == "between" and state["block"] == "block1"
    assert state["award"] == {"step": "flanker", "block": "block1", "points": 0, "interrupted": True}
    # From the between screen the next block starts as usual, with its full time.
    session.advance()
    state = session.state()
    assert (state["phase"], state["block"]) == ("running", "block2")
    assert 29 < state["remaining"] <= 30
    tail = [n for n in names(folder) if n in ("block_start", "paused", "block_end", "resumed")]
    assert tail == ["block_start", "paused", "block_end", "resumed", "block_start"]
    resumed = [e for e in events_of(folder) if e["name"] == "resumed"][-1]
    assert resumed["payload"] == {"to": "between"}


def test_a_pause_in_the_last_block_of_a_round_leads_to_the_next_round(rig):
    session, folder = rig(
        [
            *setup_steps(),
            {"id": "nback", "kind": "task", "title": "2-back", "module": None, "blocks": blocks(1, 30)},
            {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(1, 30)},
            SUMMARY,
        ]
    )
    start(session, "nback")
    session.pause("hand")
    session.resume()
    state = session.state()
    assert state["phase"] == "between" and state["blocks_done"] == state["blocks_total"] == 1
    session.advance()
    state = session.state()
    assert (state["step"], state["phase"]) == ("flanker", "waiting")
    # The interrupted round counts as played: finished (its one block was played) and never offered again.
    assert state["outcomes"]["nback"] == "finished"
    record = progress.read(folder)
    assert {s["id"]: s["outcome"] for s in record.steps}["nback"] == "finished"


def test_a_stop_after_an_interrupted_block_keeps_it_as_played(rig):
    session, folder = rig([*setup_steps(), {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(2, 30)}, SUMMARY])
    start(session, "flanker")
    session.pause("hand")
    session.stop()
    state = session.state()
    assert state["outcomes"]["flanker"] == "partial" and state["blocks_by_step"]["flanker"] == 1
    stopped = [e for e in events_of(folder) if e["name"] == "stopped"][0]
    assert stopped["payload"]["block"] is None  # no block was in progress: the pause had ended it


def test_a_pause_in_a_rest_block_restarts_the_rest_and_a_pause_in_a_video_holds_it(rig):
    session, folder = rig(
        [
            *setup_steps(),
            {"id": "video", "kind": "video", "title": "Video", "blocks": [{"id": "video", "seconds": 30, "points": 100}]},
            {"id": "rest_open", "kind": "rest", "title": "Rest, eyes open", "blocks": [{"id": "rest", "seconds": 30, "points": 100}]},
            SUMMARY,
        ]
    )
    start(session, "video")
    time.sleep(0.5)
    session.pause("hand")
    held = session.state()
    assert held["interrupted"] is None and 29 < held["remaining"] < 29.8
    session.resume()
    assert session.state()["phase"] == "running" and session.state()["block"] == "video"
    session.skip()
    start(session, "rest_open")
    time.sleep(0.5)
    session.pause("fullscreen_lost")
    assert session.state()["interrupted"] is None  # a rest is not counted as played: it starts again
    session.resume()
    state = session.state()
    assert (state["phase"], state["block"], state["blocks_done"], state["remaining"]) == ("waiting", "rest", 0, 30)
    session.advance()
    assert session.state()["phase"] == "running"
    rest = [e for e in events_of(folder) if e["step"] == "rest_open"]
    assert [e["name"] for e in rest] == ["step_start", "block_start", "paused", "block_end", "resumed", "block_start"]
    assert rest[3]["payload"]["ended_by"] == "interrupted"
    assert rest[4]["payload"] == {"to": "block_start"}


def test_a_pause_while_nothing_runs_changes_nothing(rig):
    session, folder = rig([*setup_steps(), {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(2, 1)}, SUMMARY])
    to = session.state()
    with pytest.raises(CommandRefused):
        session.pause("hand")  # the participant step
    start(session, "flanker")
    run_until(session, lambda s: s["phase"] == "between")
    before = session.state()
    with pytest.raises(CommandRefused):
        session.pause("hand")
    assert session.state()["rev"] == before["rev"] and to["rev"] < before["rev"]


# -- 6. block_finished --------------------------------------------------------------------------------


def test_block_finished_is_accepted_per_step_and_refused_otherwise(rig):
    session, folder = rig(
        [
            *setup_steps(),
            {"id": "words", "kind": "task", "title": "Words", "module": None, "may_finish_early": True, "blocks": blocks(1, 30)},
            {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(1, 30)},
            {"id": "rest_open", "kind": "rest", "title": "Rest", "blocks": [{"id": "rest", "seconds": 30, "points": 100}]},
            {"id": "recall", "kind": "recall", "title": "Delayed free recall", "module": None, "may_finish_early": True, "blocks": [{"id": "recall", "seconds": 30, "points": 100}]},
            SUMMARY,
        ]
    )
    with pytest.raises(CommandRefused):
        session.block_finished("participant_done")  # nothing is running
    start(session, "words")
    with pytest.raises(CommandRefused):
        session.block_finished("bored")  # not a reason
    with pytest.raises(CommandRefused):
        session.block_finished("participant_done", "flanker")  # not the round shown
    session.block_finished("participant_done", "words")
    state = session.state()
    assert state["phase"] == "between" and state["award"]["interrupted"] is False
    end = [e for e in events_of(folder) if e["name"] == "block_end"][-1]
    assert end["payload"]["ended_by"] == "participant_done" and end["payload"]["seconds"] < 5
    session.advance()
    start(session, "flanker")
    with pytest.raises(CommandRefused, match="not allowed"):
        session.block_finished("participant_done")  # flanker does not allow it
    session.block_finished("trials_done")  # a task step may say it ran its last trial
    assert [e for e in events_of(folder) if e["name"] == "block_end"][-1]["payload"]["ended_by"] == "trials_done"
    session.advance()
    start(session, "rest_open")
    for reason in ("participant_done", "trials_done"):
        with pytest.raises(CommandRefused):
            session.block_finished(reason)
    session.skip()
    start(session, "recall")
    with pytest.raises(CommandRefused, match="task round"):
        session.block_finished("trials_done")  # recall is not a task step
    session.pause("hand")
    with pytest.raises(CommandRefused):
        session.block_finished("participant_done")  # not while paused
    session.resume()
    session.advance()
    assert session.state()["step"] == "summary"


def test_the_default_flow_allows_finishing_early_in_words_recall_and_video_only():
    allowed = {s.id for s in flow.default_flow() if s.may_finish_early}
    assert allowed == {"words", "recall", "video"}


def test_a_task_block_drawn_by_a_page_runs_up_to_the_grace_past_its_time(rig):
    session, folder = rig(
        [
            *setup_steps(),
            {"id": "flanker", "kind": "task", "title": "Flanker", "module": "flanker", "blocks": blocks(2, 1)},
            {"id": "spatial", "kind": "task", "title": "Spatial", "module": None, "blocks": blocks(1, 1)},
            SUMMARY,
        ]
    )
    start(session, "flanker")
    began = time.monotonic()
    run_until(session, lambda s: s["phase"] == "between", timeout=20)
    took = time.monotonic() - began
    assert 1 + engine.TASK_GRACE_S - 0.1 <= took < 1 + engine.TASK_GRACE_S + 1.5
    # trials_done ends it as soon as the page says so, inside the grace.
    session.advance()
    run_until(session, lambda s: s["remaining"] == 0.0, timeout=5)
    session.block_finished("trials_done", "flanker")
    assert session.state()["phase"] == "between"
    # A placeholder (no page module) has no grace.
    session.advance()
    start(session, "spatial")
    began = time.monotonic()
    run_until(session, lambda s: s["phase"] == "between", timeout=20)
    assert time.monotonic() - began < 2


# -- 2. the headset check of the paused screen ------------------------------------------------------


def test_the_headset_check_opens_only_while_paused_and_resume_waits_for_it(rig):
    session, folder = rig([*setup_steps(), {"id": "flanker", "kind": "task", "title": "Flanker", "module": None, "blocks": blocks(2, 30)}, SUMMARY])
    first = {"states": {"F3": "good"}, "excluded": [], "thresholds_version": "x", "unix_time": 1.0}
    (folder / "calibration.json").write_text(json.dumps(first))
    start(session, "flanker")
    with pytest.raises(CommandRefused):
        session.headset_check()  # running, not paused
    session.pause("hand")
    session.headset_check()
    state = session.state()
    assert state["headset_check"]["phase"] == "contact" and state["contact_check"] == state["headset_check"]["check"]
    with pytest.raises(CommandRefused, match="Done"):
        session.resume()
    session.headset_check_done()
    state = session.state()
    assert state["contact_check"] is None
    # Without a hub there is no sample count to wait for: the next tick ends the settling.
    session.tick()
    assert session.state()["headset_check"] is None
    session.resume()
    assert session.state()["phase"] == "between"
    saved = json.loads((folder / "calibration.json").read_text())
    assert {k: saved[k] for k in first} == first  # the first result stays as it was
    assert len(saved["checks"]) == 1 and saved["checks"][0]["kind"] == "headset_check"
    assert saved["checks"][0]["utc"].endswith("Z") and saved["checks"][0]["reason"] == "hand"
    order = [n for n in names(folder) if n.startswith("headset_check")]
    assert order == ["headset_check_start", "headset_check_end", "headset_check_settled"]


# -- 4. video points ----------------------------------------------------------------------------------


def video_events(reports: list[tuple[str, float]], start_s=11.5, end_s=173.5) -> list[dict]:
    out = [{"name": "video.ready", "payload": {"start_s": start_s, "end_s": end_s}}]
    for name, at in reports:
        out.append({"name": name, "payload": {} if at is None else {"seconds": at}})
    return out


def test_video_points_count_each_full_10_s_watched_once():
    # Watched 11.5 .. 43.5 (3 full bins), with a pause at 25 s; then sought back to 15 s and watched
    # to 33 (nothing new); then sought ahead to 100 and watched 100 .. 112 (no full bin: 101.5 .. 111.5
    # is covered only from 100 on, that is all of it -> one bin).
    reports = [("video.play", 11.5)]
    reports += [("video.progress", 11.5 + k) for k in range(1, 14)]  # .. 24.5
    reports += [("video.pause", 25.0), ("video.play", 25.0)]
    reports += [("video.progress", 25.5 + k) for k in range(0, 19)]  # 25.5 .. 43.5
    reports += [("video.seek", None), ("video.play", 15.0)]
    reports += [("video.progress", 15.0 + k) for k in range(1, 19)]
    reports += [("video.seek", None), ("video.play", 100.0)]
    reports += [("video.progress", 100.0 + k) for k in range(1, 13)]  # .. 112
    result = scores.score_events(video_events(reports))
    assert result["tasks"]["video"] == {"earned": 4, "possible": 16, "trials": 4, "rule": "video_10s_watched"}
    assert result["total"] == {"earned": 4, "possible": 16}


def test_video_points_count_from_the_seek_to_the_start_of_the_part_played():
    # As the page plays it: play at 0, its seek to start_s, uncovered just after, then progress.
    events = [{"name": "video.ready", "payload": {"start_s": 11.5, "end_s": 173.5}},
              {"name": "video.play", "payload": {"seconds": 0.0}},
              {"name": "video.seek", "payload": {"to": 11.5, "reason": "range_start"}},
              {"name": "video.uncover", "payload": {"seconds": 11.6}}]
    events += [{"name": "video.progress", "payload": {"seconds": 12.4 + 1.02 * k}} for k in range(10)]  # .. 21.58
    assert scores.score_events(events)["tasks"]["video"]["earned"] == 1
    # The same seek while paused starts nothing.
    paused = [events[0], {"name": "video.seek", "payload": {"to": 11.5}}, *events[3:]]
    assert scores.score_events(paused)["tasks"]["video"]["earned"] == 1  # the uncover starts the chain at 11.6
    late = [events[0], *events[4:]]
    assert scores.score_events(late)["tasks"]["video"]["earned"] == 0  # from 12.4 on, 0.9 s of the bin is missing


def test_video_points_a_jump_or_a_gap_earns_nothing():
    reports = [("video.play", 11.5), ("video.progress", 21.6)]  # a jump of 10 s is a seek, not watching
    assert scores.score_events(video_events(reports))["tasks"]["video"]["earned"] == 0
    # Paused: the stretch from the pause to the next play is not watched, even if the position moved.
    reports = [("video.play", 11.5)] + [("video.progress", 11.5 + k) for k in range(1, 6)]
    reports += [("video.pause", 17.0), ("video.progress", 18.0)]
    reports += [("video.play", 19.0)] + [("video.progress", 19.0 + k) for k in range(1, 4)]
    assert scores.score_events(video_events(reports))["tasks"]["video"]["earned"] == 0

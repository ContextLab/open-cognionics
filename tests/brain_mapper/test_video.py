"""The video round without a browser: its block length, the engine's ``video_ended`` command over a
real pty device and the real websocket, and the ``video_host`` line of ``--check``.

Only ``test_the_video_host_check_passes_when_vimeo_answers`` needs the internet (marked
``network``); the rest runs offline.
"""

from __future__ import annotations

import json
import math
import socket
from pathlib import Path

import pytest

from brain_mapper import checks, flow
from brain_mapper.engine import CommandRefused, Session
from brain_mapper.source import LiveSource
from test_engine import (
    SHORT,
    accept,
    events_of,
    markers_of,
    next_state,
    open_ws,
    run_until,
    send,
    session_folder_of,
    start_block,
    to_step,
)

#: The part of Vimeo 19009646 played (from its frames, 2026-11-25): after the title, before the
#: closing credit and dedication.
START_S, END_S = 11.5, 173.5
#: The video block: the part played in whole seconds (162) and 3 s for the player to start.
VIDEO_SECONDS = 165.0


def video_flow(tmp_path: Path, seconds: float = 20.0) -> Path:
    """The short flow with a video block of ``seconds``."""
    raw = json.loads(SHORT.read_text())
    for step in raw:
        if step["id"] == "video":
            step["blocks"][0]["seconds"] = seconds
    path = tmp_path / "video-flow.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.fixture
def live_video(pty_device, tmp_path):
    """A session on a pty ``LiveSource``, recording, with a 20 s video block and points."""
    from brain_mapper.points import Ledger

    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    ledger = Ledger(folder / "points.jsonl")
    session = Session(flow.load(video_flow(tmp_path)), source, ledger, folder=folder)
    yield session, folder
    session.close()
    source.stop()


def test_the_default_video_block_lasts_as_long_as_the_part_played_and_a_margin():
    video = next(s for s in flow.default_flow() if s.id == "video")
    assert video.kind == "video" and video.module == "video"
    assert video.video == flow.Video(19009646, START_S, END_S)
    assert [(b.id, b.seconds, b.points) for b in video.blocks] == [("video", VIDEO_SECONDS, 100)]
    assert flow.VIDEO_SECONDS == VIDEO_SECONDS == math.ceil(END_S - START_S) + flow.VIDEO_MARGIN_SECONDS


def test_a_flow_file_gives_the_part_played_or_the_default_one(tmp_path):
    raw = json.loads(SHORT.read_text())
    video = next(step for step in raw if step["id"] == "video")
    assert "video" not in video
    assert next(s for s in flow.load(SHORT) if s.id == "video").video == flow.VIDEO
    video["video"] = {"id": 19009646, "start_s": 20, "end_s": 30.5}
    path = tmp_path / "f.json"
    path.write_text(json.dumps(raw))
    assert next(s for s in flow.load(path) if s.id == "video").video == flow.Video(19009646, 20.0, 30.5)
    for bad in (
        {"id": 19009646, "start_s": 30, "end_s": 20},
        {"id": 19009646, "start_s": -1, "end_s": 20},
        {"id": 19009646, "start_s": 1},
        {"id": "19009646", "start_s": 1, "end_s": 2},
        {"id": 0, "start_s": 1, "end_s": 2},
    ):
        video["video"] = bad
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            flow.load(path)
    del video["video"]
    next(step for step in raw if step["kind"] == "task")["video"] = {"id": 1, "start_s": 0, "end_s": 1}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="only a video step can have a video"):
        flow.load(path)


def test_video_ended_ends_the_running_video_block_early_with_its_points(live_video):
    session, folder = live_video
    start_block(session, "video")
    before = session.state()["points_total"]
    run_until(session, lambda s: s["remaining"] < 19.5)
    session.video_ended("video")
    state = session.state()
    assert (state["step"], state["phase"], state["blocks_done"]) == (
        "video",
        "between",
        1,
    )
    assert state["award"] == {"step": "video", "block": "video", "points": 100, "interrupted": False}
    assert state["points_total"] == before + 100
    end = [e for e in events_of(folder) if e["name"] == "block_end"][-1]
    assert end["step"] == "video" and end["payload"]["block"] == "video"
    assert end["payload"]["ended_by"] == "video_ended"
    assert 0.5 <= end["payload"]["seconds"] < 20
    labels = [m["label"] for m in markers_of(folder / "eeg")]
    assert f"video/block_end#{end['seq']}" in labels
    # The between screen follows as after any block; leaving it goes on to the next step.
    session.advance()
    assert session.state()["step"] == "rest_closed"


def test_video_ended_is_refused_unless_a_video_block_is_running(live_video):
    session, folder = live_video
    start_block(session, "words")
    with pytest.raises(CommandRefused, match="video round"):
        session.video_ended("words")
    to_step(session, "video")
    run_until(session, lambda s: s["phase"] == "waiting")
    with pytest.raises(CommandRefused, match="running"):
        session.video_ended("video")
    session.advance()
    with pytest.raises(CommandRefused, match="no longer the current step"):
        session.video_ended("words")
    session.pause("fullscreen_lost")
    with pytest.raises(CommandRefused, match="running"):
        session.video_ended("video")
    assert session.state()["phase"] == "paused"
    assert not [e for e in events_of(folder) if e["name"] == "block_end" and e["step"] == "video"]


def test_video_ended_over_the_websocket_ends_the_block(app, pty_device, tmp_path):
    a = app(port=pty_device.path, flow=video_flow(tmp_path))
    accept(a)
    with open_ws(a) as ws:
        while True:
            state = next_state(ws)
            if state["step"] == "video" and state["phase"] == "running":
                break
            if state["phase"] in ("waiting", "between"):
                send(ws, cmd="advance")
        send(ws, cmd="video_ended", step="video")
        state = next_state(ws, predicate=lambda m: m["phase"] == "between")
        assert state["step"] == "video" and state["award"]["points"] == 100
    end = [e for e in events_of(session_folder_of(a)) if e["name"] == "block_end"][-1]
    assert end["payload"]["ended_by"] == "video_ended"


# -- the video_host check -----------------------------------------------------------------------


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_video_host_check_warns_without_failing_when_the_host_cannot_be_reached():
    check = checks.video_host(url=f"https://127.0.0.1:{closed_port()}/", timeout=3.0)
    assert check.id == "video_host"
    assert check.ok and check.warning  # a warning: --check still exits 0
    line = check.line()
    assert line.startswith("WARN  video_host: ")
    assert "only the video round needs it" in line.lower()
    assert "Fix:" in line
    assert set(check.as_dict()) == {"id", "ok", "text", "fix"}


@pytest.mark.network
def test_the_video_host_check_passes_when_vimeo_answers():
    check = checks.video_host()
    assert check.ok and not check.warning, check
    assert check.line().startswith("PASS  video_host: player.vimeo.com")

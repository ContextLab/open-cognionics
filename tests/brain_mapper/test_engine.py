"""The flow table and the session engine, on a real replayed recording or a real pty device.

No fake clock: the engine runs on ``time.monotonic`` and the flow file
``flows/short.json`` has 2 s blocks. Times are asserted from the ``unix_time``
the engine saved with each event.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from brain_mapper import flow
from brain_mapper.engine import CommandRefused, Session
from brain_mapper.server import Client, Hub, Settings
from brain_mapper.source import LiveSource, ReplaySource

SHORT = Path(__file__).parent / "flows" / "short.json"
#: Step ids in the order of spec 6.3.
ORDER = [
    "participant",
    "device",
    "calibration",
    "words",
    "perception",
    "nback",
    "flanker",
    "spatial",
    "video",
    "rest_closed",
    "rest_open",
    "recall",
    "summary",
]
#: 2-back, flanker and spatial memory have 2 blocks each (since 2026-11-25; were 3).
BLOCKS = {"words": 2, "perception": 2, "nback": 2, "flanker": 2, "spatial": 2}
#: Block lengths of the task steps (spec 6.4), the recall (spec 6.3) and the video (the part played,
#: 11.5 s to 173.5 s, in whole seconds, and 3 s for the player to start) in the default flow;
#: the rest blocks are the 3 minutes of spec 6.3.
TASK_SECONDS = {
    "words": 145, "perception": 60, "nback": 50, "flanker": 50, "spatial": 60, "recall": 125, "video": 165,
    "rest_closed": 180, "rest_open": 180,
}  # fmt: skip
TOLERANCE = 0.3


def run_until(session, predicate, timeout=30.0):
    """Ticks the session on the real clock until ``predicate(state)`` holds."""
    deadline = time.monotonic() + timeout
    while True:
        session.tick()
        state = session.state()
        if predicate(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out; state {state}")
        time.sleep(0.01)


def to_step(session, step_id):
    """Advances through setup steps (and between-block screens) up to ``step_id``."""
    while session.state()["step"] != step_id:
        run_until(session, lambda s: s["phase"] in ("waiting", "between"))
        session.advance()


def start_block(session, step_id):
    to_step(session, step_id)
    run_until(session, lambda s: s["phase"] == "waiting")
    session.advance()
    assert session.state()["phase"] == "running"


def events_of(folder):
    return [json.loads(x) for x in (folder / "events.jsonl").read_text().splitlines()]


def markers_of(rec):
    return [json.loads(x) for x in (rec / "markers.jsonl").read_text().splitlines()]


@pytest.fixture
def live(pty_device, tmp_path):
    """A session on a pty ``LiveSource`` with the short flow, recording."""
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    rec = tmp_path / "session" / "eeg"
    source.start_recording(rec, "P001", "uuid-1")
    session = Session(flow.load(SHORT), source, None, folder=tmp_path / "session")
    yield session, source, tmp_path / "session", rec
    session.close()
    source.stop()


# -- the flow table ---------------------------------------------------------


def test_default_flow_has_the_steps_of_spec_6_3_with_the_task_block_lengths_of_spec_6_4():
    steps = flow.default_flow()
    assert [s.id for s in steps] == ORDER
    assert [s.kind for s in steps] == [
        "setup", "setup", "setup", "task", "task", "task", "task", "task",
        "video", "rest", "rest", "recall", "summary",
    ]  # fmt: skip
    for step in steps:
        assert len(step.blocks) == BLOCKS.get(
            step.id, 1 if step.kind not in ("setup", "summary") else 0
        )
        seconds = TASK_SECONDS.get(step.id, 10)
        assert all(b.seconds == seconds and (b.points > 0) for b in step.blocks)
    assert [b.id for b in steps[3].blocks] == ["list1", "list2"]


def test_short_flow_has_the_same_steps_with_2_s_blocks():
    steps = flow.load(SHORT)
    default = flow.default_flow()
    assert [(s.id, s.kind, [b.id for b in s.blocks]) for s in steps] == [
        (s.id, s.kind, [b.id for b in s.blocks]) for s in default
    ]
    assert all(b.seconds == 2 for s in steps for b in s.blocks)


@pytest.mark.parametrize("step_id", ["device", "summary"])
def test_a_flow_file_with_points_on_a_setup_or_summary_step_is_refused(step_id, tmp_path):
    raw = json.loads(SHORT.read_text())
    for step in raw:
        if step["id"] == step_id:
            step["blocks"] = [{"id": "b", "seconds": 2, "points": 5}]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="cannot have"):
        flow.load(path)


def test_a_flow_file_with_a_duplicate_step_id_is_refused(tmp_path):
    raw = json.loads(SHORT.read_text())
    raw.append(raw[0])
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="duplicate step id"):
        flow.load(path)


def test_a_flow_file_with_an_unknown_kind_or_missing_field_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"id": "a", "kind": "nonsense", "title": "A"}]))
    with pytest.raises(ValueError, match="unknown kind"):
        flow.load(path)
    path.write_text(json.dumps([{"id": "a", "kind": "setup"}]))
    with pytest.raises(ValueError, match="bad step"):
        flow.load(path)


# -- the engine, on a pty device -------------------------------------------


def test_whole_flow_in_order_with_timed_blocks_and_one_event_per_marker(live):
    session, source, folder, rec = live
    assert session.state()["step"] == "participant"
    assert session.state()["phase"] == "waiting"
    while True:
        state = run_until(session, lambda s: s["phase"] in ("waiting", "between", "done"))
        if state["phase"] == "done":
            break
        session.advance()
    assert state["step"] == "summary"
    assert state["ended_early"] is False
    source.stop()  # closes the recording, so markers.jsonl is complete
    events = events_of(folder)
    steps = [e["step"] for e in events if e["name"] == "step_start"]
    assert steps == ORDER
    # Every block lasted its length.
    starts = [e for e in events if e["name"] == "block_start"]
    ends = [e for e in events if e["name"] == "block_end"]
    assert len(starts) == len(ends) == sum(len(s.blocks) for s in flow.load(SHORT))
    for a, b in zip(starts, ends):
        assert a["step"] == b["step"] and a["block"] == b["block"]
        assert b["unix_time"] - a["unix_time"] == pytest.approx(2.0, abs=TOLERANCE)
    # One events line per marker, the same seq, in the same order.
    markers = markers_of(rec)
    assert [m["label"] for m in markers] == [f"{e['step']}/{e['name']}#{e['seq']}" for e in events]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    samples = [m["sample"] for m in markers]
    assert samples == sorted(samples)
    words = [m for m in markers if m["label"].startswith("words/block")]
    assert [m["label"].split("#")[0] for m in words] == [
        "words/block_start", "words/block_end", "words/block_start", "words/block_end",
    ]  # fmt: skip
    # A block is about 1000 samples long; the next one starts at once, so the
    # end and the next start can share a chunk position.
    assert words[1]["sample"] - words[0]["sample"] >= 900
    assert words[3]["sample"] - words[2]["sample"] >= 900
    assert words[1]["sample"] <= words[2]["sample"]
    assert {"step", "block", "name", "payload", "browser_time", "time_origin", "unix_time"} <= set(
        events[0]
    )


def test_a_block_ends_in_between_and_blocks_done_counts(live):
    session, _, _, _ = live
    start_block(session, "perception")
    state = session.state()
    assert (state["block"], state["blocks_done"], state["blocks_total"]) == ("block1", 0, 2)
    assert state["kind"] == "task"
    state = run_until(session, lambda s: s["phase"] == "between")
    assert (state["block"], state["blocks_done"], state["remaining"]) == ("block1", 1, 0.0)
    session.advance()
    state = session.state()
    assert (state["phase"], state["block"], state["blocks_done"]) == ("running", "block2", 1)


def test_remaining_counts_down_in_real_time(live):
    session, _, _, _ = live
    start_block(session, "words")
    # The engine reads time.monotonic between the two readings of ours, so the
    # fall in ``remaining`` lies between the gaps we measured around it.
    a1 = time.monotonic()
    first = session.state()["remaining"]
    b1 = time.monotonic()
    time.sleep(0.5)
    a2 = time.monotonic()
    second = session.state()["remaining"]
    b2 = time.monotonic()
    assert 0 < first <= 2.0
    assert (a2 - b1) - 1e-6 <= first - second <= (b2 - a1) + 1e-6
    assert first - second >= 0.5


def to_video(session):
    """To the video round, waiting: the rounds before it are skipped."""
    to_step(session, "words")
    while session.state()["step"] != "video":
        session.skip()


def test_pause_stops_the_timer_and_the_block_runs_its_full_length_in_total(live):
    """Review focus (a): full screen lost during a block. In the video round, which a pause holds
    (since 2026-11-25 a pause ends a task block; see test_pause_next_block.py)."""
    session, source, folder, rec = live
    to_video(session)
    session.advance()
    time.sleep(0.6)
    session.pause("fullscreen_lost")
    state = session.state()
    assert state["phase"] == "paused"
    frozen = state["remaining"]
    assert 0 < frozen < 2.0
    time.sleep(1.0)
    session.tick()
    assert session.state()["remaining"] == frozen  # two readings 1 s apart are equal
    assert session.state()["phase"] == "paused"
    session.resume()
    assert session.state()["phase"] == "running"
    run_until(session, lambda s: s["phase"] == "between")
    source.stop()
    events = [e for e in events_of(folder) if e["step"] == "video"]
    by_name = {e["name"]: e for e in events}
    assert by_name["paused"]["payload"] == {"reason": "fullscreen_lost"}
    first = events[0]
    assert first["name"] == "step_start"
    start, paused, resumed, end = (
        by_name[n] for n in ("block_start", "paused", "resumed", "block_end")
    )
    away = resumed["unix_time"] - paused["unix_time"]
    assert away >= 1.0
    running = (end["unix_time"] - start["unix_time"]) - away
    assert running == pytest.approx(2.0, abs=TOLERANCE)
    labels = [m["label"].split("#")[0] for m in markers_of(rec) if m["label"].startswith("video/")]
    assert labels == [
        "video/step_start", "video/block_start", "video/paused", "video/resumed", "video/block_end",
    ]  # fmt: skip
    samples = [m["sample"] for m in markers_of(rec) if m["label"].startswith("video/")]
    # step_start and block_start follow one another at once; the other three are apart.
    assert samples[0] <= samples[1] < samples[2] < samples[3] < samples[4]
    assert samples[3] - samples[2] >= 450  # about 1 s at 500 Hz


def test_a_block_can_be_paused_twice(live):
    """A video block (held by a pause); a task block ends at its first pause."""
    session, _, _, _ = live
    to_video(session)
    session.advance()
    for _ in range(2):
        time.sleep(0.2)
        session.pause("fullscreen_lost")
        session.resume()
    state = run_until(session, lambda s: s["phase"] == "between")
    assert state["blocks_done"] == 1


def test_pause_and_resume_outside_their_phase_are_refused(live):
    session, _, folder, _ = live
    with pytest.raises(CommandRefused):
        session.pause("fullscreen_lost")  # participant step, waiting
    with pytest.raises(CommandRefused):
        session.resume()  # running or not, nothing is paused
    start_block(session, "words")
    with pytest.raises(CommandRefused):
        session.resume()  # running
    with pytest.raises(CommandRefused):
        session.advance()  # a running block cannot be skipped
    session.pause("fullscreen_lost")
    with pytest.raises(CommandRefused):
        session.pause("fullscreen_lost")  # already paused
    with pytest.raises(CommandRefused):
        session.advance()  # a paused block cannot be skipped
    session.resume()
    run_until(session, lambda s: s["phase"] == "between")
    with pytest.raises(CommandRefused):
        session.pause("fullscreen_lost")  # between blocks
    names = [e["name"] for e in events_of(folder)]
    assert names.count("paused") == 1 and names.count("resumed") == 1


def test_a_client_event_is_stamped_and_saved_with_both_browser_times(live):
    session, source, folder, rec = live
    start_block(session, "words")
    before = time.time()
    seq = session.event("word_shown", {"word": "apple", "i": 3}, 1234.5, 1.7e12)
    after = time.time()
    source.stop()
    line = [e for e in events_of(folder) if e["seq"] == seq][0]
    assert line["name"] == "word_shown"
    assert line["step"] == "words" and line["block"] == "list1"
    assert line["payload"] == {"word": "apple", "i": 3}
    assert line["browser_time"] == 1234.5
    assert line["time_origin"] == 1.7e12
    assert before <= line["unix_time"] <= after
    labels = [m["label"] for m in markers_of(rec)]
    assert f"words/word_shown#{seq}" in labels


@pytest.mark.parametrize(
    "name, payload, browser_time",
    [("a/b", {}, 1.0), ("", {}, 1.0), ("x#1", {}, 1.0), ("ok", [], 1.0), ("ok", {}, "now")],
)
def test_a_bad_event_is_refused_and_nothing_is_saved(live, name, payload, browser_time):
    session, _, folder, _ = live
    before = len(events_of(folder))
    with pytest.raises(CommandRefused):
        session.event(name, payload, browser_time, 1.0)
    assert len(events_of(folder)) == before


def test_the_state_says_when_it_is_a_rest_step(live):
    """The page hides points and progress from the participant when ``kind`` is ``rest``."""
    session, _, _, _ = live
    for step_id in ("rest_closed", "rest_open"):
        start_block(session, step_id)
        assert session.state()["kind"] == "rest"
        session.pause("fullscreen_lost")
        session.resume()  # a pause starts the rest again from its start screen
        assert session.state()["phase"] == "waiting"
        session.advance()
        run_until(session, lambda s: s["phase"] == "between")
    to_step(session, "recall")
    assert session.state()["kind"] == "recall"


def test_on_change_is_called_for_commands_and_for_the_end_of_a_block(live):
    session, _, _, _ = live
    seen = []
    session.on_change = seen.append
    start_block(session, "words")
    assert seen[-1]["phase"] == "running"
    run_until(session, lambda s: s["phase"] == "between")
    assert seen[-1]["phase"] == "between"


# -- the engine, on a replay that ends --------------------------------------


def test_when_the_source_ends_mid_block_the_session_is_done_and_ended_early(
    recorded_session, tmp_path
):
    """Review focus 3: the replay file ends mid-session."""
    source = ReplaySource(recorded_session)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    session = Session(flow.load(SHORT), source, None, folder=folder)
    try:
        while True:
            state = run_until(
                session, lambda s: s["phase"] in ("waiting", "between", "done"), timeout=40
            )
            if state["phase"] == "done":
                break
            session.advance()
        assert state["ended_early"] is True
        assert state["step"] != "summary"  # the flow is 34 s of blocks, the recording 15 s
        last = events_of(folder)[-1]
        assert last["name"] == "source_ended"
        assert last["payload"]["reason"] == "end of recording"
        with pytest.raises(CommandRefused):
            session.advance()
        with pytest.raises(CommandRefused):
            session.event("late", {}, 1.0, 1.0)
    finally:
        session.close()
        source.stop()
    # Markers were written for everything up to the end.
    markers = markers_of(folder / "eeg")
    assert markers and markers[0]["label"] == "participant/step_start#1"


# -- over the websocket ------------------------------------------------------


def open_ws(a, role="participant", page=None):
    """A websocket; a participant sends ``page``, a random page id if not given."""
    host, port = a.host_port
    query = f"role={role}&page={page or uuid.uuid4().hex}"
    # Entered here so that it can also be used without ``with`` (reload tests).
    return connect(f"ws://{host}:{port}/ws?{query}", origin=f"http://{host}:{port}").__enter__()


def next_state(ws, timeout=10.0, predicate=lambda m: True):
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("no state message")
        try:
            message = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        if message.get("t") == "state" and predicate(message):
            return message


def send(ws, **command):
    ws.send(json.dumps(command))


def accept(a, label="P001") -> dict:
    """Submits a participant label through the real route, as the page does.

    The recording and the session start only now (nothing is saved before a label is
    accepted); returns the answer, ``{"label", "uuid"}``.
    """
    host, port = a.host_port
    request = urllib.request.Request(
        f"http://{host}:{port}/api/participant",
        data=json.dumps({"label": label}).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        return json.load(response)


def session_folder_of(a):
    """The folder of the session in progress (there is exactly one, once a label is accepted)."""
    found = [p for p in (a.data_dir / "sessions").iterdir() if p.is_dir()]
    assert len(found) == 1, found
    return found[0]


def reconnect(a, page):
    """A reload: a new socket with the page id of the old one, which may still be open."""
    ws = open_ws(a, "participant", page)
    while True:
        message = json.loads(ws.recv(timeout=10))
        assert message["t"] != "error", message
        if message["t"] == "state":
            return ws, message


def test_state_is_sent_on_connect_on_every_change_and_once_a_second(app, recorded_session):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a) as ws:
        first = next_state(ws)
        assert (first["step"], first["kind"], first["phase"]) == ("participant", "setup", "waiting")
        assert first["blocks_total"] == 0 and first["ended_early"] is False
        t0 = time.monotonic()
        next_state(ws)
        next_state(ws)
        assert 1.0 <= time.monotonic() - t0 <= 3.0  # one per second when nothing changes
        send(ws, cmd="advance")
        assert next_state(ws, predicate=lambda m: m["step"] == "device")["phase"] == "waiting"


def test_every_client_gets_a_state_each_second_however_often_others_connect(app, recorded_session):
    """The heartbeat is counted per client: other clients connecting and leaving neither
    delay nor starve it, and a client is not sent a second state right after one."""
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a) as first:
        next_state(first)
        stamps = [time.monotonic()]
        done = threading.Event()

        def read():  # arrival times are taken here, not in the loop that cycles the other clients
            while not done.is_set():
                try:
                    message = json.loads(first.recv(timeout=0.05))
                except TimeoutError:
                    continue
                if message.get("t") == "state":
                    stamps.append(time.monotonic())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        end = stamps[0] + 3.0
        while time.monotonic() < end:
            open_ws(a, "onlooker").close()
        done.set()
        reader.join(timeout=5)
        gaps = [b - a_ for a_, b in zip(stamps, stamps[1:])] + [end - stamps[-1]]
        assert len(stamps) >= 3, stamps
        assert max(gaps) <= 1.1, gaps
        # no redundant heartbeat right after a state: states of an idle session are about a second apart
        assert min(gaps[1:-1]) >= 0.9, gaps  # the first gap may hold a start-up change


def test_a_page_reloaded_mid_session_gets_the_step_and_the_timer_where_the_server_has_it(
    app, recorded_session
):
    """Review focus (b)."""
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    ws = open_ws(a, page="page-1")
    next_state(ws)
    for _ in range(4):  # participant, device, calibration -> words; then start the block
        send(ws, cmd="advance")
    state = next_state(ws, predicate=lambda m: m["phase"] == "running")
    assert (state["step"], state["block"]) == ("words", "list1")
    time.sleep(0.3)
    send(ws, cmd="pause", reason="fullscreen_lost")
    paused = next_state(ws, predicate=lambda m: m["phase"] == "paused")
    reloaded, first = reconnect(a, "page-1")  # the old socket is still open
    with reloaded:
        assert first["phase"] == "paused"
        assert first["reason"] == "fullscreen_lost"
        assert (first["step"], first["block"]) == ("words", "list1")
        # The pause ended the block: the reloaded page is told so.
        assert first["interrupted"] == paused["interrupted"] == "list1"
        assert first["remaining"] is None and paused["remaining"] is None
        time.sleep(1.0)
        later = next_state(reloaded, predicate=lambda m: m["phase"] == "paused")
        assert later["interrupted"] == "list1"
        send(reloaded, cmd="resume")
        done = next_state(reloaded, predicate=lambda m: m["phase"] == "between")
        assert done["blocks_done"] == 1 and done["award"]["interrupted"] is True


def test_a_page_reloaded_during_a_running_block_gets_the_timer_where_the_server_has_it(
    app, recorded_session
):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    ws = open_ws(a, page="page-1")
    next_state(ws)
    for _ in range(4):
        send(ws, cmd="advance")
    before = next_state(ws, predicate=lambda m: m["phase"] == "running")
    received = time.monotonic()
    time.sleep(0.5)
    ws.close()
    reloaded, first = reconnect(a, "page-1")
    elapsed = time.monotonic() - received
    with reloaded:
        assert first["phase"] == "running"
        assert (first["step"], first["block"]) == ("words", "list1")
        # The timer went on while the page was away: it is neither reset nor frozen.
        low = before["remaining"] - elapsed - 0.3
        assert low <= first["remaining"] <= before["remaining"] - 0.2


def refused(second):
    """The messages a refused connection gets before it is closed."""
    messages = []
    with pytest.raises(ConnectionClosed):
        while True:
            messages.append(json.loads(second.recv(timeout=10)))
    return messages


def test_a_second_participant_connection_with_another_page_id_is_refused_and_closed(
    app, recorded_session
):
    """Review focus (b)."""
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a, page="page-1") as first:
        next_state(first)
        messages = refused(open_ws(a, page="page-2"))
        errors = [m for m in messages if m["t"] == "error"]
        assert [m["kind"] for m in errors] == ["second_window"]
        assert "/onlooker" in errors[0]["msg"]
        assert not [m for m in messages if m["t"] == "state"]
        # The first window still works and the refused one changed nothing.
        send(first, cmd="advance")
        assert next_state(first, predicate=lambda m: m["step"] == "device")


def test_a_participant_connection_without_a_page_id_is_refused_while_one_is_connected(
    app, recorded_session
):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    host, port = a.host_port
    with open_ws(a, page="page-1") as first:
        next_state(first)
        origin = f"http://{host}:{port}"
        bare = connect(f"ws://{host}:{port}/ws?role=participant", origin=origin).__enter__()
        assert [m["kind"] for m in refused(bare) if m["t"] == "error"] == ["second_window"]


def test_the_same_page_id_replaces_the_old_socket_and_the_refusal_still_holds(
    app, recorded_session
):
    """A reload gets in at once, with the old socket still open; another page stays out."""
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    old = open_ws(a, page="page-1")
    next_state(old)
    new, first = reconnect(a, "page-1")
    with new:
        assert first["step"] == "participant"
        # The old socket is closed by the server.
        with pytest.raises(ConnectionClosed):
            while True:
                old.recv(timeout=10)
        # A different page is still refused, after the replacement.
        messages = refused(open_ws(a, page="page-2"))
        assert [m["kind"] for m in messages if m["t"] == "error"] == ["second_window"]
        send(new, cmd="advance")
        assert next_state(new, predicate=lambda m: m["step"] == "device")
        # And a second reload replaces the replacement.
        newer, again = reconnect(a, "page-1")
        with newer:
            assert again["step"] == "device"
            with pytest.raises(ConnectionClosed):
                while True:
                    new.recv(timeout=10)


def test_a_reload_after_the_old_socket_closed_is_accepted(app, recorded_session):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    first = open_ws(a, page="page-1")
    next_state(first)
    first.close()
    ws, state = reconnect(a, "page-1")
    ws.close()
    assert state["step"] == "participant"


def test_an_onlooker_receives_state_but_its_commands_are_ignored(app, recorded_session):
    """Review focus 4: an onlooker's advance changes nothing."""
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a) as participant, open_ws(a, "onlooker") as onlooker:
        next_state(participant)
        assert next_state(onlooker)["step"] == "participant"
        send(onlooker, cmd="advance")
        send(onlooker, cmd="pause", reason="x")
        send(onlooker, cmd="event", name="sneaky", payload={}, browser_time=1.0, time_origin=1.0)
        for _ in range(3):  # three state messages, over about 2 s, still on the first step
            assert next_state(onlooker)["step"] == "participant"
        send(participant, cmd="advance")
        assert next_state(onlooker, predicate=lambda m: m["step"] == "device")
    events = [json.loads(x) for x in (session_folder_of(a) / "events.jsonl").read_text().splitlines()]
    assert "sneaky" not in [e["name"] for e in events]
    assert [e["name"] for e in events if e["step"] == "participant"] == ["step_start"]


def test_a_page_event_over_the_websocket_is_saved_and_a_bad_command_is_answered(
    app, recorded_session
):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a) as ws:
        next_state(ws)
        send(ws, cmd="pause", reason="x")  # nothing is running
        error = next(
            m for m in iter(lambda: json.loads(ws.recv(timeout=10)), None) if m["t"] == "error"
        )
        assert error["kind"] == "refused"
        send(ws, cmd="nonsense")
        error = next(
            m for m in iter(lambda: json.loads(ws.recv(timeout=10)), None) if m["t"] == "error"
        )
        assert error["kind"] == "bad_command"
        send(ws, cmd="event", name="shown", payload={"k": 1}, browser_time=12.5, time_origin=99.0)
        next_state(ws)
    events = [json.loads(x) for x in (session_folder_of(a) / "events.jsonl").read_text().splitlines()]
    shown = [e for e in events if e["name"] == "shown"]
    assert len(shown) == 1
    assert (shown[0]["payload"], shown[0]["browser_time"], shown[0]["time_origin"]) == (
        {"k": 1},
        12.5,
        99.0,
    )
    markers = markers_of(session_folder_of(a) / "eeg")
    assert f"participant/shown#{shown[0]['seq']}" in [m["label"] for m in markers]


# -- fix round 1 ----------------------------------------------------------------


def strict_lines(path):
    """Lines of a JSON-lines file, refusing NaN and Infinity."""

    def refuse(constant):
        raise AssertionError(f"{constant} in {path}")

    return [json.loads(x, parse_constant=refuse) for x in path.read_text().splitlines()]


def test_a_failed_mark_leaves_the_state_and_the_events_as_they_were(live):
    """The recording is closed under the session (a real failure, not the link ending)."""
    session, source, folder, _ = live
    start_block(session, "words")
    before = session.state()
    lines = events_of(folder)
    source.stop_recording()
    with pytest.raises(RuntimeError, match="not recording"):
        session.pause("fullscreen_lost")
    state = session.state()
    assert (state["phase"], state["reason"], state["rev"]) == ("running", None, before["rev"])
    assert events_of(folder) == lines
    with pytest.raises(RuntimeError, match="not recording"):
        session.event("shown", {}, 1.0, 1.0)
    assert events_of(folder) == lines
    # The block's time runs out: the block does not end, since its end could not be saved.
    time.sleep(2.1)
    with pytest.raises(RuntimeError, match="not recording"):
        session.tick()
    state = session.state()
    assert (state["phase"], state["blocks_done"]) == ("running", 0)
    assert events_of(folder) == lines
    assert [e["seq"] for e in lines] == list(range(1, len(lines) + 1))


def test_a_failed_write_of_the_event_leaves_state_and_markers_as_they_were(live):
    session, source, folder, rec = live
    start_block(session, "words")
    before = session.state()
    lines = events_of(folder)
    session.close()  # events.jsonl can no longer be written
    with pytest.raises(ValueError):
        session.pause("fullscreen_lost")
    state = session.state()
    assert (state["phase"], state["rev"]) == ("running", before["rev"])
    source.stop()
    assert len(events_of(folder)) == len(lines)
    labels = [m["label"] for m in markers_of(rec)]
    assert not [x for x in labels if "paused" in x]
    assert len(labels) == len(lines)


def test_state_carries_the_pause_reason_and_a_revision_that_only_grows(live):
    session, _, _, _ = live
    start_block(session, "words")
    running = session.state()
    assert running["reason"] is None
    session.pause("fullscreen_lost")
    paused = session.state()
    assert paused["reason"] == "fullscreen_lost"
    assert paused["rev"] == running["rev"] + 2  # "paused", then the "block_end" of the block it ended
    assert session.state()["rev"] == paused["rev"]  # reading changes nothing
    session.resume()
    resumed = session.state()
    assert resumed["reason"] is None and resumed["rev"] == paused["rev"] + 1


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("which", ["browser_time", "time_origin"])
def test_a_non_finite_browser_time_is_refused_and_no_invalid_json_is_written(live, bad, which):
    session, source, folder, _ = live
    times = {"browser_time": 1.0, "time_origin": 1.0, which: bad}
    with pytest.raises(CommandRefused, match="finite"):
        session.event("shown", {}, times["browser_time"], times["time_origin"])
    session.event("fine", {}, 2.0, 3.0)
    source.stop()
    assert [e["name"] for e in strict_lines(folder / "events.jsonl")][-1] == "fine"


def test_a_non_finite_number_in_an_event_payload_is_refused(live):
    session, _, folder, _ = live
    with pytest.raises(CommandRefused, match="finite"):
        session.event("shown", {"x": math.nan}, 1.0, 1.0)
    assert strict_lines(folder / "events.jsonl")[-1]["name"] == "step_start"


def test_a_source_that_ends_on_the_summary_step_is_not_ended_early(recorded_session, tmp_path):
    steps = [flow.Step("participant", "setup", "P"), flow.Step("summary", "summary", "S")]
    source = ReplaySource(recorded_session)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    session = Session(steps, source, None, folder=folder)
    try:
        run_until(session, lambda s: s["phase"] == "waiting")
        session.advance()
        assert session.state()["step"] == "summary"
        state = run_until(session, lambda s: s["phase"] == "done", timeout=40)
        assert state["step"] == "summary" and state["ended_early"] is False
    finally:
        session.close()
        source.stop()


@pytest.mark.parametrize("seconds", [math.nan, math.inf, -math.inf, "2", None, True])
def test_a_flow_file_with_a_non_finite_or_non_numeric_length_is_refused(seconds, tmp_path):
    raw = json.loads(SHORT.read_text())
    raw[3]["blocks"][0]["seconds"] = seconds
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="seconds"):
        flow.load(path)


@pytest.mark.parametrize("points", [2.5, "3", None, True, 1e30])
def test_a_flow_file_with_non_integer_points_is_refused(points, tmp_path):
    raw = json.loads(SHORT.read_text())
    raw[3]["blocks"][0]["points"] = points
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="points"):
        flow.load(path)


def test_state_revisions_never_go_backwards_across_a_busy_pause_resume_sequence(
    app, recorded_session, tmp_path
):
    """Two clients read every state message of 40 quick pause/resume pairs.

    In the video round, which a pause holds (a pause ends a task block since 2026-11-25, so only
    the first pair would be accepted there)."""
    raw = json.loads(SHORT.read_text())
    for step in raw:
        if step["id"] == "video":
            step["blocks"][0]["seconds"] = 60
    path = tmp_path / "long-video.json"
    path.write_text(json.dumps(raw))
    a = app(replay=recorded_session, flow=path)
    accept(a)  # the session starts when a participant label is accepted
    with open_ws(a, page="page-1") as participant, open_ws(a, "onlooker") as onlooker:
        next_state(participant)
        next_state(onlooker)
        for _ in range(3):
            send(participant, cmd="advance")  # to the words round, waiting
        for _ in range(5):
            send(participant, cmd="skip")  # words, perception, nback, flanker, spatial
        send(participant, cmd="advance")  # the video block runs
        for _ in range(40):
            send(participant, cmd="pause", reason="fullscreen_lost")
            send(participant, cmd="resume")
        final = 1 + 3 + 5 * 2 + 1 + 80  # step_start, three advances, five skips, the start, forty pairs
        for ws in (participant, onlooker):
            revs = []
            while not revs or revs[-1] < final:
                revs.append(next_state(ws)["rev"])
            assert revs == sorted(revs), revs
            assert revs[-1] == final
        # And on through the once-a-second messages.
        for ws in (participant, onlooker):
            more = [next_state(ws)["rev"] for _ in range(2)]
            assert more == sorted(more) and more[0] >= final


def test_the_ticker_survives_a_page_whose_event_loop_was_closed(recorded_session, tmp_path):
    """A real cause of a failing heartbeat: a client whose loop closed without a disconnect.
    ``call_soon_threadsafe`` raises ``RuntimeError`` on it, from the heartbeat and from every emit."""
    settings = Settings(
        data_dir=tmp_path, port=None, replay=recorded_session, flow=SHORT, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    loop = asyncio.new_event_loop()
    try:
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")
        gone = Client(loop, "onlooker")
        assert hub.connect(gone)
        loop.close()  # the page's loop is gone and nobody called disconnect
        time.sleep(1.6)  # more than one heartbeat is due for it
        assert hub._ticker is not None and hub._ticker.is_alive()
        assert gone not in hub._clients  # forgotten, not tried again every second
        # the session still runs: a block started now ends by itself
        for _ in range(4):
            hub.session.advance()
        deadline = time.monotonic() + 8
        while hub.session.state()["phase"] != "between" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert hub.session.state()["phase"] == "between"
        # and a page that connects afterwards still gets a state every second
        other_loop = asyncio.new_event_loop()
        try:
            late = Client(other_loop, "onlooker")
            assert hub.connect(late)
            time.sleep(2.2)
            # Run the page's loop once in a thread of its own: after a Playwright test in the same
            # pytest process the main thread already has a running loop (the sync API's), and
            # ``run_until_complete`` refuses to run there.
            runner = threading.Thread(target=other_loop.run_until_complete, args=(asyncio.sleep(0),))
            runner.start()
            runner.join(timeout=10)
            assert not runner.is_alive()
            kinds = []
            while not late.messages.empty():
                kinds.append(json.loads(late.messages.get_nowait()[0])["t"])
            assert kinds.count("state") >= 2
        finally:
            hub.disconnect(late)
            other_loop.close()
    finally:
        hub.stop()


def test_a_reloaded_page_takes_over_from_a_page_whose_event_loop_was_closed(recorded_session, tmp_path):
    """The same real cause as for the heartbeat: the old participant's loop is closed."""
    settings = Settings(
        data_dir=tmp_path, port=None, replay=recorded_session, flow=SHORT, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    old_loop = asyncio.new_event_loop()
    new_loop = asyncio.new_event_loop()
    try:
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")
        old = Client(old_loop, "participant", "page-1")
        assert hub.connect(old)
        old_loop.close()  # nobody called disconnect
        new = Client(new_loop, "participant", "page-1")
        assert hub.connect(new)  # the same page again: it takes over, whatever became of the old socket
        assert hub._participant is new and old not in hub._clients
    finally:
        hub.stop()
        new_loop.close()


def test_the_ticker_survives_a_failing_tick(recorded_session, tmp_path):
    """The events file is closed under a running block: every tick that ends it fails."""
    settings = Settings(
        data_dir=tmp_path, port=None, replay=recorded_session, flow=SHORT, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    try:
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")  # the call the route makes; the session starts with it
        assert hub.session is not None
        for _ in range(4):
            hub.session.advance()
        hub.session.close()
        time.sleep(3.5)  # the block's 2 s are up; the tick has been failing since
        assert hub._ticker is not None and hub._ticker.is_alive()
        # The failure ends the session once, with a reason, instead of freezing it.
        state = hub.session.state()
        assert state["phase"] == "done" and state["ended_early"] is True
        assert state["failure"].startswith("event log failed")
        # The server keeps serving: a client that connects now is told the state, and the
        # ticker is still alive after another stretch of ticks.
        loop = asyncio.new_event_loop()
        try:
            late = Client(loop, "onlooker")
            assert hub.connect(late)
            states = []
            while not late.messages.empty():
                states.append(json.loads(late.messages.get_nowait()[0]))
            first = [m for m in states if m["t"] == "state"]
            assert len(first) == 1 and first[0]["failure"] == state["failure"]
        finally:
            hub.disconnect(late)  # the hub must not send to a loop that is about to close
            loop.close()
        time.sleep(0.5)
        assert hub._ticker.is_alive()
    finally:
        hub.stop()

"""The websocket messages: hello, link, error and the trace data."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import numpy as np

import pytest
from websockets.sync.client import connect

import open_cognionics as oc
from brain_mapper.server import CLIENT_QUEUE, TRACE_QUEUE, Client, Settings, create_app
from starlette.applications import Starlette


def open_socket(app_):
    host, port = app_.host_port
    return connect(f"ws://{host}:{port}/ws", origin=f"http://{host}:{port}")


def read_until(ws, predicate, timeout=20.0):
    """Messages up to and including the first one that satisfies ``predicate``."""
    seen = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"no matching message; saw {[m.get('t') for m in seen]}")
        try:
            message = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        seen.append(message)
        if predicate(message):
            return seen


def test_create_app_returns_a_starlette_application(tmp_path):
    settings = Settings(data_dir=tmp_path, port=None, replay=tmp_path, flow=None, http_port=1)
    app = create_app(settings)
    assert isinstance(app, Starlette)
    assert app.state.settings is settings
    assert {route.path for route in app.routes} >= {
        "/",
        "/onlooker",
        "/api/health",
        "/ws",
    }


@pytest.mark.parametrize("kind", ["replay", "live"])
def test_hello_then_trace_rows_at_the_sample_rate(kind, app, recorded_session, pty_device):
    a = app(replay=recorded_session) if kind == "replay" else app(port=pty_device.path)
    with open_socket(a) as ws:
        seen = read_until(ws, lambda m: m["t"] == "link" and m["state"] == "live")
        hello = [m for m in seen if m["t"] == "hello"]
        assert len(hello) == 1
        assert hello[0]["fs"] == 500.0
        assert hello[0]["kind"] == kind
        assert len(hello[0]["labels"]) == 20
        assert hello[0]["labels"][:3] == ["F7", "Fp1", "Fp2"]
        assert [m["state"] for m in seen if m["t"] == "link"][-1] == "live"
        # Nothing is sent until the page asks.
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        rows = 0
        started = time.monotonic()
        while time.monotonic() - started < 4.0:
            message = json.loads(ws.recv(timeout=5))
            if message["t"] != "d":
                continue
            assert len(message["x"][0]) == 20
            assert set(message) >= {"x", "lost", "bat", "imp"}
            assert isinstance(message["imp"], bool)
            assert 2.0 < message["bat"] < 6.0
            rows += len(message["x"])
        elapsed = time.monotonic() - started
        assert 0.8 * 500 * elapsed <= rows <= 1.25 * 500 * elapsed
        ws.send(json.dumps({"cmd": "traces", "on": False}))
        # Data already queued may still arrive; then it stops.
        time.sleep(0.5)
        while True:
            try:
                ws.recv(timeout=0.5)
            except TimeoutError:
                break


def test_trace_values_are_the_recording_in_microvolts(app, recorded_session):
    recorded = oc.read_recording(recorded_session).data * 1e6
    a = app(replay=recorded_session)
    with open_socket(a) as ws:
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        message = read_until(ws, lambda m: m["t"] == "d" and m["lost"] == 0)[-1]
    x = np.array(message["x"])
    assert x.shape[1] == 20
    # Find where the message's first row sits in the recording, then compare all rows.
    start = int(np.nanargmin(np.abs(recorded - x[0]).sum(axis=1)))
    block = recorded[start : start + len(x)]
    assert block.shape == x.shape
    assert np.abs(block - x).max() <= 0.006  # rounded to 0.01 uV
    assert np.abs(x).max() > 0.01


async def _offer_many():
    client = Client(asyncio.get_running_loop())
    client.offer('{"t":"hello"}', droppable=False)
    for i in range(CLIENT_QUEUE * 3):
        client.offer(f'{{"t":"d","i":{i}}}', droppable=True)
    client.offer('{"t":"error"}', droppable=False)
    client.offer('{"t":"link","state":"ended"}', droppable=False)
    out = []
    while not client.messages.empty():
        out.append(json.loads((await client.messages.get())[0]))
    return out


def test_a_client_that_falls_behind_loses_data_but_never_control_messages():
    # Run on a thread of its own: the Playwright fixtures leave a loop running here.
    results = []
    thread = threading.Thread(target=lambda: results.append(asyncio.run(_offer_many())))
    thread.start()
    thread.join()
    out = results[0]
    kinds = [m["t"] for m in out]
    # the messages that are always kept come first, in their order; then the data that was kept
    assert kinds[:3] == ["hello", "error", "link"]
    assert kinds.count("d") == CLIENT_QUEUE
    assert [m["i"] for m in out[3:]] == list(range(CLIENT_QUEUE))  # the oldest kept, in order


async def _state_after_bulk():
    client = Client(asyncio.get_running_loop())
    for i in range(TRACE_QUEUE):
        client.offer(f'{{"t":"trace","i":{i}}}', droppable="trace")
    for i in range(CLIENT_QUEUE):
        client.offer(f'{{"t":"d","i":{i}}}', droppable=True)
    client.offer('{"t":"state","rev":7}')
    client.offer('{"t":"link","state":"live"}')
    client.offer('{"t":"trace","i":-1}', droppable="trace")  # over the cap: dropped
    client.close()
    first = [json.loads(await client.next()) for _ in range(2)]
    rest = []
    while (text := await client.next()) is not None:
        rest.append(json.loads(text))
    return first, rest, dict(client.waiting)


def test_a_state_never_waits_behind_drawing_data():
    """A client with ten seconds of traces and a full data queue waiting (a slow page): a state
    and a link message offered after them are the next two sent, in their order; the data then
    follow in theirs, and the close comes last."""
    results = []
    thread = threading.Thread(target=lambda: results.append(asyncio.run(_state_after_bulk())))
    thread.start()
    thread.join()
    first, rest, waiting = results[0]
    assert first == [{"t": "state", "rev": 7}, {"t": "link", "state": "live"}]
    assert [m["t"] for m in rest] == ["trace"] * TRACE_QUEUE + ["d"] * CLIENT_QUEUE
    assert [m["i"] for m in rest if m["t"] == "trace"] == list(range(TRACE_QUEUE))
    assert waiting == {"d": 0, "trace": 0}


def test_no_data_is_sent_to_a_client_that_did_not_ask(app, recorded_session):
    a = app(replay=recorded_session)
    with open_socket(a) as ws:
        read_until(ws, lambda m: m["t"] == "link" and m["state"] == "live")
        started = time.monotonic()
        while time.monotonic() - started < 2.0:
            try:
                message = json.loads(ws.recv(timeout=0.5))
            except TimeoutError:
                continue
            assert message["t"] != "d"


def test_a_client_that_connects_later_is_told_the_state(app, recorded_session):
    a = app(replay=recorded_session)
    with open_socket(a) as first:
        read_until(first, lambda m: m["t"] == "link" and m["state"] == "live")
        with open_socket(a) as second:
            seen = read_until(second, lambda m: m["t"] == "link" and m["state"] == "live")
            assert [m["t"] for m in seen][0] in ("link", "hello")
            assert any(m["t"] == "hello" for m in seen)


def test_the_end_of_a_replay_is_reported_as_link_ended(app, recorded_session):
    a = app(replay=recorded_session)
    with open_socket(a) as ws:
        seen = read_until(ws, lambda m: m["t"] == "link" and m["state"] == "ended", timeout=40)
        assert seen[-1]["reason"] == "end of recording"


def test_a_hang_up_is_reported_as_a_link_that_is_connecting_again_with_the_disconnected_problem(app, pty_device):
    # Before: state "ended" with a reason starting "disconnected: ". The retry of a live link until
    # the device step has been passed (decision 2) makes it "connecting" with the problem set.
    a = app(port=pty_device.path)
    with open_socket(a) as ws:
        read_until(ws, lambda m: m["t"] == "link" and m["state"] == "live")
        pty_device.hang_up()
        seen = read_until(ws, lambda m: m["t"] == "link" and m["state"] == "connecting")
        assert seen[-1]["reason"].startswith("disconnected: ")
        assert seen[-1]["problem"]["code"] == "disconnected" and seen[-1]["problem"]["dropped"] is True


def test_a_device_that_cannot_be_opened_is_reported_in_the_link_problem_and_tried_again(app, tmp_path):
    # Before: one "error" message and state "ended". Now the link stays "connecting", carries the
    # problem, and the attempt is repeated (attempt 2 follows).
    a = app(port=str(tmp_path / "no-such-device"))
    with open_socket(a) as ws:
        seen = read_until(ws, lambda m: m["t"] == "link" and (m.get("problem") or {}).get("attempt") == 2)
        links = [m for m in seen if m["t"] == "link" and m.get("problem")]
        assert links[0]["state"] == "connecting"
        assert links[0]["problem"]["kind"] == "OpenError" and links[0]["problem"]["code"] == "open_error"
        assert links[0]["problem"]["msg"] == "could not open the port: No such file or directory"
        assert links[0]["problem"]["retry_s"] == 2.0
        assert not any(m["t"] in ("hello", "error") for m in seen)


def test_a_message_that_is_not_json_gets_an_error_and_the_socket_stays_open(app, recorded_session):
    a = app(replay=recorded_session)
    with open_socket(a) as ws:
        ws.send("not json")
        seen = read_until(ws, lambda m: m["t"] == "error")
        assert seen[-1]["kind"] == "bad_message"
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        read_until(ws, lambda m: m["t"] == "d")


def test_closing_a_client_does_not_stop_the_server(app, recorded_session):
    a = app(replay=recorded_session)
    with open_socket(a) as ws:
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        read_until(ws, lambda m: m["t"] == "d")
    with open_socket(a) as ws:
        ws.send(json.dumps({"cmd": "traces", "on": True}))
        read_until(ws, lambda m: m["t"] == "d")

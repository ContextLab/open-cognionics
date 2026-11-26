"""The device setup screen, in a real Chromium, against the simulated headset (a pty device).

Every state of the tip table is reached the way it arises: a silent device (``NoData``), a device
that never sends its configuration (``NoConfiguration``), a path that does not exist
(``OpenError``), a device that hangs up (``Disconnected``), a device whose stream skips samples
(lost samples). ``NoHeadset`` and ``SeveralHeadsets`` need ``--port`` left out, which would open a
plugged-in dongle, so their tips are checked by giving the page's own tip function the problem
that the real ``oc`` errors (``oc.select_headset``) turn into. A path that is a symlink, changed
while the app runs, stands for a headset that is plugged in later.

Nothing opens ``/dev/cu.usbserial-*``; every app is started with ``--port`` a pty or a missing
path, or with ``--replay``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import pytest
from websockets.sync.client import connect

import open_cognionics as oc
from brain_mapper import display
from brain_mapper.server import Client, TRACE_QUEUE
from brain_mapper.source import describe
from test_engine import SHORT, events_of, markers_of, next_state, open_ws, send
from test_participants import files_under, post_label, session_folder

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

SCREEN = "#screen"
LINK = "#device-link"


def to_device(a, page, label="P001") -> None:
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    page.fill("#participant-label", label)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=30_000)


def tip_code(page) -> str:
    return page.locator("#device-tip").get_attribute("data-tip")


def wait_tip(page, code: str, timeout=30_000) -> None:
    page.wait_for_selector(f'#device-tip[data-tip="{code}"]:not([hidden])', timeout=timeout)


def wait_link(page, state: str, timeout=30_000) -> None:
    page.wait_for_selector(f'{LINK}[data-state="{state}"]', timeout=timeout)


def point(link: Path, target: str) -> None:
    """Makes ``link`` a symbolic link to ``target``, replacing what it was (a headset plugged in)."""
    temporary = link.with_name(link.name + ".new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


# -- live ------------------------------------------------------------------------------------------


def test_the_live_screen_shows_the_steps_the_link_a_rate_and_twenty_moving_traces(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    assert page.inner_text("#device-link-word") == "Live"
    # three illustrated steps, each picture loaded
    steps = page.locator("#device-steps li")
    assert steps.count() == 3
    assert page.evaluate(
        "[...document.querySelectorAll('#device-steps img')].every(i => i.complete && i.naturalWidth > 0)"
    )
    # the rate, between 450 and 550, once the server has measured it
    page.wait_for_function("document.getElementById('device-rate').dataset.hz !== ''", timeout=15_000)
    # Three new readings in turn (the rate itself may be the same from one reading to the next at a
    # steady 500 Hz, so it is the reading number that must advance), each in range.
    read = "(() => { const d = document.getElementById('device-rate').dataset; return [d.reading, d.hz]; })()"
    rates = []
    for _ in range(3):
        before, _ = page.evaluate(read)
        page.wait_for_function(
            "b => { const d = document.getElementById('device-rate').dataset;"
            " return d.reading !== '' && Number(d.reading) > Number(b) && d.hz !== ''; }",
            arg=before,
            timeout=5_000,
        )
        _, hz = page.evaluate(read)
        rates.append(float(hz))
    assert all(450 <= r <= 550 for r in rates), rates
    # lost samples and the battery reading, as read
    assert page.inner_text("#device-lost") == "0"
    assert re.fullmatch(r"\d\.\d\d V", page.inner_text("#device-battery"))
    # twenty traces, moving: the canvas differs a second of rows later
    canvas = page.locator("#device-traces")
    assert canvas.get_attribute("data-channels") == "20"
    page.wait_for_function("+document.getElementById('device-traces').dataset.rows >= 100")
    first = canvas.screenshot()
    rows = int(canvas.get_attribute("data-rows"))
    page.wait_for_function(
        "n => +document.getElementById('device-traces').dataset.rows >= n + 50", arg=rows, timeout=10_000
    )
    second = canvas.screenshot()
    assert first != second
    ink = page.evaluate(
        """() => { const c = document.getElementById('device-traces');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 0 && d[i] < 100) n++; return n; }"""
    )
    assert ink > 1000
    # nothing to say, and Continue can be pressed
    assert page.locator("#device-tip").is_hidden()
    assert page.locator("#advance").is_enabled()


def test_the_page_stays_responsive_while_the_traces_draw(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    frames = page.evaluate(
        """() => new Promise(done => { let n = 0; const end = performance.now() + 2000;
        const tick = () => { n++; performance.now() < end ? requestAnimationFrame(tick) : done(n); };
        requestAnimationFrame(tick); })"""
    )
    assert frames >= 30, frames  # at least 15 frames a second while 20 traces are drawn
    drawn = int(page.locator("#device-traces").get_attribute("data-drawn"))
    assert drawn > 0


def test_the_trace_buffer_holds_the_same_amount_however_many_rows_arrive(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.goto(a.url)
    result = page.evaluate(
        """async () => {
        const { TraceView } = await import('/static/js/traces.js');
        const canvas = document.createElement('canvas');
        document.body.append(canvas);
        const view = new TraceView(canvas, Array.from({length: 20}, (_, i) => 'c' + i));
        const row = Array.from({length: 20}, (_, i) => 1000 * i + Math.sin(i));
        for (let k = 0; k < 20000; k++) view.push([row, row, row, row, row]);
        return { rows: +canvas.dataset.rows, buffered: +canvas.dataset.buffered,
                 capacity: +canvas.dataset.capacity, floats: view.buffers.reduce((n, b) => n + b.length, 0) };
        }"""
    )
    assert result["rows"] == 100_000
    assert result["buffered"] == result["capacity"] == 400
    assert result["floats"] == 20 * 400


def test_a_trace_message_is_dropped_when_a_page_has_ten_seconds_of_them_waiting():
    import asyncio
    import threading

    async def fill():
        client = Client(asyncio.get_running_loop())
        for i in range(TRACE_QUEUE * 3):
            client.offer(f'{{"t":"trace","i":{i}}}', droppable="trace")
        client.offer('{"t":"link"}')
        kept = []
        while not client.messages.empty():
            kept.append(json.loads((await client.messages.get())[0]))
        return kept

    out = []
    thread = threading.Thread(target=lambda: out.append(asyncio.run(fill())))
    thread.start()
    thread.join()
    kept = out[0]
    assert sum(m["t"] == "trace" for m in kept) == TRACE_QUEUE
    assert kept[0]["t"] == "link"  # control messages are never dropped, and never wait behind traces


def test_closing_a_client_after_messages_of_each_kind_ends_its_send_loop_and_counts_stay_exact():
    """``close`` must end the sender (``next`` returns ``None``) whatever kinds are waiting. A
    close that raised inside the sender left the socket open and the page never saw it drop
    (the symptom was ``#conn-lost`` never showing); the counts of waiting messages must also
    come back to zero as each is taken."""
    import asyncio
    import threading

    async def run():
        client = Client(asyncio.get_running_loop())
        client.offer('{"t":"d"}', droppable=True)
        client.offer('{"t":"trace"}', droppable="trace")
        client.offer('{"t":"link"}')
        client.close()
        got = [await client.next() for _ in range(4)]
        return got, dict(client.waiting)

    out = []
    thread = threading.Thread(target=lambda: out.append(asyncio.run(run())))
    thread.start()
    thread.join()
    got, waiting = out[0]
    assert got[-1] is None and all(g is not None for g in got[:3])
    assert waiting == {"d": 0, "trace": 0}


# -- what the server sends for the drawing ---------------------------------------------------------


def wait_message(ws, kind, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = json.loads(ws.recv(timeout=1))
        except TimeoutError:
            continue
        if message.get("t") == kind:
            return message
    raise AssertionError(f"no {kind} message")


def test_trace_messages_come_only_to_a_client_that_asked_at_fifty_rows_a_second(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    host, port = a.host_port
    url = f"ws://{host}:{port}/ws?role=onlooker"
    with connect(url, origin=f"http://{host}:{port}") as quiet, connect(url, origin=f"http://{host}:{port}") as asking:
        wait_message(asking, "link")
        asking.send(json.dumps({"cmd": "display", "on": True}))
        rows = 0
        bytes_ = 0
        started = time.monotonic()
        quiet_kinds = set()
        while time.monotonic() - started < 3:
            try:
                raw = asking.recv(timeout=1)
            except TimeoutError:
                continue
            message = json.loads(raw)
            if message["t"] == "trace":
                assert message["hz"] == display.DISPLAY_HZ
                assert all(len(r) == 20 for r in message["x"])
                rows += len(message["x"])
                bytes_ += len(raw)
        elapsed = time.monotonic() - started
        assert 35 <= rows / elapsed <= 65, rows / elapsed
        assert bytes_ / elapsed < 16_000  # about 7 kB a second
        # the other client never asked, so it got no trace data and no raw rows
        while True:
            try:
                quiet_kinds.add(json.loads(quiet.recv(timeout=0.5))["t"])
            except TimeoutError:
                break
        assert quiet_kinds and not quiet_kinds & {"trace", "d"}
        # and the one that asked can stop
        asking.send(json.dumps({"cmd": "display", "on": False}))
        time.sleep(0.5)
        while True:
            try:
                asking.recv(timeout=0.5)
            except TimeoutError:
                break
        with pytest.raises(AssertionError):
            wait_message(asking, "trace", timeout=2)


def test_the_status_message_says_the_rate_and_the_lost_samples(app):
    dev = PtyDevice.lossy_device(10)
    try:
        a = app(port=dev.path, flow=SHORT)
        with open_ws(a, role="onlooker") as ws:
            seen = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                message = wait_message(ws, "status")
                seen.append(message)
                if message["rate"] is not None and message["lost10"] > 0:
                    break
        last = seen[-1]
        assert last["lost"] >= last["lost10"] > 0
        assert isinstance(last["bat"], float) and last["imp"] is False
    finally:
        dev.hang_up()


# -- the tips ---------------------------------------------------------------------------------------


def test_no_data_shows_its_tip_and_continue_waits(app, page):
    dev = PtyDevice.silent_device()
    try:
        a = app(port=dev.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_data")
        assert "nothing has been received" in page.inner_text("#device-tip")
        assert page.locator(LINK).get_attribute("data-state") == "connecting"
        assert page.locator("#advance").is_disabled()
        # each failed attempt is followed by another, so the tip is renewed rather than dropped
        assert page.locator("#device-rate").get_attribute("data-hz") == ""
    finally:
        dev.hang_up()


def test_a_missing_port_shows_the_open_error_tip_and_it_goes_when_the_port_appears(app, pty_device, page, tmp_path):
    link = tmp_path / "headset-port"
    a = app(port=str(link), flow=SHORT)
    to_device(a, page)
    wait_tip(page, "open_error")
    assert "No such file or directory" in page.inner_text("#device-tip")
    assert page.locator("#advance").is_disabled()
    point(link, pty_device.path)  # the dongle is plugged in
    wait_link(page, "live", timeout=20_000)
    page.wait_for_selector("#device-tip", state="hidden")
    assert page.locator("#advance").is_enabled()


def test_a_headset_that_sends_no_configuration_shows_its_tip(app, page):
    dev = PtyDevice(onhead_stream(), None)  # streams, never answers 0x14
    try:
        a = app(port=dev.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_configuration", timeout=40_000)
        assert "cause is not known" in page.inner_text("#device-tip")
    finally:
        dev.hang_up()


def test_lost_samples_show_their_tip_and_the_count(app, page):
    dev = PtyDevice.lossy_device(10)
    try:
        a = app(port=dev.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "lost_samples")
        assert int(page.inner_text("#device-lost")) > 0
        assert re.search(r"\d+ in the last 10 seconds", page.inner_text("#device-tip"))
        assert page.locator(LINK).get_attribute("data-state") == "live"
    finally:
        dev.hang_up()


def test_the_lost_samples_tip_goes_when_nothing_has_been_lost_for_ten_seconds(app, page):
    dev = PtyDevice.lossy_device(10)
    try:
        a = app(port=dev.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "lost_samples")
        dev.stop_losing()
        page.wait_for_selector("#device-tip", state="hidden", timeout=40_000)
        assert page.locator(LINK).get_attribute("data-state") == "live"
        assert int(page.inner_text("#device-lost")) > 0  # the total stays, the tip goes
    finally:
        dev.hang_up()


def test_the_no_configuration_tip_goes_when_a_headset_that_answers_is_plugged_in(app, page, tmp_path):
    mute = PtyDevice(onhead_stream(), None)
    good = None
    link = tmp_path / "headset-port"
    link.symlink_to(mute.path)
    try:
        a = app(port=str(link), flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_configuration", timeout=40_000)
        good = PtyDevice(onhead_stream(), config_reply())  # made now: a device that has streamed
        point(link, good.path)  # into a pty nobody reads for a long time stalls the first read
        wait_link(page, "live", timeout=30_000)
        page.wait_for_selector("#device-tip", state="hidden")
    finally:
        mute.hang_up()
        if good is not None:
            good.hang_up()


def test_the_page_words_about_the_hardware_are_the_confirmed_ones(app, pty_device, page):
    """The headset facts of docs/device.md: power button on the box on top,
    forehead pads, ear clips on the earlobes, a wireless link of limited range."""
    a = app(port=pty_device.path, flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    text = page.inner_text("#device-steps")
    assert "Press the power button on the box on top of the headset." in text
    assert "the forehead pads on the forehead, and clip the ear clips to the earlobes." in text
    texts = page.evaluate(
        """async () => {
          const { TIPS } = await import('/static/js/tips.js');
          return Object.fromEntries(Object.entries(TIPS).map(([k, t]) =>
            [k, t.text({ports: [], msg: 'm', kind: 'k', retry_s: 2}, {lost10: 3}, false, {reason: 'r'})]));
        }"""
    )
    assert "in range of the dongle" in texts["no_data"]
    assert "in range of the dongle" in texts["lost_samples"]
    assert "If the dongle was unplugged, plug it in again; if the headset was switched off, switch it on." in texts["dropped"]


def test_a_fault_of_the_hub_thread_has_its_own_tip_and_is_not_called_a_dropped_link(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    to_device(a, page)
    tip = page.evaluate(
        """async () => {
          const { chooseTip } = await import('/static/js/tips.js');
          return chooseTip({ state: 'ended', reason: 'ValueError: boom', fault: true, problem: null }, null, true);
        }"""
    )
    assert tip["code"] == "fault" and "error inside brain-mapper" in tip["text"] and "boom" in tip["text"]
    assert "dropped" not in tip["text"]


def test_a_hang_up_shows_the_dropped_link_tip_and_it_goes_when_the_link_is_back(app, pty_device, page, tmp_path):
    link = tmp_path / "headset-port"
    link.symlink_to(pty_device.path)
    a = app(port=str(link), flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    pty_device.hang_up()
    wait_tip(page, "dropped")
    assert page.inner_text("#device-link-word") == "Dropped"
    assert "dropped" in page.inner_text("#device-tip")
    # the attempts that follow find the port gone (OpenError); that does not replace the news that
    # the link dropped
    page.wait_for_function("+document.getElementById('device-link').dataset.attempt >= 1", timeout=15_000)
    assert page.locator(LINK).get_attribute("data-problem") == "open_error"
    assert tip_code(page) == "dropped"
    assert page.locator("#advance").is_disabled()
    other = PtyDevice(onhead_stream(), config_reply())
    try:
        point(link, other.path)
        wait_link(page, "live", timeout=20_000)
        page.wait_for_selector("#device-tip", state="hidden")
        assert page.locator("#advance").is_enabled()
    finally:
        other.hang_up()


def test_no_data_goes_when_a_headset_that_answers_is_plugged_in(app, pty_device, page, tmp_path):
    silent = PtyDevice.silent_device()
    link = tmp_path / "headset-port"
    link.symlink_to(silent.path)
    try:
        a = app(port=str(link), flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_data")
        point(link, pty_device.path)
        wait_link(page, "live", timeout=20_000)
        page.wait_for_selector("#device-tip", state="hidden")
    finally:
        silent.hang_up()


def test_the_tips_for_no_dongle_and_several_dongles_use_what_the_oc_errors_say(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.goto(a.url)
    try:
        oc.select_headset([])
    except oc.NoHeadset as error:
        none = describe(error)
    ports = [oc.PortInfo("/dev/cu.fake-A", "Cognionics Quick-20", None), oc.PortInfo("/dev/cu.fake-B", "Cognionics Quick-20", None)]
    try:
        oc.select_headset(ports)
    except oc.SeveralHeadsets as error:
        several = describe(error)
    assert none.code == "no_headset" and several.code == "several_headsets"
    assert several.ports == ("/dev/cu.fake-A", "/dev/cu.fake-B")

    def tip(problem):
        link = {"state": "connecting", "reason": problem.msg, "problem": {**problem.as_dict(), "dropped": False, "attempt": 1, "retry_s": 2.0}}
        return page.evaluate(
            "async link => (await import('/static/js/tips.js')).chooseTip(link, null)", link
        )

    assert tip(none)["code"] == "no_headset" and "No headset dongle" in tip(none)["text"]
    found = tip(several)
    assert found["code"] == "several_headsets"
    assert "/dev/cu.fake-A, /dev/cu.fake-B" in found["text"] and "--port" in found["text"]


def test_every_state_of_the_table_has_its_own_tip(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.goto(a.url)
    codes = page.evaluate(
        """async () => { const { TIPS, chooseTip } = await import('/static/js/tips.js');
        const out = {};
        for (const code of ['no_headset','several_headsets','open_error','no_data','no_configuration']) {
          out[code] = chooseTip({state:'connecting', problem:{code, kind:code, msg:'m', ports:['p'], dropped:false, attempt:1, retry_s:2}}, null).text;
        }
        out.dropped = chooseTip({state:'connecting', problem:{code:'open_error', kind:'OpenError', msg:'m', ports:[], dropped:true, attempt:1, retry_s:2}}, null).text;
        out.lost_samples = chooseTip({state:'live', problem:null}, {lost10: 7}).text;
        out.none = chooseTip({state:'live', problem:null}, {lost10: 0});
        return out; }"""
    )
    assert codes.pop("none") is None
    assert len(set(codes.values())) == 7 and all(codes.values())
    assert "7 in the last 10 seconds" in codes["lost_samples"]
    assert not re.search(r"\blow\b.*battery|battery.*\blow\b", " ".join(codes.values()), re.I)


# -- a replay --------------------------------------------------------------------------------------


def test_a_replay_names_the_recording_file_and_retries_nothing(app, recorded_session, page):
    a = app(replay=recorded_session, flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    text = page.inner_text("#device-replay")
    assert "runs from a recording" in text and f": {recorded_session.name}." in text
    assert str(recorded_session) not in page.content() and str(recorded_session.parent) not in text
    assert page.locator("#device-steps").is_hidden()
    assert page.locator("#device-tip").is_hidden()
    assert page.locator("#advance").is_enabled()
    page.wait_for_function("+document.getElementById('device-traces').dataset.rows > 50")


# -- the order: label first, link after -------------------------------------------------------------


def test_a_label_is_accepted_without_a_link_and_the_recording_starts_when_the_link_comes_up(app, pty_device, page, tmp_path):
    link = tmp_path / "headset-port"
    a = app(port=str(link), flow=SHORT)
    to_device(a, page)
    wait_tip(page, "open_error")
    folder = session_folder(a)
    assert (folder / "session.json").is_file() and not (folder / "eeg").exists()
    first = events_of(folder)[0]
    assert first["name"] == "step_start" and first["payload"] == {"marked": False}
    assert json.loads((a.data_dir / "participants.json").read_text())  # the label is listed
    # Continue cannot pass the device step while nothing is recorded
    assert page.locator("#advance").is_disabled()
    point(link, pty_device.path)
    wait_link(page, "live", timeout=20_000)
    page.wait_for_selector("#advance:not([disabled])")
    assert (folder / "eeg" / "raw.bin").is_file()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
    # the marker of the first event does not exist; every event after the link has one
    names = [m["label"] for m in markers_of(folder / "eeg")]
    assert names and not any(n.endswith("#1") for n in names), names
    assert any(n.startswith("device/step_start") or n.startswith("calibration/step_start") for n in names)


def test_the_device_step_is_refused_over_the_socket_while_nothing_is_recorded(app, tmp_path, pty_device):
    link = tmp_path / "headset-port"
    a = app(port=str(link), flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        status, body = post_label(a, "P001")
        assert status == 200, body
        send(ws, cmd="advance")  # participant -> device is allowed
        next_state(ws, predicate=lambda m: m["step"] == "device")
        send(ws, cmd="advance")  # device -> calibration is not, without a link
        error = next(m for m in iter(lambda: json.loads(ws.recv(timeout=10)), None) if m["t"] == "error")
        assert error["kind"] == "refused" and "not live yet" in error["msg"]
        point(link, pty_device.path)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (session_folder(a) / "eeg" / "raw.bin").exists():
                break
            time.sleep(0.1)
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "calibration")


def test_a_recording_that_cannot_start_when_the_link_comes_up_ends_the_session_once_with_a_failure(app, pty_device, tmp_path):
    link = tmp_path / "headset-port"
    a = app(port=str(link), flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        assert post_label(a, "P001")[0] == 200
        folder = session_folder(a)
        folder.chmod(0o500)  # a real cause: eeg/ cannot be made inside it
        try:
            point(link, pty_device.path)
            state = next_state(ws, timeout=30, predicate=lambda m: m["phase"] == "done")
        finally:
            folder.chmod(0o700)
        assert state["failure"] and "recording could not be started" in state["failure"]
        assert state["ended_early"] is True
        time.sleep(1.5)
    names = [e["name"] for e in events_of(folder)]
    assert names.count("ended") == 1, names
    assert not (folder / "eeg").exists()


def test_a_link_lost_before_the_device_step_is_passed_is_tried_again_and_the_recording_is_set_aside(app, tmp_path, page):
    first = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    second = None
    try:
        a = app(port=str(link), flow=SHORT)
        to_device(a, page)
        wait_link(page, "live")
        folder = session_folder(a)
        page.wait_for_function("+document.getElementById('device-traces').dataset.rows > 50")
        first.hang_up()
        wait_tip(page, "dropped")
        assert page.locator(f'{SCREEN}[data-screen="device"]').count() == 1  # the screen stays
        second = PtyDevice(onhead_stream(), config_reply())
        point(link, second.path)
        wait_link(page, "live", timeout=20_000)
        page.wait_for_selector("#advance:not([disabled])")
        # the recording of the first link is whole and set aside; a new one is running
        aside = folder / "eeg-lost-1"
        assert (aside / "raw.bin").is_file() and (aside / "meta.json").is_file()
        assert (folder / "eeg" / "raw.bin").is_file()
        assert [e["name"] for e in events_of(folder)].count("source_ended") == 0
    finally:
        first.hang_up()
        if second is not None:
            second.hang_up()


def test_a_link_lost_after_the_device_step_keeps_the_session_and_the_link_is_tried_again(app, page, tmp_path):
    """A drop after the device step pauses the session (since 2026-11-25), it does
    not end it (until then this test asserted the ended screen and a ``source_ended`` event). The
    recording of the first link is set aside, the link is tried again, and a new recording starts
    when it is back."""
    first = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    second = None
    try:
        a = app(port=str(link), flow=SHORT)
        to_device(a, page)
        wait_link(page, "live")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
        folder = session_folder(a)
        first.hang_up()
        deadline = time.monotonic() + 30
        while "link_lost" not in [e["name"] for e in events_of(folder)]:
            assert time.monotonic() < deadline, "no link_lost"
            time.sleep(0.05)
        assert page.locator(f'{SCREEN}[data-screen="calibration"]').count() == 1  # not ended
        second = PtyDevice(onhead_stream(), config_reply())
        point(link, second.path)
        deadline = time.monotonic() + 40
        while "link_back" not in [e["name"] for e in events_of(folder)]:
            assert time.monotonic() < deadline, "no link_back"
            time.sleep(0.05)
        names = [e["name"] for e in events_of(folder)]
        assert "source_ended" not in names and "ended" not in names
        assert (folder / "eeg-lost-1" / "raw.bin").is_file() and (folder / "eeg" / "raw.bin").is_file()
        assert page.locator(f'{SCREEN}[data-screen="ended"]').count() == 0
    finally:
        first.hang_up()
        if second is not None:
            second.hang_up()


def test_nothing_the_screen_asks_for_changes_what_is_recorded(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    to_device(a, page)
    wait_link(page, "live")
    page.wait_for_function("+document.getElementById('device-traces').dataset.rows > 100")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
    folder = session_folder(a)
    raw = (folder / "eeg" / "raw.bin").read_bytes()
    played = pty_device.played()
    # every byte recorded is a byte the device played, in order
    assert raw and played.find(raw[:2000]) >= 0
    assert uuid.UUID(json.loads((folder / "session.json").read_text())["uuid"])
    assert files_under(folder / "eeg")

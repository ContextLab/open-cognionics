"""The calibration step, in a real Chromium and over the real websocket, against the simulated
headset (a pty device, ``tests/python/pty_device.py``).

The device of most tests answers the contact-check bytes as the headset does: after ``0x11`` it
plays the recording made with the check on (``tests/fixtures/impedance_on_2s.bin``, whose packets
say so), after ``0x12`` the on-head recording made with it off (``onhead_10s.bin``); it starts with
the check on, like the test headset. A second device plays the check-on recording whatever it is
sent: a headset that never confirms the check is off. The tests look at what was written
(``written()``: ``0x11`` on, ``0x12`` off, ``0x14`` the configuration request; nothing else may
ever be written) and at the recording that was made.

The flow is ``flows/short.json`` with sub-steps on its calibration step: eyes open and eyes
closed of ``SECONDS`` each (the blink cues keep their fixed timing).
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

import pytest

import open_cognionics as oc
from brain_mapper import contact
from brain_mapper.engine import SETTLE_S
from test_engine import SHORT, events_of, markers_of, next_state, open_ws, send
from test_participants import post_label, session_folder
from test_onlooker_browser import WEBGL_STATE, WEBGL_WATCH

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import FIXTURES, PtyDevice, config_reply, onhead_stream  # noqa: E402

SCREEN = "#screen"
SECONDS = 3.0
ALLOWED = {0x11, 0x12, 0x14}
TOLERANCE = 0.3


def contact_stream() -> bytes:
    return (FIXTURES / "impedance_on_2s.bin").read_bytes()


@pytest.fixture
def device():
    """Answers 0x11 and 0x12 by playing the check-on or the check-off recording."""
    dev = PtyDevice(onhead_stream(), config_reply(), contact_stream=contact_stream(), contact_on=True)
    yield dev
    dev.hang_up()


@pytest.fixture
def stubborn():
    """Plays the check-on recording whatever it is sent: the check never reads as off."""
    dev = PtyDevice(contact_stream(), config_reply())
    yield dev
    dev.hang_up()


@pytest.fixture
def calib_flow(tmp_path) -> Path:
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": SECONDS, "eyes_closed": SECONDS}
    path = tmp_path / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path


def wait_until(predicate, timeout=30.0, what="condition", page=None):
    """Polls ``predicate``. With ``page``, the wait goes through Playwright, which delivers the
    page's events (websocket frames) only while one of its calls is running."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        if page is not None:
            page.wait_for_timeout(50)
        else:
            time.sleep(0.05)


def only_allowed(dev) -> None:
    assert set(dev.written()) <= ALLOWED, dev.written().hex()


def contact_bytes(dev) -> bytes:
    """The contact-check bytes written so far, in order (the configuration requests left out)."""
    return bytes(b for b in dev.written() if b != 0x14)


def last_contact_byte(dev) -> int | None:
    written = contact_bytes(dev)
    return written[-1] if written else None


def measured(message: dict) -> bool:
    return message.get("t") == "contact" and isinstance(message.get("states"), dict) and bool(message["states"])


def next_message(ws, predicate, timeout=20.0, seen=None) -> dict:
    """The next message of any kind for which ``predicate`` holds; ``seen`` collects all."""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("no such message")
        try:
            message = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        if seen is not None:
            seen.append(message)
        if predicate(message):
            return message


def watch_socket(page) -> list[dict]:
    """Every message the page's websocket receives, parsed."""
    seen: list[dict] = []

    def on_socket(ws):
        ws.on("framereceived", lambda payload: seen.append(json.loads(payload)))

    page.on("websocket", on_socket)
    return seen


def to_calibration(a, page, label="P001", query="?test=1") -> None:
    page.goto(a.url + "/" + query)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    page.fill("#participant-label", label)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=30_000)
    page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"][data-substep="contact"]', timeout=30_000)


def ws_to_contact(a, ws, label="P001", wait_measure=True) -> dict:
    """Over the socket alone: label, device step, into the contact sub-step; then, by default,
    until the first measure of the check is in."""
    next_state(ws)
    status, body = post_label(a, label)
    assert status == 200, body
    send(ws, cmd="advance")
    next_state(ws, predicate=lambda m: m["step"] == "device")
    wait_until(lambda: (session_folder(a) / "eeg" / "raw.bin").exists(), what="the recording")
    send(ws, cmd="advance")
    state = next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "contact")
    if wait_measure:
        next_message(ws, measured, timeout=20)
    return state


def pass_signal_check(page) -> None:
    """After settling: waits for the signal check's result (``signal_done``) and continues."""
    page.wait_for_selector(f'{SCREEN}[data-substep="signal_done"]', timeout=40_000)
    page.wait_for_selector("#advance:not([disabled])", timeout=10_000)
    page.click("#advance")


def ws_pass_signal_check(ws) -> dict:
    """Over the socket: waits for the signal check's result (``signal_done``) and continues."""
    done = next_state(ws, timeout=40, predicate=lambda m: m.get("substep") == "signal_done")
    send(ws, cmd="advance")
    return done


def names_of(folder) -> list[str]:
    return [e["name"] for e in events_of(folder) if e["step"] == "calibration"]


def event_time(folder, name) -> float:
    found = [e for e in events_of(folder) if e["step"] == "calibration" and e["name"] == name]
    assert len(found) == 1, (name, found)
    return found[0]["unix_time"]


def stop_app(a) -> None:
    a.process.send_signal(signal.SIGINT)
    a.process.wait(timeout=20)


def marker_sample(folder, name: str) -> int:
    found = [m["sample"] for m in markers_of(folder / "eeg") if m["label"].split("#")[0].endswith("/" + name)]
    assert len(found) == 1, (name, found)
    return found[0]


def last_check_on_before(folder, sample: int) -> tuple[int, float]:
    """The end (sample number) of the last stretch of the recording made with the check on before
    ``sample``, decoded from the recording itself; and its sample rate."""
    replay = oc.replay(folder / "eeg")
    end = -1
    for chunk in replay:
        if chunk.index >= sample:
            break
        if chunk.impedance_on:
            end = chunk.index + chunk.samples
    assert end >= 0, "no stretch with the check on"
    return end, replay.config.sample_rate


# -- the whole step in the browser ----------------------------------------------------------------


def test_the_calibration_step_from_contact_check_to_blinks(app, device, calib_flow, page):
    messages = watch_socket(page)
    a = app(port=device.path, flow=calib_flow)
    to_calibration(a, page)
    folder = session_folder(a)
    # entering the contact sub-step switches the contact check on
    wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
    ons = contact_bytes(device).count(0x11)
    assert ons == 1
    # the first second after 0x11 is not measured; then the states arrive once a second, and the
    # screen and the 3-D view follow the last one
    wait_until(lambda: len([m for m in messages if measured(m)]) >= 2, what="contact messages", page=page)
    first = next(m for m in messages if m.get("t") == "contact")
    assert first["settling"] is True and first["states"] is None
    page.wait_for_selector("#contact-count[data-good]")
    assert page.locator("#calib-view .labels button").count() == 20

    def shown_matches_last() -> bool:
        last = [m for m in messages if measured(m)][-1]
        good = sum(1 for s in last["states"].values() if s == "good")
        if last["good"] != good or len(last["states"]) != 20:
            raise AssertionError(f"bad contact message {last}")
        shown = page.evaluate(
            """() => {
              const h = window.__calibration.view.headset;
              const count = document.getElementById("contact-count");
              return {
                drawn: Object.fromEntries(h.contacts.map((c) => [c.name, h.state(c.name)])),
                pills: [...document.querySelectorAll("#contact-pills .contact-pill")].map((e) => [e.dataset.contact, e.dataset.state]),
                good: count.dataset.good,
                text: count.textContent,
              };
            }"""
        )
        attention = [[n, s] for n, s in last["states"].items() if s != "good"]
        # a new message can come between the two reads: then this is tried again
        return (
            shown["drawn"] == last["states"]
            and sorted(shown["pills"]) == sorted(attention)
            and shown["good"] == str(good)
            and shown["text"].startswith(f"{good} of 20 contacts are good")
        )

    wait_until(shown_matches_last, timeout=10, what="the screen to match the last contact message", page=page)
    # Setup shows no score (since 2026-11-26): no score, and no line about points.
    assert "earns no points" not in page.inner_text("#screen")
    assert page.inner_text("#hud-label") == "Setup 3 of 3: Calibration"
    assert not page.locator("#hud-points").is_visible()
    # a pill brings its contact forward
    first_pill = page.locator("#contact-pills .contact-pill").first
    name = first_pill.get_attribute("data-contact")
    first_pill.click()
    page.wait_for_function(f"window.__calibration.view.focused() === {json.dumps(name)}")

    # "Check again" goes back to the contact sub-step and switches the check on again; Continue
    # waits for a measure of the new check
    first_check = [m for m in messages if measured(m)][0]["check"]
    page.click("#check-again")
    wait_until(lambda: contact_bytes(device).count(0x11) == ons + 1, what="a second 0x11")
    assert names_of(folder).count("contact_start") == 2
    page.wait_for_selector("#advance[disabled]", timeout=5_000)
    wait_until(
        lambda: [m for m in messages if measured(m)][-1]["check"] != first_check,
        what="a measure of the new check",
        page=page,
    )
    wait_until(shown_matches_last, timeout=10, what="the screen to show the new check", page=page)
    page.wait_for_selector("#advance:not([disabled])")

    # Continue: the check goes off, the data confirm it and settle, then eyes open, eyes closed
    # with the tone, five blink cues
    page.click("#advance")
    wait_until(lambda: last_contact_byte(device) == 0x12, what="0x12")
    page.wait_for_selector(f'{SCREEN}[data-substep="settling"]', timeout=10_000)
    pass_signal_check(page)
    page.wait_for_selector(f'{SCREEN}[data-substep="eyes_open"]', timeout=20_000)
    page.wait_for_selector(f'{SCREEN}[data-substep="eyes_closed"]', timeout=20_000)
    page.wait_for_selector(f'{SCREEN}[data-substep="blinks"]', timeout=20_000)
    page.wait_for_selector(f'{SCREEN}[data-substep="done"]', timeout=60_000)
    # nothing scored (the score is not shown during setup since 2026-11-26: read its text)
    assert page.text_content("#hud-points") == "\u2014"
    assert contact_bytes(device).count(0x11) == ons + 1  # nothing switched it on again

    names = names_of(folder)
    for name in ("contact_start", "contact_end", "settling_start", "eyes_open_start", "eyes_open_end",
                 "eyes_closed_start", "eyes_closed_end", "blinks_start", "blinks_end", "tone"):  # fmt: skip
        assert name in names, (name, names)
    assert names.count("blink_cue") == 5
    order = [n for n in names if n != "blink_cue"]
    assert order.index("contact_end") < order.index("settling_start") < order.index("signal_start")
    assert order.index("signal_start") < order.index("signal_end") < order.index("eyes_open_start")
    assert order.index("eyes_open_start") < order.index("eyes_open_end")
    assert order.index("eyes_open_end") <= order.index("eyes_closed_start") < order.index("eyes_closed_end")
    assert order.index("eyes_closed_end") <= order.index("blinks_start") < order.index("blinks_end")
    for sub in ("eyes_open", "eyes_closed"):
        took = event_time(folder, f"{sub}_end") - event_time(folder, f"{sub}_start")
        assert abs(took - SECONDS) <= TOLERANCE, (sub, took)
    # each cue is logged with the page's clock, like a task stimulus: its place in the schedule
    # (the server's clock) and how late it was drawn; none is ever left out
    cues = [e for e in events_of(folder) if e["name"] == "blink_cue"]
    assert [e["payload"]["n"] for e in cues] == [1, 2, 3, 4, 5]
    assert [e["payload"]["due_s"] for e in cues] == [2.0, 4.0, 6.0, 8.0, 10.0]
    assert all(isinstance(e["payload"]["late_ms"], (int, float)) and e["payload"]["late_ms"] >= 0 for e in cues)
    assert all(isinstance(e["browser_time"], float) and e["time_origin"] > 1e12 for e in cues)
    assert all(b["browser_time"] > a_["browser_time"] for a_, b in zip(cues, cues[1:]))
    tone = next(e for e in events_of(folder) if e["name"] == "tone")
    assert tone["payload"]["hz"] > 0 and tone["payload"]["audio"] in ("played", "unavailable")
    # every event of the step has its marker in the recording
    marks = [m["label"] for m in markers_of(folder / "eeg")]
    for e in events_of(folder):
        if e["step"] == "calibration":
            assert f"calibration/{e['name']}#{e['seq']}" in marks

    # the result: the states when Continue was pressed, the bad ones left out, the version
    saved = json.loads((folder / "calibration.json").read_text())
    assert set(saved) >= {"states", "excluded", "thresholds_version", "unix_time"}
    assert len(saved["states"]) == 20 and set(saved["states"].values()) <= {"good", "marginal", "bad"}
    assert saved["excluded"] == [n for n, s in saved["states"].items() if s == "bad"]
    assert saved["thresholds_version"] == contact.THRESHOLDS.version
    end = next(e for e in events_of(folder) if e["name"] == "contact_end")
    assert end["payload"]["excluded"] == saved["excluded"]
    states = [m for m in messages if m.get("t") == "state"]
    assert states[-1]["calibration"] == saved
    if saved["excluded"]:
        assert all(n in page.inner_text("#calib-done") for n in saved["excluded"])

    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="words"]', timeout=10_000)
    assert page.inner_text("#hud-points") == "\u2014"  # nothing scored
    assert contact_bytes(device).count(0x11) == ons + 1
    assert last_contact_byte(device) == 0x12
    only_allowed(device)
    # the eyes-open baseline starts after the last sample recorded with the check on, plus the
    # settling time, by the recording's own sample numbers
    stop_app(a)
    start = marker_sample(folder, "eyes_open_start")
    last_on, fs = last_check_on_before(folder, start)
    assert start >= last_on + SETTLE_S * fs, (start, last_on)


def test_a_fit_view_that_arrives_after_the_contact_check_was_left_is_not_drawn(app, device, calib_flow, page):
    """The head meshes (``meshes.json``, the real file from the real server) are held back until
    the participant has pressed Continue and eyes open has begun; when they arrive the fit view
    is not built: no canvas, no WebGL context."""
    page.add_init_script(WEBGL_WATCH)
    held = []
    page.route("**/assets/meshes.json", lambda route: held.append(route))
    a = app(port=device.path, flow=calib_flow)
    to_calibration(a, page)
    wait_until(lambda: held, what="the request for the meshes", page=page)
    assert page.evaluate("window.__calibration.loading") is True
    page.wait_for_selector("#advance:not([disabled])", timeout=30_000)  # measured
    page.click("#advance")
    pass_signal_check(page)
    page.wait_for_selector(f'{SCREEN}[data-substep="eyes_open"]', timeout=20_000)
    with page.expect_response("**/assets/meshes.json"):
        held[0].continue_()
    page.wait_for_function("window.__calibration.loading === false", timeout=20_000)
    assert page.evaluate("window.__calibration.view") is None
    assert page.locator("canvas").count() == 0
    assert page.evaluate(WEBGL_STATE)["made"] == 0
    only_allowed(device)


def test_a_page_reload_during_the_contact_check_switches_it_off_until_check_again(app, device, calib_flow, page):
    a = app(port=device.path, flow=calib_flow)
    to_calibration(a, page)
    wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
    page.wait_for_selector("#contact-count[data-good]", timeout=20_000)
    page.reload()
    wait_until(lambda: last_contact_byte(device) == 0x12, what="0x12 after the reload")
    page.wait_for_selector(f'{SCREEN}[data-substep="contact"][data-check="off"]', timeout=20_000)
    assert "contact_check_off" in names_of(session_folder(a))
    # no measure of a check that is on: Continue is off and the screen says what to do
    assert page.locator("#advance").is_disabled()
    assert "Press Check again" in page.inner_text("#contact-off")
    page.click("#check-again")
    wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11 after Check again")
    page.wait_for_selector(f'{SCREEN}[data-substep="contact"][data-check="on"]')
    page.wait_for_selector("#advance:not([disabled])", timeout=20_000)
    only_allowed(device)


def test_continue_is_refused_until_the_check_has_been_measured(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws, wait_measure=False)
        send(ws, cmd="advance")
        error = next_message(ws, lambda m: m.get("t") == "error")
        assert error["kind"] == "refused" and "measure" in error["msg"]
        next_message(ws, measured)
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m.get("substep") == "settling")
    assert "contact_end" in names_of(session_folder(a))
    only_allowed(device)


def test_eyes_open_is_kept_by_the_server_and_a_pause_stops_its_clock(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a, page="page-1") as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="advance")
        ws_pass_signal_check(ws)
        running = next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "eyes_open")
        assert running["phase"] == "running" and running["remaining"] <= SECONDS
        time.sleep(1.0)
        send(ws, cmd="pause", reason="fullscreen_lost")
        paused = next_state(ws, predicate=lambda m: m["phase"] == "paused")
        assert paused["substep"] == "eyes_open"
        time.sleep(2.0)  # longer than what was left would allow, were the clock running
        again = next_state(ws, predicate=lambda m: True)
        assert again["substep"] == "eyes_open" and again["remaining"] == paused["remaining"]
        send(ws, cmd="resume")
        next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "eyes_closed")
    folder = session_folder(a)
    events = [e for e in events_of(folder) if e["step"] == "calibration"]
    # Since 2026-11-25 any pause starts the part again from its beginning on Resume (a baseline
    # with a gap is not a baseline), as a lost link did already.
    restarted = [e for e in events if e["name"] == "part_restarted"]
    assert [(e["payload"]["part"], e["payload"]["reason"]) for e in restarted] == [("eyes_open", "fullscreen_lost")]
    starts = [e["unix_time"] for e in events if e["name"] == "eyes_open_start"]
    at = {e["name"]: e["unix_time"] for e in events}
    assert len(starts) == 2 and starts[1] >= at["resumed"]
    ran = at["eyes_open_end"] - starts[1]
    assert abs(ran - SECONDS) <= TOLERANCE, ran
    only_allowed(device)


def test_a_headset_that_never_confirms_the_check_is_off_does_not_start_the_baseline(app, stubborn, calib_flow):
    a = app(port=stubborn.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="advance")
        state = next_state(ws, timeout=30, predicate=lambda m: m.get("substep") == "not_off")
        assert state["phase"] == "waiting" and state["contact_check"] is None
        # 0x12 was sent again meanwhile, and nothing switched the check on
        assert contact_bytes(stubborn).count(0x12) >= 2 and last_contact_byte(stubborn) == 0x12
        # Check again goes back to the contact check
        send(ws, cmd="check_again")
        next_state(ws, predicate=lambda m: m.get("substep") == "contact" and m["contact_check"] is not None)
        wait_until(lambda: last_contact_byte(stubborn) == 0x11, what="0x11")
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["phase"] == "done")
    names = names_of(session_folder(a))
    assert "contact_not_off" in names and "eyes_open_start" not in names
    wait_until(lambda: last_contact_byte(stubborn) == 0x12, what="0x12 after the stop")
    only_allowed(stubborn)


def test_a_round_does_not_start_while_the_check_is_still_going_off(app, device, calib_flow):
    """Skipping from the contact check into the first round: its first block waits until the data
    show the check off for the settling time."""
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="skip", step="calibration")
        next_state(ws, predicate=lambda m: m["step"] == "words")
        send(ws, cmd="advance")
        error = next_message(ws, lambda m: m.get("t") == "error")
        assert error["kind"] == "refused" and "contact check" in error["msg"]
        started = None
        deadline = time.monotonic() + 20
        while started is None:
            assert time.monotonic() < deadline
            time.sleep(0.5)
            send(ws, cmd="advance")
            message = next_message(ws, lambda m: m.get("t") == "error" or (m.get("t") == "state" and m["phase"] == "running"))
            if message.get("t") == "state":
                started = message
    folder = session_folder(a)
    stop_app(a)
    start = marker_sample(folder, "block_start")
    last_on, fs = last_check_on_before(folder, start)
    assert start >= last_on + SETTLE_S * fs, (start, last_on)
    only_allowed(device)


# -- the contact check is never left on -----------------------------------------------------------


def test_the_link_coming_up_switches_the_contact_check_off(app, device, calib_flow):
    """The headset of the pilot recordings starts with its contact check on (its configuration
    says ``impedance_at_power_on``), so every link that comes up is sent 0x12 first."""
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        next_state(ws)
        wait_until(lambda: contact_bytes(device)[:1] == b"\x12", what="0x12 at link up")
    assert device.written()[:2] == bytes([0x14, 0x12])
    wait_until(lambda: not device.contact_on, what="the device to switch its check off")
    only_allowed(device)


def test_a_lost_link_that_comes_back_is_sent_0x12(app, calib_flow, tmp_path):
    first = PtyDevice(contact_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    second = None
    try:
        a = app(port=str(link), flow=calib_flow)
        with open_ws(a) as ws:
            next_state(ws)
            assert post_label(a, "P001")[0] == 200
            wait_until(lambda: last_contact_byte(first) == 0x12, what="0x12 to the first device")
            first.hang_up()
            second = PtyDevice(contact_stream(), config_reply())
            temporary = link.with_name("headset-port.new")
            temporary.symlink_to(second.path)
            temporary.replace(link)
            wait_until(lambda: last_contact_byte(second) == 0x12, timeout=30, what="0x12 to the device that came back")
            assert second.written()[:2] == bytes([0x14, 0x12])
            only_allowed(second)
    finally:
        first.hang_up()
        if second is not None:
            second.hang_up()


@pytest.mark.parametrize("how", ["skip", "stop"])
def test_skip_or_stop_during_the_contact_check_switches_it_off(app, device, calib_flow, how):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        state = ws_to_contact(a, ws)
        assert state["contact_check"] is not None
        wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
        send(ws, cmd=how, **({"step": "calibration"} if how == "skip" else {}))
        after = next_state(ws, predicate=lambda m: m["step"] != "calibration" or m["phase"] == "done")
        assert after["contact_check"] is None
        wait_until(lambda: last_contact_byte(device) == 0x12, what="0x12")
    names = names_of(session_folder(a))
    assert ("skipped" if how == "skip" else "stopped") in names
    assert "contact_end" not in names  # no result is saved for a check that was not continued
    assert not (session_folder(a) / "calibration.json").exists()
    only_allowed(device)


@pytest.mark.parametrize("sub", ["eyes_open", "blinks"])
def test_skipping_a_later_sub_step_leaves_the_contact_check_off(app, device, calib_flow, sub):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="advance")
        ws_pass_signal_check(ws)
        next_state(ws, timeout=40, predicate=lambda m: m.get("substep") == sub)
        send(ws, cmd="skip", step="calibration")
        next_state(ws, predicate=lambda m: m["step"] == "words")
    assert contact_bytes(device).count(0x11) == 1 and last_contact_byte(device) == 0x12
    assert json.loads((session_folder(a) / "calibration.json").read_text())["thresholds_version"]
    only_allowed(device)


def test_the_end_of_a_session_with_a_failure_during_the_contact_check_switches_it_off(device, calib_flow, tmp_path):
    """A failure ends the session at once (``Session.fail``, which the hub calls when the
    recording cannot be started); the check must not stay on after it."""
    import asyncio

    from brain_mapper.server import Client, Hub, Settings

    hub = Hub(Settings(data_dir=tmp_path, port=device.path, replay=None, flow=calib_flow, http_port=1))
    hub.start()
    loop = asyncio.new_event_loop()
    try:
        assert hub.ready.wait(20)
        hub.accept("P001")
        page = Client(loop, "participant", "page-1")
        assert hub.connect(page)
        hub.command(page, {"cmd": "advance"})  # participant -> device
        wait_until(lambda: hub.source.recording, what="the recording")
        hub.command(page, {"cmd": "advance"})  # device -> calibration, contact
        wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
        hub.session.fail("the recording could not be started (a test of the ending)")
        assert hub.session.state()["contact_check"] is None
        wait_until(lambda: last_contact_byte(device) == 0x12, what="0x12")
    finally:
        hub.stop()
        loop.close()
    only_allowed(device)


def test_the_end_of_the_session_leaves_the_contact_check_off(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
        state = None
        deadline = time.monotonic() + 240
        while state is None or state["phase"] != "done":
            assert time.monotonic() < deadline, state
            state = next_state(ws, timeout=30)
            if state["phase"] in ("waiting", "between") and state.get("substep") not in ("settling",):
                send(ws, cmd="advance")
                state = next_state(ws, timeout=30, predicate=lambda m, rev=state["rev"]: m["rev"] > rev)
        assert state["stopped"] is False and state["failure"] is None
    names = names_of(session_folder(a))
    assert "blinks_end" in names
    assert contact_bytes(device).count(0x11) == 1 and last_contact_byte(device) == 0x12
    only_allowed(device)


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_shutting_the_process_down_during_the_contact_check_switches_it_off(app, device, calib_flow, sig):
    a = app(port=device.path, flow=calib_flow)
    ws = open_ws(a)  # the page stays connected: only the process going down switches the check off
    try:
        ws_to_contact(a, ws, wait_measure=False)
        wait_until(lambda: last_contact_byte(device) == 0x11, what="0x11")
        time.sleep(0.5)
        assert last_contact_byte(device) == 0x11
        a.process.send_signal(sig)
        a.process.wait(timeout=20)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    assert last_contact_byte(device) == 0x12
    only_allowed(device)


def test_contact_messages_are_sent_only_while_the_check_is_on(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws, wait_measure=False)
        got: list[dict] = []
        while len([m for m in got if measured(m)]) < 3:
            next_message(ws, lambda m: m.get("t") == "contact", seen=got)
        contacts = [m for m in got if m.get("t") == "contact"]
        # first the settling second, then measures, each of a later stretch of the recording
        assert contacts[0]["settling"] is True and contacts[0]["states"] is None
        measures = [m for m in contacts if measured(m)]
        assert all(not m["settling"] for m in measures)
        samples = [m["sample"] for m in measures]
        assert all(b > a_ for a_, b in zip(samples, samples[1:])), samples
        for m in measures:
            assert len(m["states"]) == 20 and set(m["states"].values()) <= {"good", "marginal", "bad"}
            assert m["good"] == sum(1 for s in m["states"].values() if s == "good")
        send(ws, cmd="advance")  # Continue: the check goes off
        later: list[dict] = []
        ws_pass_signal_check(ws)
        next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "eyes_open")
        next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "eyes_closed")
        # (next_state reads past every other message; collect again over eyes closed)
        next_message(ws, lambda m: m.get("t") == "state" and m.get("substep") == "blinks", timeout=20, seen=later)
        assert not [m for m in later if m.get("t") == "contact"]
    only_allowed(device)

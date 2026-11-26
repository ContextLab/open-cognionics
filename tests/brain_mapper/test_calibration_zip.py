"""The calibration step together with the session zip and the onlooker window (merged here).

A real ``brain-mapper`` runs on the simulated headset of the calibration tests (it answers ``0x11``
and ``0x12`` by playing the check-on or the check-off recording); the participant is a real
Chromium page or the real websocket, the onlooker a real Chromium page. What is checked:

- the ``calibration.json`` the contact check writes is the first entry of
  ``channel_quality_history`` in the session.json of the folder and of the zip, with its
  ``thresholds_version`` as ``contact_thresholds_version``;
- the onlooker shows "Contact check not done yet", then the live states of the real ``contact``
  messages, then the states saved at calibration (the ``calibration`` field of the state);
- the timeline (session.json and the summary screen) lists the calibration's parts;
- a stop during ``contact`` or ``settling`` writes ``0x12`` last and the zip is made;
- a session whose headset never linked writes no contact byte and is zipped without EEG.

Only ``0x11``, ``0x12`` and ``0x14`` are ever written to a device.
"""

from __future__ import annotations

import json

import pytest

from brain_mapper import contact
from test_calibration_browser import (
    SCREEN,
    pass_signal_check,
    ws_pass_signal_check,
    SECONDS,
    contact_bytes,
    contact_stream,
    last_contact_byte,
    measured,
    next_message,
    only_allowed,
    to_calibration,
    wait_until,
    ws_to_contact,
)
from test_engine import SHORT, next_state, open_ws, send
from test_onlooker_browser import LONG
from test_package import next_package, wait_packaged, zip_contents, zips_in
from test_participants import post_label, session_folder, stop
from test_skip_browser import answer_yes, ask

from pty_device import PtyDevice, config_reply, onhead_stream

STATES = {"good", "marginal", "bad"}


@pytest.fixture
def device():
    """As in test_calibration_browser: answers 0x11 and 0x12, and starts with the check on."""
    dev = PtyDevice(onhead_stream(), config_reply(), contact_stream=contact_stream(), contact_on=True)
    yield dev
    dev.hang_up()


@pytest.fixture
def calib_flow(tmp_path):
    """The short flow with eyes open and eyes closed of ``SECONDS`` each, as in test_calibration_browser."""
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": SECONDS, "eyes_closed": SECONDS}
    path = tmp_path / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path
PARTS = ["contact", "settling", "signal", "eyes_open", "eyes_closed", "blinks"]


def open_look(browser, a):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    look = context.new_page()
    look.goto(a.url + "/onlooker?test=1")
    look.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=LONG)
    return context, look


def through_calibration(ws, timeout=60) -> dict:
    """From the contact sub-step (measured): Continue, then wait until the calibration is done."""
    send(ws, cmd="advance")
    ws_pass_signal_check(ws)
    return next_state(ws, timeout=timeout, predicate=lambda m: m.get("substep") == "done")


def parts_of(timeline: list[dict]) -> list[dict]:
    return [t for t in timeline if t.get("part") is not None]


# -- a. calibration.json in session.json and the zip -----------------------------------------------


def test_a_real_contact_check_is_the_first_entry_of_the_quality_history_in_the_zip(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="advance")  # Continue: the states of the last second are the result
        folder = session_folder(a)
        wait_until(lambda: (folder / "calibration.json").exists(), what="calibration.json")
        next_state(ws, timeout=20, predicate=lambda m: m.get("calibration") is not None)
        send(ws, cmd="stop")
        saved = next_package(ws, "saved")
    written = (folder / "calibration.json").read_bytes()
    result = json.loads(written)
    assert len(result["states"]) == 20 and set(result["states"].values()) <= STATES
    assert result["excluded"] == [k for k, v in result["states"].items() if v == "bad"]
    assert result["thresholds_version"] == contact.THRESHOLDS.version
    path = wait_packaged(folder)
    assert saved["file"] == path.name
    content = zip_contents(path)
    assert content["calibration.json"] == written
    for meta in (json.loads(content["session.json"]), json.loads((folder / "session.json").read_text())):
        assert meta["channel_quality_history"] == [result]
        assert meta["contact_thresholds_version"] == contact.THRESHOLDS.version
        assert "calibration_problem" not in meta
    only_allowed(device)


# -- b. the onlooker during and after the contact check -------------------------------------------


def test_the_onlooker_shows_the_live_contact_states_and_then_the_saved_ones(app, device, calib_flow, browser):
    a = app(port=device.path, flow=calib_flow)
    context, look = open_look(browser, a)
    try:
        line = look.locator("#ol-contact")
        assert line.inner_text().strip() == "Contact check not done yet"
        with open_ws(a) as ws:
            ws_to_contact(a, ws, wait_measure=False)
            # the first message of the check is settling (no states): the onlooker says the check is on
            first = next_message(ws, lambda m: m.get("t") == "contact", timeout=20)
            assert first["settling"] is True and first["states"] is None
            look.wait_for_function(
                "document.getElementById('ol-contact').dataset.source === 'settling'", timeout=5_000
            )
            assert line.inner_text().strip() == "Contact check on: the first measure comes in a second"
            # then the measured states, as the real messages give them
            last = next_message(ws, measured, timeout=20)
            look.wait_for_function(
                "document.getElementById('ol-contact').dataset.source === 'live'", timeout=5_000
            )

            def shown() -> bool:
                text = line.inner_text().strip()
                drawn = look.evaluate(
                    "() => { const h = window.__onlooker.fit.headset;"
                    " return Object.fromEntries(h.contacts.map((c) => [c.name, h.state(c.name)])); }"
                )
                return text == f"Live contact check: {last['good']} of 20 contacts good" and drawn == last["states"]

            # a newer message can come between the reads: take the newest each time
            def newest() -> bool:
                nonlocal last
                try:
                    last = next_message(ws, measured, timeout=0.2)
                except AssertionError:
                    pass
                return shown()

            wait_until(newest, timeout=15, what="the onlooker to show the live states", page=look)
            send(ws, cmd="advance")  # Continue: the check goes off and the result is saved
            state = next_state(ws, timeout=20, predicate=lambda m: m.get("calibration") is not None)
        saved = state["calibration"]["states"]
        good = sum(1 for s in saved.values() if s == "good")
        look.wait_for_function(
            "document.getElementById('ol-contact').dataset.source === 'saved'", timeout=10_000
        )
        assert line.inner_text().strip() == f"Saved at calibration: {good} of 20 contacts good"
        drawn = look.evaluate(
            "() => { const h = window.__onlooker.fit.headset;"
            " return Object.fromEntries(h.contacts.map((c) => [c.name, h.state(c.name)])); }"
        )
        assert drawn == saved
        assert saved == json.loads((session_folder(a) / "calibration.json").read_text())["states"]
        assert look.evaluate("window.__onlooker.errors") == []
    finally:
        context.close()
    only_allowed(device)


# -- c. the calibration's parts in the timeline and on the summary screen ------------------------


def test_the_timeline_lists_the_calibration_parts_in_order(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        through_calibration(ws)
        send(ws, cmd="stop")
        saved = next_package(ws, "saved")
    folder = session_folder(a)
    meta = json.loads(zip_contents(wait_packaged(folder))["session.json"])
    parts = parts_of(meta["timeline"])
    assert [p["part"] for p in parts] == PARTS
    assert all(p["step"] == "calibration" and p["block"] is None and p["outcome"] == "finished" for p in parts)
    for p in parts:
        assert p["seconds"] is not None and p["seconds"] >= 0 and p["start_sample"] is not None
    by = {p["part"]: p for p in parts}
    for name in ("eyes_open", "eyes_closed"):
        assert abs(by[name]["seconds"] - SECONDS) < 0.6, by[name]
    starts = [p["start_unix"] for p in parts]
    assert starts == sorted(starts)
    # the step itself is still one entry, before its parts
    steps = [t for t in meta["timeline"] if t["step"] == "calibration" and t.get("part") is None]
    assert len(steps) == 1 and steps[0]["block"] is None
    # what the summary screen is given lists the same parts
    assert [t.get("part") for t in saved["summary"]["timeline"] if t.get("part")] == PARTS


def test_a_stop_in_eyes_open_ends_that_part_as_left(app, device, calib_flow):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        send(ws, cmd="advance")
        ws_pass_signal_check(ws)
        next_state(ws, timeout=30, predicate=lambda m: m.get("substep") == "eyes_open")
        send(ws, cmd="stop")
        next_package(ws, "saved")
    meta = json.loads((session_folder(a) / "session.json").read_text())
    parts = parts_of(meta["timeline"])
    assert [p["part"] for p in parts] == ["contact", "settling", "signal", "eyes_open"]
    assert [p["outcome"] for p in parts] == ["finished", "finished", "finished", "left"]


def test_the_summary_screen_lists_the_calibration_parts(app, device, calib_flow, page):
    a = app(port=device.path, flow=calib_flow)
    to_calibration(a, page)
    page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
    page.click("#advance")
    pass_signal_check(page)
    page.wait_for_selector(f'{SCREEN}[data-substep="done"]', timeout=60_000)
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=30_000)
    page.wait_for_selector("#summary-download", state="visible", timeout=30_000)
    rows = page.locator("#summary-timeline tbody tr[data-part]")
    assert [rows.nth(i).get_attribute("data-part") for i in range(rows.count())] == PARTS
    cells = [rows.nth(i).locator("td").all_inner_texts() for i in range(rows.count())]
    assert [c[0] for c in cells] == ["Calibration"] * 6
    assert [c[1] for c in cells] == ["Contact check", "Settling", "Signal check", "Eyes open", "Eyes closed", "Blinks"]
    only_allowed(device)


# -- c. a stop during the contact check or the settling --------------------------------------------


@pytest.mark.parametrize("sub", ["contact", "settling"])
def test_a_stop_during_the_check_or_its_settling_writes_0x12_and_makes_the_zip(app, device, calib_flow, sub):
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        ws_to_contact(a, ws)
        assert last_contact_byte(device) == 0x11 or wait_until(lambda: last_contact_byte(device) == 0x11)
        if sub == "settling":
            send(ws, cmd="advance")
            next_state(ws, timeout=10, predicate=lambda m: m.get("substep") == "settling")
        send(ws, cmd="stop")
        final = next_state(ws, predicate=lambda m: m["phase"] == "done")
        assert final["contact_check"] is None and final["stopped"] is True
        saved = next_package(ws, "saved")
    wait_until(lambda: last_contact_byte(device) == 0x12, what="0x12")
    folder = session_folder(a)
    path = wait_packaged(folder)
    assert zips_in(folder.parent) == [path.name] == [saved["file"]]
    meta = json.loads(zip_contents(path)["session.json"])
    assert meta["complete"] is False and meta["recording"] is True
    assert ("calibration.json" in zip_contents(path)) == (sub == "settling")
    assert contact_bytes(device).count(0x11) == 1
    only_allowed(device)


# -- d. the contact-off byte and a session whose headset never linked -----------------------------


def test_a_session_that_never_linked_writes_no_contact_byte_and_is_zipped_without_eeg(app, calib_flow):
    silent = PtyDevice.silent_device()
    try:
        a = app(port=silent.path, flow=calib_flow)
        assert post_label(a, "P001")[0] == 200
        folder = session_folder(a)
        with open_ws(a) as ws:
            next_state(ws)
            send(ws, cmd="advance")  # to the device step, which cannot be passed without a link
            next_state(ws, predicate=lambda m: m["step"] == "device")
            send(ws, cmd="stop")
            final = next_state(ws, predicate=lambda m: m["phase"] == "done")
            assert final["contact_check"] is None
            next_package(ws, "saved")
        stop(a)
        assert a.process.returncode == 0
        written = silent.written()
    finally:
        silent.hang_up()
    assert set(written) <= {0x14}, written.hex()  # no link: no contact byte at all
    content = zip_contents(wait_packaged(folder))
    assert not any(name.startswith("eeg") for name in content)
    meta = json.loads(content["session.json"])
    assert meta["recording"] is False and meta["channel_quality_history"] == []
    assert meta["contact_thresholds_version"] is None


def test_the_link_up_0x12_comes_before_the_session_and_the_zip_still_records_everything(app, device, calib_flow):
    """The device starts with its check on: 0x12 goes out at link-up, before any session, and a
    session played after it is zipped with its recording and no calibration result."""
    a = app(port=device.path, flow=calib_flow)
    with open_ws(a) as ws:
        next_state(ws)
        wait_until(lambda: contact_bytes(device)[:1] == b"\x12", what="0x12 at link up")
        assert device.written()[:2] == bytes([0x14, 0x12])
        ws_to_contact(a, ws)
        send(ws, cmd="skip", step="calibration")
        next_state(ws, predicate=lambda m: m["step"] == "words")
        send(ws, cmd="stop")
        next_package(ws, "saved")
    assert contact_bytes(device) == bytes([0x12, 0x11, 0x12])
    content = zip_contents(wait_packaged(session_folder(a)))
    meta = json.loads(content["session.json"])
    assert meta["recording"] is True and "eeg/raw.bin" in content
    assert meta["channel_quality_history"] == [] and "calibration.json" not in content
    only_allowed(device)

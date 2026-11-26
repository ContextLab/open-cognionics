"""The onlooker window (``/onlooker``) in a real Chromium, next to a real participant page.

Every test starts the ``brain-mapper`` command on a pseudo-terminal device (the simulated
headset), opens the participant page in one browser context and the onlooker page in another,
and reads the DOM of both. Nothing opens a real serial port.

What cannot be tried here: the live contact states of the contact check and the states saved at
calibration come from the calibration task (another worktree). ``test_contact_states_...`` feeds
the page's own message handler a ``contact`` message written out by hand, which shows that the
page draws them; that the server sends such a message is checked after the merge.
"""

from __future__ import annotations

import json
import shutil
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit

from test_engine import SHORT, accept, events_of, markers_of, open_ws, send, session_folder_of
from test_shell_browser import (
    SCREEN,
    advance_until,
    begin,
    free_port,
    long_flow,
    press,
    score_text,
    screen_of,
    server_state,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import FIXTURES, PtyDevice, config_reply, onhead_stream  # noqa: E402

LONG = 120_000


def open_onlooker(browser, a, test=True, query=""):
    """A second browser context (a second window) showing the onlooker page."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    sent: list[str] = []

    def watch(ws):
        ws.on("framesent", lambda payload: sent.append(payload))

    page.on("websocket", watch)
    page.sent = sent
    page.goto(a.url + "/onlooker" + (("?test=1" + query) if test else ""))
    page.wait_for_selector("#ol-root")
    page.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=LONG)
    return context, page


def wait_state(page, step, phase=None, timeout=2000):
    cond = f"document.getElementById('ol-root').dataset.step === '{step}'"
    if phase:
        cond += f" && document.getElementById('ol-root').dataset.phase === '{phase}'"
    page.wait_for_function(cond, timeout=timeout)


def play_block(page):
    """Starts the block shown (it is waiting) and waits for the between-blocks screen."""
    page.click("#advance", timeout=10_000)
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=60_000)


# ---------------------------------------------------------------------------------------------


def open_onlooker_timed(browser, a):
    """The onlooker page, with the time the page received each websocket frame for it: the
    browser's own record (CDP ``Network.webSocketFrameReceived``), put on the wall clock with the
    ``wallTime`` of the socket's handshake. Returns the context, the page and the list of
    ``(unix time, message)`` it fills."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    origins: dict[str, tuple[float, float]] = {}
    frames: list[tuple[float, dict]] = []

    def handshake(event):
        origins[event["requestId"]] = (event["timestamp"], event["wallTime"])

    def frame(event):
        base = origins.get(event["requestId"])
        try:
            message = json.loads(event["response"]["payloadData"])
        except ValueError:
            return
        if base is not None:
            frames.append((base[1] + event["timestamp"] - base[0], message))

    cdp.on("Network.webSocketWillSendHandshakeRequest", handshake)
    cdp.on("Network.webSocketFrameReceived", frame)
    cdp.send("Network.enable")
    page.goto(a.url + "/onlooker?test=1")
    page.wait_for_selector("#ol-root")
    page.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=LONG)
    return context, page, frames


def test_the_onlooker_page_follows_the_participants_step_within_two_seconds(
    app, pty_device, page, browser
):
    """Each step and block change of the participant reaches the onlooker within 2 s: from the
    engine's change (the ``unix_time`` of the ``step_start`` or ``block_start`` event it writes
    as it changes) to the onlooker's browser receiving the first ``state`` that shows it, on the
    same machine clock; the onlooker then shows that step and phase.

    Measured on 2026-11-25 (in the server: engine change to the onlooker socket's send at most
    2 ms, nothing waiting ahead of the state), the delay is in the page: the headless Chromium of
    the tests draws the onlooker's two 3-D views in software, which held its main thread about
    0.1 s a frame and about 1 s while the views first compiled (long tasks of the page; with the
    GPU, 0 ms but the first compile). The receipt times here took 0.11 to 0.19 s under load. The
    time the page then takes to show the state is not bounded here; that is the test browser."""
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look, frames = open_onlooker_timed(browser, a)
    try:
        wait_state(look, "participant", timeout=10_000)
        seen = []
        for step in ("device", "calibration"):
            advance_until(page, step, "waiting")
            wait_state(look, step, "waiting", timeout=LONG)
            seen.append((step, "waiting"))
        for step in ("words", "perception", "rest_closed", "rest_open", "recall"):
            advance_until(page, step, "waiting")
            wait_state(look, step, "waiting", timeout=LONG)
            press(page)
            wait_state(look, step, "running", timeout=LONG)
            seen += [(step, "waiting"), (step, "running")]
        assert seen[0] == ("device", "waiting")
        events = events_of(session_folder_of(a))
        delays = {}
        for step, phase in seen:
            name = "step_start" if phase == "waiting" else "block_start"
            changed = next(e["unix_time"] for e in events if e["step"] == step and e["name"] == name)
            received = min(
                t for t, m in frames if m.get("t") == "state" and m["step"] == step and m["phase"] == phase
            )
            delays[(step, phase)] = round(received - changed, 3)
        assert all(-0.05 <= d <= 2.0 for d in delays.values()), delays
    finally:
        context.close()


def test_the_points_total_matches_the_participants_between_blocks_and_never_moves_in_a_block(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        look.evaluate(
            """() => {
              window.__log = [];
              const root = document.getElementById('ol-root');
              const points = document.getElementById('ol-points');
              const note = () => window.__log.push([root.dataset.phase, points.textContent.trim()]);
              note();
              const watch = new MutationObserver(note);
              watch.observe(root, {attributes: true, attributeFilter: ['data-phase']});
              watch.observe(points, {childList: true, characterData: true, subtree: true});
            }"""
        )
        advance_until(page, "words", "waiting")
        for block in range(2):
            if block == 0:
                play_block(page)  # Start
            else:
                press(page)  # Continue on the between screen starts the next block
            ledger = 100 * (block + 1)
            # Both show the session's score (earned of possible) of the server's state.
            truth = score_text(server_state(
                a, lambda m: m["phase"] == "between" and m["points_total"] == ledger, timeout=60
            ))
            page.wait_for_function(
                "(v) => document.getElementById('hud-points').textContent.trim() === v",
                arg=truth,
                timeout=10_000,
            )
            look.wait_for_function(
                "(v) => document.getElementById('ol-points').textContent.trim() === v",
                arg=truth,
                timeout=2000,
            )
        log = look.evaluate("window.__log")
        assert [phase for phase, _ in log if phase == "running"], "no block was seen running"
        for (_, before), (phase, after) in zip(log, log[1:]):
            if before != after:
                assert phase == "between", f"the total changed in phase {phase}: {log}"
    finally:
        context.close()


def test_commands_sent_by_hand_over_the_onlookers_socket_change_nothing(
    app, pty_device, page, browser, tmp_path
):
    a = app(port=pty_device.path, flow=long_flow(tmp_path))
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "words", "waiting")
        before = server_state(a)
        hand = [
            {"cmd": "advance"},
            {"cmd": "pause", "reason": "hand"},
            {"cmd": "resume"},
            {"cmd": "skip", "step": "words"},
            {"cmd": "stop"},
            {"cmd": "event", "name": "hand", "payload": {}},
        ]
        for command in hand:
            look.evaluate("(c) => window.__onlooker.sendRaw(JSON.stringify(c))", command)
        # a message the server answers makes sure the commands before it were read
        look.evaluate("window.__onlooker.sendRaw('not json')")
        look.wait_for_function(
            "window.__onlooker.errors.some((e) => e.kind === 'bad_message')", timeout=10_000
        )
        after = server_state(a)
        keys = ("step", "phase", "blocks_done", "points_total", "stopped", "rev", "reason")
        assert {k: after[k] for k in keys} == {k: before[k] for k in keys}
        assert after["phase"] == "waiting"
        assert screen_of(page) == ["placeholder", "words", "waiting"]

        # now while a block runs: pause, skip and stop by hand do nothing either
        press(page)
        wait_state(look, "words", "running", timeout=5000)
        run = server_state(a)
        look.evaluate("window.__onlooker.errors.length = 0")
        for command in hand:
            look.evaluate("(c) => window.__onlooker.sendRaw(JSON.stringify(c))", command)
        look.evaluate("window.__onlooker.sendRaw('not json')")
        look.wait_for_function(
            "window.__onlooker.errors.some((e) => e.kind === 'bad_message')", timeout=10_000
        )
        later = server_state(a)
        assert later["phase"] == "running"
        assert later["rev"] == run["rev"]
        assert not later["stopped"]
        folder = session_folder_of(a)
        names = [m.get("label", "") for m in markers_of(folder / "eeg")]
        assert not any("hand" in n or "paused" in n or "skipped" in n or "stopped" in n for n in names)
    finally:
        context.close()


def test_the_page_itself_sends_only_display_commands(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "calibration", "waiting")
        wait_state(look, "calibration", "waiting", timeout=2000)
        look.wait_for_function("document.getElementById('ol-traces').dataset.rows > 0", timeout=30_000)
        assert look.sent, "the page sent nothing, not even the display request"
        for text in look.sent:
            assert json.loads(text)["cmd"] in ("display",), text
    finally:
        context.close()


def test_sample_data_is_on_screen_beside_the_network_and_the_score_is_not_measured(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        look.wait_for_function("window.__onlooker.status === 'ready'", timeout=LONG)
        badge = look.locator("#ol-network .sample-badge")
        assert badge.is_visible()
        assert badge.inner_text().strip() == "Sample data"
        network = look.locator("#ol-network").bounding_box()
        box = badge.bounding_box()
        assert network["x"] <= box["x"] and box["x"] + box["width"] <= network["x"] + network["width"]
        assert network["y"] <= box["y"] and box["y"] + box["height"] <= network["y"] + network["height"]
        counts = look.evaluate("window.__onlooker.network.counts()")
        assert counts["nodes"] == 32 and counts["links"] > 0
        assert look.locator("#ol-score").inner_text().strip().lower().endswith("not measured yet")
        # no digit that could read as a result sits in the score or the network cards
        for sel in ("#ol-score", "#ol-network-note"):
            assert not any(ch.isdigit() for ch in look.locator(sel).inner_text()), sel
        # it stays through every step
        advance_until(page, "words", "waiting")
        wait_state(look, "words", "waiting", timeout=2000)
        assert badge.is_visible()
    finally:
        context.close()


def test_the_participants_label_is_not_shown_and_not_in_any_url(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    urls: list[str] = []
    look.on("request", lambda r: urls.append(r.url))
    try:
        advance_until(page, "calibration", "waiting")  # types P001
        wait_state(look, "calibration", "waiting", timeout=2000)
        look.reload()
        look.wait_for_selector("#ol-root")
        wait_state(look, "calibration", "waiting", timeout=10_000)
        assert "P001" not in look.locator("body").inner_text()
        assert not any("P001" in u for u in urls + [look.url])
    finally:
        context.close()


def test_traces_move_and_keep_a_fixed_size(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "device", "waiting")
        canvas = look.locator("#ol-traces")
        look.wait_for_function("document.getElementById('ol-traces').dataset.rows > 20", timeout=30_000)
        first = int(canvas.get_attribute("data-rows"))
        look.wait_for_function(
            f"Number(document.getElementById('ol-traces').dataset.rows) > {first + 25}", timeout=30_000
        )
        drawn = int(canvas.get_attribute("data-drawn"))
        look.wait_for_function(
            f"Number(document.getElementById('ol-traces').dataset.drawn) > {drawn}", timeout=10_000
        )
        assert look.evaluate(
            """() => { const c = document.getElementById('ol-traces');
              const x = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
              for (let i = 3; i < x.length; i += 4) if (x[i] > 0) return true; return false; }"""
        )
        # a flood of rows (as a 30 minute session would send) cannot grow the buffer
        before = look.evaluate("document.getElementsByTagName('*').length")
        look.evaluate(
            """() => { const row = Array(20).fill(1.5);
              for (let k = 0; k < 400; k++) window.__onlooker.onMessage({t: 'trace', hz: 50,
                x: Array.from({length: 50}, () => row)}); }"""
        )
        assert int(canvas.get_attribute("data-buffered")) <= int(canvas.get_attribute("data-capacity"))
        assert int(canvas.get_attribute("data-rows")) > 20_000
        for k in range(30):
            look.evaluate(
                "(k) => window.__onlooker.onMessage({t: 'state', ...window.__onlooker.lastState, rev: 10000 + k})", k
            )
        assert look.evaluate("document.getElementsByTagName('*').length") == before
    finally:
        context.close()


def test_closing_or_reloading_the_onlooker_leaves_the_session_and_the_participant_alone(
    app, pty_device, page, browser, tmp_path
):
    a = app(port=pty_device.path, flow=long_flow(tmp_path, 8))
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "words", "waiting")
        press(page)
        wait_state(look, "words", "running", timeout=5000)
        folder = session_folder_of(a)
        markers_before = len(markers_of(folder / "eeg"))
        look.reload()
        look.wait_for_selector("#ol-root")
        wait_state(look, "words", "running", timeout=10_000)
        assert screen_of(page) == ["placeholder", "words", "running"]
    finally:
        context.close()
    # the participant page is still its page: no second-window refusal, no pause, the block ends
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=60_000)
    assert page.locator("#conn-lost").is_hidden()
    assert page.locator("#fs-lost").is_hidden()
    state = server_state(a, lambda m: m["phase"] == "between")
    assert state["blocks_done"] == 1 and state["points_total"] == 100
    names = [m.get("label", "") for m in markers_of(folder / "eeg")][markers_before:]
    assert not any("paused" in n for n in names), names
    ledger = [json.loads(x) for x in (folder / "points.jsonl").read_text().splitlines()]
    assert len(ledger) == 1


#: What only the network view needs: its two modules and the sample network data.
NETWORK_FILES = ("/js/scene3d/network-view.js", "/js/scene3d/network-parts.js", "/assets/sample-network.json")
#: Keeps every WebGL context a page makes (the real ones, returned unchanged) in ``window.__webgl``.
WEBGL_WATCH = """(() => {
  window.__webgl = [];
  const original = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
    const ctx = original.call(this, kind, ...rest);
    if (ctx && /webgl/i.test(String(kind)) && !window.__webgl.some((e) => e.ctx === ctx)) {
      window.__webgl.push({ canvas: this, ctx });
    }
    return ctx;
  };
})();"""
WEBGL_STATE = """() => ({
  made: window.__webgl.length,
  live: window.__webgl.filter((e) => !e.ctx.isContextLost()).length,
  attached: window.__webgl.filter((e) => e.canvas.isConnected).length,
})"""


def calibration_flow(tmp_path) -> Path:
    """``flows/short.json`` with sub-steps on its calibration step (3 s eyes open and closed)."""
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": 3.0, "eyes_closed": 3.0}
    path = tmp_path / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path


def no_3d_view(page, where) -> None:
    """No 3-D view of any kind in the page, and no WebGL context alive or in the document."""
    assert page.locator(".scene3d, .sample-badge, canvas, #calib-view, #ol-network").count() == 0, where
    assert "Sample data" not in page.locator("body").inner_text(), where
    gl = page.evaluate(WEBGL_STATE)
    assert gl["live"] == 0 and gl["attached"] == 0, (where, gl)


def test_the_participant_page_never_shows_the_network_view(app, page, tmp_path):
    """The participant page never shows the network view during the task, video, rest and recall
    steps: there is no 3-D view or WebGL canvas in its document, and it never requests the
    network view's modules or the sample network data. The 3-D fit view of the headset is drawn
    only on the calibration step (contact check), and is gone, its WebGL context released and
    its element removed, once the participant has left the contact check."""
    check_on = (FIXTURES / "impedance_on_2s.bin").read_bytes()
    device = PtyDevice(onhead_stream(), config_reply(), contact_stream=check_on, contact_on=True)
    try:
        a = app(port=device.path, flow=calibration_flow(tmp_path))
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        page.add_init_script(WEBGL_WATCH)
        begin(a, page)
        no_3d_view(page, "participant")
        advance_until(page, "calibration", "waiting")
        # the contact check: the fit view is drawn, with one live WebGL context
        page.wait_for_selector(f'{SCREEN}[data-substep="contact"] #calib-view canvas', timeout=30_000)
        gl = page.evaluate(WEBGL_STATE)
        assert gl["live"] == 1 and gl["attached"] == 1, gl
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)  # measured
        press(page)
        # the signal check shows the fit view again (one context); Continue leaves it
        page.wait_for_selector(f'{SCREEN}[data-substep="signal_done"]', timeout=40_000)
        page.wait_for_selector("#advance:not([disabled])", timeout=10_000)
        press(page)
        # the later parts of calibration: the fit view's canvas is gone and its context released
        for sub in ("eyes_open", "done"):
            page.wait_for_selector(f'{SCREEN}[data-substep="{sub}"]', timeout=60_000)
            assert page.locator("canvas").count() == 0, sub
            gl = page.evaluate(WEBGL_STATE)
            assert gl["live"] == 0 and gl["attached"] == 0, (sub, gl)
        steps = ["words", "perception", "nback", "flanker", "spatial", "video", "rest_closed", "rest_open", "recall"]
        for step in steps:
            advance_until(page, step, "waiting")
            for phase in ("waiting", "running"):
                if phase == "running":
                    press(page)
                    page.wait_for_selector(f'{SCREEN}[data-step="{step}"][data-phase="running"]', timeout=10_000)
                no_3d_view(page, (step, phase))
            page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=60_000)
            no_3d_view(page, (step, "between"))
        paths = [urlsplit(u).path for u in requests]
        assert any(p.endswith("/js/scene3d/fit-view.js") for p in paths)  # requests are seen
        assert not [p for p in paths if p.endswith(NETWORK_FILES)], paths
        assert page.evaluate(WEBGL_STATE)["made"] == 1  # only the fit view ever made one
    finally:
        device.hang_up()


def test_rest_steps_show_points_and_progress_on_the_onlooker_but_no_task_content(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "words", "waiting")
        play_block(page)
        advance_until(page, "rest_closed", "waiting")
        press(page)
        wait_state(look, "rest_closed", "running", timeout=5000)
        assert page.locator("#hud").is_hidden()  # the participant sees neither
        assert look.locator("#ol-points").is_visible()
        assert look.locator("#ol-progress").is_visible()
        text = look.locator("body").inner_text().lower()
        for forbidden in ("close your eyes", "fixation", "keep still"):
            assert forbidden not in text
        assert look.locator("#fixation-cross, .instruction, .rest-body").count() == 0
    finally:
        context.close()


def test_the_onlooker_is_not_full_screen_and_asks_for_nothing(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        assert look.locator("#fs-enter, #fs-lost, input, textarea").count() == 0
        assert look.locator("button:not(.labels button)").count() == 0
        assert look.evaluate("document.fullscreenElement === null")
        advance_until(page, "words", "waiting")
        press(page)
        wait_state(look, "words", "running", timeout=5000)
        assert look.evaluate("document.fullscreenElement === null")
        assert look.locator("button:not(.labels button), a[href]").count() == 0
    finally:
        context.close()


def test_without_a_calibration_the_headset_says_the_contact_check_is_not_done(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        look.wait_for_function("window.__onlooker.status === 'ready'", timeout=LONG)
        assert look.locator("#ol-contact").inner_text().strip() == "Contact check not done yet"
        states = look.evaluate(
            "Object.values(window.__onlooker.fit.headset.contacts.map((c) => window.__onlooker.fit.headset.state(c.name)))"
        )
        assert len(states) == 20 and set(states) == {"unknown"}
    finally:
        context.close()


def test_the_page_draws_contact_states_given_to_it_by_a_handwritten_message(
    app, pty_device, page, browser
):
    """The messages are written by hand and given to the page's handler: this shows what the page
    draws, not that the server sends them. The server path is tested after the calibration
    merge."""
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        look.wait_for_function("window.__onlooker.status === 'ready'", timeout=LONG)
        look.evaluate(
            """() => window.__onlooker.onMessage({t: 'contact', good: 2,
              states: {Cz: 'good', Fz: 'good', O1: 'bad', P3: 'marginal'}})"""
        )
        got = look.evaluate(
            "['Cz', 'O1', 'P3', 'F3'].map((n) => window.__onlooker.fit.headset.state(n))"
        )
        assert got == ["good", "bad", "marginal", "unknown"]
        text = look.locator("#ol-contact").inner_text()
        assert "2" in text and "live" in text.lower()
        # once no contact message has come for a while, it is called the last check, with its age
        look.wait_for_function(
            "document.getElementById('ol-contact').textContent.includes('last check')", timeout=15_000
        )
        stale = look.locator("#ol-contact").inner_text()
        assert "Contact states from the last check" in stale and "ago" in stale
        assert "live" not in stale.lower()
        # a state message that carries a calibration field: saved states, shown after the check
        look.evaluate(
            """() => window.__onlooker.onMessage({t: 'state', ...window.__onlooker.lastState,
              rev: 99999, calibration: {states: {Cz: 'marginal', Fz: 'good'}}})"""
        )
        assert look.evaluate("window.__onlooker.fit.headset.state('Cz')") == "marginal"
        assert "calibration" in look.locator("#ol-contact").inner_text().lower()
        # a damaged message does not stop the page
        look.evaluate("() => window.__onlooker.onMessage({t: 'contact', states: {Cz: 'purple', Nope: 'good'}})")
        assert look.evaluate("window.__onlooker.fit.headset.state('Cz')") == "marginal"
    finally:
        context.close()


STOPPED = "The 3-D view stopped. Trying to bring it back."
GAVE_UP = "The 3-D view could not be restarted. Reload this window."


def lose(look, view):
    """Really loses the WebGL context of a view (the browser's own extension)."""
    look.evaluate(
        f"window.__ext_{view} = window.__onlooker.{view}.stage.renderer.getContext()"
        f".getExtension('WEBGL_lose_context'); window.__ext_{view}.loseContext()"
    )


def restore(look, view):
    # the extension is not available from a lost context, so lose() kept it
    look.evaluate(f"window.__ext_{view}.restoreContext()")


def test_a_lost_webgl_context_that_is_restored_brings_both_views_back(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a, query="&restore_ms=60000")
    try:
        look.wait_for_function("window.__onlooker.status === 'ready'", timeout=LONG)
        for view, box in (("network", "#ol-network"), ("fit", "#ol-fit")):
            lose(look, view)
            look.wait_for_selector(f"{box} .scene-message", timeout=10_000)
            assert look.locator(f"{box} .scene-message").inner_text().strip() == STOPPED
            assert "WebGL is off" not in look.locator("body").inner_text()
            restore(look, view)
            look.wait_for_selector(f"{box} .scene-message", state="detached", timeout=10_000)
            frames = look.evaluate(f"window.__onlooker.{view}.stage.frames()")
            look.wait_for_function(
                f"window.__onlooker.{view}.stage.frames() > {frames + 5}", timeout=10_000
            )
        look.evaluate("window.__onlooker.onMessage({t: 'contact', good: 1, states: {Cz: 'good'}})")
        assert look.evaluate("window.__onlooker.fit.headset.state('Cz')") == "good"
    finally:
        context.close()


def test_a_context_that_is_not_restored_is_rebuilt_a_few_times_and_then_given_up(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a, query="&restore_ms=300")
    try:
        look.wait_for_function("window.__onlooker.status === 'ready'", timeout=LONG)
        for _ in range(3):
            look.evaluate("window.__onlooker.mark = window.__onlooker.fit")
            lose(look, "fit")
            # a new scene is built, with a new context, and the message goes away
            look.wait_for_function(
                "window.__onlooker.fit !== window.__onlooker.mark"
                " && !document.querySelector('#ol-fit .scene-message')",
                timeout=20_000,
            )
        # states given while views were rebuilt are drawn on the new scene
        look.evaluate("window.__onlooker.onMessage({t: 'contact', good: 1, states: {Cz: 'good'}})")
        assert look.evaluate("window.__onlooker.fit.headset.state('Cz')") == "good"
        lose(look, "fit")
        look.wait_for_function(
            "(() => { const m = document.querySelector('#ol-fit .scene-message');"
            f" return !!m && m.textContent.trim() === '{GAVE_UP}'; }})()",
            timeout=20_000,
        )
        # nothing calls into the dropped view, the network view and the page are not affected
        look.evaluate("window.__onlooker.onMessage({t: 'contact', good: 2, states: {Cz: 'bad'}})")
        assert "2 of 20" in look.locator("#ol-contact").inner_text()
        assert look.locator("#ol-network .scene-message").count() == 0
        advance_until(page, "calibration", "waiting")
        wait_state(look, "calibration", "waiting", timeout=2000)
    finally:
        context.close()


def test_the_onlooker_follows_a_restarted_server_from_its_new_start(
    app, pty_device, page, browser, tmp_path
):
    port = free_port()
    a = app(port=pty_device.path, flow=SHORT, http_port=port)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    look = context.new_page()
    look.goto(a.url + "/onlooker?test=1")
    look.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=LONG)
    second = None
    try:
        # a session on the first server moves its state revision above zero
        accept(a)
        with open_ws(a, "participant") as ws:
            send(ws, cmd="advance")
            wait_state(look, "device", timeout=10_000)
        assert look.evaluate("window.__onlooker.lastState.rev") > 0
        a.process.send_signal(signal.SIGINT)
        a.process.wait(timeout=20)
        look.wait_for_selector("#ol-lost:not([hidden])", timeout=20_000)
        second = PtyDevice(onhead_stream(), config_reply())
        b = app(port=second.path, flow=SHORT, http_port=port, data_dir=tmp_path / "again")
        assert b.url == a.url
        look.wait_for_selector("#ol-lost", state="hidden", timeout=30_000)
        # the new server is at its first step, no points: the page follows it, not the old rev
        wait_state(look, "participant", "waiting", timeout=10_000)
        assert look.locator("#ol-points").inner_text().strip() == "\u2014"  # nothing scored
        look.wait_for_function("document.getElementById('ol-traces').dataset.rows > 20", timeout=60_000)
        assert look.locator("#ol-link").get_attribute("data-state") == "live"
        accept(b)
        with open_ws(b, "participant") as ws:
            send(ws, cmd="advance")
            wait_state(look, "device", timeout=10_000)
    finally:
        if second is not None:
            second.hang_up()
        context.close()


def test_the_participant_page_follows_a_restarted_server_too(
    app, pty_device, page, tmp_path
):
    port = free_port()
    a = app(port=pty_device.path, flow=SHORT, http_port=port)
    begin(a, page)
    advance_until(page, "calibration", "waiting")
    a.process.send_signal(signal.SIGINT)
    a.process.wait(timeout=20)
    page.wait_for_selector("#conn-lost:not([hidden])", timeout=20_000)
    second = PtyDevice(onhead_stream(), config_reply())
    try:
        app(port=second.path, flow=SHORT, http_port=port, data_dir=tmp_path / "again")
        page.wait_for_selector("#conn-lost", state="hidden", timeout=30_000)
        page.wait_for_selector(f'{SCREEN}[data-step="participant"]', timeout=20_000)
    finally:
        second.hang_up()


def test_the_onlooker_is_not_told_the_name_of_a_replayed_recording(
    app, recorded_session, tmp_path
):
    named = tmp_path / "P777_20260101_000000"
    shutil.copytree(recorded_session, named)
    a = app(replay=named, flow=SHORT)
    with open_ws(a, "participant") as ws:
        mine = _hello(ws)
    assert mine["name"] == "P777_20260101_000000"  # the participant page names the file
    with open_ws(a, "onlooker") as ws:
        texts = [ws.recv(timeout=10) for _ in range(3)]
    assert all("P777" not in t for t in texts), texts
    assert "name" not in next(json.loads(t) for t in texts if json.loads(t)["t"] == "hello")


def test_the_score_card_is_called_network_score(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        assert look.locator("#ol-score-title").inner_text().strip() == "Network score"
        assert "egmentation" not in look.locator("body").inner_text()
    finally:
        context.close()


def _hello(ws) -> dict:
    for _ in range(6):
        message = json.loads(ws.recv(timeout=10))
        if message["t"] == "hello":
            return message
    raise AssertionError("no hello")



def test_the_page_names_the_link_state_and_the_round(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        advance_until(page, "words", "waiting")
        wait_state(look, "words", "waiting", timeout=2000)
        assert look.locator("#ol-step").inner_text().strip() == "Words"
        assert "Block 1 of 2" in look.locator("#ol-block").inner_text()
        assert look.locator("#ol-link").get_attribute("data-state") == "live"
        assert look.locator("#ol-progress-text").inner_text().strip().endswith("blocks done")
    finally:
        context.close()

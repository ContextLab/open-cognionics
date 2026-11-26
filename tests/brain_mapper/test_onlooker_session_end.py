"""The onlooker window at the end of a session, next to the session zip (both merged here).

A real ``brain-mapper`` runs on the simulated headset (a pseudo-terminal device); the participant
page and the onlooker page are real Chromium pages, and a second onlooker is a raw websocket.
What is checked:

- no ``package`` message (the session record: label, folder, zip name, summary) and no zip key
  ever reaches an onlooker, during the session, after it, or on a connection opened after it;
- the onlooker says that the session ended, and how (finished, stopped, ended early), with no
  download control and no participant label;
- a server restarted after a kill zips the session it left behind, and the onlooker window
  reconnects to it and follows it from its new start (its revisions count from 0 again).

To know that an onlooker socket has been sent everything queued after the zip was saved, the test
hangs up the headset once the participant has the ``saved`` message: the ``link`` message that
follows is sent to every client after the ``package`` message, so every frame before it is read.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from test_engine import SHORT, accept, open_ws, send, session_folder_of
from test_onlooker_browser import LONG, wait_state
from test_package import next_package, wait_packaged, zips_in
from test_progress import drive_to
from test_shell_browser import advance_until, begin, free_port
from test_summary_browser import finish_the_route

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

ENDED = "#ol-ended"


def watch_onlooker(browser, a):
    """The onlooker page in its own context, with every websocket frame it is sent kept in order."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    look = context.new_page()
    received: list[str] = []

    def watch(ws):
        ws.on("framereceived", lambda payload: received.append(payload))

    look.on("websocket", watch)
    look.received = received
    look.goto(a.url + "/onlooker?test=1")
    look.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=LONG)
    return context, look


class Collector:
    """Reads every message ``ws`` is sent, on a thread (so its queue never fills), up to and
    including the first ``link`` that is not live."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.seen: list[dict] = []
        self.dropped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                message = json.loads(self.ws.recv())
            except ConnectionClosed:
                return  # the test closed it; what was read is in ``seen``
            self.seen.append(message)
            if message["t"] == "link" and message["state"] != "live":
                self.dropped.set()
                return

    def until_the_link_drops(self, timeout: float = 30.0) -> list[dict]:
        assert self.dropped.wait(timeout), self.seen[-5:]
        return self.seen


def snapshot(a) -> list[dict]:
    """What a new onlooker connection is sent on connecting: everything up to its first state."""
    seen = []
    with open_ws(a, "onlooker") as ws:
        while True:
            message = json.loads(ws.recv(timeout=10))
            seen.append(message)
            if message["t"] == "state":
                return seen


def key_of(saved: dict) -> str:
    link = saved["download"]
    assert link and link.startswith("/api/session/zip?key="), saved
    return link.removeprefix("/api/session/zip?key=")


def nothing_of_the_record(texts: list[str], key: str) -> None:
    """Not one of ``texts`` is a ``package`` message, holds the zip key or names the participant."""
    assert texts
    for text in texts:
        assert json.loads(text)["t"] != "package", text
        assert key not in text and "/api/session/zip" not in text, text
        assert "P001" not in text, text


def no_download_control(look) -> None:
    assert look.locator("a[download], [href*='/api/session/zip'], #summary-download").count() == 0
    body = look.locator("body").inner_text()
    assert "P001" not in body and ".zip" not in body and "ownload" not in body, body


# ---------------------------------------------------------------------------------------------


def test_a_finished_session_ends_on_the_onlooker_and_its_record_never_reaches_one(
    app, pty_device, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = watch_onlooker(browser, a)
    try:
        with open_ws(a, "onlooker") as ws:
            during = Collector(ws)
            # the participant page plays the whole short flow and presses Finish on the summary
            finish_the_route(page)
            folder = session_folder_of(a)
            path = wait_packaged(folder)
            page.wait_for_selector("#summary-download", state="visible", timeout=30_000)
            href = page.get_attribute("#summary-download", "href")
            key = href.removeprefix("/api/session/zip?key=")
            assert key and key != href
            # the onlooker page says the session is over, and how, and offers nothing to download
            look.wait_for_selector(f"{ENDED}:not([hidden])", timeout=10_000)
            assert look.locator("#ol-root").get_attribute("data-ended") == "finished"
            assert look.inner_text(ENDED).strip() == "The session is over: the participant finished it."
            no_download_control(look)
            # an onlooker that connects after the zip was saved is told nothing of it either
            late = snapshot(a)
            nothing_of_the_record([json.dumps(m) for m in late], key)
            pty_device.hang_up()
            seen = during.until_the_link_drops()
        nothing_of_the_record([json.dumps(m) for m in seen], key)
        look.wait_for_selector("#ol-link[data-state]:not([data-state='live'])", timeout=10_000)
        nothing_of_the_record(look.received, key)
        assert path.name not in "".join(look.received)
        # the onlooker page kept its socket the whole time: what it was sent ends with that link
        assert any(json.loads(t)["t"] == "link" and json.loads(t)["state"] != "live" for t in look.received)
    finally:
        context.close()


def test_a_stopped_session_ends_on_the_onlooker_with_no_download_and_no_record(
    app, pty_device, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    context, look = watch_onlooker(browser, a)
    try:
        with open_ws(a) as participant, open_ws(a, "onlooker") as ws:
            during = Collector(ws)
            drive_to(participant, "words")
            wait_state(look, "words", "waiting", timeout=10_000)
            send(participant, cmd="stop")
            saved = next_package(participant, "saved")
            key = key_of(saved)
            look.wait_for_selector(f"{ENDED}:not([hidden])", timeout=10_000)
            assert look.locator("#ol-root").get_attribute("data-ended") == "stopped"
            assert look.inner_text(ENDED).strip() == "The participant stopped the session."
            no_download_control(look)
            pty_device.hang_up()
            seen = during.until_the_link_drops()
        nothing_of_the_record([json.dumps(m) for m in seen], key)
        nothing_of_the_record([json.dumps(m) for m in snapshot(a)], key)
        look.wait_for_selector("#ol-link[data-state]:not([data-state='live'])", timeout=10_000)
        nothing_of_the_record(look.received, key)
    finally:
        context.close()


def test_a_session_that_ends_early_says_so_on_the_onlooker(app, recorded_session, browser):
    # A replay that reaches its end ends the session early (since 2026-11-25 a lost headset link
    # pauses instead; until then this test hung up the simulated headset).
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)
    context, look = watch_onlooker(browser, a)
    try:
        with open_ws(a) as participant:
            drive_to(participant, "words")
            wait_state(look, "words", "waiting", timeout=10_000)
            assert look.locator(ENDED).is_hidden()
            assert look.locator("#ol-root").get_attribute("data-ended") == ""
            saved = next_package(participant, "saved")  # the replay ends: the session ends early
            key = key_of(saved)
            look.wait_for_selector(f"{ENDED}:not([hidden])", timeout=20_000)
            assert look.locator("#ol-root").get_attribute("data-ended") == "early"
            assert look.inner_text(ENDED).strip() == "The session ended before its last step."
            no_download_control(look)
        nothing_of_the_record([json.dumps(m) for m in snapshot(a)], key)
        nothing_of_the_record(look.received, key)
    finally:
        context.close()


def test_a_restarted_server_zips_the_left_session_and_the_onlooker_follows_its_new_start(
    app, pty_device, page, browser, tmp_path
):
    port = free_port()
    a = app(port=pty_device.path, flow=SHORT, http_port=port)
    begin(a, page)
    context, look = watch_onlooker(browser, a)
    second = None
    try:
        advance_until(page, "words", "waiting")  # types P001; the session folder exists from here
        wait_state(look, "words", "waiting", timeout=10_000)
        old_rev = look.evaluate("window.__onlooker.lastState.rev")
        assert old_rev > 0
        folder = session_folder_of(a)
        sessions = a.data_dir / "sessions"
        a.process.send_signal(signal.SIGKILL)  # no zip is made: the next start has to
        a.process.wait(timeout=20)
        assert zips_in(sessions) == []
        look.wait_for_selector("#ol-lost:not([hidden])", timeout=20_000)
        before = len(look.received)

        second = PtyDevice(onhead_stream(), config_reply())
        b = app(port=second.path, flow=SHORT, http_port=port, data_dir=a.data_dir)
        assert b.url == a.url
        # recovery ran: one line naming the folder, and the zip next to it
        told = [line for line in b.printed if folder.name in line]
        assert len(told) == 1 and "saved now" in told[0], b.printed
        assert zips_in(sessions) == [f"{folder.name}.zip"]
        # the onlooker reconnects and follows the new server from its first step, not the old rev
        look.wait_for_selector("#ol-lost", state="hidden", timeout=30_000)
        wait_state(look, "participant", "waiting", timeout=10_000)
        assert look.evaluate("window.__onlooker.lastState.rev") < old_rev
        assert look.locator("#ol-points").inner_text().strip() == "\u2014"  # nothing scored
        assert look.locator(ENDED).is_hidden()
        look.wait_for_function("document.getElementById('ol-link').dataset.state === 'live'", timeout=30_000)
        after = look.received[before:]
        assert after and not any(json.loads(t)["t"] == "package" for t in after)
        assert not any("P001" in t or folder.name in t for t in after)
        # and it goes on following the new session, which the participant page (reconnected to
        # the new server too) starts
        advance_until(page, "device", "waiting")
        wait_state(look, "device", "waiting", timeout=10_000)
        assert len(zips_in(sessions)) == 1 and len([p for p in sessions.iterdir() if p.is_dir()]) == 2
    finally:
        if second is not None:
            second.hang_up()
        context.close()

"""The shell page in a real Chromium, against the real server.

Every test starts the ``brain-mapper`` command on a pseudo-terminal device (or
on a real recording), opens its page with Playwright and clicks through it. The
asserts read the DOM (``#screen`` carries ``data-screen``, ``data-step`` and
``data-phase``), the websocket messages of a second client, and the files the
session wrote. Where a negative has to be shown (something does not change)
the test reads the server's own state messages or the log of DOM changes the
page itself recorded, not a fixed wait.

Headless Chromium honours ``requestFullscreen`` from a real click and sets
``document.fullscreenElement`` (checked for Task 6, step 1), so the tests run
headless.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from brain_mapper import flow
from brain_mapper.server import Settings, create_app
from test_engine import SHORT, events_of, markers_of, next_state, open_ws, session_folder_of

SCREEN = "#screen"
#: Seconds of a block in the flows the tests need to be long enough to act in.
LONG_BLOCK = 20

#: Records every change of ``data-screen`` / ``data-step`` as one (screen, step) pair.
RECORD_SCREENS = """
window.__screens = [];
new MutationObserver(() => {
  const el = document.getElementById('screen');
  if (!el) return;
  const pair = [el.dataset.screen || '', el.dataset.step || ''];
  const last = window.__screens[window.__screens.length - 1];
  if (!last || last[0] !== pair[0] || last[1] !== pair[1]) window.__screens.push(pair);
}).observe(document, {
  subtree: true, attributes: true, attributeFilter: ['data-screen', 'data-step'],
});
"""

#: Records every text the points total has shown, in order.
#: Records, at every change of the points total, the total and the "N points" sentence of the
#: between-blocks screen (when it is there), so that the two can be compared.
RECORD_PAIRS = """
window.__pairs = [];
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('hud-points');
  new MutationObserver(() => {
    const sentence = document.getElementById('between-total');
    if (sentence) window.__pairs.push([el.textContent.trim(), sentence.textContent.trim()]);
  }).observe(el, {childList: true, characterData: true, subtree: true});
});
"""

RECORD_HUD = """
window.__hud = [];
document.addEventListener('DOMContentLoaded', () => {
  const el = document.getElementById('hud-points');
  const note = () => {
    const text = el.textContent.trim();
    if (window.__hud[window.__hud.length - 1] !== text) window.__hud.push(text);
  };
  note();
  new MutationObserver(note).observe(el, {childList: true, characterData: true, subtree: true});
});
"""


def long_flow(tmp_path: Path, seconds: float = LONG_BLOCK) -> Path:
    """The short flow with blocks of ``seconds``: time to act inside a block."""
    raw = json.loads(SHORT.read_text())
    for step in raw:
        for block in step["blocks"]:
            block["seconds"] = seconds
    path = tmp_path / "long.json"
    path.write_text(json.dumps(raw))
    return path


def begin(a, page) -> None:
    """Opens the page, enters full screen from the button and waits for the first step."""
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_function("document.fullscreenElement !== null")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')


def press(page, timeout: float | None = None) -> None:
    """Clicks ``#advance``. On the participant screen it first types the label ``P001``:
    nothing goes on from there until the server has accepted one."""
    field = page.locator("#participant-label")
    if field.count() and not field.input_value():
        field.fill("P001")
    if timeout is None:
        page.click("#advance")
    else:
        page.click("#advance", timeout=timeout)


def advance_until(page, step: str, phase: str, timeout: float = 90.0) -> None:
    """Clicks ``#advance`` until the page shows ``step`` in ``phase`` (not for ``between``)."""
    assert phase != "between"
    target = f'{SCREEN}[data-step="{step}"][data-phase="{phase}"]'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.locator(target).count():
            return
        # The button found is the one clicked: a click that re-looked for ``#advance`` after the
        # screen had moved on (the state arrived between the look and the click) pressed the next
        # screen's Continue or Start, which left a step at once (seen under load: device left
        # 97 ms after it began, so an onlooker never showed it).
        button = page.query_selector("#advance:not([disabled])")
        if button is not None:
            try:
                field = page.query_selector("#participant-label")
                on_participant = field is not None
                if on_participant and not field.input_value():
                    field.fill("P001")
                button.click(timeout=2000)
                if on_participant:
                    # Continue there sends the label first; a second press before the screen has
                    # moved on would land on the next screen's Continue.
                    page.wait_for_function(
                        "document.getElementById('screen').dataset.screen !== 'participant'",
                        timeout=10_000,
                    )
            except PlaywrightError:
                pass  # the screen changed under the click (the button is gone); look again
        else:
            page.wait_for_timeout(50)
    raise AssertionError(f"never reached {step} / {phase}; at {screen_of(page)}")


def screen_of(page) -> tuple[str, str, str]:
    return page.evaluate(
        "(() => { const e = document.getElementById('screen');"
        " return [e.dataset.screen, e.dataset.step || '', e.dataset.phase || '']; })()"
    )


def server_state(a, predicate=lambda m: True, timeout=10.0) -> dict:
    """The first state message a read-only websocket client is sent that fits ``predicate``."""
    with open_ws(a, "onlooker") as ws:
        return next_state(ws, timeout=timeout, predicate=predicate)


def remaining_of(page) -> float:
    return float(page.get_attribute("#time-left", "data-remaining"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: What the HUD shows before any point was possible.
DASH = "\u2014"


def score_text(state: dict) -> str:
    """The session total of a state's ``scores`` as the HUD writes it: "15 of 64", or the dash."""
    total = state["scores"]["total"]
    return f"{total['earned']} of {total['possible']}" if total["possible"] else DASH


def flow_with_points(tmp_path: Path, step: str, points: int, seconds: float = 2) -> Path:
    """The short flow with ``points`` on every block of ``step``."""
    raw = json.loads(SHORT.read_text())
    for item in raw:
        for block in item["blocks"]:
            block["seconds"] = seconds
            if item["id"] == step:
                block["points"] = points
    path = tmp_path / "points.json"
    path.write_text(json.dumps(raw))
    return path


class InProcess:
    """The real server in this process, so a test can cut the participant's connection.

    ``drop()`` closes the participant's websocket and makes the server refuse new
    connections (the Host check reads ``settings.http_port``), so the page sees a real
    closed socket and real refused reconnects until ``restore()``.
    """

    def __init__(self, pty_device, tmp_path: Path, flow_path: Path) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.settings = Settings(tmp_path / "data", pty_device.path, None, flow_path, self.port)
        self.app = create_app(self.settings)
        self.hub = self.app.state.hub
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        self.hub.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.hub.stop()

    def drop(self) -> None:
        self.settings.http_port = 0
        client = self.hub._participant
        assert client is not None
        client.loop.call_soon_threadsafe(client.close)

    def restore(self) -> None:
        self.settings.http_port = self.port

    def state(self) -> dict:
        return self.hub.session.state()

    def wait_state(self, predicate, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if predicate(state):
                return state
            time.sleep(0.05)
        raise AssertionError(f"state never fitted; last {self.state()}")


@pytest.fixture
def inprocess(pty_device, tmp_path):
    started: list[InProcess] = []

    def make(flow_path: Path) -> InProcess:
        server = InProcess(pty_device, tmp_path, flow_path)
        started.append(server)
        server.start()
        return server

    yield make
    for server in started:
        server.stop()


# -- the walk --------------------------------------------------------------------


def test_the_session_walks_through_every_round(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.add_init_script(RECORD_SCREENS)
    begin(a, page)
    while True:
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
        last = screen_of(page)[0] == "summary"
        press(page)
        if last:
            break
    page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")
    seen = [tuple(p) for p in page.evaluate("window.__screens") if p[0] != "connecting"]
    expected = [
        ("start", "participant"),
        ("participant", "participant"),
        ("device", "device"),
        ("calibration", "calibration"),
    ]
    for step in flow.load(SHORT):
        if step.kind in ("task", "video", "recall"):
            expected += [("placeholder", step.id), ("between", step.id)] * len(step.blocks)
        elif step.kind == "rest":
            expected += [("rest", step.id)]
    expected += [("summary", "summary")]
    assert seen == expected


def test_the_start_screen_says_what_happens(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    assert page.locator("#fs-enter").is_visible()
    text = page.inner_text(SCREEN)
    assert "Esc" in text  # how to leave full screen
    assert page.locator("#open-onlooker").is_visible()
    for title in ("Words", "Spatial memory", "Rest, eyes closed", "Delayed free recall"):
        assert title in text
    assert not page.locator("#hud").is_visible()
    with page.context.expect_page() as opened:
        page.click("#open-onlooker")
    assert opened.value.url.endswith("/onlooker")


def test_the_flow_is_served_to_the_page(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    deadline = time.monotonic() + 15
    while True:
        try:
            steps = json.load(urllib.request.urlopen(a.url + "/api/flow"))
            break
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert [s["id"] for s in steps] == [s.id for s in flow.load(SHORT)]
    assert "spatial" in [s["id"] for s in steps]
    spatial = next(s for s in steps if s["id"] == "spatial")
    assert spatial["title"] == "Spatial memory" and len(spatial["blocks"]) == 2  # was 3 before 2026-11-25


# -- points ----------------------------------------------------------------------


def test_the_total_changes_only_between_blocks(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    # The HUD shows the session's score (earned of possible), the server's total of the state.
    total = DASH
    for _ in range(2):  # both blocks of the first round
        assert page.inner_text("#hud-points") == total
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
        samples = []
        while screen_of(page)[2] == "running":
            samples.append(page.inner_text("#hud-points"))
            page.wait_for_timeout(200)
        # The block is over; every reading during it was the old total.
        assert len(samples) >= 3
        assert set(samples) == {total}
        page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
        total = score_text(server_state(a, lambda m: m["phase"] == "between"))
        page.wait_for_function(
            "(v) => document.getElementById('hud-points').textContent.trim() === v", arg=total
        )


def test_after_continue_the_hud_shows_the_new_total_for_the_whole_block(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.add_init_script(RECORD_HUD)
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    total = score_text(server_state(a, lambda m: m["phase"] == "between"))
    page.click("#advance")  # at once
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"][data-phase="running"]')
    samples = []
    while screen_of(page)[2] == "running":
        samples.append(page.inner_text("#hud-points"))
        page.wait_for_timeout(100)
    assert len(samples) >= 5
    assert set(samples) == {total}, samples  # the new total, whole, for the entire block


@pytest.mark.parametrize("width", [900, 1024])
def test_the_header_holds_the_longest_round_label_on_one_line(app, pty_device, browser, width):
    # The longest labels the page writes for the default flow: "Round n of N · <title>" and, on
    # the between-blocks screen, with ", block b of B done" after it.
    steps = flow.default_flow()
    rounds = [s for s in steps if s.blocks]
    plain = [f"Round {i + 1} of {len(rounds)} \u00b7 {s.title}" for i, s in enumerate(rounds)]
    tails = [
        f"{label}, block {len(s.blocks)} of {len(s.blocks)} done" for label, s in zip(plain, rounds)
    ]
    context = browser.new_context(viewport={"width": width, "height": 800})
    page = context.new_page()
    try:
        a = app(port=pty_device.path)
        page.goto(a.url)
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
        for label in plain + tails:
            box = page.evaluate(
                """(text) => {
                  document.getElementById('hud').hidden = false;
                  const l = document.getElementById('hud-label');
                  l.textContent = text;
                  const r = l.getBoundingClientRect();
                  const mid = document.querySelector('.hud-mid').getBoundingClientRect();
                  const right = document.querySelector('.hud-right').getBoundingClientRect();
                  const line = parseFloat(getComputedStyle(l).lineHeight)
                    || parseFloat(getComputedStyle(l).fontSize) * 1.5;
                  return {h: r.height, line, right: r.right, scroll: l.scrollWidth - l.clientWidth,
                          mid: mid.left, midWidth: mid.width, end: right.right,
                          page: document.documentElement.scrollWidth - innerWidth};
                }""",
                label,
            )
            assert box["h"] <= box["line"] * 1.2, (label, box)  # one line
            assert box["scroll"] <= 0, (label, box)  # nothing cut off
            assert box["right"] <= box["mid"], (label, box)  # clear of the progress bar
            assert box["midWidth"] >= 120, (label, box)  # which keeps a usable width
            assert box["end"] <= width and box["page"] <= 0, (label, box)
    finally:
        context.close()


def test_no_points_on_setup_screens(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    page.add_init_script(RECORD_HUD)
    begin(a, page)
    names = {"participant": "Participant", "device": "Headset", "calibration": "Calibration"}
    for i, name in enumerate(("participant", "device", "calibration"), start=1):
        page.wait_for_selector(f'{SCREEN}[data-screen="{name}"]')
        assert page.locator("#hud").is_visible()
        # No score on setup (since 2026-11-26): the score and its caption are not
        # shown, and the label says which setup step this is.
        assert not page.locator("#hud-points").is_visible()
        assert not page.locator(".hud-caption").is_visible()
        assert page.inner_text("#hud-label") == f"Setup {i} of 3: {names[name]}"
        assert page.locator("#hud-progress").is_visible()
        press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"]')
    assert page.locator("#hud-points").is_visible()
    assert page.evaluate("window.__hud") == [DASH]


@pytest.mark.parametrize("reduced", [True, False])
def test_the_hud_goes_straight_to_the_new_total_with_or_without_reduced_motion(app, pty_device, browser, reduced):
    a = app(port=pty_device.path, flow=SHORT)
    context = browser.new_context(reduced_motion="reduce" if reduced else "no-preference")
    try:
        page = context.new_page()
        page.add_init_script(RECORD_HUD)
        page.add_init_script(RECORD_PAIRS)
        begin(a, page)
        advance_until(page, "words", "waiting")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
        total = score_text(server_state(a, lambda m: m["phase"] == "between"))
        page.wait_for_function(
            "(v) => document.getElementById('hud-points').textContent.trim() === v", arg=total
        )
        seen = page.evaluate("window.__hud")
        pairs = page.evaluate("window.__pairs")
        sentence = page.inner_text("#between-total")
    finally:
        context.close()
    words = "nothing scored yet" if total == DASH else f"{total} points"
    assert sentence == words
    assert all(sentence_ == ("nothing scored yet" if hud == DASH else f"{hud} points") for hud, sentence_ in pairs), pairs
    # No count-up: the HUD shows the old total, then the new one, nothing between.
    assert seen == ([DASH] if total == DASH else [DASH, total])


# -- full screen -----------------------------------------------------------------


def test_leaving_full_screen_pauses_a_block_and_returning_resumes_it(app, pty_device, page, tmp_path):
    """Leaving full screen pauses; since 2026-11-25 the pause ends the task
    block that ran (the session resumes on the next block), and returning goes to the between-blocks screen."""
    a = app(port=pty_device.path, flow=long_flow(tmp_path, 6))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    assert not page.locator("#fs-lost").is_visible()
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    text = page.inner_text("#fs-lost")
    assert "Full screen was closed" in text
    assert page.locator("#fs-return").is_visible()
    assert "This block has ended and is not repeated" in text  # a task block; rest says otherwise
    # The server's own states say which block the pause ended, and there is no time left to show.
    with open_ws(a, "onlooker") as ws:
        first = next_state(ws, predicate=lambda m: m["phase"] == "paused")
    assert first["reason"] == "fullscreen_lost"
    assert first["interrupted"] == "list1" and first["remaining"] is None
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    assert page.evaluate("document.fullscreenElement !== null")
    # The block ended at the pause, and the files say so.
    folder = session_folder_of(a)
    events = events_of(folder)
    by_name = {e["name"]: e for e in events if e["step"] == "words" and e["block"] == "list1"}
    assert by_name["block_end"]["payload"]["ended_by"] == "interrupted"
    ran = by_name["block_end"]["payload"]["seconds"]
    assert ran == pytest.approx(by_name["paused"]["unix_time"] - by_name["block_start"]["unix_time"], abs=0.3)
    assert ran < 6.0
    labels = [m["label"] for m in markers_of(folder / "eeg")]
    assert any(x.startswith("words/paused#") for x in labels)
    assert any(x.startswith("words/resumed#") for x in labels)
    assert by_name["paused"]["payload"]["reason"] == "fullscreen_lost"
    assert by_name["resumed"]["payload"] == {"to": "between"}


def test_leaving_full_screen_on_a_setup_screen_does_not_pause(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "device", "waiting")
    page.evaluate(
        "new Promise(r => { document.addEventListener('fullscreenchange', r, {once: true});"
        " document.exitFullscreen(); })"
    )
    assert page.evaluate("document.fullscreenElement === null")
    page.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert not page.locator("#fs-lost").is_visible()
    assert screen_of(page)[0] == "device"
    page.click("#advance")  # the setup carries on
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
    assert [e["name"] for e in events_of(session_folder_of(a)) if e["name"] == "paused"] == []


def test_leaving_full_screen_between_blocks_asks_for_it_before_going_on(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    assert server_state(a)["phase"] == "between"  # nothing to pause between blocks
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    assert page.evaluate("document.fullscreenElement !== null")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')


# -- rest ------------------------------------------------------------------------


def test_rest_shows_only_the_instruction_or_the_cross(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    seen = {}
    for step in ("rest_closed", "rest_open"):
        advance_until(page, step, "waiting")
        assert not page.locator("#hud").is_visible()
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-step="{step}"][data-phase="running"]')
        seen[step] = page.evaluate(
            """() => ({
              text: document.getElementById('screen').innerText.trim(),
              points: !!document.getElementById('hud-points').offsetParent,
              progress: !!document.getElementById('hud-progress').offsetParent,
              // A timer, a progress bar or a clock reading anywhere on the screen counts.
              timers: [...document.querySelectorAll('#screen *, #hud *')].filter((e) =>
                e.offsetParent && (e.id === 'time-left' || e.hasAttribute('data-remaining')
                  || e.getAttribute('role') === 'progressbar' || e.tagName === 'TIME')).length,
              clock: /[0-9]+:[0-9][0-9]/.test(document.getElementById('screen').innerText),
              cross: (document.getElementById('fixation-cross')
                      ?.getBoundingClientRect().width ?? 0) > 0,  // SVG has no offsetParent
            })"""
        )
        page.wait_for_selector(f'{SCREEN}[data-step="{step}"][data-phase="between"]')
        assert not page.locator("#hud-points").is_visible()  # hidden after the block too
        page.click("#advance")
    closed, opened = seen["rest_closed"], seen["rest_open"]
    # While the period runs, eyes closed is a dark screen with nothing on it (the instruction is
    # on the start screen before it since 2026-11-25; it used to stay: "Close your eyes and keep still.").
    assert closed["text"] == ""
    assert closed["cross"] is False
    assert opened["text"] == "" and opened["cross"] is True
    for state in seen.values():
        assert state["points"] is False and state["progress"] is False
        assert state["timers"] == 0 and state["clock"] is False
    # Back on the next screen the total is shown again and includes the rest blocks.
    page.wait_for_selector(f'{SCREEN}[data-step="recall"]')
    assert page.locator("#hud-points").is_visible()
    # The session's score as the server has it after 13 blocks (words 2, perception 2, nback 2,
    # flanker 2, spatial 2, video 1, rest 2), with the rest blocks in it (they add nothing).
    total = score_text(server_state(a, lambda m: m["step"] == "recall"))
    page.wait_for_function(
        "(v) => document.getElementById('hud-points').textContent.trim() === v", arg=total
    )


# -- reload, second window, ending --------------------------------------------------


def test_a_reload_returns_to_the_current_step(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=long_flow(tmp_path))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="words"][data-phase="running"]')
    page.reload()
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"][data-step="words"]')
    page.wait_for_selector("#fs-lost", state="visible")  # a reload leaves full screen
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    assert "reloaded" in page.inner_text("#fs-lost")
    assert not page.evaluate("document.fullscreenElement !== null")
    # The reload's pause ended the block: no time left is shown,
    # and Resume goes on from the between-blocks screen.
    with open_ws(a, "onlooker") as ws:
        first = next_state(ws, predicate=lambda m: m["phase"] == "paused")
    assert first["step"] == "words" and first["block"] == "list1"
    assert first["interrupted"] == "list1" and first["remaining"] is None
    assert "This block has ended and is not repeated" in page.inner_text("#fs-lost")
    assert first["reason"] == "page_reloaded"
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]')
    assert page.evaluate("document.fullscreenElement !== null")


def test_a_reload_on_a_setup_step_shows_that_step(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "device", "waiting")
    page.reload()
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert not page.locator("#fs-lost").is_visible()


def test_a_second_participant_window_is_refused(app, pty_device, page, browser):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    other = browser.new_context()
    try:
        second = other.new_page()
        sockets = []
        second.on("websocket", lambda ws: sockets.append(ws))
        second.goto(a.url)
        second.wait_for_selector(f'{SCREEN}[data-screen="refused"]')
        link = second.locator("#onlooker-link")
        assert link.get_attribute("href") == "/onlooker"
        assert "another window" in second.inner_text(SCREEN)
        count = len(sockets)
        # It does not try again: the retry delay of the page is a second, and none came.
        second.wait_for_timeout(2500)
        assert len(sockets) == count == 1
    finally:
        other.close()
    # The first window is untouched and still drives the session.
    assert screen_of(page)[0] == "participant"
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')


def test_a_session_that_loses_its_source_says_why(app, recorded_session, page):
    a = app(replay=recorded_session, flow=SHORT)
    begin(a, page)
    # a session is under way (a label was accepted well inside the 15 s of the recording);
    # only then does the recording run out
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=10_000)
    assert session_folder_of(a).name.startswith("P001_")
    deadline = time.monotonic() + 60
    while screen_of(page)[0] != "ended":
        assert time.monotonic() < deadline, screen_of(page)
        button = page.locator("#advance:not([disabled])")
        if button.count():
            try:
                press(page, timeout=1000)
            except PlaywrightTimeout:
                pass
        page.wait_for_timeout(100)
    text = page.inner_text(SCREEN)
    assert "The session has ended" in text
    assert "end of recording" in page.inner_text("#ended-reason")
    assert page.locator("#advance").count() == 0  # nothing to press that would look like going on
    assert not page.locator("#fs-lost").is_visible()
    names = [e["name"] for e in events_of(session_folder_of(a))]
    assert "source_ended" in names


def test_a_session_ended_by_a_failure_shows_the_failure(inprocess, page):
    """A real session whose event log cannot be written ends once and the page says so."""
    a = inprocess(SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    a.hub.session._events.close()  # the next write fails, as a full disk would
    page.wait_for_selector(f'{SCREEN}[data-screen="ended"]', timeout=15_000)
    assert "The session has ended" in page.inner_text(SCREEN)
    assert "event log failed" in page.inner_text("#ended-reason")
    assert page.locator("#advance").count() == 0


# -- what the page loads -------------------------------------------------------------


def test_the_page_makes_no_request_to_another_host_and_loads_its_fonts(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    host, port = a.host_port
    urls = []
    page.on("request", lambda r: urls.append(r.url))
    begin(a, page)
    while True:  # the whole short flow, to the summary
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
        last = screen_of(page)[0] == "summary"
        press(page)
        if last:
            break
    page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")
    assert urls
    for url in urls:
        assert url.startswith((f"http://{host}:{port}/", f"ws://{host}:{port}/", "data:", "blob:")), url
    assert any(u.endswith(".woff2") for u in urls)
    loaded = page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
    assert {"Inter", "Bricolage Grotesque"} <= {x.strip("\"'") for x in loaded}


def test_the_page_never_loads_a_font_or_script_from_a_cdn():
    web = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "web"
    for path in list(web.rglob("*.html")) + list(web.rglob("*.css")) + list(web.rglob("*.js")):
        if "vendor" in path.parts:
            continue
        text = path.read_text()
        if path.relative_to(web).as_posix() == "js/screens/video.js":
            # The one exception: the video screen's player (see "Video" in docs/brain-mapper.md).
            text = text.replace('"https://player.vimeo.com"', "")
        for forbidden in ("googleapis", "gstatic", "cdn.", "https://", "http://"):
            assert forbidden not in text.replace("http://www.w3.org", ""), (path, forbidden)
    for font in (web / "fonts").glob("*.woff2"):
        assert font.read_bytes()[:4] == b"wOF2", font
    assert (web / "fonts" / "OFL-Inter.txt").is_file()
    assert (web / "fonts" / "OFL-Bricolage.txt").is_file()


# -- fix round 1: lost connection, totals from the server, duplicates, layout, package ------


def test_a_pause_that_could_not_be_sent_is_sent_when_the_socket_is_back(inprocess, page, tmp_path):
    a = inprocess(long_flow(tmp_path, 6))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    a.drop()
    page.wait_for_selector("#conn-lost", state="visible")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    assert a.state()["phase"] == "running"  # the pause could not reach the server
    a.restore()
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=15_000)
    paused = a.state()
    assert (paused["phase"], paused["reason"]) == ("paused", "fullscreen_lost")
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    events = {
        e["name"]: e
        for e in events_of(a.hub.folder)
        if e["step"] == "words" and e["block"] == "list1"
    }
    # The late pause ended the block (since 2026-11-25; before, the block ran on to its full 6 s:
    # "assert ran == pytest.approx(6.0, abs=0.3)"): it ended at the pause, before its time was up.
    end = events["block_end"]
    assert end["payload"]["ended_by"] == "interrupted" and 0 < end["payload"]["seconds"] < 6.0
    assert events["paused"]["unix_time"] <= end["unix_time"] <= events["resumed"]["unix_time"]


@pytest.mark.parametrize("points", [100, 250])
def test_a_block_end_missed_while_the_socket_was_down_shows_the_right_total_and_gain(
    inprocess, page, tmp_path, points
):
    a = inprocess(flow_with_points(tmp_path, "words", points))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    a.drop()
    page.wait_for_selector("#conn-lost", state="visible")
    state = a.wait_state(lambda s: s["phase"] == "between")  # the block ended and its points message went by
    assert page.inner_text("#hud-points") == DASH
    a.restore()
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    # The page shows the scores of the server's state (the ledger's points are not shown): the
    # round's points so far and the session total, here nothing scored (the 2-second list cuts
    # its trial short).
    total = score_text(state)
    page.wait_for_function(
        "(v) => document.getElementById('hud-points').textContent.trim() === v", arg=total
    )
    assert total == DASH
    assert page.inner_text("#between-gained") == DASH
    assert page.inner_text("#between-total") == "nothing scored yet"


def test_a_reload_during_the_between_screen_shows_the_total_and_the_gain(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=flow_with_points(tmp_path, "words", 250))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    page.reload()
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    # The round's points so far and the session total, from the state: nothing scored here.
    assert page.inner_text("#between-gained") == DASH
    page.wait_for_function("(v) => document.getElementById('hud-points').textContent.trim() === v", arg=DASH)
    assert page.inner_text("#between-total") == "nothing scored yet"
    # And the next between screen is not "Done" or a leftover: it shows its own award.
    page.click("#fs-return")  # full screen is off after a reload
    page.wait_for_selector("#fs-lost", state="hidden")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    page.wait_for_function("(v) => document.getElementById('hud-points').textContent.trim() === v", arg=DASH)
    assert page.inner_text("#between-gained") == DASH
    assert page.inner_text("#between-lead") == "Words, points so far"


def test_the_pause_overlay_gives_a_plain_sentence_for_any_reason(inprocess, page, tmp_path):
    a = inprocess(long_flow(tmp_path, 8))
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    a.hub.session.pause("")  # no reason given
    page.wait_for_selector("#fs-lost", state="visible")
    text = page.inner_text("#fs-lost")
    assert "null" not in text and "undefined" not in text and "Paused:" not in text
    assert "The block is paused." in text
    a.hub.session.resume()  # the pause ended the block: on to the between screen and the next block
    page.wait_for_selector("#fs-lost", state="hidden")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]')
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    a.hub.session.pause("operator request")
    page.wait_for_selector("#fs-lost", state="visible")
    assert "operator request" in page.inner_text("#fs-lost")
    assert "This block has ended and is not repeated" in page.inner_text("#fs-lost")


def test_the_pause_overlay_during_rest_shows_no_time_left(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "rest_closed", "waiting")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_closed"][data-phase="running"]')
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    text = page.inner_text("#fs-lost")
    assert "Full screen was closed." in text
    assert "Time left" not in text and not any(c.isdigit() for c in text)
    assert not page.locator("#time-left").count()


def test_a_duplicated_tab_is_refused_and_a_reload_is_not(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page_id = page.evaluate("sessionStorage.getItem('brain-mapper-page')")
    assert page_id
    # A duplicated tab starts with a copy of the first tab's sessionStorage.
    second = page.context.new_page()
    second.add_init_script(f"sessionStorage.setItem('brain-mapper-page', '{page_id}')")
    second.goto(a.url)
    second.wait_for_selector(f'{SCREEN}[data-screen="refused"]', timeout=10_000)
    assert second.locator("#onlooker-link").get_attribute("href") == "/onlooker"
    # The first tab still has the session, and a reload of it keeps its place.
    assert screen_of(page)[0] == "participant"
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    page.reload()
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert not page.locator(f'{SCREEN}[data-screen="refused"]').count()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')


def test_the_flow_endpoint_refuses_a_foreign_host(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    _, port = a.host_port
    deadline = time.monotonic() + 15
    while True:
        try:
            assert urllib.request.urlopen(a.url + "/api/flow").status == 200
            break
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(0.1)
    request = urllib.request.Request(a.url + "/api/flow", headers={"Host": f"evil.example:{port}"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)
    assert refused.value.code == 403


@pytest.mark.parametrize("size", [(900, 900), (1024, 768), (1440, 900)])
def test_the_start_screen_fits_the_window_without_scrolling(app, pty_device, browser, size):
    a = app(port=pty_device.path, flow=SHORT)
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    try:
        page = context.new_page()
        page.goto(a.url)
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
        page.wait_for_function("document.fonts.status === 'loaded'")
        height, inner = page.evaluate(
            "[document.documentElement.scrollHeight, window.innerHeight]"
        )
        assert height <= inner, (height, inner)
        for selector in ("#fs-enter", "#open-onlooker"):
            box = page.locator(selector).bounding_box()
            assert box and box["y"] + box["height"] <= inner
        last = page.locator(".route li").last.bounding_box()
        assert last and last["y"] + last["height"] <= inner
    finally:
        context.close()


@pytest.mark.parametrize(
    "path, kind",
    [
        ("/", "text/html"),
        ("/static/css/tokens.css", "text/css"),
        ("/static/css/shell.css", "text/css"),
        ("/static/js/app.js", "javascript"),
        ("/static/js/screens/between.js", "javascript"),
        ("/static/fonts/inter-latin-wght-normal.woff2", "font/woff2"),
        ("/static/fonts/bricolage-grotesque-latin-wght-normal.woff2", "font/woff2"),
        ("/static/fonts/OFL-Inter.txt", "text/plain"),
        ("/static/fonts/OFL-Bricolage.txt", "text/plain"),
    ],
)
def test_the_installed_package_serves_its_page_files(app, pty_device, path, kind):
    a = app(port=pty_device.path, flow=SHORT)
    response = urllib.request.urlopen(a.url + path)
    body = response.read()
    assert response.status == 200
    assert kind in response.headers["Content-Type"]
    assert body
    if path.endswith(".woff2"):
        assert body[:4] == b"wOF2"


def test_the_built_wheel_holds_the_web_files(tmp_path):
    root = Path(__file__).resolve().parents[2]
    out = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--package", "brain-mapper", "--out-dir", str(out)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out.glob("brain_mapper-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    source = root / "brain_mapper" / "src"
    expected = {
        str(p.relative_to(source))
        for p in (source / "brain_mapper" / "web").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    for needed in (
        "brain_mapper/web/index.html",
        "brain_mapper/web/js/app.js",
        "brain_mapper/web/css/shell.css",
        "brain_mapper/web/fonts/inter-latin-wght-normal.woff2",
        "brain_mapper/web/fonts/bricolage-grotesque-latin-wght-normal.woff2",
    ):
        assert needed in expected, needed
    assert expected <= names, sorted(expected - names)

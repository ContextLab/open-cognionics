"""The video round in a real Chromium: the real Vimeo embed, its events in ``events.jsonl`` with
markers, the end of the video ending the round, the offline path, the origin check, and that no
page of a whole session loads anything from another host outside the video step.

Tests marked ``network`` need the internet: they play the real player of Vimeo 19009646. They are
run like every other test, never skipped; without the internet they fail. The others block the
player's host in the browser (a Playwright route that aborts requests to ``player.vimeo.com``
only) and need no internet.

The network tests run in a headed Chromium (a window on the screen; Playwright's
``headless=False``, nothing else changed: the user agent and the launch are Playwright's own),
so they need a display as well as the internet. On 2026-11-25 Vimeo's player answered the
headless Chromium of the other browser tests with Cloudflare's check for automated browsers (HTTP
401 and a Turnstile page) instead of the player, and never reported ``ready``; the headed
Chromium was given the player. If the check is shown all the same, a network test fails at the
wait for ``ready`` and says the HTTP status.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from brain_mapper import flow
from test_engine import SHORT, events_of, markers_of, session_folder_of
from test_shell_browser import SCREEN, advance_until, begin

PLAYER = "https://player.vimeo.com"
#: The player's address exactly: the video and Vimeo's own display parameters, nothing of ours.
EMBED = "https://player.vimeo.com/video/19009646"
EMBED_QUERY = {
    "dnt": "1",
    "title": "0",
    "byline": "0",
    "portrait": "0",
    "fullscreen": "0",
    "controls": "0",
    "keyboard": "0",
}
#: The part of the video played (flow.py, ``VIDEO``).
START_S, END_S = flow.VIDEO.start_s, flow.VIDEO.end_s
SEEK = "{method: 'setCurrentTime', value: %s}, 'https://player.vimeo.com'"
#: How long the page waits for the player's ``ready`` before it says the video could not be loaded.
READY_LIMIT_S = 15.0
VIDEO = '#screen[data-screen="video"]'


def video_flow(tmp_path: Path, seconds: float) -> Path:
    """``flows/short.json`` (2 s blocks, placeholders) with the video round drawn by its own
    screen (``module`` left out: the step id, ``video``) and a video block of ``seconds``."""
    raw = json.loads(SHORT.read_text())
    for step in raw:
        if step["id"] == "video":
            del step["module"]
            step["blocks"][0]["seconds"] = seconds
    path = tmp_path / "video-flow.json"
    path.write_text(json.dumps(raw))
    return path


@pytest.fixture(scope="module")
def headed(browser):
    """A headed Chromium (a window), launched as the suite's headless one is, but with a window."""
    chromium = browser.browser_type.launch(headless=False)
    yield chromium
    chromium.close()


@pytest.fixture
def net_page(headed):
    """A page of the headed Chromium, for the network tests that need the real player."""
    context = headed.new_context()
    page = context.new_page()
    # A window that is not in front is refused full screen ("not granted"): another browser of
    # the test (the onlooker's) may have taken the focus.
    page.bring_to_front()
    yield page
    context.close()


@pytest.fixture
def sized_page(headed, request):
    """A page of the headed Chromium with the viewport given by the test's parameter."""
    width, height = request.param
    context = headed.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.bring_to_front()
    yield page
    context.close()


#: Asks the player for its video's title (``getVideoTitle``, a method of Vimeo's player.js) and
#: resolves with the answer.
VIDEO_TITLE = """() => new Promise((resolve) => {
  const frame = document.getElementById('video-player');
  addEventListener('message', function answer(event) {
    if (event.origin !== 'https://player.vimeo.com' || event.source !== frame.contentWindow) return;
    let data = event.data;
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch (e) { return; } }
    if (data && data.method === 'getVideoTitle') { removeEventListener('message', answer); resolve(data.value); }
  });
  frame.contentWindow.postMessage({method: 'getVideoTitle'}, 'https://player.vimeo.com');
})"""

#: Keeps the time (``performance.now()`` clock) of the last click on Start.
START_CLICK = """document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'advance') window.__startClick = e.timeStamp;
}, true);"""


def video_events(folder: Path) -> list[dict]:
    return [e for e in events_of(folder) if e["name"].startswith("video.")]


def wait_events(a, predicate, what: str, timeout: float = 30.0) -> list[dict]:
    """Reads ``events.jsonl`` until ``predicate(events)`` holds; returns the events."""
    deadline = time.monotonic() + timeout
    while True:
        events = events_of(session_folder_of(a))
        if predicate(events):
            return events
        assert time.monotonic() < deadline, f"{what}; events: {[e['name'] for e in events][-30:]}"
        time.sleep(0.1)


def names(events) -> list[str]:
    return [e["name"] for e in events]


def after(events, seq: int, name: str) -> list[dict]:
    return [e for e in events if e["seq"] > seq and e["name"] == name]


def markers_rise(folder: Path, events: list[dict]) -> None:
    """Each event has its marker, and the marker samples rise with ``seq``."""
    marks = {m["label"]: m["sample"] for m in markers_of(folder / "eeg")}
    samples = []
    for e in sorted(events, key=lambda e: e["seq"]):
        label = f"{e['step']}/{e['name']}#{e['seq']}"
        assert label in marks, label
        samples.append(marks[label])
    assert samples == sorted(samples) and samples[0] < samples[-1], samples


def to_video(a, page) -> None:
    begin(a, page)
    watch_player(page)
    advance_until(page, "video", "waiting")
    page.wait_for_selector(VIDEO)


def watch_player(page) -> None:
    """Keeps the status of every answer to a request for the player's page (``page.player``)."""
    if hasattr(page, "player"):
        return
    page.player = []
    page.context.on(
        "response",
        lambda r: page.player.append(r.status) if r.url.startswith(EMBED) else None,
    )


def wait_ready(page) -> None:
    """Waits for Start, which the page offers once the player said ``ready``; says what the
    player's host answered if it never did (401 is Cloudflare's check for automated browsers)."""
    try:
        page.wait_for_selector(f"{VIDEO} #advance", timeout=READY_LIMIT_S * 1000)
    except Exception as error:
        raise AssertionError(
            f"the player never reported ready; HTTP status of its page: {page.player}; "
            f"on screen: {page.inner_text('#video-round')!r}"
        ) from error


def player_request(requests) -> object:
    found = [r for r in requests if r.url.startswith(EMBED)]
    assert found, [r.url for r in requests]
    return found[0]


# -- the real player ------------------------------------------------------------------------------


@pytest.mark.network
def test_the_real_embed_plays_and_logs_ready_play_progress_pause_and_resume(
    app, pty_device, net_page, tmp_path
):
    page = net_page
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 30))
    requests = []
    page.context.on("request", lambda r: requests.append(r))
    page.add_init_script(START_CLICK)
    to_video(a, page)
    assert page.inner_text("#video-instructions").startswith("A short video will play.")
    # Start is offered once the player has said it is ready.
    wait_ready(page)
    folder = session_folder_of(a)
    assert names(video_events(folder)) == ["video.ready"]
    title = page.evaluate(VIDEO_TITLE)
    assert isinstance(title, str) and title
    print(f"MEASURED the player's video title: {title!r}")

    # A message that claims to come from the player but comes from the page itself is ignored.
    page.evaluate(
        """() => {
          const forged = {event: 'play', data: {seconds: 99, percent: 0.5, duration: 180}};
          window.postMessage(forged, '*');
          window.postMessage(JSON.stringify(forged), '*');
        }"""
    )

    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    start = next(e for e in events_of(folder) if e["name"] == "block_start" and e["step"] == "video")

    def playing(events):
        progress = after(events, start["seq"], "video.progress")
        return after(events, start["seq"], "video.play") and len(progress) >= 2

    events = wait_events(a, playing, "no play and two progress events")
    first_play = after(events, start["seq"], "video.play")[0]
    print(
        f"MEASURED Start click to video.play: {first_play['browser_time'] - page.evaluate('window.__startClick'):.0f} ms"
    )
    # The first thing shown is the part played: the cover goes at or after start_s.
    events = wait_events(a, lambda ev: after(ev, start["seq"], "video.uncover"), "never uncovered")
    uncover = after(events, start["seq"], "video.uncover")[0]
    assert uncover["payload"]["seconds"] >= START_S - 0.05, uncover
    first_seek = after(events, start["seq"], "video.seek")[0]["payload"]
    assert (first_seek["to"], first_seek["reason"]) == (START_S, "range_start"), first_seek
    print(
        f"MEASURED Start click to uncover: {uncover['browser_time'] - page.evaluate('window.__startClick'):.0f} ms"
    )
    progress = after(events, start["seq"], "video.progress")
    assert all(START_S - 0.05 <= e["payload"]["seconds"] <= END_S for e in progress), progress
    positions = [e["payload"]["seconds"] for e in progress]
    assert positions == sorted(positions) and positions[0] < positions[-1], positions
    assert all(e["payload"]["duration"] == 180 for e in progress)
    # at most one a second (the page's own clock)
    times = [e["browser_time"] for e in progress]
    assert all(b - a_ >= 1000 for a_, b in zip(times, times[1:])), times

    # Leaving full screen pauses the block and the video ...
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    paused = next(e for e in events_of(folder) if e["name"] == "paused")
    events = wait_events(
        a,
        lambda ev: after(ev, paused["seq"], "video.pause"),
        "no video.pause after the pause",
    )
    held = after(events, paused["seq"], "video.pause")[0]["payload"]["seconds"]
    # ... and returning resumes both.
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    resumed = next(e for e in events_of(folder) if e["name"] == "resumed")
    events = wait_events(
        a,
        lambda ev: after(ev, resumed["seq"], "video.play") and after(ev, resumed["seq"], "video.progress"),
        "no video.play and progress after the resume",
    )
    # while paused the player did not move on
    between = [
        e for e in events if paused["seq"] < e["seq"] < resumed["seq"] and e["name"] == "video.progress"
    ]
    assert all(e["payload"]["seconds"] <= held + 0.5 for e in between), (held, between)
    assert after(events, resumed["seq"], "video.progress")[0]["payload"]["seconds"] >= held

    # The block's time runs out before the video's: the round ends cleanly, the player is gone.
    page.wait_for_selector(f'{SCREEN}[data-screen="between"][data-step="video"]', timeout=60_000)
    assert page.locator("iframe").count() == 0
    events = events_of(folder)
    end = next(e for e in events if e["name"] == "block_end" and e["step"] == "video")
    assert "ended_by" not in end["payload"]  # its time was up
    late = [e for e in video_events(folder) if e["seq"] > end["seq"]]
    assert all(e["server_mono_ns"] - end["server_mono_ns"] < 1e9 for e in late), late  # in flight at most

    # Never anything forged; every video event has its marker, at samples rising with seq.
    assert all(e["payload"].get("seconds") != 99 for e in video_events(folder))
    markers_rise(folder, video_events(folder))
    # The player was asked for with Vimeo's own parameters only, and with no referrer.
    request = player_request(requests)
    parts = urlsplit(request.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == EMBED
    assert dict(parse_qsl(parts.query)) == EMBED_QUERY
    assert "referer" not in {k.lower() for k in request.headers}
    assert page.evaluate("document.referrer") == ""  # nothing leaves in a document referrer either


#: Points of the window, on a 9 x 9 grid from edge to edge (a pixel in from each edge).
GRID = """() => {
  const out = [];
  for (let i = 0; i < 9; i++) for (let j = 0; j < 9; j++) {
    const x = Math.min(innerWidth - 1, Math.max(0, Math.round((innerWidth * i) / 8)));
    const y = Math.min(innerHeight - 1, Math.max(0, Math.round((innerHeight * j) / 8)));
    const hit = document.elementFromPoint(x, y);
    out.push([x, y, hit ? (hit.id || hit.tagName) : null]);
  }
  return out;
}"""


def full_frame(page) -> dict:
    """What fills the window while the video plays: the iframe's box, the window, what is at
    each grid point, whether the page can scroll and which element is in full screen."""
    return page.evaluate(
        """(grid) => {
          const box = document.getElementById('video-player').getBoundingClientRect();
          // The iframe takes no pointer events while it plays (the page must see the pointer
          // move), so a hit test passes through it: it is asked with the iframe hit-testable.
          const frame = document.getElementById('video-player');
          frame.style.pointerEvents = 'auto';
          const hits = eval(grid)();
          frame.style.pointerEvents = '';
          return {
            box: [box.x, box.y, box.width, box.height],
            window: [innerWidth, innerHeight],
            hits,
            scroll: [document.scrollingElement.scrollHeight > innerHeight
                     && getComputedStyle(document.documentElement).overflowY !== 'hidden',
                     getComputedStyle(document.documentElement).overflowY],
            fullscreen: document.fullscreenElement === document.documentElement,
          };
        }""",
        GRID,
    )


@pytest.mark.network
@pytest.mark.parametrize(
    "sized_page", [(1024, 768), (1920, 1080)], indirect=True, ids=["1024x768", "1920x1080"]
)
def test_the_playing_video_fills_the_window_and_the_controls_come_on_input(
    app, pty_device, sized_page, tmp_path
):
    page = sized_page
    width, height = page.viewport_size["width"], page.viewport_size["height"]
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 40))
    to_video(a, page)
    wait_ready(page)
    folder = session_folder_of(a)
    # Before Start the controls are there, under the player.
    assert page.is_visible("#skip-round") and page.is_visible("#stop-here")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    start = next(e for e in events_of(folder) if e["name"] == "block_start" and e["step"] == "video")
    events = wait_events(a, lambda ev: after(ev, start["seq"], "video.play"), "no video.play")
    play = after(events, start["seq"], "video.play")[0]["payload"]
    # The size is logged: the whole window, in CSS px, and the device pixel ratio.
    assert (play["frame_css_width"], play["frame_css_height"]) == (width, height), play
    assert play["device_pixel_ratio"] > 0
    ready = next(e for e in events if e["name"] == "video.ready")["payload"]
    assert (ready["start_s"], ready["end_s"]) == (START_S, END_S), ready
    assert 0 < ready["frame_css_width"] <= width and ready["device_pixel_ratio"] > 0, ready

    # The controls go 3 s after the last input (the click on Start).
    page.wait_for_selector('#video-round[data-controls="hidden"]', timeout=6000)
    shown = full_frame(page)
    assert shown["box"] == [0, 0, width, height], shown
    assert shown["window"] == [width, height], shown
    assert all(hit == "video-player" for _, _, hit in shown["hits"]), shown["hits"]
    assert not page.is_visible("#hud") and not page.is_visible("#skip-round")
    assert shown["scroll"] == [False, "hidden"], shown
    page.mouse.wheel(0, 600)
    assert page.evaluate("[scrollX, scrollY]") == [0, 0]
    # The app's own full screen stays: the root, never the player's.
    assert shown["fullscreen"], shown

    # A pointer move brings the controls up (over the player, at the bottom) ...
    page.mouse.move(width / 2, height / 2)
    page.mouse.move(width / 2 + 40, height / 2 + 30)
    page.wait_for_selector('#video-round[data-controls="shown"]', timeout=2000)
    assert page.is_visible("#skip-round") and page.is_visible("#stop-here")
    bar = page.locator("#video-bar").bounding_box()
    assert bar["y"] + bar["height"] <= height and bar["y"] > height / 2, bar
    # ... and they go again after 3 s without input; a key press brings them back.
    page.wait_for_selector('#video-round[data-controls="hidden"]', timeout=6000)
    page.keyboard.press("Shift")
    page.wait_for_selector('#video-round[data-controls="shown"]', timeout=2000)
    page.wait_for_selector('#video-round[data-controls="hidden"]', timeout=6000)

    # The video played on all this time and nothing paused the block.
    events = wait_events(
        a, lambda ev: len(after(ev, start["seq"], "video.progress")) >= 4, "no four progress events"
    )
    assert not after(events, start["seq"], "paused") and not after(events, start["seq"], "video.pause")
    assert page.evaluate("document.fullscreenElement === document.documentElement")

    # Paused (full screen left): the controls are up and stay up.
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_timeout(3500)
    assert page.get_attribute("#video-round", "data-controls") == "shown"
    assert page.is_visible("#fs-skip-round") and page.is_visible("#fs-stop-here")
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    wait_events(
        a,
        lambda ev: after(ev, next(e for e in ev if e["name"] == "resumed")["seq"], "video.play"),
        "no video.play after the resume",
    )
    # "Skip this round" over the playing video ends the round.
    page.mouse.move(10, 10)
    page.click("#skip-round")
    page.click("#leave-yes")
    page.wait_for_function("document.querySelector('#screen').dataset.step !== 'video'")
    assert any(e["name"] == "skipped" and e["step"] == "video" for e in events_of(folder))
    assert page.locator("iframe").count() == 0
    assert not page.evaluate("document.documentElement.classList.contains('video-full')")


@pytest.mark.network
def test_the_end_of_the_part_played_ends_the_round_and_seeks_outside_it_are_pulled_back(
    app, pty_device, net_page, tmp_path
):
    page = net_page
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 60))
    to_video(a, page)
    wait_ready(page)
    folder = session_folder_of(a)
    page.click("#advance")
    start = wait_events(a, lambda ev: "video.uncover" in names(ev), "never uncovered")
    start = next(e for e in start if e["name"] == "video.uncover")

    def seek(to: float) -> int:
        """Moves the play head, as the player's own bar would (it is hidden: ``controls=0``)."""
        seq = max(e["seq"] for e in events_of(folder))
        page.evaluate(f"document.getElementById('video-player').contentWindow.postMessage({SEEK % to})")
        return seq

    # A seek to the very start (the title) or past the end (the dedication) is covered and pulled
    # back to where the video was; neither is ever shown.
    for to in (0, 178):
        seq = seek(to)
        events = wait_events(
            a,
            lambda ev, seq=seq: after(ev, seq, "video.seek") and after(ev, seq, "video.uncover"),
            f"a seek to {to} was not pulled back",
        )
        back = after(events, seq, "video.seek")[0]["payload"]
        assert back["reason"] == "out_of_range" and START_S <= back["to"] < END_S, back
        assert after(events, seq, "video.uncover")[0]["payload"]["seconds"] >= START_S - 0.05
        assert not after(events, seq, "video.range_end") and not after(events, seq, "block_end")

    # Played on to end_s, the round ends there.
    seq = seek(END_S - 5)
    events = wait_events(a, lambda ev: after(ev, seq, "video.range_end"), "no video.range_end", timeout=30)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"][data-step="video"]')
    assert page.locator("iframe").count() == 0
    events = events_of(folder)
    ranged = next(e for e in events if e["name"] == "video.range_end")
    assert END_S <= ranged["payload"]["seconds"] < END_S + 0.3, ranged
    assert ranged["payload"]["end_s"] == END_S
    end = next(e for e in events if e["name"] == "block_end" and e["step"] == "video")
    assert end["seq"] > ranged["seq"]
    assert end["payload"]["ended_by"] == "video_ended" and end["payload"]["seconds"] < 60
    assert "video.ended" not in names(events)  # the player never reached its own end
    # Nothing outside the part was reported as seen.
    seen = [e["payload"]["seconds"] for e in events if e["name"] in ("video.progress", "video.uncover")]
    assert seen and all(START_S - 0.05 <= t <= END_S for t in seen), seen
    assert start["payload"]["seconds"] >= START_S - 0.05
    ledger = [json.loads(x) for x in (folder / "points.jsonl").read_text().splitlines()]
    assert ledger[-1]["step"] == "video" and ledger[-1]["amount"] == 100
    markers_rise(folder, [e for e in events if e["step"] == "video"])


# -- without the player ---------------------------------------------------------------------------


def test_without_the_player_the_round_says_so_within_the_limit_and_can_be_skipped(
    app, pty_device, page, tmp_path
):
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 30))
    tried: list[str] = []

    def refuse(route):
        tried.append(route.request.url)
        route.abort("internetdisconnected")

    page.context.route(lambda url: urlsplit(url).netloc == "player.vimeo.com", refuse)
    page.add_init_script(
        """new MutationObserver(() => {
             const s = document.getElementById('screen');
             if (s && s.dataset.screen === 'video' && !window.__videoAt) window.__videoAt = performance.now();
           }).observe(document, {subtree: true, attributes: true, attributeFilter: ['data-screen']});"""
    )
    to_video(a, page)
    assert page.locator("#video-player").count() == 1
    assert page.locator("#advance").count() == 0  # no Start before the player is ready
    # Messages that claim the player is ready, posted by the page itself, are ignored.
    page.evaluate(
        """() => {
          window.postMessage({event: 'ready'}, '*');
          window.postMessage('{"event":"ready"}', '*');
          window.postMessage({event: 'ready'}, 'https://player.vimeo.com');
        }"""
    )
    page.wait_for_selector("#video-problem", state="visible", timeout=(READY_LIMIT_S + 5) * 1000)
    shown_after = page.evaluate("performance.now() - window.__videoAt") / 1000
    assert READY_LIMIT_S <= shown_after < READY_LIMIT_S + 1.5, shown_after
    assert (
        page.inner_text("#video-problem") == "The video could not be loaded. Check the internet connection."
    )
    assert page.locator("#advance").count() == 0
    assert page.locator("#video-retry").is_visible() and page.locator("#skip-round").is_visible()
    assert tried and all(urlsplit(u).netloc == "player.vimeo.com" for u in tried)
    folder = session_folder_of(a)
    events = video_events(folder)
    assert names(events) == ["video.error"]
    assert events[0]["payload"]["reason"] == "not_ready" and events[0]["payload"]["waited_ms"] == 15000
    # Try again asks for the player once more (and is refused once more).
    count = len(tried)
    page.click("#video-retry")
    page.wait_for_selector("#video-problem", state="hidden")
    page.wait_for_function("document.getElementById('video-player') !== null")
    deadline = time.monotonic() + 10
    while len(tried) == count:
        assert time.monotonic() < deadline, "Try again did not ask for the player"
        page.wait_for_timeout(100)
    # Skip this round goes on to the next step; the session goes on.
    page.click("#skip-round")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_closed"][data-phase="waiting"]')
    assert page.locator("iframe").count() == 0
    skipped = [e for e in events_of(folder) if e["name"] == "skipped"]
    assert skipped and skipped[0]["step"] == "video"
    assert "video.ready" not in names(events_of(folder))


# -- a whole session ------------------------------------------------------------------------------

#: Says, on the console, every screen the page shows (with the number of iframes in the page then)
#: and every iframe put into or taken out of the page, with the screen shown at that moment.
SAY_SCREENS = """new MutationObserver((records) => {
  const s = document.getElementById('screen');
  if (!s) return;
  const now = `${s.dataset.screen}/${s.dataset.step}`;
  for (const r of records) {
    for (const n of r.addedNodes) if (n.nodeName === 'IFRAME' || (n.querySelector && n.querySelector('iframe'))) console.log(`iframe+ ${now}`);
    for (const n of r.removedNodes) if (n.nodeName === 'IFRAME' || (n.querySelector && n.querySelector('iframe'))) console.log(`iframe- ${now}`);
  }
  if (now !== window.__said) {
    window.__said = now;
    console.log(`screen ${now} iframes ${document.querySelectorAll('iframe').length}`);
  }
}).observe(document, {subtree: true, childList: true, attributes: true, attributeFilter: ['data-screen', 'data-step']});"""


def made_by_main_frame(request, page) -> bool:
    try:
        return request.frame == page.main_frame
    except Exception:  # a request of no frame (a service worker): not the page's own
        return False


@pytest.mark.network
def test_a_whole_session_loads_nothing_from_another_host_outside_the_video_step(
    app, pty_device, net_page, browser, tmp_path
):
    page = net_page
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 8))
    local = urlsplit(a.url).netloc
    lines: list[str] = []  # what SAY_SCREENS says
    requests: list[
        tuple[str, bool]
    ] = []  # every request of the page's context, and whether its main frame made it
    page.context.on("request", lambda r: requests.append((r.url, made_by_main_frame(r, page))))
    page.on("console", lambda m: lines.append(m.text) if m.text.startswith(("screen ", "iframe")) else None)
    page.add_init_script(SAY_SCREENS)
    onlooker = browser.new_context()
    look = onlooker.new_page()
    look_urls: list[str] = []
    onlooker.on("request", lambda r: look_urls.append(r.url))
    try:
        look.goto(a.url + "/onlooker")
        begin(a, page)
        watch_player(page)
        advance_until(page, "video", "waiting")
        # The player either says it is ready (the video then plays for a while) or the page says
        # it could not be loaded (the round is then skipped). Either way every request is kept.
        page.wait_for_selector(
            f"{VIDEO} #advance, #video-problem:not([hidden])",
            timeout=(READY_LIMIT_S + 5) * 1000,
        )
        # The onlooker shows the step, never the video.
        look.wait_for_function("document.getElementById('ol-step').textContent === 'Video'")
        assert look.locator("iframe").count() == 0
        if page.locator(f"{VIDEO} #advance").count():
            page.click("#advance")
            wait_events(a, lambda ev: "video.progress" in names(ev), "no progress")
            assert look.inner_text("#ol-time").strip()  # and the time
        else:
            page.click("#skip-round")
            page.click("#leave-yes")
        assert page.player  # the player's host was asked, and it answered
        # On to the end of the session.
        deadline = time.monotonic() + 120
        while not page.locator(f'{SCREEN}[data-phase="done"]').count():
            assert time.monotonic() < deadline, page.evaluate(
                "document.getElementById('screen').dataset.screen"
            )
            button = page.query_selector("#advance:not([disabled])")
            if button is not None:
                try:
                    button.click(timeout=2000)
                except Exception:  # the screen moved on under the click; look again
                    pass
            page.wait_for_timeout(100)
    finally:
        onlooker.close()

    screens = [x for x in lines if x.startswith("screen ")]
    assert any(s.startswith("screen video/video ") for s in screens), screens
    assert screens[-1].startswith("screen summary/summary"), screens
    # The iframe is put in only on the video screen, is taken out when the screen is left, and
    # no other screen ever has one.
    frames = [x for x in lines if x.startswith("iframe")]
    assert frames and all(x == "iframe+ video/video" for x in frames if x.startswith("iframe+")), frames
    assert frames[-1].startswith("iframe- "), frames
    for s in screens:
        assert s.endswith("iframes 0") or s.startswith("screen video/video "), s

    # The page itself loaded nothing from another host; every such request came from the iframe.
    def foreign(url: str) -> bool:
        return urlsplit(url).netloc != local and not url.startswith(("data:", "blob:"))

    assert [u for u, main in requests if main and foreign(u)] == []
    assert any(urlsplit(u).netloc == "player.vimeo.com" for u, _ in requests)
    assert all(urlsplit(u).netloc == local for u in look_urls if not u.startswith(("data:", "blob:")))

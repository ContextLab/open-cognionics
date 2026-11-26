"""The pause, the headset check of the paused screen, the rest tones and the video points, in a real
Chromium on the real server and the simulated headset (``tests/python/pty_device.py``).

The pause rules: a pause ends the running block and
Resume goes on with the next one; the paused screen can open the contact check; each rest has an
instruction screen, a start tone and an end tone; the video earns a point for each full 10 s
watched. Nothing waits on a fixed sleep for an assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

from brain_mapper import scores
from test_engine import events_of, markers_of, next_state, open_ws, send, session_folder_of
from test_participants import request
from test_link_pause import ALLOWED, Plug, scored, until, wait_paused
from test_video_browser import START_S, headed, net_page, to_video, video_flow, wait_ready  # noqa: F401
from test_shell_browser import SCREEN, begin
from test_whole_session_browser import contact_device, drive
from shots import ShotsDir

#: Between two blocks of a task that starts on Enter (flanker): the task's prompt for the next block.
PROMPT_BETWEEN = f'{SCREEN}[data-screen="task"][data-phase="between"] #task-area[data-ready="block"]'

SHOTS = ShotsDir("shots-night-engine")
SIZES = [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)]


def flow_file(tmp_path: Path, steps: list[dict]) -> Path:
    path = tmp_path / "pause-flow.json"
    path.write_text(
        json.dumps(
            [
                {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
                {"id": "device", "kind": "setup", "title": "Device setup", "blocks": []},
                *steps,
                {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
            ]
        )
    )
    return path


def task(step: str, title: str, n: int, seconds: float) -> dict:
    return {
        "id": step,
        "kind": "task",
        "title": title,
        "blocks": [{"id": f"block{i + 1}", "seconds": seconds, "points": 100} for i in range(n)],
    }


def running_with_trials(a, step: str, count: int, block: str | None = None):
    def enough(look) -> bool:
        try:
            folder = session_folder_of(a)
        except (AssertionError, FileNotFoundError):
            return False
        trials = [e for e in scored(folder, step) if block is None or e["block"] == block]
        return len(trials) >= count and look["phase"] == "running" and look["step"] == step

    return enough


def shot(page, name: str, under_overlay: bool = False) -> None:
    """A screenshot to look at. ``under_overlay``: the full-screen notice (there because the layout
    check left full screen) is hidden for the picture only, so the screen under it can be seen."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    if under_overlay:
        page.evaluate("document.getElementById('fs-lost').style.visibility = 'hidden'")
    try:
        page.screenshot(path=str(SHOTS / f"{name}.png"))
    finally:
        if under_overlay:
            page.evaluate("document.getElementById('fs-lost').style.visibility = ''")


LAYOUT = """(sel) => {
  const box = document.querySelector(sel).getBoundingClientRect();
  return {x: box.x, y: box.y, w: box.width, h: box.height, vw: innerWidth, vh: innerHeight,
          sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight};
}"""


def check_layout(page, selector: str, name: str) -> None:
    """At each of the four sizes: the box is inside the window, centered, of a sensible width, and
    nothing scrolls. Screenshots at 3840x1600 and 1512x982."""
    # A window in full screen cannot be resized: the paused screen is the same out of it.
    page.evaluate("document.fullscreenElement ? document.exitFullscreen() : null")
    page.wait_for_function("document.fullscreenElement === null")
    for width, height in SIZES:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(100)  # one layout after the resize (the boxes are read, not timed)
        b = page.evaluate(LAYOUT, selector)
        assert b["x"] >= 0 and b["y"] >= 0 and b["x"] + b["w"] <= b["vw"] + 0.5 and b["y"] + b["h"] <= b["vh"] + 0.5, (name, width, b)
        assert abs((b["x"] + b["w"] / 2) - b["vw"] / 2) <= 0.02 * b["vw"], (name, width, b)
        assert b["w"] <= 1300, (name, width, b)  # a centered column, not the far edges
        assert b["sw"] <= b["vw"] and b["sh"] <= b["vh"], (name, width, b)
        if (width, height) in ((3840, 1600), (1512, 982)):
            shot(page, f"{name}-{width}x{height}", under_overlay=not selector.startswith("#fs-"))
    page.set_viewport_size({"width": 1512, "height": 982})


# -- 1. pause by hand mid-flanker -> between screen -> next block -------------------------------------


def test_a_pause_by_hand_mid_flanker_goes_on_with_the_next_block(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    a = app(port=pty_device.path, flow=flow_file(tmp_path, [task("flanker", "Flanker", 2, 40)]))
    begin(a, page)
    drive(page, a, running_with_trials(a, "flanker", 2), timeout=180)
    folder = session_folder_of(a)
    before = len(scored(folder, "flanker"))
    page.evaluate("document.exitFullscreen()")
    wait_paused(page)
    page.wait_for_function("document.getElementById('fs-lost-left').textContent.includes('next block')")
    assert page.inner_text("#fs-lost-left") == (
        "This block has ended and is not repeated. Press Resume to go on to the next block."
    )
    assert page.is_visible("#fs-check") and page.is_enabled("#fs-check")
    check_layout(page, "#fs-lost .card", "paused-interrupted")
    end = until(lambda: [e for e in events_of(folder) if e["name"] == "block_end"], "block_end")[0]
    assert end["block"] == "block1" and end["payload"]["ended_by"] == "interrupted"
    assert end["payload"]["reason"] == "fullscreen_lost" and end["payload"]["points"] == 0
    page.click("#fs-return")
    # Flanker starts on Enter: between two of its blocks the task's own prompt is the screen the
    # participant goes on from (app.js), and it says that the pause ended the last block.
    page.wait_for_selector(PROMPT_BETWEEN, timeout=10_000)
    assert "ended by the pause" in page.inner_text("#task-ready")
    assert page.inner_text("#task-block") == "Block 2 of 2"
    shot(page, "between-after-interrupt-1512x982")
    # One Enter on that prompt starts the next block, as after any block.
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]', timeout=10_000)
    drive(page, a, running_with_trials(a, "flanker", 1, block="block2"), timeout=120)
    events = events_of(folder)
    starts = [e["block"] for e in events if e["name"] == "block_start"]
    assert starts == ["block1", "block2"]  # block1 is not repeated
    # The trials of block 1 stay in the data and in the scores.
    first = [e for e in scored(folder, "flanker") if e["block"] == "block1"]
    assert len(first) >= before
    resumed = [e for e in events if e["name"] == "resumed"]
    assert [e["payload"] for e in resumed] == [{"to": "between"}]


# -- pause in the last block of a round -----------------------------------------------------------------


def test_a_pause_in_the_last_block_of_a_round_leads_to_the_next_round(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    steps = [task("flanker", "Flanker", 1, 40), task("spatial", "Spatial memory", 1, 30)]
    a = app(port=pty_device.path, flow=flow_file(tmp_path, steps))
    begin(a, page)
    drive(page, a, running_with_trials(a, "flanker", 1), timeout=180)
    page.evaluate("document.exitFullscreen()")
    wait_paused(page)
    page.wait_for_function("document.getElementById('fs-lost-left').textContent.includes('next round')")
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=10_000)
    assert page.inner_text("#between-next") == "Spatial memory"
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="spatial"][data-phase="waiting"]', timeout=10_000)
    folder = session_folder_of(a)
    record = json.loads((folder / "progress.json").read_text())
    assert {s["id"]: s["outcome"] for s in record["steps"]}["flanker"] == "finished"


# -- link silent mid-block -> paused -> headset check -> done -> resume -> next block -------------------


def test_silence_mid_block_then_the_headset_check_then_resume_goes_on_with_the_next_block(app, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    plug = Plug(tmp_path, make=contact_device)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=flow_file(tmp_path, [task("flanker", "Flanker", 2, 40)]))
        begin(a, page)
        drive(page, a, running_with_trials(a, "flanker", 1), timeout=180)
        folder = session_folder_of(a)
        device.go_silent()
        wait_paused(page)
        page.wait_for_selector("#fs-check[disabled]", timeout=10_000)
        assert page.inner_text("#fs-wait").strip() == "Waiting for the headset"
        device.speak_again()
        until(lambda: [e for e in events_of(folder) if e["name"] == "link_back"], "link_back", 40)
        page.wait_for_selector("#fs-check:not([disabled])", timeout=10_000)
        on_before = device.written().count(0x11)
        page.click("#fs-check")
        page.wait_for_selector("#fs-check-panel:not([hidden])", timeout=10_000)
        assert page.is_hidden("#fs-return")  # Resume waits until Done
        until(lambda: device.written().count(0x11) > on_before, "0x11 for the headset check", 10)
        page.wait_for_selector("#hc-count[data-good]", timeout=30_000)
        assert page.inner_text("#hc-count").endswith("contacts are good")
        page.wait_for_function("window.__fitReady !== undefined || document.querySelector('#hc-view canvas') !== null", timeout=30_000)
        check_layout(page, "#fs-check-panel", "headset-check")
        off_before = device.written().count(0x12)
        page.click("#hc-done")
        page.wait_for_selector("#fs-check-panel", state="hidden", timeout=10_000)
        until(lambda: device.written().count(0x12) > off_before, "0x12 after Done", 10)
        # Settling: Resume is enabled only once the data show the check off for the settling time.
        settled = until(
            lambda: [e for e in events_of(folder) if e["name"] == "headset_check_settled"], "settled", 20
        )[0]
        page.wait_for_selector("#fs-return:not([disabled])", timeout=10_000)
        page.click("#fs-return")
        page.wait_for_selector(PROMPT_BETWEEN, timeout=10_000)
        assert "ended by the pause" in page.inner_text("#task-ready")
        page.keyboard.press("Enter")
        drive(page, a, running_with_trials(a, "flanker", 1, block="block2"), timeout=120)
        events = events_of(folder)
        order = [e["name"] for e in events if e["name"] in ("link_lost", "link_back", "headset_check_start", "headset_check_end", "headset_check_settled", "resumed", "block_start")]
        assert order == ["block_start", "link_lost", "link_back", "headset_check_start", "headset_check_end", "headset_check_settled", "resumed", "block_start"]
        end = next(e for e in events if e["name"] == "headset_check_end")
        assert len(end["payload"]["states"]) == 20 and end["payload"]["measured"] is True
        resumed = next(e for e in events if e["name"] == "resumed")
        assert resumed["seq"] > settled["seq"]
        saved = json.loads((folder / "calibration.json").read_text())
        assert [c["kind"] for c in saved["checks"]] == ["headset_check"] and len(saved["checks"][0]["states"]) == 20
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- rest: instructions, tones, in order -------------------------------------------------------------


def test_each_rest_has_instructions_a_start_tone_and_an_end_tone_logged_in_order(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    rests = [
        {"id": "rest_closed", "kind": "rest", "title": "Rest, eyes closed", "blocks": [{"id": "rest", "seconds": 3, "points": 100}]},
        {"id": "rest_open", "kind": "rest", "title": "Rest, eyes open", "blocks": [{"id": "rest", "seconds": 3, "points": 100}]},
    ]
    a = app(port=pty_device.path, flow=flow_file(tmp_path, rests))
    begin(a, page)
    drive(page, a, lambda look: look["step"] == "rest_closed" and look["phase"] == "waiting", timeout=120)
    page.wait_for_selector("#rest-instruction")
    assert page.inner_text("#rest-instruction") == (
        "Close your eyes when you hear the tone. Keep them closed and sit still. Open them when you hear the tone again."
    )
    check_layout(page, ".rest-body", "rest-closed-instructions")
    page.click("#fs-return")  # back to full screen (the layout check left it)
    page.wait_for_function("document.fullscreenElement !== null")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_closed"][data-phase="running"]')
    assert page.inner_text(".rest-body").strip() == ""  # a dark screen, nothing on it
    page.wait_for_selector(f'{SCREEN}[data-step="rest_closed"][data-phase="between"]', timeout=20_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="waiting"]')
    assert page.inner_text("#rest-instruction") == "Look at the cross and sit still. A tone marks the start and the end."
    shot(page, "rest-open-instructions-1512x982")
    page.click("#advance")
    page.wait_for_selector("#fixation-cross")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="between"]', timeout=20_000)
    folder = session_folder_of(a)
    events = until(
        lambda: (lambda ev: ev if sum(e["name"] == "rest.tone_end" for e in ev) == 2 else None)(events_of(folder)),
        "both end tones",
    )
    marks = {m["label"]: m["sample"] for m in markers_of(folder / "eeg")}
    for rest in ("rest_closed", "rest_open"):
        mine = [e for e in events if e["step"] == rest and e["name"] in ("block_start", "rest.tone_start", "block_end", "rest.tone_end")]
        assert [e["name"] for e in mine] == ["block_start", "rest.tone_start", "block_end", "rest.tone_end"]
        samples = [marks[f"{rest}/{e['name']}#{e['seq']}"] for e in mine]
        assert samples == sorted(samples)
        start, end = mine[1]["payload"], mine[3]["payload"]
        assert (start["hz"], end["hz"], start["ms"], end["ms"]) == (440, 660, 300, 300)
        assert start["played"] is True and end["played"] is True, (start, end)
        assert end["audio_time"] > start["audio_time"]  # the audio clock runs on
        # The tones bracket the period: about the 3 s of the block apart, by the EEG samples.
        gap = (samples[3] - samples[1]) / 500
        assert 2.9 <= gap <= 3.6, gap


def test_a_pause_in_a_rest_restarts_it_from_its_start_screen(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    rests = [{"id": "rest_open", "kind": "rest", "title": "Rest, eyes open", "blocks": [{"id": "rest", "seconds": 30, "points": 100}]}]
    a = app(port=pty_device.path, flow=flow_file(tmp_path, rests))
    begin(a, page)
    drive(page, a, lambda look: look["step"] == "rest_open" and look["phase"] == "running", timeout=120)
    page.evaluate("document.exitFullscreen()")
    wait_paused(page)
    assert page.inner_text("#fs-lost-left") == "Resume starts this rest again from the beginning."
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="waiting"]', timeout=10_000)
    page.wait_for_selector("#rest-instruction")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    folder = session_folder_of(a)
    events = until(
        lambda: (lambda ev: ev if sum(e["name"] == "rest.tone_start" for e in ev) == 2 else None)(events_of(folder)),
        "a second start tone",
    )
    kept = ("block_start", "rest.tone_start", "paused", "block_end", "resumed")
    names = [e["name"] for e in events if e["step"] == "rest_open" and e["name"] in kept]
    assert names == ["block_start", "rest.tone_start", "paused", "block_end", "resumed", "block_start", "rest.tone_start"]


def test_block_finished_over_the_websocket_is_accepted_or_refused_per_step(app, pty_device, tmp_path):
    """The participant socket: ``participant_done`` is refused for a step without
    ``may_finish_early`` (flanker) and accepted for one with it (words); ``trials_done`` ends a task
    block; both log ``block_end`` with ``ended_by``."""
    words = {**task("words", "Words", 1, 60), "module": None, "may_finish_early": True}
    flanker = {**task("flanker", "Flanker", 1, 60), "module": None}
    a = app(port=pty_device.path, flow=flow_file(tmp_path, [words, flanker]))
    with open_ws(a, "participant") as ws:
        next_state(ws)
        status, body = request(a, "POST", "/api/participant", {"label": "FINQ1"})
        assert status == 200, body
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
        folder = session_folder_of(a)
        until(lambda: (folder / "eeg" / "raw.bin").exists(), "the recording")
        send(ws, cmd="advance")
        next_state(ws, timeout=20, predicate=lambda m: m["step"] == "words" and m["phase"] == "waiting")
        send(ws, cmd="advance")
        next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "running")
        send(ws, cmd="block_finished", reason="participant_done", step="words")
        state = next_state(ws, timeout=10, predicate=lambda m: m["phase"] == "between")
        assert state["award"]["points"] == 100 and state["award"]["interrupted"] is False
        send(ws, cmd="advance")
        next_state(ws, timeout=20, predicate=lambda m: m["step"] == "flanker" and m["phase"] == "waiting")
        send(ws, cmd="advance")
        next_state(ws, timeout=20, predicate=lambda m: m["step"] == "flanker" and m["phase"] == "running")
        send(ws, cmd="block_finished", reason="participant_done", step="flanker")
        while True:
            message = json.loads(ws.recv(timeout=10))
            if message.get("t") == "error":
                break
        assert message["kind"] == "refused" and "not allowed" in message["msg"]
        send(ws, cmd="block_finished", reason="trials_done", step="flanker")
        next_state(ws, timeout=10, predicate=lambda m: m["phase"] == "between")
    ends = [e for e in events_of(folder) if e["name"] == "block_end"]
    assert [(e["step"], e["payload"]["ended_by"]) for e in ends] == [("words", "participant_done"), ("flanker", "trials_done")]


# -- 4. video points with a pause in the watched range ------------------------------------------------


def test_video_points_for_a_watched_range_with_a_pause_in_it(app, pty_device, net_page, tmp_path):  # noqa: F811
    """The real player (needs the internet, as test_video_browser.py's network tests): about 40 s
    watched with a pause in the middle earns one point per full 10 s from the start of the part
    played, each once; the between screen shows it as earned of possible (16)."""
    page = net_page
    a = app(port=pty_device.path, flow=video_flow(tmp_path, 45))
    to_video(a, page)
    wait_ready(page)
    folder = session_folder_of(a)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    until(lambda: [e for e in events_of(folder) if e["name"] == "video.progress" and e["payload"]["seconds"] >= START_S + 15], "15 s watched", 60)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    held = until(lambda: [e for e in events_of(folder) if e["name"] == "video.pause"], "video.pause")[0]
    assert page.inner_text("#fs-lost-left").startswith("The clock is stopped.")  # the video is held
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=90_000)
    events = events_of(folder)
    reports = [e for e in events if e["name"] in ("video.progress", "video.pause", "video.range_end") and e["seq"] > held["seq"]]
    # Played on from where it was held: no gap and no jump back in the reports after the pause.
    positions = [e["payload"]["seconds"] for e in reports if "seconds" in e["payload"]]
    assert positions == sorted(positions) and positions[0] - held["payload"]["seconds"] < 2.5
    last = max(e["payload"]["seconds"] for e in events if e["name"] in ("video.progress", "video.pause") and "seconds" in e["payload"])
    expected = int((last - START_S + 0.5) // 10)
    assert expected >= 3, last
    result = scores.score_folder(folder)["tasks"]["video"]
    assert (result["earned"], result["possible"], result["rule"]) == (expected, 16, "video_10s_watched")
    page.wait_for_function(f"document.getElementById('between-gained').textContent === '{expected} of 16'")
    page.wait_for_function(f"document.getElementById('hud-points').textContent === '{expected} of 16'")
    shot(page, "between-video-points")

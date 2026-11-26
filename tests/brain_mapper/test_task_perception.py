"""The perception task (``web/js/tasks/perception.js``) in a real Chromium, on the real server.

Each test starts ``brain-mapper`` on a pseudo-terminal device with a flow that plays the
perception task (``flows/perception.json``, or the same with other block lengths), drives the page
with the keyboard as a participant would, and reads the DOM, the session's ``events.jsonl``, the
recording's markers and ``behavior/perception.csv``. The side of each grating, which a scripted
participant needs to give a right (or a wrong) answer, is read from the stimulus event the page
logged. Nothing waits on a fixed sleep.
"""

from __future__ import annotations

import csv
import json
import math
import re
import signal
import socket
import time
from pathlib import Path

import pytest

from brain_mapper import behavior
from test_engine import events_of, markers_of, session_folder_of
from pty_device import PtyDevice, config_reply, onhead_stream
from test_shell_browser import SCREEN, advance_until, begin, free_port, server_state

FLOW = Path(__file__).parent / "flows" / "perception.json"
AREA = "#task-area"
#: One display frame at 60 Hz plus 5 ms.
TOLERANCE_MS = 17 + 5
#: The server's rule for payload keys and parts.
NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")

#: Records every practice feedback text the page shows, in order.
RECORD_FEEDBACK = """
window.__feedback = [];
new MutationObserver(() => {
  const el = document.getElementById('task-feedback');
  if (!el || el.hidden || !el.textContent) return;
  const last = window.__feedback[window.__feedback.length - 1];
  if (el.dataset.trial !== (last && last[0])) window.__feedback.push([el.dataset.trial, el.textContent]);
}).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
"""


def flow_with(tmp_path: Path, *seconds: float) -> Path:
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [
        {"id": f"block{i + 1}", "seconds": s, "points": 100} for i, s in enumerate(seconds)
    ]
    path = tmp_path / "perception.json"
    path.write_text(json.dumps(raw))
    return path


def events(folder: Path) -> list[dict]:
    """The events of ``events.jsonl`` (whole lines only: the server may be writing the last)."""
    text = (folder / "events.jsonl").read_text()
    return [json.loads(x) for x in text.split("\n")[:-1] if x]


def named(folder: Path, suffix: str) -> list[dict]:
    return [e for e in events(folder) if e["name"] == f"perception.{suffix}"]


def wait_until(check, what: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"never: {what}"
        time.sleep(0.02)


def bindable(port: int) -> bool:
    """Whether brain-mapper could listen on ``port`` now (it binds without SO_REUSEADDR)."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def to_instructions(a, page) -> None:
    begin(a, page)
    advance_until(page, "perception", "waiting")
    page.wait_for_selector("#task-instructions", state="visible")


def awaiting(page, trial: int, practice: bool, timeout: float = 15_000) -> None:
    page.wait_for_selector(
        f'{AREA}[data-trial="{trial}"][data-practice="{str(practice).lower()}"]'
        '[data-awaiting="true"]',
        timeout=timeout,
    )


def grating_of(folder: Path, block: str, trial: int, practice: bool) -> dict:
    def find():
        for e in named(folder, "stim"):
            p = e["payload"]
            if (p["part"], p["block"], p["trial"], p["practice"]) == (
                "grating",
                block,
                trial,
                practice,
            ):
                return e
        return None

    return wait_until(find, f"the grating of {block} trial {trial}")


def respond(page, folder: Path, block: str, trial: int, practice: bool, how: str) -> None:
    """Trial ``trial`` awaits an answer: press the right key, the wrong one, or nothing."""
    awaiting(page, trial, practice)
    if how == "miss":
        return
    side = grating_of(folder, block, trial, practice)["payload"]["side"]
    right = "f" if side == "left" else "j"
    page.keyboard.press(right if how == "correct" else ("j" if right == "f" else "f"))


def start(page, what: str, timeout: float = 30_000) -> None:
    """Wait for the Enter prompt of the practice or the block, then press Enter."""
    page.wait_for_selector(f'{AREA}[data-ready="{what}"]', timeout=timeout)
    page.keyboard.press("Enter")


#: The end of a block: the next block's prompt on the task screen, or the between screen.
BLOCK_OVER = (
    f'{SCREEN}[data-screen="task"][data-phase="between"] {AREA}[data-ready="block"], '
    f'{SCREEN}[data-screen="between"]'
)


def no_error(message: dict) -> bool:
    """Not an error, or the answer of a server without ``block_finished`` (not shown)."""
    return message.get("t") != "error" or "block_finished" in message.get("msg", "")


def practise(page, folder: Path, plan=("correct",) * 6) -> None:
    start(page, "practice")
    for i, how in enumerate(plan):
        respond(page, folder, "block1", i, True, how)
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=15_000)


def staircase(outcomes):
    """The brief's staircase in Python: start 8, 2-down-1-up, factor 1.26, 0.2..20 deg.
    Returns (tilt shown, reversal) per trial."""
    tilt, run, direction, out = 8.0, 0, None, []
    for correct in outcomes:
        step = None
        shown = tilt
        if correct:
            run += 1
            if run == 2:
                step, run, tilt = "down", 0, max(0.2, tilt / 1.26)
        else:
            step, run, tilt = "up", 0, min(20.0, tilt * 1.26)
        out.append((shown, step is not None and direction is not None and step != direction))
        if step is not None:
            direction = step
    return out


def check_markers(folder: Path) -> None:
    """Every perception event has a marker, and the samples rise with ``seq``."""
    samples = {
        int(m["label"].rsplit("#", 1)[1]): m["sample"] for m in markers_of(folder / "eeg")
    }
    ours = [e for e in events(folder) if e["name"].startswith("perception.")]
    assert ours
    for event in ours:
        assert event["seq"] in samples, event
    ordered = [samples[e["seq"]] for e in ours]
    assert ordered == sorted(ordered) and ordered[-1] > ordered[0]
    stims = [e for e in ours if e["name"] == "perception.stim"]
    resps = [e for e in ours if e["name"] == "perception.resp"]
    assert stims and resps


# -- instructions and practice ------------------------------------------------------------------------


def test_instructions_then_practice_with_feedback(app, pty_device, page):
    page.add_init_script(RECORD_FEEDBACK)
    a = app(port=pty_device.path, flow=FLOW)
    to_instructions(a, page)
    text = page.inner_text("#task-instructions")
    for words in ("F", "J", "left", "right", "guess", "last press counts"):
        assert words in text, words
    assert page.inner_text("#task-side-left") == "F\ntilted left"
    assert page.inner_text("#task-side-right") == "J\ntilted right"
    assert page.inner_text("#task-block") == "Practice: 6 trials"
    assert page.locator("#practice, #advance").count() == 0  # Enter starts the practice
    folder = session_folder_of(a)
    plan = ("correct", "wrong", "miss", "correct", "correct", "correct")
    practise(page, folder, plan)
    assert server_state(a)["phase"] == "waiting"  # practice runs before the block's timer
    trials = named(folder, "trial")
    assert [t["payload"]["trial"] for t in trials] == list(range(6))
    for t, how in zip(trials, plan, strict=True):
        p = t["payload"]
        assert p["practice"] is True and p["scored"] is False and p["status"] == "done"
        assert p["tilt_deg"] == 15 and p["staircase_trial"] is None
        assert "reversal" not in p  # practice does not move the staircase
        assert p["correct"] is (how == "correct")
        if how == "miss":
            assert p["response"] is None and p["key"] is None and p["rt_ms"] is None
        else:
            assert p["key"] in ("KeyF", "KeyJ") and 0 <= p["rt_ms"] < 1500
            assert p["response"] == ("left" if p["key"] == "KeyF" else "right")
            assert (p["response"] == p["side"]) is (how == "correct")
    feedback = page.evaluate("window.__feedback")
    assert [f[1] for f in feedback] == [
        "Correct",
        "Incorrect",
        "No response",
        "Correct",
        "Correct",
        "Correct",
    ]
    assert "Practice is over" in page.inner_text("#task-ready")


# -- the scored trials: answers, staircase, timing, markers, behavior file --------------------------


def test_scripted_participant_staircase_timing_markers_and_behavior_file(app, pty_device, page):
    a = app(port=pty_device.path, flow=FLOW)
    to_instructions(a, page)
    folder = session_folder_of(a)
    practise(page, folder)
    plan = ["correct", "correct", "wrong", "correct", "correct", "correct", "miss", "correct"]
    pressed: dict[tuple[str, int], str] = {}
    for block in ("block1", "block2"):
        start(page, "block")
        page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]')
        trial = 0
        while True:
            target = (
                f'{AREA}[data-trial="{trial}"][data-practice="false"][data-awaiting="true"], '
                f"{BLOCK_OVER}"
            )
            page.wait_for_selector(target, timeout=20_000)
            if page.locator(BLOCK_OVER).count():
                break
            how = plan[len(pressed) % len(plan)]
            pressed[(block, trial)] = how
            respond(page, folder, block, trial, False, how)
            trial += 1
    page.click("#advance")  # the summary
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)

    all_events = events(folder)
    trials = [e for e in all_events if e["name"] == "perception.trial"]
    scored = [t["payload"] for t in trials if not t["payload"]["practice"]]
    done = [p for p in scored if p["status"] == "done"]
    # 2 s lead-in, trials of 3100-3500 ms: at least 12 in the 45 s block and 1 in the 8 s block;
    # none is cut short.
    assert len(done) >= 13 and len(done) == len(scored)
    # Every trial the script answered (or let pass) is logged with the expected outcome.
    for p in done:
        how = pressed[(p["block"], p["trial"])]
        assert p["correct"] is (how == "correct"), p
        if how == "miss":
            assert p["response"] is None and p["key"] is None and p["rt_ms"] is None
        else:
            assert p["key"] in ("KeyF", "KeyJ") and 0 <= p["rt_ms"] < 1500, p
            assert p["response"] == ("left" if p["key"] == "KeyF" else "right")
            assert (p["responses"], p["changed"]) == (1, False)
        assert p["feedback"] == {"correct": "Correct", "wrong": "Incorrect", "miss": "No response"}[how]
        assert p["gamma_corrected"] is False
        assert p["fixation_ms"] == 500 and 600 <= p["gap_ms"] <= 1000
        assert 0 <= p["phase_deg"] < 360 and p["side"] in ("left", "right")
        assert p["dpr"] == 1 and p["patch_px"] == round(0.3 * p["size_css"])
        assert p["canvas_px"] == round(p["size_css"] * p["dpr"])
        # drawn on the page's navy; the stripes rise to the white of --stim (contrast 1)
        assert (p["background_color"], p["peak_color"]) == ("#121826", "#ffffff")
        assert p["contrast"] == 1
        assert p["signed_tilt_deg"] == (-p["tilt_deg"] if p["side"] == "left" else p["tilt_deg"])
        # whole frames: requested and shown, measured from the frame period of this display
        assert p["frame_ms"] == pytest.approx(1000 / 60, abs=0.2)
        assert p["stimulus_frames"] == 12
        assert p["fixation_frames"] == round((p["gap_ms"] + 500) / p["fixation_frame_ms"])
        for f in ("frame_ms", "fixation_frame_ms", "stimulus_frame_ms"):
            assert p[f] == pytest.approx(1000 / 60, abs=0.2)
        assert p["late_frames"] == p["grating_late_frames"] + p["blank_late_frames"]
    # The staircase: every trial that reached its answer moved it, in order, across both blocks.
    moved = [p for p in scored if "reversal" in p]
    assert [p["staircase_trial"] for p in moved] == list(range(len(moved)))
    assert {p["block"] for p in moved} == {"block1", "block2"}
    want = staircase([p["correct"] for p in moved])
    for p, (tilt, reversal) in zip(moved, want, strict=True):
        assert p["tilt_deg"] == pytest.approx(tilt, rel=1e-9), p
        assert p["reversal"] is reversal, p
    assert any(p["reversal"] for p in moved)
    first2 = next(p for p in moved if p["block"] == "block2")
    last1 = [p for p in moved if p["block"] == "block1"][-1]
    assert first2["tilt_deg"] == pytest.approx(last1["next_tilt_deg"])  # carried on
    assert first2["staircase_trial"] == last1["staircase_trial"] + 1

    # Timing: from the browser times of the stimulus events of each finished trial.
    stims = {}
    stim_events = {}
    for e in all_events:
        if e["name"] == "perception.stim" and not e["payload"]["practice"]:
            p = e["payload"]
            key = (p["block"], p["trial"], p["part"])
            assert key not in stims, key  # each part is logged once (the cross too)
            stims[key] = e["browser_time"]
            stim_events[key] = p
    assert {k[2] for k in stims} == {"fixation", "grating", "blank", "feedback"}
    errors: dict[str, list[float]] = {"stimulus": [], "fixation": [], "onset": []}
    for p in done:
        k = (p["block"], p["trial"])
        on = {w: stims[(*k, w)] for w in ("fixation", "grating", "blank")}
        # the cross is on from the start of the gap until the grating: gap_ms + 500 ms
        errors["stimulus"].append(on["blank"] - on["grating"] - 200)
        errors["fixation"].append(on["grating"] - on["fixation"] - p["gap_ms"] - p["fixation_ms"])
        assert p["stimulus_ms_shown"] == on["blank"] - on["grating"]
        assert p["fixation_ms_shown"] == on["grating"] - on["fixation"]
        # the runner's schedule: the grating due fixation_frames after the cross, the blank
        # stimulus_frames after the grating; the trial keeps the late frames the runner logged
        grating = stim_events[(*k, "grating")]
        blank = stim_events[(*k, "blank")]
        assert grating["frames"] == p["fixation_frames"] and blank["frames"] == 12
        assert grating["due_ms"] == pytest.approx(
            on["fixation"] + p["fixation_frames"] * p["fixation_frame_ms"], abs=1e-6
        )
        assert blank["due_ms"] == pytest.approx(
            on["grating"] + 12 * p["stimulus_frame_ms"], abs=1e-6
        )
        assert (grating["late_frames"], blank["late_frames"]) == (
            p["grating_late_frames"],
            p["blank_late_frames"],
        )
        errors["onset"] += [on["grating"] - grating["due_ms"], on["blank"] - blank["due_ms"]]
        # the feedback comes in the frame nearest to 1500 ms after the grating, the window's end
        feedback = stim_events[(*k, "feedback")]
        assert feedback["due_ms"] == pytest.approx(on["grating"] + 1500, abs=1e-6)
        errors["onset"].append(stims[(*k, "feedback")] - feedback["due_ms"])
    print("timing errors (ms):", {w: [round(x, 1) for x in v] for w, v in errors.items()})
    for what, values in errors.items():
        assert max(abs(x) for x in values) <= TOLERANCE_MS, (what, values)
    # Reaction times are the response's browser time minus the grating's.
    resps = {
        (e["payload"]["block"], e["payload"]["trial"]): e
        for e in all_events
        if e["name"] == "perception.resp" and not e["payload"]["practice"]
    }
    for p in done:
        k = (p["block"], p["trial"])
        if p["rt_ms"] is not None:
            assert p["rt_ms"] == resps[k]["browser_time"] - stims[(*k, "grating")]
    check_markers(folder)

    # The behavior file: one row per trial event, the values as logged.
    path = folder / "behavior" / "perception.csv"
    wait_until(path.is_file, "behavior/perception.csv")
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(trials)
    for row, event in zip(rows, trials, strict=True):
        p = event["payload"]
        assert int(row["seq"]) == event["seq"] and row["sample"] != ""
        assert row["block"] == p["block"] and row["trial"] == str(p["trial"])
        assert row["correct"] == ("" if p["correct"] is None else json.dumps(p["correct"]))
        assert row["rt_ms"] == ("" if p["rt_ms"] is None else json.dumps(p["rt_ms"]))
        assert row["side"] == p["side"] and row["gamma_corrected"] == "false"
        assert math.isclose(float(row["tilt_deg"]), p["tilt_deg"])
    header = list(rows[0])
    assert header[: len(behavior.FIRST)] == behavior.FIRST
    for field in (
        "tilt_deg",
        "side",
        "response",
        "key",
        "correct",
        "rt_ms",
        "reversal",
        "staircase_trial",
        "phase_deg",
        "fixation_ms",
        "gap_ms",
        "dpr",
        "patch_px",
        "background_color",
        "peak_color",
        "late_frames",
        "frame_ms",
    ):
        assert field in header, field
    # no field was shadowed or renamed: no param_, result_ or payload_ columns
    assert not [h for h in header if h.startswith(("param_", "result_", "payload_"))], header


# -- pause, reload, skip ------------------------------------------------------------------------------


def test_pause_and_reload_keep_the_staircase_and_skip_leaves_from_inside(
    app, pty_device, page, tmp_path
):
    # Before the night of 2026-11-25 a resume went on with the block. Now a pause abandons the
    # block (its trials never go on) and the next block starts from its prompt; the staircase
    # still carries on across the pause and the reload.
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 14, 14, 60))
    to_instructions(a, page)
    folder = session_folder_of(a)
    practise(page, folder)
    start(page, "block")
    respond(page, folder, "block1", 0, False, "correct")
    respond(page, folder, "block1", 1, False, "correct")  # two right: down to 8 / 1.26
    awaiting(page, 2, False)
    page.evaluate("document.exitFullscreen()")  # a pause in the middle of trial 2
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_function("document.getElementById('task-area').dataset.awaiting === 'false'")
    page.click("#fs-return")
    start(page, "block")  # block 2, from its prompt
    respond(page, folder, "block2", 0, False, "wrong")  # up to 8 again
    awaiting(page, 1, False)
    page.reload()  # a reload in the middle of block 2's trial 1
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_selector("#fs-lost", state="visible")
    page.click("#fs-return")
    # Block 2 is abandoned too: block 3 starts from its prompt, its trials numbered from 0.
    start(page, "block")
    respond(page, folder, "block3", 0, False, "correct")
    respond(page, folder, "block3", 1, False, "correct")  # two right after the reload: down
    awaiting(page, 2, False)
    # Skip the round from inside the task during a trial: leaving full screen pauses the block,
    # and the pause overlay offers the skip.
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')

    scored = {
        (t["payload"]["block"], t["payload"]["trial"]): t["payload"]
        for t in named(folder, "trial")
        if not t["payload"]["practice"]
    }
    # The reloaded page may start a trial before the server's pause reaches it; that trial is
    # abandoned too.
    extra = sorted(k[1] for k in scored if k[0] == "block2" and k[1] >= 2)
    status = {k: (p["status"], p.get("reason")) for k, p in scored.items()}
    assert status == {
        ("block1", 0): ("done", None),
        ("block1", 1): ("done", None),
        ("block1", 2): ("abandoned", "fullscreen_lost"),
        ("block2", 0): ("done", None),
        ("block2", 1): ("abandoned", "page_closed"),
        **{("block2", i): ("abandoned", "page_reloaded") for i in extra},
        ("block3", 0): ("done", None),
        ("block3", 1): ("done", None),
        ("block3", 2): ("abandoned", "fullscreen_lost"),
    }
    down = 8 / 1.26
    # (staircase_trial, tilt shown, moved the staircase)
    want = {
        ("block1", 0): (0, 8.0, True),
        ("block1", 1): (1, 8.0, True),
        ("block1", 2): (2, down, False),  # abandoned: shown at the next tilt, did not move it
        ("block2", 0): (2, down, True),
        ("block2", 1): (3, 8.0, False),
        **{("block2", i): (3, 8.0, False) for i in extra},
        ("block3", 0): (3, 8.0, True),  # after the reload the staircase goes on where it was
        ("block3", 1): (4, 8.0, True),
        ("block3", 2): (5, down, False),
    }
    for k, (index, tilt, moved) in want.items():
        p = scored[k]
        assert p["staircase_trial"] == index, (k, p)
        assert p["tilt_deg"] == pytest.approx(tilt), (k, p)
        assert ("reversal" in p) is moved, (k, p)
    assert scored[("block2", 0)]["reversal"] is True  # down then up
    assert scored[("block3", 1)]["reversal"] is True  # up then down
    assert scored[("block3", 1)]["next_tilt_deg"] == pytest.approx(down)
    check_markers(folder)


@pytest.mark.parametrize("size, dpr", [((1024, 768), 1), ((1920, 1080), 2)])
def test_layout_fits_the_window_and_the_canvas_is_drawn_at_device_pixels(
    app, pty_device, browser, tmp_path, size, dpr
):
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 6, 6))
    context = browser.new_context(
        viewport={"width": size[0], "height": size[1]}, device_scale_factor=dpr
    )
    try:
        page = context.new_page()
        to_instructions(a, page)
        page.wait_for_function("document.fonts.status === 'loaded'")

        def inside(selector):
            box = page.locator(selector).bounding_box()
            assert box, selector
            assert box["x"] >= 0 and box["y"] >= 0, (selector, box)
            assert box["x"] + box["width"] <= size[0], (selector, box)
            assert box["y"] + box["height"] <= size[1], (selector, box)
            return box

        def no_scroll():
            w, h = page.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
            )
            assert w <= size[0] and h <= size[1], (w, h)

        for selector in ("#task-instructions", "#task-enter", "#task-side-left", "#task-side-right"):
            inside(selector)
        no_scroll()
        folder = session_folder_of(a)
        start(page, "practice")
        awaiting(page, 0, True)
        panel = inside("#task-panel")
        canvas = inside("#perception-canvas")
        left, right = inside("#task-side-left"), inside("#task-side-right")
        no_scroll()
        # the canvas fills the stimulus area, which is centered between the key labels
        for edge in ("x", "y", "width", "height"):
            assert canvas[edge] == pytest.approx(panel[edge], abs=0.5), (edge, canvas, panel)
        # the canvas is the page's own ground at its edges and corners (no square shows), and the
        # stimulus area has no ground of its own
        ground = page.evaluate(
            """() => {
              const c = document.getElementById('perception-canvas');
              const ctx = c.getContext('2d'), w = c.width, h = c.height;
              const at = (x, y) => Array.from(ctx.getImageData(x, y, 1, 1).data);
              return {body: getComputedStyle(document.body).backgroundColor,
                      area: getComputedStyle(document.getElementById('task-panel')).backgroundColor,
                      px: [at(0, 0), at(w - 1, 0), at(0, h - 1), at(w - 1, h - 1),
                           at(Math.floor(w / 2), 0), at(0, Math.floor(h / 2))]};
            }"""
        )
        assert ground["body"] == "rgb(18, 24, 38)" and ground["area"] == "rgba(0, 0, 0, 0)"
        assert all(px == [18, 24, 38, 255] for px in ground["px"]), ground
        assert abs(panel["x"] + panel["width"] / 2 - size[0] / 2) <= 1
        assert left["x"] + left["width"] <= panel["x"]
        assert panel["x"] + panel["width"] <= right["x"]
        respond(page, folder, "block1", 0, True, "correct")
        trial = wait_until(lambda: named(folder, "trial"), "the first trial")[0]["payload"]
        assert trial["dpr"] == dpr
        assert trial["size_css"] == pytest.approx(canvas["width"], abs=0.5)
        assert trial["canvas_px"] == round(trial["size_css"] * dpr)
        assert trial["patch_px"] == round(0.3 * trial["size_css"])
        width = page.evaluate("document.getElementById('perception-canvas').width")
        assert width == trial["canvas_px"]
        # every other phase: the rest of practice, the trials, between the blocks, the summary
        for i in range(1, 6):
            respond(page, folder, "block1", i, True, "correct")
        page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=15_000)
        no_scroll()
        start(page, "block")
        awaiting(page, 0, False)
        for selector in ("#task-panel", "#perception-canvas", "#task-side-left", "#task-side-right"):
            inside(selector)
        no_scroll()
        page.wait_for_selector(BLOCK_OVER, timeout=20_000)
        no_scroll()
        start(page, "block")
        awaiting(page, 0, False)
        no_scroll()
        page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
        no_scroll()
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=20_000)
        no_scroll()
    finally:
        context.close()


# -- a press during the grating, a trial cut short by the block end --------------------------------


def test_a_press_during_the_grating_counts_no_trial_is_cut_short_and_no_event_is_refused(
    app, pty_device, page, tmp_path
):
    received: list[dict] = []

    def on_socket(ws):
        def on_frame(payload):
            try:
                received.append(json.loads(payload))
            except (TypeError, ValueError):
                pass

        ws.on("framereceived", on_frame)

    page.on("websocket", on_socket)
    warnings: list[str] = []
    page.on("console", lambda m: warnings.append(m.text) if m.type == "warning" else None)
    # 20 s blocks: with the 2 s lead-in and trials of 3100-3500 ms, at least 5 trials a block, so
    # some presses land while the 200 ms grating is still on (8 s blocks hold one trial each now).
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 20, 20))
    to_instructions(a, page)
    folder = session_folder_of(a)
    practise(page, folder)
    for _block in ("block1", "block2"):
        start(page, "block")
        page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]')
        trial = 0
        while True:
            target = (
                f'{AREA}[data-trial="{trial}"][data-practice="false"][data-awaiting="true"], '
                f"{BLOCK_OVER}"
            )
            page.wait_for_selector(target, timeout=20_000)
            if page.locator(BLOCK_OVER).count():
                break
            page.keyboard.press("f")  # at once: the grating is still on (200 ms)
            trial += 1
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')

    all_events = events(folder)
    scored = [
        e["payload"]
        for e in all_events
        if e["name"] == "perception.trial" and not e["payload"]["practice"]
    ]
    stims = {
        (e["payload"]["block"], e["payload"]["trial"], e["payload"]["part"]): e["browser_time"]
        for e in all_events
        if e["name"] == "perception.stim" and not e["payload"]["practice"]
    }
    resps = [e for e in all_events if e["name"] == "perception.resp" and not e["payload"]["practice"]]
    # Presses made while the grating was on are responses (not stray keys), with rt_ms < 200.
    during = [
        r
        for r in resps
        if r["browser_time"] < stims[(r["payload"]["block"], r["payload"]["trial"], "blank")]
    ]
    assert during, [r["payload"]["rt_ms"] for r in resps]
    for r in during:
        assert 0 <= r["payload"]["rt_ms"] < 200
        k = (r["payload"]["block"], r["payload"]["trial"])
        # the press does not cut the grating short: it stays its 12 frames
        assert stims[(*k, "blank")] - stims[(*k, "grating")] == pytest.approx(200, abs=1)
        record = next(
            p
            for p in scored
            if (p["block"], p["trial"]) == (r["payload"]["block"], r["payload"]["trial"])
        )
        assert record["key"] == "KeyF" and record["response"] == "left"
        assert record["rt_ms"] == r["payload"]["rt_ms"]
    assert all(p["rt_ms"] is None or p["rt_ms"] >= 0 for p in scored)
    # Before the night of 2026-11-25 the trial in progress at each block's end was cut short. Now
    # a trial that cannot end before the block's end is not started: every trial is done.
    for block in ("block1", "block2"):
        mine = [p for p in scored if p["block"] == block]
        assert mine and all(p["status"] == "done" and p["scored"] for p in mine)
    moved = [p for p in scored if "reversal" in p]
    assert [p["staircase_trial"] for p in moved] == list(range(len(moved)))
    last1 = [p for p in moved if p["block"] == "block1"][-1]
    first2 = next(p for p in moved if p["block"] == "block2")
    assert first2["staircase_trial"] == last1["staircase_trial"] + 1
    assert first2["tilt_deg"] == pytest.approx(last1["next_tilt_deg"])
    # Every event of the task in both full blocks was taken: no error from the server, every
    # event written acknowledged, none waiting in the page, and the page's counter has no gap.
    assert [m for m in received if not no_error(m)] == [], received
    ours = [e for e in all_events if e["name"].startswith("perception.")]
    acked = {m["cid"] for m in received if m.get("t") == "ack" and "refused" not in m}
    assert {e["payload"]["cid"] for e in ours} <= acked
    assert not [m for m in received if m.get("t") == "ack" and "refused" in m]
    counters = sorted(int(e["payload"]["cid"].rsplit("-", 1)[1]) for e in events(folder) if "cid" in e["payload"])
    assert counters == list(range(1, len(counters) + 1))
    page.wait_for_function(  # every event acknowledged: no outbox left in the page
        "!Object.keys(sessionStorage).some((k) => k.startsWith('brain-mapper-task/outbox/'))"
    )
    assert not warnings, warnings  # no event name or key the runner warns about
    for e in ours:
        for key in [*e["payload"], e["payload"].get("part", "stim")]:
            assert NAME.fullmatch(key), (key, e)


# -- a trial that ends after the block's end -----------------------------------------------------------


def test_a_trial_that_cannot_end_before_the_block_end_is_not_started_even_with_late_messages(
    app, pty_device, page, tmp_path
):
    # Before the night of 2026-11-25 this test held the server's messages back around the end of
    # block 1 so that the trial in progress ended after the block's end and was logged cut short.
    # Now the page starts a trial only when it ends before the block's end, by its own clock: with
    # the server's messages held back from the first trial until after the block's end, every
    # trial of block 1 is done, none is cut short, the page says its trials are done
    # (block_finished), and the staircase goes on in block 2.
    hold: dict = {"on": False, "queue": []}
    sent: list[dict] = []

    def route(ws):
        server = ws.connect_to_server()

        def from_server(message):
            if hold["on"]:
                hold["queue"].append(message)
            else:
                ws.send(message)

        def from_page(message):
            try:
                sent.append(json.loads(message))
            except (TypeError, ValueError):
                pass
            server.send(message)

        server.on_message(from_server)
        ws.on_message(from_page)
        hold["page"] = ws

    page.route_web_socket(re.compile(r"/ws\?"), route)

    def pumping(check):
        # The route's handlers (both directions) run only while this thread is inside a
        # Playwright call; a wait made of file reads and time.sleep would leave the page's own
        # messages unforwarded, so every look first gives Playwright 20 ms to dispatch them.
        def look():
            page.wait_for_timeout(20)
            return check()

        return look

    a = app(port=pty_device.path, flow=flow_with(tmp_path, 12, 60))
    to_instructions(a, page)
    folder = session_folder_of(a)
    practise(page, folder)
    start(page, "block")
    awaiting(page, 0, False)
    state = server_state(a, lambda m: m.get("phase") == "running")
    hold["on"] = True
    over = time.monotonic() + state["remaining"] + 1.0
    trial = 0

    def play():
        # Answer every trial the page starts, until a second after the block's end.
        nonlocal trial
        waiting = f'{AREA}[data-trial="{trial}"][data-practice="false"][data-awaiting="true"]'
        if page.locator(waiting).count():
            page.keyboard.press("f")
            trial += 1
        return time.monotonic() > over

    wait_until(pumping(play), "the end of block 1", timeout=30)
    assert [m for m in sent if m.get("cmd") == "block_finished"] == [
        {"cmd": "block_finished", "reason": "trials_done"}
    ]
    hold["on"] = False
    for message in hold["queue"]:
        hold["page"].send(message)
    hold["queue"].clear()
    start(page, "block")
    awaiting(page, 0, False)
    respond(page, folder, "block2", 0, False, "correct")
    records = wait_until(
        pumping(lambda: (
            [t["payload"] for t in named(folder, "trial") if not t["payload"]["practice"]]
            if any(t["payload"]["block"] == "block2" for t in named(folder, "trial"))
            else None
        )),
        "the first trial of block 2",
    )
    block1 = [p for p in records if p["block"] == "block1"]
    assert len(block1) == trial >= 2
    assert all(p["status"] == "done" and p["scored"] for p in block1)
    assert not [p for p in records if p["status"] == "cut_short"]
    first2 = next(p for p in records if p["block"] == "block2")
    assert first2["staircase_trial"] == block1[-1]["staircase_trial"] + 1
    assert first2["tilt_deg"] == pytest.approx(block1[-1]["next_tilt_deg"])


# -- a continued session with the same seed in the same tab --------------------------------------


def test_a_continued_session_with_the_same_seed_starts_the_staircase_at_8(
    app, pty_device, page, tmp_path
):
    port = free_port()  # the same origin both times: the tab keeps its sessionStorage
    # A round before perception, skipped, so that the stopped session can be continued (a session
    # is offered for continuing once it has played or skipped a round).
    raw = json.loads(flow_with(tmp_path, 60, 60).read_text())
    raw.insert(1, {"id": "example", "kind": "task", "title": "Example", "blocks": [
        {"id": "block1", "seconds": 5, "points": 100}
    ]})
    flow = tmp_path / "example-then-perception.json"
    flow.write_text(json.dumps(raw))
    first = app(port=pty_device.path, flow=flow, http_port=port)
    begin(first, page)
    advance_until(page, "example", "waiting")
    page.click("#skip-round")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-step="perception"][data-phase="waiting"]')
    page.wait_for_selector("#task-instructions", state="visible")
    folder1 = session_folder_of(first)
    seed = server_state(first)["seed"]
    practise(page, folder1)
    start(page, "block")
    respond(page, folder1, "block1", 0, False, "correct")
    respond(page, folder1, "block1", 1, False, "correct")  # down to 8 / 1.26
    awaiting(page, 2, False)
    # Session A ends with Ctrl-C in block 1, without a pause: the cut block is not played, so the
    # round can be played again. (Until 2026-11-25 it was stopped from the pause screen:
    # 'page.evaluate("document.exitFullscreen()")', then '#fs-stop-here  # stopped in block 1: the
    # round can be played again'. Since then a pause ends block 1 and it counts as played, so the
    # round would be partial and not offered again; test_pause_next_block.py covers that case.)
    moved = [p for p in (t["payload"] for t in named(folder1, "trial")) if "reversal" in p]
    assert moved[-1]["next_tilt_deg"] == pytest.approx(8 / 1.26)
    page.goto("about:blank")  # the tab stays (and its sessionStorage); its sockets close first
    first.process.send_signal(signal.SIGINT)
    first.process.wait(timeout=30)
    wait_until(lambda: bindable(port), f"port {port} free again", timeout=120)
    assert not [e for e in events_of(folder1) if e["name"] == "paused"]

    device = PtyDevice(onhead_stream(), config_reply())
    try:
        second = app(port=device.path, flow=flow, http_port=port, data_dir=first.data_dir)
        assert second.url == first.url
        begin(second, page)  # the same tab
        # P001 again: the page offers to continue the earlier session, which keeps its seed.
        page.fill("#participant-label", "P001")
        page.wait_for_function(
            "document.getElementById('advance').textContent.startsWith('Continue from')"
        )
        page.click("#advance")  # the label is saved and the session goes on to perception
        page.wait_for_selector(f'{SCREEN}[data-step="perception"][data-phase="waiting"]')
        assert server_state(second)["seed"] == seed
        folder2 = next(
            p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p != folder1
        )
        page.wait_for_selector(f'{AREA}[data-ready="practice"], {AREA}[data-ready="block"]')
        if page.locator(f'{AREA}[data-ready="practice"]').count():
            practise(page, folder2)
        start(page, "block")
        page.wait_for_selector(f'{AREA}[data-practice="false"][data-awaiting="true"]')
        n = int(page.get_attribute(AREA, "data-trial"))
        respond(page, folder2, "block1", n, False, "correct")
        record = wait_until(
            lambda: [
                t["payload"] for t in named(folder2, "trial") if not t["payload"]["practice"]
            ],
            "the first scored trial of the second session",
        )[0]
        assert record["tilt_deg"] == 8.0 and record["staircase_trial"] == 0
        assert record["reversal"] is False
    finally:
        device.hang_up()

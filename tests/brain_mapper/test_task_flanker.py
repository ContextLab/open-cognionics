"""The flanker task (``web/js/tasks/flanker.js``) in a real Chromium, against the real server.

Each test starts ``brain-mapper`` on a pseudo-terminal device with the flanker flow
(``flows/flanker.json``, block lengths changed per test), and a scripted participant reads which
way the middle arrow points (``#flanker-field`` carries ``data-phase`` and ``data-target``) and
presses F or J, the wrong key, or nothing. The asserts read the DOM, the websocket messages,
``events.jsonl``, the recording's markers and ``behavior/flanker.csv``. Nothing waits on a fixed
sleep.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from test_engine import events_of, markers_of, session_folder_of
from test_participants import stop
from test_progress import device  # noqa: F401  (a fixture)
from test_shell_browser import SCREEN, advance_until, begin, free_port, server_state
from test_tasks_browser import wait_for_file

FLOW = Path(__file__).parent / "flows" / "flanker.json"
AREA = "#task-area"
PANEL = "#task-panel"
FIELD = "#flanker-field"
KEY = {"left": "f", "right": "j"}
CODE = {"left": "KeyF", "right": "KeyJ"}
OTHER = {"left": "right", "right": "left"}
#: The task's own fields of ``flanker.trial`` and the runner's answer fields.
FIELDS = {
    "congruent",
    "target",
    "flankers",
    "fixation_ms",
    "gap_ms",
    "response",
    "key",
    "rt_ms",
    "correct",
}


def flanker_flow(tmp_path: Path, seconds: list[float]) -> Path:
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [
        {"id": f"block{i + 1}", "seconds": s, "points": 100}
        for i, s in enumerate(seconds)
    ]
    path = tmp_path / "flanker.json"
    path.write_text(json.dumps(raw))
    return path


def to_instructions(a, page, step: str = "flanker") -> None:
    begin(a, page)
    advance_until(page, step, "waiting")
    page.wait_for_selector("#task-instructions", state="visible")


def awaiting_selector(trial: int, practice: bool) -> str:
    return (
        f'{AREA}[data-trial="{trial}"][data-practice="{str(practice).lower()}"]'
        '[data-awaiting="true"]'
    )


def act(page, how: str) -> str:
    """In a trial awaiting a key: presses the right key, the wrong one, or nothing ('miss').
    Returns the middle arrow's direction shown."""
    target = page.evaluate(
        "() => { const f = document.getElementById('flanker-field');"
        " return f.dataset.phase === 'arrows' ? f.dataset.target : null; }"
    )
    assert target in ("left", "right")
    if how == "correct":
        page.keyboard.press(KEY[target])
    elif how == "wrong":
        page.keyboard.press(KEY[OTHER[target]])
    return target


def practise(page, plan: list[str]) -> None:
    """Enter on the prompt starts the practice; each trial is answered as ``plan`` says and its
    feedback read; then the prompt for the block is up."""
    page.wait_for_selector(f'{AREA}[data-ready="practice"]')
    page.keyboard.press("Enter")
    expected = {"correct": "Correct", "wrong": "Incorrect", "miss": "No response"}
    for i, how in enumerate(plan):
        page.wait_for_selector(awaiting_selector(i, True), timeout=15_000)
        act(page, how)
        page.wait_for_selector(f'#task-feedback[data-trial="{i}"][data-practice="true"]', timeout=5_000)
        assert page.inner_text("#task-feedback") == expected[how]
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=15_000)


def start_block(page) -> None:
    """Enter on the prompt starts the block (the page sends ``advance``)."""
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=15_000)
    page.keyboard.press("Enter")


def cell(value) -> str:
    """A value as the behavior file writes it: null empty, a string as it is, else JSON."""
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def named(folder: Path, suffix: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == f"flanker.{suffix}"]


def where(e: dict) -> tuple:
    p = e["payload"]
    return (p["block"], p["trial"], p["practice"])


def samples_of(folder: Path) -> dict[int, int]:
    return {
        int(m["label"].rsplit("#", 1)[1]): m["sample"]
        for m in markers_of(folder / "eeg")
    }


def stims_by_trial(folder: Path) -> dict[tuple, dict[str, dict]]:
    """Per trial: its ``flanker.stim`` events by part, ``arrows_off`` (the arrows taken off at the
    end of the window) and ``blank``, the ``flanker.stim_off`` that took the feedback off (the
    start of the gap)."""
    out: dict[tuple, dict[str, dict]] = {}
    for e in events_of(folder):
        if e["name"] == "flanker.stim":
            out.setdefault(where(e), {})[e["payload"]["part"]] = e
        elif e["name"] == "flanker.stim_off":
            name = "blank" if e["payload"]["part"] == "feedback" else f"{e['payload']['part']}_off"
            out.setdefault(where(e), {}).setdefault(name, e)
    return out


def no_error(message: dict) -> bool:
    """Not an error, or the answer of a server without ``block_finished`` (not shown)."""
    return message.get("t") != "error" or "block_finished" in message.get("msg", "")


def frame_of(e: dict) -> float:
    return e["payload"]["frame_ms"]


# -- instructions and practice ----------------------------------------------------------------------


def test_instructions_then_practice_with_feedback(app, pty_device, page):
    a = app(port=pty_device.path, flow=FLOW)
    to_instructions(a, page)
    # The rule leads (since 2026-11-26; before, "MIDDLE arrow" inside one paragraph).
    text = page.inner_text("#task-instructions")
    assert text.startswith("Answer the direction of the middle arrow. Ignore the other arrows."), text
    assert "last press counts" in text
    # A white example row of five arrows, the middle one marked, on the instruction screen only.
    assert page.locator("#task-example svg.flanker-example-arrow").count() == 5
    assert page.inner_text("#task-example li:nth-child(3) .task-example-bottom") == "middle: J"
    assert page.inner_text("#task-side-left") == "F\nleft"
    assert page.inner_text("#task-side-right") == "J\nright"
    assert page.inner_text("#task-block") == "Practice: 6 trials"
    assert page.locator("#practice, #advance").count() == 0  # Enter starts the practice
    plan = ["correct", "wrong", "miss", "correct", "correct", "correct"]
    practise(page, plan)
    assert (
        server_state(a)["phase"] == "waiting"
    )  # practice runs before the block's timer
    assert "Practice is over" in page.inner_text("#task-ready")
    folder = session_folder_of(a)
    trials = named(folder, "trial")
    assert [t["payload"]["trial"] for t in trials] == list(range(6))
    for t, how in zip(trials, plan):
        p = t["payload"]
        assert FIELDS <= set(p)
        assert p["practice"] is True and p["scored"] is False and p["status"] == "done"
        assert p["correct"] is (how == "correct")
        if how == "miss":
            assert (p["response"], p["key"], p["rt_ms"]) == (None, None, None)
        else:
            answer = p["target"] if how == "correct" else OTHER[p["target"]]
            assert (p["response"], p["key"]) == (answer, CODE[answer])
            assert 0 < p["rt_ms"] < 1500
    assert sum(t["payload"]["congruent"] for t in trials) == 3
    stims = stims_by_trial(folder)
    for t, how in zip(trials, plan):
        parts = stims[where(t)]
        assert list(parts) == ["fixation", "arrows", "arrows_off", "feedback", "blank"]
        feedback, blank = parts["feedback"], parts["blank"]
        expected = {"correct": "Correct", "wrong": "Incorrect", "miss": "No response"}[how]
        assert t["payload"]["feedback"] == expected == feedback["payload"]["feedback"]
        # The feedback stays 500 ms (30 frames at 60 Hz), counted from its own frame.
        f = frame_of(blank)
        shown = blank["browser_time"] - feedback["browser_time"]
        assert abs(shown - 500) <= f / 2 + 1, shown
        assert blank["payload"]["late_frames"] == 0
        # Answered or not, the feedback comes on the frame nearest to 1500 ms after the arrows,
        # the frame that takes the arrows off.
        due = parts["arrows"]["browser_time"] + 1500
        assert feedback["payload"]["due_ms"] == pytest.approx(due)
        assert abs(feedback["browser_time"] - due) <= f / 2 + 1
        assert parts["arrows_off"]["browser_time"] == feedback["browser_time"]


def test_the_arrows_are_shapes_in_the_stimulus_panel(app, pty_device, page):
    a = app(port=pty_device.path, flow=FLOW)
    to_instructions(a, page)
    page.keyboard.press("Enter")
    page.wait_for_selector(awaiting_selector(0, True), timeout=15_000)
    drawn = page.evaluate(
        """() => {
          const f = document.getElementById('flanker-field');
          const svg = f.querySelector('svg');
          return {
            parent: f.parentElement.id,
            phase: f.dataset.phase,
            arrows: svg.querySelectorAll('.flanker-arrow').length,
            dirs: Array.from(svg.querySelectorAll('.flanker-arrow')).map((x) => x.dataset.dir),
            text: f.textContent.trim(),
            target: f.dataset.target, flankers: f.dataset.flankers,
            ink: getComputedStyle(svg.querySelector('.flanker-arrow')).fill,
            panel: getComputedStyle(f.parentElement).backgroundColor,
          };
        }"""
    )
    assert drawn["parent"] == "task-panel"
    assert drawn["phase"] == "arrows"
    assert drawn["arrows"] == 5
    assert drawn["text"] == ""  # no glyphs: the arrows are SVG shapes
    flank = drawn["flankers"]
    assert drawn["dirs"] == [flank, flank, drawn["target"], flank, flank]
    # White arrows (--stim) straight on the page's navy: the stimulus area has no ground.
    assert (drawn["ink"], drawn["panel"]) == ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)")


# -- a scripted block: correct, rt, misses, markers, behavior file, timing, delivery ---------------


def plan_for(i: int) -> str:
    return "miss" if i % 7 == 5 else "wrong" if i % 7 == 3 else "correct"


def test_a_scripted_participant_is_logged_scored_timed_and_written_out(
    app, pty_device, page, tmp_path
):
    # Every message the server sends the page: none may be an error (a refused event).
    received: list[dict] = []

    def on_socket(ws):
        ws.on("framereceived", lambda frame: received.append(json.loads(frame)))

    page.on("websocket", on_socket)
    # Nor any console warning or error (a payload key outside the naming rule is only a warning).
    console: list[str] = []
    page.on(
        "console",
        lambda m: console.append(f"{m.type}: {m.text}")
        if m.type in ("warning", "error")
        else None,
    )
    a = app(port=pty_device.path, flow=flanker_flow(tmp_path, [50, 3]))
    to_instructions(a, page)
    practise(page, ["correct"] * 6)
    start_block(page)
    between = f'{SCREEN}[data-phase="between"]'
    shown = {}
    i = 0
    while True:
        page.wait_for_selector(
            f"{awaiting_selector(i, False)}, {between}", timeout=15_000
        )
        if page.locator(between).count():
            break
        shown[i] = act(page, plan_for(i))
        i += 1
    assert "F" in page.inner_text("#task-instructions")  # the next block's instructions
    # Every event of the full block was acknowledged as stored; none was refused.
    page.wait_for_function(
        "async () => (await import('/static/js/tasks/runner.js')).pendingEvents() === 0",
        timeout=15_000,
    )
    assert [m for m in received if not no_error(m)] == []
    assert page.locator("#notice").is_hidden()
    # Block 2 (3 s): no trial fits after the lead-in, so none is started.
    start_block(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]')
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    assert [m for m in received if not no_error(m)] == []
    folder = session_folder_of(a)
    events = events_of(folder)
    ours = [e for e in events if e["name"].startswith("flanker.")]
    acked = {m["cid"] for m in received if m.get("t") == "ack"}
    assert all(
        e["payload"]["cid"] in acked for e in ours
    )  # each one stored and acknowledged
    trials = named(folder, "trial")
    block1 = [
        t
        for t in trials
        if t["payload"]["block"] == "block1" and not t["payload"]["practice"]
    ]
    done = [t for t in block1 if t["payload"]["status"] == "done"]
    # 2 s lead-in, then trials of 3200-3600 ms: 13 or 14 fit in 50 s, and none is cut short.
    assert len(done) in (13, 14)
    assert all(t["payload"]["status"] == "done" for t in block1)

    # Correct, response, key and rt_ms as the script answered.
    resps = {where(e): e for e in named(folder, "resp")}
    stims = stims_by_trial(folder)
    for t in done:
        p = t["payload"]
        how = plan_for(p["trial"])
        assert p["target"] == shown[p["trial"]]
        assert p["scored"] is True
        assert p["correct"] is (how == "correct")
        if how == "miss":
            assert (p["response"], p["key"], p["rt_ms"]) == (None, None, None)
            assert where(t) not in resps
        else:
            answer = p["target"] if how == "correct" else OTHER[p["target"]]
            assert (p["response"], p["key"]) == (answer, CODE[answer])
            resp = resps[where(t)]
            assert resp["payload"]["key"] == CODE[answer]
            assert (
                p["rt_ms"]
                == resp["browser_time"] - stims[where(t)]["arrows"]["browser_time"]
            )
            assert p["arrows_browser_ms"] == stims[where(t)]["arrows"]["browser_time"]
            assert 0 < p["rt_ms"] < 1500  # a script answers within a few ms
            assert (p["responses"], p["changed"]) == (1, False)

    # The trial list is the one the seeded generator gives for this block.
    seed = json.loads((folder / "progress.json").read_text())["seed"]
    expected = page.evaluate(
        """async (seed) => {
          const { rng } = await import('/static/js/tasks/rng.js');
          const L = await import('/static/js/tasks/flanker-logic.js');
          return L.blockTrials(rng(seed, 'flanker', 'block1', 'trials'));
        }""",
        seed,
    )
    for t in block1:
        p = t["payload"]
        want = expected[p["trial"]]
        assert {k: p[k] for k in want} == want

    # Markers: every flanker event has one, and the samples follow the order of the events.
    samples = samples_of(folder)
    assert all(e["seq"] in samples for e in ours)
    ordered = [samples[e["seq"]] for e in events if e["seq"] in samples]
    assert ordered == sorted(ordered) and ordered[-1] > ordered[0]
    for t in done:
        s = stims[where(t)]
        seqs = [s["fixation"]["seq"], s["arrows"]["seq"], s["blank"]["seq"]]
        if where(t) in resps:
            seqs.insert(2, resps[where(t)]["seq"])
        assert [samples[q] for q in seqs] == sorted(samples[q] for q in seqs)
        assert samples[seqs[-1]] > samples[seqs[0]]

    # Timing over the scored trials of the full block. Onsets: each change that had a due time is
    # on the frame nearest to it. Durations: fixation 500 ms, arrows 1500 ms whether a key was
    # pressed or not, feedback 500 ms, the gap to the next trial's cross (anchored to its end).
    onset_error = 0.0
    duration_error = {"fixation": 0.0, "arrows": 0.0, "gap": 0.0, "after_key": 0.0}
    # The gap is a whole number of frames, the nearest to gap_ms (700-1100 ms is not a multiple
    # of a frame): what is left after that rounding is the scheduling error.
    gap_residual = 0.0
    anchored = 0
    timeouts = 0
    for t in done:
        p = t["payload"]
        s = stims[where(t)]
        f = frame_of(s["arrows"])
        assert f == pytest.approx(1000 / 60, abs=1.0)  # measured on the page
        for e in (s["fixation"], s["arrows"], s["arrows_off"], s["feedback"], s["blank"]):
            if "due_ms" in e["payload"]:
                assert e["payload"]["late_frames"] == 0, e
                onset_error = max(
                    onset_error, abs(e["browser_time"] - e["payload"]["due_ms"])
                )
        assert "due_ms" in s["arrows"]["payload"]
        fixation = s["arrows"]["browser_time"] - s["fixation"]["browser_time"]
        duration_error["fixation"] = max(
            duration_error["fixation"], abs(fixation - 500)
        )
        # A key does not end the arrows: they stay the whole window.
        arrows = s["arrows_off"]["browser_time"] - s["arrows"]["browser_time"]
        duration_error["arrows"] = max(duration_error["arrows"], abs(arrows - 1500))
        off = s["arrows_off"]["payload"]
        assert off["due_ms"] == pytest.approx(s["arrows"]["browser_time"] + 1500)
        assert off["late_frames"] == 0, off
        feedback = s["blank"]["browser_time"] - s["feedback"]["browser_time"]
        duration_error["after_key"] = max(duration_error["after_key"], abs(feedback - 500))
        if where(t) not in resps:
            timeouts += 1
        after = stims.get(("block1", p["trial"] + 1, False))
        if after and "fixation" in after:
            gap = after["fixation"]["browser_time"] - s["blank"]["browser_time"]
            duration_error["gap"] = max(duration_error["gap"], abs(gap - p["gap_ms"]))
            frames = round(p["gap_ms"] / f)
            assert abs(gap - p["gap_ms"]) <= f / 2 + abs(gap - frames * f) + 0.01
            gap_residual = max(gap_residual, abs(gap - frames * f))
            if "due_ms" in after["fixation"]["payload"]:
                anchored += 1
    assert anchored >= 12  # the next cross is anchored to the end of the gap
    assert console == []
    assert timeouts >= 2  # the script misses every 7th trial
    print(
        f"worst onset error {onset_error:.2f} ms; worst duration error (ms): {duration_error}; "
        f"gap after rounding to whole frames {gap_residual:.2f} ms; {timeouts} timeouts, none late"
    )
    f = 1000 / 60
    assert onset_error <= f / 2 + 1, onset_error
    assert duration_error["fixation"] <= f / 2 + 1, duration_error  # 500 ms: 30 frames
    assert duration_error["arrows"] <= f / 2 + 1, duration_error  # 1500 ms: 90 frames
    assert duration_error["after_key"] <= f / 2 + 1, duration_error  # feedback 500 ms: 30 frames
    assert gap_residual <= f / 2 + 1, gap_residual

    # The behavior file has the trial events, with clean columns.
    path = folder / "behavior" / "flanker.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(trials)
    header = set(rows[0])
    assert FIELDS <= header
    assert {
        "fixation_browser_ms",
        "arrows_browser_ms",
        "arrows_off_ms",
        "feedback_off_ms",
    } <= header
    assert not [c for c in header if c.startswith(("param_", "result_"))]
    assert not [c for c in header if c.startswith("payload_")]
    for row, t in zip(rows, trials):
        p = t["payload"]
        assert int(row["seq"]) == t["seq"]
        assert row["sample"] == str(samples[t["seq"]])
        for key in (
            "correct",
            "response",
            "key",
            "rt_ms",
            "congruent",
            "target",
            "gap_ms",
        ):
            assert row[key] == cell(p[key]), (key, row[key], p[key])

    # The scoring function, given every logged record (practice, cut short and all), counts
    # what the script did in the done scored trials only.
    records = [t["payload"] for t in trials]
    assert not any(r["status"] == "cut_short" for r in records)
    assert any(r["practice"] for r in records)
    s = page.evaluate(
        "async (r) => (await import('/static/js/tasks/flanker-logic.js')).score(r)",
        records,
    )
    later = [
        t["payload"]
        for t in trials
        if t["payload"]["block"] != "block1" and t["payload"]["status"] == "done"
    ]
    assert all(
        p["response"] is None for p in later
    )  # nobody answered in block 2 (no trial fits in it)
    assert s["n_trials"] == len(done) + len(later)
    assert s["n_misses"] == sum(
        plan_for(t["payload"]["trial"]) == "miss" for t in done
    ) + len(later)
    for kind, congruent in (("congruent", True), ("incongruent", False)):
        mine = [t["payload"] for t in done if t["payload"]["congruent"] is congruent]
        mine += [p for p in later if p["congruent"] is congruent]
        right = sum(
            plan_for(p["trial"]) == "correct" for p in mine if p["block"] == "block1"
        )
        assert s[kind]["n"] == len(mine)
        assert s[kind]["n_correct"] == right


# -- layout ------------------------------------------------------------------------------------------


def inside(page) -> dict:
    """Every visible element of the task area, and the arrows, lie inside the viewport."""
    return page.evaluate(
        """() => {
          const out = [];
          for (const e of document.querySelectorAll('#task-area *')) {
            const r = e.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            if (r.left < -0.5 || r.top < -0.5 || r.right > innerWidth + 0.5 || r.bottom > innerHeight + 0.5) {
              out.push(e.id || e.className.baseVal || e.className || e.tagName);
            }
          }
          return {outside: out, scroll: document.documentElement.scrollHeight > innerHeight};
        }"""
    )


#: The center of the bounding box of the elements matching a selector, in viewport pixels.
CENTRE = """(sel) => {
  const rs = Array.from(document.querySelectorAll(sel)).map((e) => e.getBoundingClientRect());
  const left = Math.min(...rs.map((r) => r.left)), right = Math.max(...rs.map((r) => r.right));
  const top = Math.min(...rs.map((r) => r.top)), bottom = Math.max(...rs.map((r) => r.bottom));
  return [(left + right) / 2, (top + bottom) / 2];
}"""


@pytest.mark.parametrize("width,height", [(1024, 768), (1920, 1080)])
def test_layout_at_1024x768_and_1920x1080(app, pty_device, browser, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        a = app(port=pty_device.path, flow=FLOW)
        to_instructions(a, page)
        assert inside(page) == {"outside": [], "scroll": False}
        page.keyboard.press("Enter")
        page.wait_for_selector(
            f'{FIELD}[data-phase="fixation"][data-trial="0"]', timeout=15_000
        )
        cross = page.evaluate(CENTRE, ".flanker-fix")
        assert inside(page) == {"outside": [], "scroll": False}
        page.wait_for_selector(awaiting_selector(0, True), timeout=15_000)
        result = inside(page)
        assert page.locator("#task-side-left").is_visible() and page.locator("#task-side-right").is_visible()
        assert result == {"outside": [], "scroll": False}, result
        # The field fills the shared panel, which is centered across the screen.
        panel = page.locator(PANEL).bounding_box()
        box = page.locator(FIELD).bounding_box()
        for k in ("x", "y", "width", "height"):
            assert box[k] == pytest.approx(panel[k], abs=0.5), k
        assert abs(panel["x"] + panel["width"] / 2 - width / 2) < 1
        # The cross and the middle arrow are both at the center of the panel.
        centre = (panel["x"] + panel["width"] / 2, panel["y"] + panel["height"] / 2)
        middle = page.evaluate(CENTRE, '.flanker-arrow[data-index="2"]')
        for x, y in (cross, middle):
            assert abs(x - centre[0]) < 1 and abs(y - centre[1]) < 1, (x, y, centre)
        assert abs(middle[0] - cross[0]) < 0.5 and abs(middle[1] - cross[1]) < 0.5
        # The arrows are inside the panel, between the key labels.
        arrows = page.evaluate(
            "() => Array.from(document.querySelectorAll('.flanker-arrow'))"
            ".map((a) => { const r = a.getBoundingClientRect(); return [r.left, r.right]; })"
        )
        assert panel["x"] < min(r[0] for r in arrows)
        assert max(r[1] for r in arrows) < panel["x"] + panel["width"]
        left = page.locator("#task-side-left").bounding_box()
        right = page.locator("#task-side-right").bounding_box()
        assert left["x"] + left["width"] <= panel["x"] and panel["x"] + panel["width"] <= right["x"]
        page.keyboard.press("f")
        page.wait_for_selector('#task-feedback[data-trial="0"]', timeout=5_000)
        assert inside(page) == {"outside": [], "scroll": False}
        page.wait_for_selector(
            f'{AREA}[data-trial="0"][data-practice="true"] {PANEL}:empty', timeout=5_000
        )
        assert inside(page) == {"outside": [], "scroll": False}  # the blank gap
        page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=60_000)
        assert inside(page) == {"outside": [], "scroll": False}  # practice is over
    finally:
        context.close()


def test_the_task_screen_does_not_scroll_at_1024x768(app, pty_device, browser):
    """The page does not scroll at 1024x768, on the instructions or during the trials (the
    shared panel, the key legend and the leave controls fit)."""
    context = browser.new_context(viewport={"width": 1024, "height": 768})
    page = context.new_page()
    try:
        a = app(port=pty_device.path, flow=FLOW)
        to_instructions(a, page)
        assert inside(page)["scroll"] is False
        page.keyboard.press("Enter")
        page.wait_for_selector(awaiting_selector(0, True), timeout=15_000)
        assert inside(page)["scroll"] is False, page.evaluate(
            "document.documentElement.scrollHeight"
        )
    finally:
        context.close()


# -- keys outside the arrows -------------------------------------------------------------------------


def test_a_key_in_the_fixation_or_the_gap_is_a_stray_key_not_the_next_answer(
    app, pty_device, page, tmp_path
):
    a = app(port=pty_device.path, flow=flanker_flow(tmp_path, [60, 60]))
    to_instructions(a, page)
    practise(page, ["correct"] * 6)
    start_block(page)
    # Trial 0: F during the fixation cross, then nothing during the arrows.
    page.wait_for_selector(
        f'{FIELD}[data-phase="fixation"][data-trial="0"]', timeout=15_000
    )
    page.keyboard.press("f")
    page.wait_for_selector(
        awaiting_selector(0, False), timeout=5_000
    )  # still awaiting a key
    # Trial 1: answered, then J during the blank gap (the panel is empty).
    page.wait_for_selector(awaiting_selector(1, False), timeout=15_000)
    act(page, "correct")
    page.wait_for_selector(
        f'{AREA}[data-trial="1"][data-practice="false"] {PANEL}:empty', timeout=5_000
    )
    page.keyboard.press("j")
    # Trial 2: nothing pressed; the J of the gap must not answer it.
    page.wait_for_selector(awaiting_selector(2, False), timeout=15_000)
    page.wait_for_selector(awaiting_selector(3, False), timeout=15_000)
    folder = session_folder_of(a)
    scored = {
        t["payload"]["trial"]: t["payload"]
        for t in named(folder, "trial")
        if not t["payload"]["practice"]
    }
    strays = [
        e["payload"] for e in named(folder, "stray") if not e["payload"]["practice"]
    ]
    assert [(p["trial"], p["key"], p["awaiting"]) for p in strays] == [
        (0, "KeyF", False),
        (1, "KeyJ", False),
    ]
    resps = [
        e["payload"]["trial"]
        for e in named(folder, "resp")
        if not e["payload"]["practice"]
    ]
    assert resps == [1]
    for n in (0, 2):
        p = scored[n]
        assert (p["response"], p["key"], p["correct"], p["rt_ms"]) == (
            None,
            None,
            False,
            None,
        )
    assert scored[1]["correct"] is True


#: Records the time (``KeyboardEvent.timeStamp``) and code of every key pressed on the page.
RECORD_KEYS = """
window.__keys = [];
window.addEventListener('keydown', (e) => window.__keys.push([e.timeStamp, e.code]), true);
"""


def test_a_key_in_the_fixation_or_the_gap_does_not_change_their_durations(
    app, pty_device, page, tmp_path
):
    # A key changes no duration: the cross (500 ms), the arrows (the 1500 ms window) and the
    # blank gap after the feedback (gap_ms). In every trial F is pressed while the cross is up,
    # the right key while the arrows are up, and J in the blank gap. The page records when each key was pressed;
    # for every trial with a key inside its cross the arrows are due the whole number of frames
    # nearest to 500 ms after the cross's frame, and for every trial with a key inside its gap the
    # next cross is due the whole number of frames nearest to gap_ms after the blank's frame:
    # the schedule of a trial with no extra key.
    page.add_init_script(RECORD_KEYS)
    a = app(port=pty_device.path, flow=flanker_flow(tmp_path, [60, 60]))
    to_instructions(a, page)
    practise(page, ["correct"] * 6)
    start_block(page)
    count = 10
    for i in range(count):
        page.wait_for_selector(f'{FIELD}[data-phase="fixation"][data-trial="{i}"]', timeout=15_000)
        page.keyboard.press("f")
        page.wait_for_selector(awaiting_selector(i, False), timeout=15_000)
        act(page, "correct")
        page.wait_for_selector(
            f'{AREA}[data-trial="{i}"][data-practice="false"] {PANEL}:empty', timeout=5_000
        )
        page.keyboard.press("j")
    page.wait_for_selector(f'{FIELD}[data-phase="fixation"][data-trial="{count}"]', timeout=15_000)
    keys = [t for t, _ in page.evaluate("window.__keys")]
    folder = session_folder_of(a)
    stims = stims_by_trial(folder)
    trials = {where(t): t["payload"] for t in named(folder, "trial")}
    in_fixation = in_gap = 0
    for i in range(count):
        s = stims[("block1", i, False)]
        p = trials[("block1", i, False)]
        assert p["status"] == "done" and p["correct"] is True
        cross, arrows, blank = s["fixation"], s["arrows"], s["blank"]
        # The arrows stay the whole window whatever was pressed.
        assert abs(s["arrows_off"]["browser_time"] - arrows["browser_time"] - 1500) <= frame_of(arrows) / 2 + 1
        after = stims[("block1", i + 1, False)]["fixation"]
        if any(cross["browser_time"] <= k < arrows["browser_time"] for k in keys):
            in_fixation += 1
            f = frame_of(arrows)
            q = arrows["payload"]
            assert q["frames"] == round(p["fixation_ms"] / f)
            # within 1 ms: the frame period is a running median and may move by a hundredth of a
            # ms; a schedule moved by a key would be off by at least a frame
            assert q["due_ms"] == pytest.approx(cross["browser_time"] + q["frames"] * f, abs=1.0)
            shown = arrows["browser_time"] - cross["browser_time"]
            assert abs(shown - q["frames"] * f) <= q["late_frames"] * f + f / 2 + 1, shown
        if any(blank["browser_time"] <= k < after["browser_time"] for k in keys):
            in_gap += 1
            q = after["payload"]
            f = q["frame_ms"]
            assert "due_ms" in q  # the next cross is anchored to the end of this trial's gap
            frames = round(p["gap_ms"] / f)
            assert q["due_ms"] == pytest.approx(blank["browser_time"] + frames * f, abs=1.0)
            shown = after["browser_time"] - blank["browser_time"]
            assert abs(shown - frames * f) <= q["late_frames"] * f + f / 2 + 1, shown
    # Most keys land where they were aimed (a busy machine can push one past its part).
    assert in_fixation >= count // 2 and in_gap >= count // 2, (in_fixation, in_gap)


# -- pause and resume, skip from inside the task -----------------------------------------------------


def test_pause_mid_trial_abandons_the_block_then_skip_from_inside_the_task(
    app, pty_device, page, tmp_path
):
    # Before the night of 2026-11-25 a resume went on with the block. Now a pause abandons the
    # block: its trials never go on, and the next block starts from its prompt.
    a = app(port=pty_device.path, flow=flanker_flow(tmp_path, [12, 30]))
    to_instructions(a, page)
    practise(page, ["correct"] * 6)
    start_block(page)
    page.wait_for_selector(awaiting_selector(0, False), timeout=15_000)
    act(page, "correct")
    page.wait_for_selector(awaiting_selector(1, False), timeout=15_000)
    target = act(page, "miss")
    page.evaluate("document.exitFullscreen()")  # a pause in the middle of trial 1
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_function(
        "document.getElementById('task-area').dataset.awaiting === 'false'"
    )
    # Nothing of the trial stays under the pause overlay.
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0
    page.click("#fs-return")
    start_block(page)  # block 2, from its prompt
    page.wait_for_selector(awaiting_selector(0, False), timeout=15_000)
    act(page, "correct")
    page.wait_for_selector(awaiting_selector(1, False), timeout=15_000)
    page.keyboard.press(
        KEY[act(page, "miss")]
    )  # trial 1 of block 2 is answered, then paused in its gap
    page.wait_for_selector(
        f'{AREA}[data-trial="1"][data-practice="false"] {PANEL}:empty', timeout=5_000
    )
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    folder = session_folder_of(a)
    scored = [
        t["payload"] for t in named(folder, "trial") if not t["payload"]["practice"]
    ]
    assert [(p["block"], p["trial"], p["status"]) for p in scored] == [
        ("block1", 0, "done"),
        ("block1", 1, "abandoned"),
        ("block2", 0, "done"),
        ("block2", 1, "abandoned"),
    ]
    assert scored[1]["reason"] == "fullscreen_lost" and scored[1]["scored"] is False
    assert scored[1]["target"] == target and scored[1]["response"] is None
    # An abandoned trial answered before the pause keeps its answer and key.
    assert scored[3]["response"] == scored[3]["target"]
    assert scored[3]["key"] == CODE[scored[3]["target"]]
    assert scored[0]["correct"] is True and scored[2]["correct"] is True
    page.click("#advance")  # Finish: the behavior file is written
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]')
    path = folder / "behavior" / "flanker.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 10  # 6 practice + 4 scored


# -- two sessions with the same seed in one tab ------------------------------------------------------


def same_seed_flow(tmp_path: Path) -> Path:
    """The flanker task as the words step and as the delayed recall step: recall alone in a later
    session takes the seed of the session in which the words step was played."""

    def step(id_, kind, title):
        return {
            "id": id_,
            "kind": kind,
            "title": title,
            "module": "flanker",
            "blocks": [{"id": "block1", "seconds": 6, "points": 100}],
        }

    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        step("words", "task", "Words"),
        step("recall", "recall", "Delayed free recall"),
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "same_seed.json"
    path.write_text(json.dumps(raw))
    return path


def first_practice_target(page) -> str:
    page.wait_for_selector(f'{AREA}[data-ready="practice"]', timeout=15_000)
    page.keyboard.press("Enter")
    page.wait_for_selector(awaiting_selector(0, True), timeout=15_000)
    return page.evaluate("document.getElementById('flanker-field').dataset.target")


def test_a_later_session_with_the_same_seed_in_the_same_tab_starts_clean(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    port = free_port()
    flow_path = same_seed_flow(tmp_path)
    first = app(port=device().path, flow=flow_path, http_port=port)
    for step in ("words", "recall"):
        if step == "words":
            to_instructions(first, page, "words")
        else:
            advance_until(page, "recall", "waiting")
        target = first_practice_target(page)
        page.keyboard.press(KEY[target])
        for i, how in enumerate(["correct"] * 5, start=1):
            page.wait_for_selector(awaiting_selector(i, True), timeout=15_000)
            act(page, how)
        start_block(page)
        page.wait_for_selector(awaiting_selector(0, False), timeout=15_000)
        act(page, "correct")
        page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
    state_a = server_state(first)
    seed, session_a = state_a["seed"], state_a["session_id"]
    folder_a = session_folder_of(first)
    practice_a = [
        t["payload"]
        for t in named(folder_a, "trial")
        if t["payload"]["practice"] and t["step"] == "recall"
    ]
    stop(first)

    # The same tab, the same address: recall alone, in a new session with the same seed.
    second = app(
        port=device().path, flow=flow_path, http_port=port, data_dir=first.data_dir
    )
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="recall"]')
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    advance_until(page, "recall", "waiting")
    state_b = server_state(second)
    assert state_b["seed"] == seed and state_b["session_id"] != session_a
    # Practice is offered again and starts at trial 0, with the same material as before.
    assert first_practice_target(page) == practice_a[0]["target"]
    assert len(practice_a) == 6  # the first session played the recall step's practice

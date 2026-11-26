"""The 2-back task screen (``web/js/tasks/nback.js``) in a real Chromium, on the real server.

Each test starts ``brain-mapper`` on a pseudo-terminal device with the flow
``flows/nback.json`` (or a copy with other block lengths), and a scripted participant presses
F and J the way a person would. The expected answers come from the task's own letter lists,
rebuilt in the page from the session seed (``rng.js`` + ``nback-logic.js``). The checks read
``events.jsonl``, the recording's markers and ``behavior/nback.csv``; waits are on page state
(``#task-area`` data attributes) and files, never on fixed sleeps.

The letter is drawn in the shared stimulus panel (``#task-panel``); the fixation cross is the
panel's own ``::after`` content while the panel is empty (``css/tasks/nback.css``), so the cross
comes on in the frame that takes the letter off (the letter's ``nback.stim_off``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from brain_mapper import behavior
from test_engine import events_of, session_folder_of
from test_participants import stop
from test_progress import device  # noqa: F401  (a fixture)
from test_shell_browser import SCREEN, advance_until, begin, free_port, server_state
from test_skip_browser import start_screen_to_participant, type_label
from test_tasks_browser import practise, samples_of, wait_for_file

FLOW = Path(__file__).parent / "flows" / "nback.json"
EXAMPLE = Path(__file__).parent / "flows" / "example.json"
AREA = "#task-area"
#: One display frame (17 ms) plus 5 ms.
SLACK_MS = 22


def flow_with(tmp_path: Path, *seconds: float) -> Path:
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [
        {"id": f"block{i + 1}", "seconds": s, "points": 100} for i, s in enumerate(seconds)
    ]
    path = tmp_path / "nback.json"
    path.write_text(json.dumps(raw))
    return path


def to_instructions(a, page) -> None:
    begin(a, page)
    advance_until(page, "nback", "waiting")
    page.wait_for_selector("#task-instructions", state="visible")


def start(page, what: str) -> None:
    """Enter on the prompt: ``practice`` starts the practice, ``block`` the block."""
    page.wait_for_selector(f'{AREA}[data-ready="{what}"]', timeout=40_000)
    page.keyboard.press("Enter")


def lists_of(page, seed: int, block: str, label: str) -> list[dict]:
    """The letter list the runner gives the task for this block and label."""
    return page.evaluate(
        """async ([seed, block, label]) => {
          const { rng } = await import('/static/js/tasks/rng.js');
          const m = await import('/static/js/tasks/nback-logic.js');
          const r = rng(seed, 'nback', block, label);
          const gen = { int: r.int, pick: r.pick, shuffle: r.shuffle, float: r.next };
          return label === 'practice' ? m.practiceTrials(gen) : m.blockTrials(gen);
        }""",
        [seed, block, label],
    )


def awaiting(page, trial: int, practice: bool, timeout: float = 20_000) -> None:
    page.wait_for_selector(
        f'{AREA}[data-trial="{trial}"][data-practice="{str(practice).lower()}"]'
        '[data-awaiting="true"]',
        timeout=timeout,
    )


def right_key(trial: dict) -> str:
    return "j" if trial["target"] else "f"


def wrong_key(trial: dict) -> str:
    return "f" if trial["target"] else "j"


def play(page, trials: list[dict], practice: bool, plan: dict[int, str], first: int = 0,
         count: int | None = None) -> None:
    """Answers trials ``first`` .. ``first + count - 1``: right unless ``plan`` says
    ``none`` (no key), ``wrong``, or ``double`` (the wrong key, then the right one: the last
    press in the window counts)."""
    count = len(trials) - first if count is None else count
    for i in range(first, first + count):
        awaiting(page, i, practice)
        what = plan.get(i, "right")
        trial = trials[i]
        if what == "none" or not trial["needs_response"]:
            continue
        if what in ("wrong", "double"):
            page.keyboard.press(wrong_key(trial))
        if what != "wrong":
            page.keyboard.press(right_key(trial))


def named(folder: Path, suffix: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == f"nback.{suffix}"]


def expected(trial: dict, what: str) -> tuple:
    """(response, correct) for a trial answered as ``what``."""
    if not trial["needs_response"]:
        return None, None
    if what == "none":
        return None, False
    key = wrong_key(trial) if what == "wrong" else right_key(trial)
    response = "same" if key == "j" else "different"
    return response, what != "wrong"


def shown_as(listed: list[dict], i: int, start: int) -> dict:
    """Letter ``i`` of the list as shown in a run that started at ``start`` (the brief's rule,
    written out here independently of the module): after a resume the first two letters
    need no answer, and a letter is judged only against letters of its own run."""
    t = {**listed[i], "after_resume": False}
    if start > 0 and i - start < 2:
        t.update(after_resume=True, needs_response=False, target=False, lure=None)
    elif start > 0 and t["lure"] == "3-back" and i - 3 < start:
        t["lure"] = None
    return t


def check_trials(records: list[dict], trials: list[dict], plan: dict[int, str],
                 starts: dict[int, int] | None = None) -> None:
    for record in records:
        p = record["payload"]
        trial = shown_as(trials, p["trial"], (starts or {}).get(p["trial"], 0))
        for field in ("letter", "position", "needs_response", "target", "lure", "tail",
                      "after_resume"):
            assert p[field] == trial[field], (field, p)
        response, correct = expected(trial, plan.get(p["trial"], "right"))
        assert (p["response"], p["correct"]) == (response, correct), p
        if response is None:
            assert p["rt_ms"] is None and p["key"] is None
        else:
            assert 0 < p["rt_ms"] < 2500, p
            assert p["key"] == ("KeyJ" if response == "same" else "KeyF")


def check_block_end(folder: Path, block: str) -> None:
    """No stimulus after the block's end, and no trial cut short by it: a letter is shown only
    when its whole trial (3200 ms) fits before the end, so every trial of the block is done (or
    abandoned by a pause) and every done trial had its full 2400 ms window.

    Clocks: the server's `unix_time` is the wall clock, which can be stepped while a block runs,
    so "before the end" is judged on the server's monotonic clock (`server_mono_ns`)."""
    events = events_of(folder)
    ended = [e for e in events if e["name"] == "block_end" and e["payload"]["block"] == block]
    assert len(ended) == 1
    end_mono = ended[0]["server_mono_ns"]
    stims = [e for e in events if e["name"] in ("nback.stim", "nback.stim_off") and not e["payload"]["practice"]
             and e["payload"]["block"] == block]
    trials = [e["payload"] for e in events if e["name"] == "nback.trial"
              and not e["payload"]["practice"] and e["payload"]["block"] == block]
    # A block ended by a pause (2026-11-25 rule) ends when the server takes the pause, and the page
    # takes the letter of the trial the pause abandoned off after that: only that letter's
    # stim_off may come after the end. Every onset, and every other change, comes before it.
    interrupted = ended[0]["payload"].get("ended_by") == "interrupted"
    abandoned = {t["trial"] for t in trials if t["status"] == "abandoned"}
    for stim in stims:
        if interrupted and stim["name"] == "nback.stim_off" and stim["payload"]["trial"] in abandoned:
            continue
        assert stim["server_mono_ns"] < end_mono, stim
    assert not [t for t in trials if t["status"] == "cut_short"], trials
    for t in trials:
        if t["status"] == "done":
            assert t["window_ms"] == 2400, t


def check_timing(folder: Path, practice: bool, block: str) -> int:
    """Letter for 500 ms; onset k = first onset + k x 3200 ms (asked for exactly, `due_ms`), each
    within one frame + 5 ms; the feedback of an answerable letter from onset + 2400 ms to onset
    + 2900 ms. Returns the number of trials checked."""
    ours = [
        e for e in events_of(folder)
        if e["name"] in ("nback.stim", "nback.stim_off")
        and e["payload"]["practice"] == practice and e["payload"]["block"] == block
    ]
    done = {
        t["payload"]["trial"]: t["payload"] for t in named(folder, "trial")
        if t["payload"]["practice"] == practice and t["payload"]["block"] == block
        and t["payload"]["status"] == "done"
    }
    letters = {e["payload"]["trial"]: e for e in ours
               if e["name"] == "nback.stim" and e["payload"]["part"] == "letter"}
    trials = sorted(done)
    first = letters[trials[0]]["browser_time"]
    run_first = first
    for k, n in enumerate(trials):
        stim = letters[n]
        onset = stim["browser_time"]
        assert onset == done[n]["letter_browser_ms"]
        assert stim["payload"]["frame_ms"] > 0
        if k == 0:
            assert done[n]["scheduled_onset_ms"] is None and "due_ms" not in stim["payload"]
        else:
            # Anchored to the run's first onset: asked for at exactly first + k x 3200 ms.
            assert stim["payload"]["due_ms"] == run_first + k * 3200 == done[n]["scheduled_onset_ms"]
            assert stim["payload"]["late_frames"] == done[n]["letter_late_frames"]
        assert abs(onset - (first + k * 3200)) <= SLACK_MS, (n, onset - first - k * 3200)
        if k:
            gap = onset - letters[trials[k - 1]]["browser_time"]
            assert abs(gap - 3200) <= SLACK_MS, (n, gap)
        # The letter goes after 500 ms (the cross comes on), whatever was pressed.
        after = [e for e in ours if e["payload"]["trial"] == n and e is not stim
                 and e["browser_time"] >= onset]
        assert after, n
        off = after[0]
        assert off["name"] == "nback.stim_off" and off["payload"]["part"] == "letter", off
        assert abs(off["browser_time"] - onset - 500) <= SLACK_MS, (n, off)
        feedback = [e for e in after if e["payload"]["part"] == "feedback"]
        if done[n]["needs_response"]:
            # The feedback in the frame the window ends, for 500 ms; the next letter 300 ms later.
            shown, gone = feedback
            assert shown["name"] == "nback.stim" and gone["name"] == "nback.stim_off"
            assert shown["payload"]["feedback"] == done[n]["feedback"]
            assert abs(shown["browser_time"] - onset - 2400) <= SLACK_MS, (n, shown)
            assert abs(gone["browser_time"] - shown["browser_time"] - 500) <= SLACK_MS, (n, gone)
        else:
            assert feedback == [] and done[n]["feedback"] is None
    return len(trials)


def check_markers(folder: Path) -> None:
    events = events_of(folder)
    samples = samples_of(folder)
    ours = [e for e in events if e["name"].startswith("nback.")]
    assert ours
    for event in ours:
        assert event["seq"] in samples, event
    ordered = [samples[e["seq"]] for e in ours]
    assert ordered == sorted(ordered) and ordered[-1] > ordered[0]
    stims = {(e["payload"]["block"], e["payload"]["trial"], e["payload"]["practice"]): e
             for e in ours if e["name"] == "nback.stim" and e["payload"]["part"] == "letter"}
    for resp in (e for e in ours if e["name"] == "nback.resp"):
        p = resp["payload"]
        stim = stims[(p["block"], p["trial"], p["practice"])]
        assert samples[resp["seq"]] > samples[stim["seq"]]
        assert p["rt_ms"] == resp["browser_time"] - stim["browser_time"]


def check_behavior(folder: Path) -> None:
    trials = named(folder, "trial")
    path = folder / "behavior" / "nback.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    header, body = rows[0], rows[1:]
    assert len(body) == len(trials)
    for row, event in zip(body, trials):
        cells = dict(zip(header, row))
        assert int(cells["seq"]) == event["seq"]
        p = event["payload"]
        assert cells["letter"] == p["letter"]
        assert cells["correct"] == ("" if p["correct"] is None else json.dumps(p["correct"]))
        assert cells["response"] == ("" if p["response"] is None else p["response"])
        assert cells["rt_ms"] == ("" if p["rt_ms"] is None else json.dumps(p["rt_ms"]))
        if p["scored"]:
            # The runner's join: the letter's offset after its onset, in the CSV.
            assert abs(float(cells["letter_off_ms"]) - 500) <= SLACK_MS, cells
    assert set(behavior.FIRST) <= set(header)
    # Clean column names: no parameter or result was renamed by the runner.
    assert not [h for h in header if h.startswith(("param_", "result_"))], header


def watch_errors(page) -> list[str]:
    """The server's error messages to this page (a refused event is one), and the page's console
    warnings (an event key outside the naming rule gets one)."""
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type in ("warning", "error") else None)

    def frames(ws) -> None:
        # (A server without `block_finished` answers it with an error the page does not show.)
        ws.on(
            "framereceived",
            lambda f: errors.append(f) if '"error"' in str(f) and "block_finished" not in str(f) else None,
        )

    page.on("websocket", frames)
    return errors


def all_acknowledged(page) -> None:
    page.wait_for_function(
        "import('/static/js/tasks/runner.js').then((r) => r.pendingEvents() === 0)",
        timeout=20_000,
    )


def test_instructions_practice_and_a_full_block_are_timed_logged_and_scored(app, pty_device, page):
    a = app(port=pty_device.path, flow=FLOW)
    errors = watch_errors(page)
    to_instructions(a, page)
    # The rule in short lines (since 2026-11-26; before: one paragraph with "J if it
    # is the same", "F if it is different" and "The first two letters need no answer").
    text = page.inner_text("#task-instructions")
    assert text.split("\n")[:4] == [
        "Compare each letter with the letter two positions earlier.",
        "Same: J    Different: F",
        "Do not answer the first two letters.",
        "You can change your answer; your last press counts.",
    ], text
    # The worked example: a bracket joins each letter from the third on to the letter two before it.
    pairs = page.evaluate(
        """() => {
          const cells = [...document.querySelectorAll('#task-example li')];
          const mid = (e) => { const r = e.getBoundingClientRect(); return (r.left + r.right) / 2; };
          return cells.map((li, i) => {
            const s = getComputedStyle(li, '::before');
            if (s.content === 'none') return null;
            const right = mid(li), width = parseFloat(s.width);
            return Math.round(right - width - mid(cells[i - 2]));
          });
        }"""
    )
    assert pairs[:2] == [None, None]
    assert all(p is not None and abs(p) <= 5 for p in pairs[2:]), pairs
    # "Practice: 10 trials" sits with the Enter prompt, between the key labels.
    assert page.locator(".task-cue #task-block").count() == 1
    # One worked example before practice: what a 2-back match is, and that the first two
    # letters need no answer.
    example = page.inner_text("#task-example")
    assert example.split("\n")[:4] == ["B", "no answer", "K", "no answer"]
    assert "J: same as 2 back" in example and "F: K was 2 back" in example
    assert page.inner_text("#task-side-left") == "F\ndifferent"
    assert page.inner_text("#task-side-right") == "J\nsame"
    assert page.inner_text("#task-block") == "Practice: 10 trials"
    # No fixation cross under the Enter prompt (it would sit on the prompt's words).
    assert page.evaluate(
        "getComputedStyle(document.getElementById('task-panel'), '::after').content"
    ) == "none"
    assert page.locator("#practice, #advance").count() == 0  # Enter starts the practice
    seed = server_state(a)["seed"]
    practice = lists_of(page, seed, "block1", "practice")
    assert sum(t["target"] for t in practice) == 3

    # Practice: one wrong answer, one without answer, the rest right; feedback after each.
    start(page, "practice")
    practice_plan = {3: "wrong", 5: "none"}
    play(page, practice, True, practice_plan)
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=20_000)
    assert page.locator("#task-example").is_hidden()  # not needed any more
    assert server_state(a)["phase"] == "waiting"  # practice runs before the block's timer
    folder = session_folder_of(a)
    records = [t for t in named(folder, "trial") if t["payload"]["practice"]]
    assert [t["payload"]["trial"] for t in records] == list(range(10))
    assert all(t["payload"]["status"] == "done" and not t["payload"]["scored"] for t in records)
    check_trials(records, practice, practice_plan)
    feedback = [e["payload"] for e in named(folder, "stim") if e["payload"]["part"] == "feedback"]
    # Feedback for the 8 answerable letters only, with the trial's correctness.
    words = {True: "Correct", False: "Incorrect"}
    by_trial = {r["payload"]["trial"]: r["payload"] for r in records}
    assert [f["trial"] for f in feedback] == list(range(2, 10))
    for f in feedback:
        t = by_trial[f["trial"]]
        assert f["feedback"] == ("No response" if t["response"] is None else words[t["correct"]])
    assert check_timing(folder, True, "block1") == 10

    # The 50 s block: exactly 14 letters, all shown and scored; a miss, a wrong answer and a
    # trial with two key presses (the wrong key, then the right one: the last counts).
    trials = lists_of(page, seed, "block1", "trials")
    first14 = trials[:14]
    assert sum(t["target"] for t in first14) == 4
    assert sorted(t["lure"] for t in first14 if t["lure"]) == ["1-back", "3-back"]
    plan = {4: "none", 7: "wrong", 9: "double", 12: "none"}
    start(page, "block")
    play(page, trials, False, plan, count=14)
    # Block 2 starts from its prompt on the task screen, with its instructions.
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="between"] {AREA}[data-ready="block"]', timeout=30_000)
    assert page.inner_text("#task-instructions").startswith("Same as before")
    start(page, "block")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=60_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=15_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)

    block1 = [
        t for t in named(folder, "trial")
        if not t["payload"]["practice"] and t["payload"]["block"] == "block1"
    ]
    done = [t for t in block1 if t["payload"]["status"] == "done"]
    assert [t["payload"]["trial"] for t in done] == list(range(14))
    assert all(t["payload"]["scored"] and not t["payload"]["tail"] for t in done)
    # No 15th letter: it would not end before the block's end, so it is not started.
    assert len(block1) == 14
    shown = {s["payload"]["trial"] for s in named(folder, "stim")
             if not s["payload"]["practice"] and s["payload"]["block"] == "block1"}
    assert shown == set(range(14))
    check_trials(done, trials, plan)
    # Trial 9: both presses logged, the last one is the answer; no stray key.
    nine = done[9]["payload"]
    assert (nine["responses"], nine["changed"], nine["correct"]) == (2, True, True)
    assert nine["rt_first_ms"] < nine["rt_last_ms"] == nine["rt_ms"]
    assert [s["payload"] for s in named(folder, "stray") if not s["payload"]["practice"]] == []
    assert all(t["payload"]["stray_presses"] == 0 for t in done)
    assert check_timing(folder, False, "block1") == 14
    check_block_end(folder, "block1")
    check_block_end(folder, "block2")
    check_markers(folder)
    check_behavior(folder)
    # Every event of the task was taken: none refused, all acknowledged.
    all_acknowledged(page)
    assert errors == []
    assert all(t["payload"]["window_ms"] == 2400 and t["payload"]["frame_ms"] > 0 for t in done)

    # The scores of the block, from the logged records, against a count made here.
    got = page.evaluate(
        "async (records) => (await import('/static/js/tasks/nback.js')).scores(records)",
        [t["payload"] for t in block1],
    )
    answerable = [(i, t) for i, t in enumerate(first14) if t["needs_response"]]
    hits = sum(1 for i, t in answerable if t["target"] and plan.get(i, "right") in ("right", "double"))
    fas = sum(1 for i, t in answerable if not t["target"] and plan.get(i) == "wrong")
    assert got["targets"] == 4 and got["non_targets"] == 8
    assert got["hits"] == hits and got["false_alarms"] == fas
    assert got["no_response"] == 2


def play_until_the_block_ends(page, trials: list[dict], first: int) -> int:
    """Answers every letter from ``first`` on until the between-blocks screen shows; returns
    the number of the first trial not reached."""
    i = first
    while True:
        target = (
            f'{AREA}[data-trial="{i}"][data-practice="false"][data-awaiting="true"], '
            f'{SCREEN}[data-screen="between"]'
        )
        page.wait_for_selector(target, timeout=20_000)
        if page.locator(f'{SCREEN}[data-screen="between"]').count():
            return i
        if trials[i]["needs_response"]:
            page.keyboard.press(right_key(trials[i]))
        i += 1


def test_a_pause_abandons_the_letter_and_the_block_and_skip_leaves(
    app, pty_device, page, tmp_path
):
    # Before the night of 2026-11-25 a resume went on with the block (the two letters after it
    # needed no answer). Now a pause abandons the block: its letters never go on, and the next
    # block starts from its prompt and instructions.
    # Three blocks, so that a round left after two blocks ended by a pause is partly played
    # (a block a pause ends counts as played; with two blocks the round would be finished).
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 30, 20, 20))
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practice = lists_of(page, seed, "block1", "practice")
    listed = lists_of(page, seed, "block1", "trials")
    start(page, "practice")
    play(page, practice, True, {})
    start(page, "block")
    play(page, listed, False, {}, count=3)
    awaiting(page, 3, False)
    page.keyboard.press(right_key(listed[3]))  # trial 3 is answered and still in progress ...
    page.evaluate("document.exitFullscreen()")  # ... when the block is paused
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    # Nothing of the task under the pause overlay: no letter, no cross.
    assert page.evaluate(
        "(() => { const p = document.getElementById('task-panel');"
        " return [p.childElementCount, getComputedStyle(p, '::after').content]; })()"
    ) == [0, "none"]
    assert page.evaluate(  # and the paused page does not scroll
        "document.documentElement.scrollHeight <= innerHeight"
        " && document.documentElement.scrollWidth <= innerWidth"
    )
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"] {AREA}[data-ready="block"]', timeout=40_000)
    assert page.inner_text("#task-block") == "Block 2 of 3"
    folder = session_folder_of(a)
    records = [t for t in named(folder, "trial") if not t["payload"]["practice"]
               and t["payload"]["block"] == "block1"]
    by_trial = {t["payload"]["trial"]: t["payload"] for t in records}
    assert sorted(by_trial) == [0, 1, 2, 3]  # nothing after the pause
    # Trial 3: abandoned by the pause; its answer is kept as "same"/"different".
    three = by_trial[3]
    assert (three["status"], three["reason"]) == ("abandoned", "fullscreen_lost")
    assert three["response"] == ("same" if listed[3]["target"] else "different")
    assert three["key"] == ("KeyJ" if listed[3]["target"] else "KeyF")
    # `correct` is what `run` returns, so a trial abandoned before it returned has none;
    # the answer and the key are the runner's, kept for it.
    assert three["correct"] is None and not three["scored"]
    check_trials([t for t in records if t["payload"]["status"] == "done"], listed, {})
    check_block_end(folder, "block1")
    got = page.evaluate(
        "async (records) => (await import('/static/js/tasks/nback.js')).scores(records)",
        [t["payload"] for t in records],
    )
    # Letters 0 and 1 need no answer, 2 was answered right, 3 was abandoned.
    assert got["targets"] + got["non_targets"] == 1
    assert got["misses"] == 0 and got["false_alarms"] == 0 and got["no_response"] == 0

    # Block 2: paused in its first trial, then the round is skipped from the pause screen.
    start(page, "block")
    awaiting(page, 0, False)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    # Blocks 1 and 2 were played (each ended by a pause) and block 3 was not, so the round is
    # "partial" (the engine's word).
    assert server_state(a)["outcomes"]["nback"] == "partial"
    check_markers(folder)
    page.click("#advance")  # Finish
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]')
    wait_for_file(folder / "behavior" / "nback.csv")


@pytest.mark.parametrize("size", [(1024, 768), (1920, 1080)])
def test_the_layout_fits_the_viewport(app, pty_device, browser, size):
    width, height = size
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        a = app(port=pty_device.path, flow=FLOW)
        to_instructions(a, page)

        def inside(selector: str) -> dict:
            box = page.locator(selector).bounding_box()
            assert box is not None, selector
            assert box["x"] >= 0 and box["y"] >= 0, (selector, box)
            assert box["x"] + box["width"] <= width and box["y"] + box["height"] <= height, (
                selector, box,
            )
            return box

        def no_scroll() -> None:
            # The page never scrolls, in any phase.
            sw, sh = page.evaluate(
                "[document.documentElement.scrollWidth, document.documentElement.scrollHeight]"
            )
            assert sw <= width and sh <= height, (sw, sh)

        for selector in ("#task-title", "#task-instructions", "#task-example", "#task-enter", "#task-side-left", "#task-side-right"):
            inside(selector)
        no_scroll()
        start(page, "practice")
        awaiting(page, 0, True)
        page.wait_for_selector("#task-panel .nback-letter", state="visible")
        shown = page.evaluate("document.querySelector('.nback-letter')?.textContent ?? ''")
        assert shown == "" or (len(shown) == 1 and shown in "BCDFGHJKLMNPQRSTVWXZ"), shown
        box = inside("#task-panel")
        left, right = inside("#task-side-left"), inside("#task-side-right")
        assert abs(box["x"] + box["width"] / 2 - width / 2) <= 2
        assert abs(box["width"] - box["height"]) <= 1 and box["width"] >= 0.3 * min(width, height)
        # The key labels beside the panel, outside it.
        assert left["x"] + left["width"] <= box["x"] and right["x"] >= box["x"] + box["width"]
        no_scroll()
        letter = inside("#task-panel .nback-letter")
        assert letter["height"] <= box["height"]
        # Between letters the empty panel shows the fixation cross.
        page.wait_for_selector("#task-panel:empty", state="attached")
        assert page.evaluate(
            "getComputedStyle(document.getElementById('task-panel'), '::after').content"
        ) == '"+"'
        no_scroll()
    finally:
        context.close()


def test_a_continued_session_with_the_same_seed_in_the_same_tab_starts_the_task_clean(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    # Session A: the example round (so there is something to continue from), then the 2-back
    # practice and two letters of its first block, then a stop. Session B continues it in the
    # same tab with the same seed: the 2-back starts again from its practice and letter 0.
    flow_path = tmp_path / "flow.json"
    example = json.loads(EXAMPLE.read_text())[1]
    example["blocks"] = [{"id": "block1", "seconds": 2, "points": 100}]
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [{"id": "block1", "seconds": 30, "points": 100}]
    flow_path.write_text(json.dumps([raw[0], example, raw[1], raw[2]]))
    port = free_port()
    first = app(port=device().path, flow=flow_path, http_port=port)
    begin(first, page)
    advance_until(page, "example", "waiting")
    page.wait_for_selector("#task-instructions", state="visible")
    practise(page)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    advance_until(page, "nback", "waiting")
    state_a = server_state(first)
    seed = state_a["seed"]
    practice = lists_of(page, seed, "block1", "practice")
    listed = lists_of(page, seed, "block1", "trials")
    start(page, "practice")
    play(page, practice, True, {})
    start(page, "block")
    play(page, listed, False, {}, count=2)
    awaiting(page, 2, False)
    # Session A ends with Ctrl-C in the middle of the block, without a pause: the cut block is not
    # played, so the round is offered again. (Until 2026-11-25 it was stopped from the pause
    # screen: 'page.evaluate("document.exitFullscreen()")  # stopped from the pause screen', then
    # "#fs-stop-here". Since then a pause ends the block and it counts as played, so the round
    # would be done and not offered again; test_pause_next_block.py covers that case.)
    earlier = session_folder_of(first)
    page.goto("about:blank")  # the tab stays (and its sessionStorage); its sockets close first
    stop(first)
    assert not [e for e in events_of(earlier) if e["name"] == "paused"]

    second = app(port=device().path, flow=flow_path, http_port=port, data_dir=first.data_dir)
    start_screen_to_participant(second, page)
    type_label(page, "P001")
    page.wait_for_selector("#continue-offer", state="visible")
    page.click("#advance")
    advance_until(page, "nback", "waiting")
    state_b = server_state(second)
    assert state_b["seed"] == seed and state_b["session_id"] != state_a["session_id"]
    # Nothing of session A's progress: practice is offered again, from its first letter.
    start(page, "practice")
    awaiting(page, 0, True)
    newer = next(f for f in (first.data_dir / "sessions").iterdir() if f.is_dir() and f.name != earlier.name)
    page.wait_for_function(
        "import('/static/js/tasks/runner.js').then((r) => r.pendingEvents() === 0)",
        timeout=20_000,
    )
    stims = [e["payload"] for e in events_of(newer) if e["name"] == "nback.stim"]
    assert stims[0]["trial"] == 0 and stims[0]["practice"] is True
    assert stims[0]["letter"] == practice[0]["letter"]


WATCH_LETTERS = """
window.__letterAt = {};
new MutationObserver(() => {
  const area = document.getElementById('task-area');
  if (document.querySelector('#task-panel .nback-letter') && area) {
    const k = area.dataset.practice + ':' + area.dataset.trial;
    if (!(k in window.__letterAt)) window.__letterAt[k] = performance.now();
  }
}).observe(document.body, {subtree: true, childList: true});
"""


def press_at(page, trial: int, practice: bool, after_ms: float, key: str) -> None:
    """Presses `key` once the page clock is `after_ms` past when the letter of `trial` came up
    (checked every frame in the page)."""
    page.wait_for_function(
        "([k, ms]) => window.__letterAt[k] !== undefined"
        " && performance.now() >= window.__letterAt[k] + ms",
        arg=[f"{str(practice).lower()}:{trial}", after_ms],
        polling="raf",
        timeout=20_000,
    )
    page.keyboard.press(key)


def test_a_key_press_during_the_letter_does_not_shorten_it(app, pty_device, page, tmp_path):
    # Keys 50, 250 and 450 ms after the onset (while the letter is up) and none: every letter
    # stays up 500 ms (the whole number of frames nearest it, within 1 ms), in practice and in a
    # scored block, and each answer keeps its own response time.
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 30, 3))
    to_instructions(a, page)
    page.evaluate(WATCH_LETTERS)
    seed = server_state(a)["seed"]
    when = {2: 50, 3: 250, 4: 450}
    for practice in (True, False):
        listed = lists_of(page, seed, "block1", "practice" if practice else "trials")
        start(page, "practice" if practice else "block")
        for trial, ms in when.items():
            press_at(page, trial, practice, ms, right_key(listed[trial]))
        awaiting(page, 6, practice)  # trial 5: no key
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=40_000)
    all_acknowledged(page)
    events = events_of(session_folder_of(a))
    for practice in (True, False):
        ours = [e for e in events if e["name"] in ("nback.stim", "nback.stim_off")
                and e["payload"]["practice"] == practice and e["payload"]["block"] == "block1"]
        trials = {e["payload"]["trial"]: e["payload"] for e in events
                  if e["name"] == "nback.trial" and e["payload"]["practice"] == practice
                  and e["payload"]["status"] == "done"}
        for n in range(7):
            stim = next(e for e in ours if e["name"] == "nback.stim"
                        and e["payload"]["part"] == "letter" and e["payload"]["trial"] == n)
            off = next(e for e in ours if e["payload"]["trial"] == n and e is not stim
                       and e["browser_time"] >= stim["browser_time"])
            frame = stim["payload"]["frame_ms"]
            duration = off["browser_time"] - stim["browser_time"]
            # 500 ms is a whole number of frames at 60 Hz (30); `frame_ms` is rounded to 0.1 ms,
            # so 30 x frame_ms can be 1 ms off: the duration is checked against 500 ms itself.
            assert round(500 / frame) * frame == pytest.approx(500, abs=1.5), frame
            assert abs(duration - 500) <= 1, (practice, n, duration, frame)
            # ... and the next letter is not pushed back by the feedback after no answer (trial 5).
            assert stim["payload"].get("late_frames", 0) == 0, (practice, n, stim["payload"])
        for trial, ms in when.items():
            t = trials[trial]
            assert t["response"] == ("same" if t["target"] else "different") and t["correct"], t
            assert t["rt_ms"] == t["resp_browser_ms"] - t["letter_browser_ms"]
            assert ms - 1 <= t["rt_ms"] <= ms + 60, (trial, t["rt_ms"])
        assert trials[5]["response"] is None

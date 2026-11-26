"""The spatial memory task (``web/js/tasks/spatial.js``) in a real Chromium.

Each test starts ``brain-mapper`` on a pseudo-terminal device with the flow
``flows/spatial.json`` (block lengths changed where a test needs it), drives the page with the
keyboard as a participant would, and reads the DOM, the server's state and websocket messages,
``events.jsonl``, the recording's markers and ``behavior/spatial.csv``. The scripted participant
knows the right answer because it builds the same trial list from the session seed with the
task's own modules (``rng.js`` and ``spatial-logic.js``), the way the runner does. Nothing waits
on a fixed sleep.
"""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path

import pytest

import frame_probe
from test_engine import events_of, markers_of, session_folder_of
from test_participants import stop
from test_progress import device  # noqa: F401  (a fixture)
from test_shell_browser import SCREEN, advance_until, begin, free_port, server_state
from test_tasks_browser import wait_for_file, wait_until

FLOW = Path(__file__).parent / "flows" / "spatial.json"
AREA = "#task-area"
GRID = "#spatial-grid"
#: One display frame at 60 Hz plus 5 ms.
TOLERANCE_MS = 17 + 5
NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
PARAMS = ("load", "lit", "probe", "probe_lit", "gap_ms")


def spatial_flow(tmp_path: Path, seconds: float, blocks: int = 1) -> Path:
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [
        {"id": f"block{i + 1}", "seconds": seconds, "points": 100} for i in range(blocks)
    ]
    path = tmp_path / "spatial.json"
    path.write_text(json.dumps(raw))
    return path


def to_instructions(a, page, step: str = "spatial") -> None:
    begin(a, page)
    advance_until(page, step, "waiting")
    page.wait_for_selector("#task-instructions", state="visible")


def planned(page, seed: int, block: str, label: str, step: str = "spatial") -> list[dict]:
    """The trial list of ``block`` (``label`` "trials" or "practice") built from the seed."""
    return page.evaluate(
        """async ([seed, block, label]) => {
          const { rng } = await import('/static/js/tasks/rng.js');
          const L = await import('/static/js/tasks/spatial-logic.js');
          const g = rng(seed, 'spatial', block, label);
          return label === 'practice' ? L.practiceTrials(g) : L.blockTrials(g);
        }""",
        [seed, block, label],
    )


def awaiting(page, trial: int, practice: bool, timeout: float = 20_000) -> None:
    page.wait_for_selector(
        f'{AREA}[data-trial="{trial}"][data-practice="{str(practice).lower()}"]'
        '[data-awaiting="true"]',
        timeout=timeout,
    )


def right_key(t: dict) -> str:
    return "j" if t["probe_lit"] else "f"


def wrong_key(t: dict) -> str:
    return "f" if t["probe_lit"] else "j"


def answer(page, trial: int, practice: bool, key: str) -> None:
    awaiting(page, trial, practice)
    page.keyboard.press(key)


def in_part(page, trial: int, part: str, timeout: float = 20_000) -> None:
    page.wait_for_selector(
        f'{AREA}[data-trial="{trial}"] {GRID}[data-part="{part}"]', timeout=timeout
    )


def feedback_is(page, text: str) -> None:
    page.wait_for_function(
        "(text) => { const g = document.getElementById('spatial-grid');"
        " const f = document.getElementById('task-feedback');"
        " return !!g && g.dataset.part === 'feedback' && !!f && f.textContent === text; }",
        arg=text,
        timeout=20_000,
    )


def start(page, what: str, timeout: float = 30_000) -> None:
    """Wait for the Enter prompt of the practice or the block, then press Enter."""
    page.wait_for_selector(f'{AREA}[data-ready="{what}"]', timeout=timeout)
    page.keyboard.press("Enter")


def practise_right(page, trials: list[dict]) -> None:
    start(page, "practice")
    for i, t in enumerate(trials):
        answer(page, i, True, right_key(t))
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=20_000)


def named(folder: Path, suffix: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == f"spatial.{suffix}"]


def samples_of(folder: Path) -> dict[int, int]:
    return {
        int(m["label"].rsplit("#", 1)[1]): m["sample"] for m in markers_of(folder / "eeg")
    }


def key_of(event: dict) -> tuple:
    p = event["payload"]
    return (p["block"], p["trial"], p["practice"])


def by_part(folder: Path, suffix: str) -> dict[tuple, dict[str, dict]]:
    """``spatial.<suffix>`` events by trial and part; at most one per part and trial."""
    out: dict[tuple, dict[str, dict]] = {}
    for e in named(folder, suffix):
        part = e["payload"]["part"]
        assert part not in out.setdefault(key_of(e), {}), ("one per part and trial", e)
        out[key_of(e)][part] = e
    return out


def stop_session(page) -> None:
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-stop-here")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=20_000)


def check_markers(folder: Path) -> None:
    """Every spatial event has a marker, and the samples follow the order of the events."""
    events = events_of(folder)
    samples = samples_of(folder)
    ours = [e for e in events if e["name"].startswith("spatial.")]
    assert ours
    for event in ours:
        assert event["seq"] in samples, event
    ordered = [samples[e["seq"]] for e in events if e["seq"] in samples]
    assert ordered == sorted(ordered)
    assert ordered[-1] > ordered[0]


def server_messages(page) -> list[dict]:
    """Every message the server sends the page from now on (filled while the test runs)."""
    got: list[dict] = []

    def on_socket(ws):
        def on_frame(payload):
            try:
                got.append(json.loads(payload))
            except (TypeError, ValueError):
                got.append({"t": "unreadable", "raw": str(payload)})

        ws.on("framereceived", on_frame)

    page.on("websocket", on_socket)
    return got


def test_a_scripted_participant_is_logged_scored_and_written_out(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=spatial_flow(tmp_path, 60))
    to_instructions(a, page)
    assert page.evaluate("document.activeElement.id") == "task-area"
    sides = page.inner_text("#task-side-left") + " " + page.inner_text("#task-side-right")
    assert page.inner_text("#task-side-left") == "F\nnot filled"
    assert page.inner_text("#task-side-right") == "J\nfilled"
    text = page.inner_text("#task-instructions")
    # "Filled" squares, never "lit" (ambiguous on a dark ground).
    assert "filled" in text
    assert not re.search(r"\blit\b|light up", text + " " + sides)
    assert "J" in text and "F" in text and "last press counts" in text
    assert page.locator("#practice, #advance").count() == 0  # Enter starts the practice
    assert page.inner_text("#task-block") == "Practice: 3 trials"
    seed = server_state(a)["seed"]
    practice = planned(page, seed, "block1", "practice")
    assert [t["load"] for t in practice] == [2, 4, 2]

    # Practice: right, wrong, no answer; each gets 500 ms of feedback.
    start(page, "practice")
    answer(page, 0, True, right_key(practice[0]))
    feedback_is(page, "Correct")
    answer(page, 1, True, wrong_key(practice[1]))
    feedback_is(page, "Incorrect")
    awaiting(page, 2, True)
    feedback_is(page, "No response")
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=20_000)
    assert server_state(a)["phase"] == "waiting"  # practice runs before the block's timer
    folder = session_folder_of(a)
    got = named(folder, "trial")
    assert [t["payload"]["trial"] for t in got] == [0, 1, 2]
    for t, plan in zip(got, practice):
        p = t["payload"]
        assert {k: p[k] for k in PARAMS} == plan
        assert p["practice"] is True and p["scored"] is False and p["status"] == "done"
    assert [(t["payload"]["response"], t["payload"]["key"]) for t in got] == [
        ("yes", "KeyJ") if practice[0]["probe_lit"] else ("no", "KeyF"),
        ("no", "KeyF") if practice[1]["probe_lit"] else ("yes", "KeyJ"),
        (None, None),
    ]
    assert [t["payload"]["correct"] for t in got] == [True, False, False]
    assert got[2]["payload"]["rt_ms"] is None

    # The block: right, wrong, no answer, a key before the probe then right; stop in trial 4.
    trials = planned(page, seed, "block1", "trials")
    start(page, "block")
    answer(page, 0, False, right_key(trials[0]))
    answer(page, 1, False, wrong_key(trials[1]))
    awaiting(page, 2, False)  # trial 2 gets no answer
    in_part(page, 3, "lit")
    page.keyboard.press("f")  # before the probe: a stray key, not an answer
    answer(page, 3, False, right_key(trials[3]))
    awaiting(page, 4, False)
    stop_session(page)

    check_markers(folder)
    scored = [t for t in named(folder, "trial") if not t["payload"]["practice"]]
    assert [t["payload"]["trial"] for t in scored] == [0, 1, 2, 3, 4]
    assert [t["payload"]["status"] for t in scored] == [
        "done", "done", "done", "done", "abandoned",
    ]
    assert scored[4]["payload"]["reason"] == "fullscreen_lost"
    for t, plan in zip(scored, trials):
        assert {k: t["payload"][k] for k in PARAMS} == plan
    expected = [
        ("yes" if trials[0]["probe_lit"] else "no", True),
        ("no" if trials[1]["probe_lit"] else "yes", False),
        (None, False),
        ("yes" if trials[3]["probe_lit"] else "no", True),
    ]
    assert [(t["payload"]["response"], t["payload"]["correct"]) for t in scored[:4]] == expected
    assert [t["payload"]["scored"] for t in scored] == [True, True, True, True, False]

    stims = by_part(folder, "stim")
    offs = by_part(folder, "stim_off")
    resps = {key_of(e): e for e in named(folder, "resp")}
    for t in scored[:4] + got:
        k = key_of(t)
        s = stims[k]
        assert set(s) == {"lit", "probe", "feedback"} and set(offs[k]) == {"lit", "probe", "feedback"}
        assert s["feedback"]["payload"]["feedback"] == t["payload"]["feedback"]
        # The probe stays the whole 3 s window, answered or not; the feedback comes in that frame.
        f = s["probe"]["payload"]["frame_ms"]
        assert abs(offs[k]["probe"]["browser_time"] - s["probe"]["browser_time"] - 3000) <= f / 2 + 1
        assert s["feedback"]["browser_time"] == offs[k]["probe"]["browser_time"]
        assert s["lit"]["payload"]["lit"] == t["payload"]["lit"]
        assert s["lit"]["payload"]["load"] == t["payload"]["load"]
        assert s["probe"]["payload"]["probe"] == t["payload"]["probe"]
        assert s["probe"]["payload"]["probe_lit"] == t["payload"]["probe_lit"]
        # Both onsets and the lit offset were put on a frame by the runner, which logged when.
        for e in (s["lit"], s["probe"], offs[k]["lit"]):
            assert {"due_ms", "late_frames", "frame_ms"} <= set(e["payload"])
        assert s["lit"]["seq"] < offs[k]["lit"]["seq"] < s["probe"]["seq"] < t["seq"]
        assert t["payload"]["lit_browser_ms"] == s["lit"]["browser_time"]
        assert t["payload"]["probe_browser_ms"] == s["probe"]["browser_time"]
        if t["payload"]["response"] is None:
            assert k not in resps and t["payload"]["rt_ms"] is None
            continue
        r = resps[k]
        rt = t["payload"]["rt_ms"]
        # rt is counted from the probe onset, not from the lit squares.
        assert rt == r["browser_time"] - s["probe"]["browser_time"]
        assert r["payload"]["rt_ms"] == rt
        assert 0 < rt < 3000
        assert r["payload"]["key"] == t["payload"]["key"]
        assert r["payload"]["key"] == ("KeyJ" if t["payload"]["response"] == "yes" else "KeyF")
        assert (t["payload"]["responses"], t["payload"]["changed"]) == (1, False)
    # Trial 4 was abandoned before its probe could be answered.
    assert (scored[4]["payload"]["response"], scored[4]["payload"]["rt_ms"]) == (None, None)
    strays = named(folder, "stray")
    assert [(key_of(s), s["payload"]["key"], s["payload"]["awaiting"]) for s in strays] == [
        (("block1", 3, False), "KeyF", False)
    ]

    path = folder / "behavior" / "spatial.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    trial_events = named(folder, "trial")
    assert len(rows) == len(trial_events) == 8
    header = list(rows[0])
    # Clean columns: none of the task's names came out as param_, result_ or payload_. (The
    # runner's own block, trial and practice, in every trial payload, are written as payload_<key>
    # next to the fixed columns of the same name, for every task.)
    assert not [c for c in header if c.startswith(("param_", "result_"))]
    assert {c for c in header if c.startswith("payload_")} <= {
        "payload_block", "payload_trial", "payload_practice",
    }
    for column in ("lit_seq", "lit_off_ms", "probe_seq", "probe_off_ms", "load", "probe_lit", "key"):
        assert column in header
    by_seq = {int(r["seq"]): r for r in rows}
    for t in trial_events:
        row = by_seq[t["seq"]]
        p = t["payload"]
        assert row["response"] == (p["response"] or "")
        # null is an empty cell (the abandoned trial has correct: null)
        assert row["correct"] == ("" if p["correct"] is None else json.dumps(p["correct"]))
        assert row["rt_ms"] == ("" if p["rt_ms"] is None else json.dumps(p["rt_ms"]))
        assert json.loads(row["lit"]) == p["lit"]
        assert row["probe_lit"] == json.dumps(p["probe_lit"])
        assert row["sample"] == str(samples_of(folder)[t["seq"]])

    # The scoring function over the logged records: the 4 done trials of the block.
    summary = page.evaluate(
        """async (records) => {
          const L = await import('/static/js/tasks/spatial-logic.js');
          const { scoredRecords } = await import('/static/js/tasks/runner.js');
          return [L.score(records), L.score(scoredRecords(records))];
        }""",
        [t["payload"] for t in trial_events],
    )
    assert summary[0] == summary[1]
    assert summary[0]["n_trials"] == 4 and summary[0]["no_response"] == 1


def test_two_full_blocks_keep_their_timing_and_no_event_is_refused(app, pty_device, page, tmp_path):
    messages = server_messages(page)
    frame_probe.install(page)  # what the page did around a late change, for the message
    a = app(port=pty_device.path, flow=spatial_flow(tmp_path, 60, blocks=2))
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practise_right(page, planned(page, seed, "block1", "practice"))
    ends = {"block1": f'{SCREEN}[data-phase="between"] {AREA}[data-ready="block"]',
            "block2": f'{SCREEN}[data-screen="between"]'}
    for block in ("block1", "block2"):
        start(page, "block")
        trials = planned(page, seed, block, "trials")
        i = 0
        while True:
            # The next trial, or the end of the block (no trial is cut short by the clock).
            page.wait_for_selector(f"{AREA}[data-trial=\"{i}\"][data-practice=\"false\"][data-awaiting=\"true\"], {ends[block]}", timeout=20_000)
            if page.locator(ends[block]).count():
                break
            page.keyboard.press(right_key(trials[i]))
            i += 1
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=20_000)
    page.wait_for_function(
        "async () => (await import('/static/js/tasks/runner.js')).pendingEvents() === 0",
        timeout=20_000,
    )
    folder = session_folder_of(a)
    check_markers(folder)

    # No event was refused: no error from the server, every event acknowledged and written.
    # (A server without block_finished, sent when no more trials fit, answers it with an error
    # the page does not show.)
    assert [m for m in messages if m.get("t") in ("error", "unreadable")
            and "block_finished" not in m.get("msg", "")] == []
    acked = {m["cid"] for m in messages if m.get("t") == "ack" and "refused" not in m}
    ours = [e for e in events_of(folder) if e["name"].startswith("spatial.")]
    assert {e["payload"]["cid"] for e in ours} <= acked
    for e in ours:
        for key in e["payload"]:
            assert NAME.match(key), (e["name"], key)
        if "part" in e["payload"]:
            assert NAME.match(e["payload"]["part"])

    done = {
        key_of(t): t
        for t in named(folder, "trial")
        if not t["payload"]["practice"] and t["payload"]["status"] == "done"
    }
    # 2 s lead-in, then trials of 7800-8200 ms: 7 fit in each 60 s block.
    assert len(done) == 14
    stims = by_part(folder, "stim")
    offs = by_part(folder, "stim_off")
    # Every change put on a frame (spatial.lead_in only announces the lead-in: its due_ms is the
    # lead-in's end, logged with its late frames as spatial.lead_in_end).
    scheduled = [e for e in ours if "due_ms" in e["payload"] and e["name"] != "spatial.lead_in"]
    late = [e for e in scheduled if e["payload"]["late_frames"]]
    print(f"late changes: {len(late)} of {len(scheduled)} scheduled ({len(late) / len(scheduled):.2%})")
    errors = []
    for k, t in sorted(done.items()):
        p = t["payload"]
        assert p["correct"] is True
        lit, probe = stims[k]["lit"], stims[k]["probe"]
        lit_off = offs[k]["lit"]
        assert "probe" in offs[k]  # the probe is taken off at the end of its window
        probe_off, feedback, feedback_off = offs[k]["probe"], stims[k]["feedback"], offs[k]["feedback"]
        for e in (lit, probe, lit_off, probe_off, feedback, feedback_off):
            assert e["payload"]["late_frames"] == 0, (e, frame_probe.around(page, e["browser_time"]))
            errors.append((f"{e['name']} {e['payload']['part']} onset vs due",
                           e["browser_time"] - e["payload"]["due_ms"]))
        errors.append(("fixation + gap", lit["browser_time"] - p["fixation_browser_ms"] - p["gap_ms"] - 500))
        errors.append(("lit shown", lit_off["browser_time"] - lit["browser_time"] - 1000))
        errors.append(("lit to probe", probe["browser_time"] - lit["browser_time"] - 3000))
        errors.append(("probe shown", probe_off["browser_time"] - probe["browser_time"] - 3000))
        errors.append(("feedback shown", feedback_off["browser_time"] - feedback["browser_time"] - 500))
    worst = max(errors, key=lambda e: abs(e[1]))
    print(f"timing: {len(errors)} intervals over {len(done)} trials, worst {worst[0]} {worst[1]:+.2f} ms")
    for what, error in errors:
        assert abs(error) <= TOLERANCE_MS, (what, error)


@pytest.mark.parametrize("size", [(1024, 768), (1920, 1080)])
def test_the_grid_fits_in_the_panel_between_the_key_labels(app, pty_device, browser, size):
    width, height = size
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        a = app(port=pty_device.path, flow=FLOW)
        to_instructions(a, page)
        inside = """(ids) => ids.map((sel) => {
            const r = document.querySelector(sel).getBoundingClientRect();
            return [sel, r.left >= 0 && r.top >= 0 && r.right <= innerWidth
                    && r.bottom <= innerHeight && r.width > 0 && r.height > 0];
          })"""
        intro = ["#task-title", "#task-instructions", "#task-enter", "#task-side-left", "#task-side-right", "#skip-round"]
        assert all(ok for _, ok in page.evaluate(inside, intro)), page.evaluate(inside, intro)
        assert no_scroll(page)
        page.keyboard.press("Enter")
        in_part(page, 0, "fixation")
        assert no_scroll(page)
        box = """(sel) => { const r = document.querySelector(sel).getBoundingClientRect();
                            return {x: r.left, y: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height}; }"""
        panel = page.evaluate(box, "#task-panel")
        for sel in (GRID, "#spatial-fixation", '[data-cell="0"]', '[data-cell="24"]'):
            b = page.evaluate(box, sel)
            assert b["w"] > 0 and b["h"] > 0, sel
            assert panel["x"] <= b["x"] and b["r"] <= panel["r"], sel
            assert panel["y"] <= b["y"] and b["b"] <= panel["b"], sel
        fix, grid = page.evaluate(box, "#spatial-fixation"), page.evaluate(box, GRID)
        assert abs((fix["x"] + fix["r"]) / 2 - (grid["x"] + grid["r"]) / 2) < 2  # the cross is centered
        assert abs((fix["y"] + fix["b"]) / 2 - (grid["y"] + grid["b"]) / 2) < 2
        assert abs(grid["w"] - grid["h"]) < 1  # square
        assert abs(grid["x"] - (width - grid["r"])) < 2  # centered on the screen
        assert grid["w"] >= 0.8 * panel["w"]
        sides = [GRID, "#task-side-left", "#task-side-right", "#spatial-fixation"]
        assert all(ok for _, ok in page.evaluate(inside, sides))
        left, right = page.evaluate(box, "#task-side-left"), page.evaluate(box, "#task-side-right")
        assert left["r"] <= panel["x"] and right["x"] >= panel["r"]  # the key labels beside the panel
        in_part(page, 0, "probe")
        assert all(ok for _, ok in page.evaluate(inside, sides[:3]))
        assert page.locator(f"{GRID} .spatial-cell").count() == 25
        assert page.locator("#task-panel [data-probe='true']").count() == 1
        # The probe: a thicker white outline and a steady glow; inside, the page's navy (not
        # filled), and nothing flashes.
        probe = page.evaluate(
            """() => {
              const style = (e) => { const s = getComputedStyle(e); return {
                bg: s.backgroundColor, border: s.borderTopColor, width: parseFloat(s.borderTopWidth),
                shadow: s.boxShadow, animation: s.animationName, transition: s.transitionDuration}; };
              return {probe: style(document.querySelector("#task-panel [data-probe='true']")),
                      cell: style(document.querySelector('#spatial-grid [data-cell="0"]')),
                      body: getComputedStyle(document.body).backgroundColor};
            }"""
        )
        assert probe["probe"]["bg"] == probe["body"] == "rgb(18, 24, 38)"
        assert probe["probe"]["border"] == probe["cell"]["border"] == "rgb(255, 255, 255)"
        assert probe["probe"]["width"] >= 2 * probe["cell"]["width"]
        assert probe["probe"]["shadow"] != "none" and probe["cell"]["shadow"] == "none"
        assert probe["probe"]["animation"] == "none" and probe["probe"]["transition"] == "0s"
        assert probe["cell"]["bg"] == "rgba(0, 0, 0, 0)"
        assert no_scroll(page)
    finally:
        context.close()


def no_scroll(page) -> bool:
    """The page does not scroll: nothing reaches past the viewport."""
    return page.evaluate(
        "(() => { const e = document.scrollingElement;"
        " return e.scrollHeight <= innerHeight && e.scrollWidth <= innerWidth; })()"
    )


SAMPLER = """() => {
  window.__frames = [];
  window.__sampling = true;
  const area = document.getElementById('task-area');
  const step = (ts) => {
    if (!window.__sampling) return;
    const grid = document.querySelector('#task-panel > .task-background > #spatial-grid');
    window.__frames.push({
      ts,
      trial: area.dataset.trial,
      grid: !!grid && grid.querySelectorAll('.spatial-cell').length === 25
        && grid.getBoundingClientRect().width > 0,
      cross: !!grid && !!grid.querySelector('#spatial-fixation'),
    });
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}"""


def test_the_grid_is_on_the_screen_in_every_frame_across_trials(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=spatial_flow(tmp_path, 60))
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practise_right(page, planned(page, seed, "block1", "practice"))
    trials = planned(page, seed, "block1", "trials")
    start(page, "block")
    in_part(page, 0, "fixation")
    page.evaluate(SAMPLER)  # from here on, one sample in every animation frame
    for i in range(4):  # trial 2 gets no answer: its probe times out
        if i == 2:
            awaiting(page, i, False)
        else:
            answer(page, i, False, right_key(trials[i]))
    in_part(page, 4, "lit")
    frames = page.evaluate("window.__sampling = false, window.__frames")
    # Four trial boundaries (0-1, 1-2, 2-3, 3-4), sampled in every frame.
    assert {f["trial"] for f in frames} >= {"0", "1", "2", "3", "4"}
    assert len(frames) > 4 * 4000 / 17 * 0.8
    frame_ms = page.evaluate("Number(document.getElementById('task-area').dataset.frameMs)")
    gaps = [b["ts"] - a["ts"] for a, b in zip(frames, frames[1:])]
    assert max(gaps) < 2.5 * frame_ms  # no frame was missed by the sampler
    missing = [f for f in frames if not (f["grid"] and f["cross"])]
    assert missing == []


def test_a_pause_abandons_the_trial_and_the_block_and_the_round_can_be_skipped(
    app, pty_device, page, tmp_path
):
    # Before the night of 2026-11-25 a resume went on with the block. Now a pause abandons the
    # block: its trials never go on, and the next block starts from its prompt. (20 s blocks:
    # the rest of block 1 after the pause runs out with nothing on the screen.)
    a = app(port=pty_device.path, flow=spatial_flow(tmp_path, 20, blocks=2))
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practise_right(page, planned(page, seed, "block1", "practice"))
    trials = planned(page, seed, "block1", "trials")
    second = planned(page, seed, "block2", "trials")
    start(page, "block")
    answer(page, 0, False, right_key(trials[0]))
    in_part(page, 1, "lit")  # pause while the squares are lit
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_function("document.getElementById('task-area').dataset.awaiting === 'false'")
    # Nothing of the trial stays under the pause overlay.
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0
    page.click("#fs-return")
    start(page, "block")  # block 2, from its prompt
    assert page.inner_text("#task-block") == "Block 2 of 2"
    answer(page, 0, False, right_key(second[0]))
    awaiting(page, 1, False)
    page.evaluate("document.exitFullscreen()")  # pause again, then skip the round from there
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=20_000)
    folder = session_folder_of(a)
    scored = [t for t in named(folder, "trial") if not t["payload"]["practice"]]
    status = {(t["payload"]["block"], t["payload"]["trial"]): t["payload"]["status"] for t in scored}
    assert status == {("block1", 0): "done", ("block1", 1): "abandoned",
                      ("block2", 0): "done", ("block2", 1): "abandoned"}
    for t in scored:
        assert t["payload"]["scored"] == (t["payload"]["status"] == "done")
        if t["payload"]["status"] == "abandoned":
            assert t["payload"]["reason"] == "fullscreen_lost"
    for t in scored:
        plan = (trials if t["payload"]["block"] == "block1" else second)[t["payload"]["trial"]]
        assert {k: t["payload"][k] for k in PARAMS} == plan
    assert scored[2]["payload"]["correct"] is True
    stims = by_part(folder, "stim")
    assert set(stims[("block1", 1, False)]) == {"lit"}  # paused before the probe
    # The lit squares taken off by the pause are logged as their offset.
    assert set(by_part(folder, "stim_off")[("block1", 1, False)]) == {"lit"}
    check_markers(folder)


def test_practice_stopped_after_an_answer_starts_again_from_its_first_trial(app, pty_device, page):
    a = app(port=pty_device.path, flow=FLOW)
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practice = planned(page, seed, "block1", "practice")
    start(page, "practice")
    answer(page, 0, True, wrong_key(practice[0]))
    feedback_is(page, "Incorrect")
    page.evaluate("document.exitFullscreen()")  # during the feedback, after the answer
    page.wait_for_function("document.getElementById('task-area').dataset.mode === 'instructions'")
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    start(page, "practice")
    for i, t in enumerate(practice):
        answer(page, i + 1, True, right_key(t))  # the runner's numbers go on: 1, 2, 3
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=20_000)
    folder = session_folder_of(a)
    wait_until(lambda: len(named(folder, "trial")) == 4, "four practice records")
    records = [t["payload"] for t in named(folder, "trial")]
    first = records[0]
    assert (first["trial"], first["status"], first["reason"]) == (0, "abandoned", "fullscreen_lost")
    # The answer given before the stop is kept as the answer and the key, not a raw key code.
    assert (first["response"], first["key"]) == (
        ("no", "KeyF") if practice[0]["probe_lit"] else ("yes", "KeyJ")
    )
    # The practice list starts again: loads 2, 4, 2 with their own squares.
    assert [r["trial"] for r in records[1:]] == [1, 2, 3]
    for r, plan in zip(records[1:], practice):
        assert {k: r[k] for k in PARAMS} == plan
        assert r["status"] == "done" and r["correct"] is True


def spatial_and_recall_flow(tmp_path: Path) -> Path:
    def step(id_, kind, title, seconds):
        return {
            "id": id_, "kind": kind, "title": title, "module": "spatial",
            "blocks": [{"id": "block1", "seconds": seconds, "points": 100}],
        }

    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        step("words", "task", "Words", 8),
        step("recall", "recall", "Delayed free recall", 8),
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "spatial_recall.json"
    path.write_text(json.dumps(raw))
    return path


def test_a_later_session_with_the_same_seed_in_the_same_tab_starts_clean(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    # Recall alone takes the seed of the session in which the words were studied, so the spatial
    # module played there gets the same seed in a new session, in the same tab.
    port = free_port()
    flow_path = spatial_and_recall_flow(tmp_path)
    first = app(port=device().path, flow=flow_path, http_port=port)
    begin(first, page)
    advance_until(page, "words", "waiting")
    seed = server_state(first)["seed"]
    practise_right(page, planned(page, seed, "block1", "practice"))
    start(page, "block")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=20_000)
    advance_until(page, "recall", "waiting")
    practise_right(page, planned(page, seed, "block1", "practice"))  # recall's practice is done
    session_a = server_state(first)["session_id"]
    stop(first)

    second = app(port=device().path, flow=flow_path, http_port=port, data_dir=first.data_dir)
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="recall"]')
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    advance_until(page, "recall", "waiting")
    state = server_state(second)
    assert state["seed"] == seed and state["session_id"] != session_a
    # Nothing of the first session: practice is offered again and starts at trial 0 with the
    # first practice trial of the seed.
    practice = planned(page, seed, "block1", "practice")
    start(page, "practice")
    answer(page, 0, True, right_key(practice[0]))
    folder = next(  # the second session's folder, next to the first one's
        f for f in (second.data_dir / "sessions").iterdir()
        if (f / "session.json").is_file()
        and json.loads((f / "session.json").read_text())["session_id"] == state["session_id"]
    )
    wait_until(lambda: len(named(folder, "trial")) == 1, "the first practice record")
    record = named(folder, "trial")[0]["payload"]
    assert record["trial"] == 0 and {k: record[k] for k in PARAMS} == practice[0]


def test_a_key_before_the_probe_is_stray_and_changes_no_duration(app, pty_device, page, tmp_path):
    # One key per trial (the runner logs one stray key per trial): J in the fixation of trial 0,
    # F while the squares are lit in trial 1, X in the empty grid of trial 2; trial 3 gets none.
    # Each trial is then answered right.
    frame_probe.install(page)  # what the page did around a late change, for the message
    a = app(port=pty_device.path, flow=spatial_flow(tmp_path, 60))
    to_instructions(a, page)
    seed = server_state(a)["seed"]
    practise_right(page, planned(page, seed, "block1", "practice"))
    trials = planned(page, seed, "block1", "trials")
    start(page, "block")
    for i, (part, key) in enumerate([("fixation", "j"), ("lit", "f"), ("retention", "x")]):
        in_part(page, i, part)
        page.keyboard.press(key)
        answer(page, i, False, right_key(trials[i]))
    answer(page, 3, False, right_key(trials[3]))
    awaiting(page, 4, False)
    # The display's frame period, as the mean of 240 frame intervals (frame_ms in the events is a
    # median of intervals rounded to 0.1 ms, too coarse to add up over 80 frames).
    f = page.evaluate(
        """() => new Promise((done) => { const ts = [];
             const step = (t) => { ts.push(t);
               if (ts.length > 240) done((ts[240] - ts[0]) / 240); else requestAnimationFrame(step); };
             requestAnimationFrame(step); })"""
    )
    stop_session(page)
    folder = session_folder_of(a)
    strays = {key_of(s)[1]: s["payload"] for s in named(folder, "stray") if not s["payload"]["practice"]}
    assert {i: (s["key"], s["awaiting"]) for i, s in strays.items()} == {
        0: ("KeyJ", False), 1: ("KeyF", False), 2: ("KeyX", False),
    }
    done = {t["payload"]["trial"]: t["payload"] for t in named(folder, "trial")
            if not t["payload"]["practice"] and t["payload"]["status"] == "done"}
    resps = {key_of(r)[1]: r for r in named(folder, "resp") if not r["payload"]["practice"]}
    stims, offs = by_part(folder, "stim"), by_part(folder, "stim_off")
    for i in range(4):
        p = done[i]
        # The answer is the key pressed at the probe, not the earlier key.
        assert (p["response"], p["key"], p["correct"]) == (
            ("yes", "KeyJ", True) if trials[i]["probe_lit"] else ("no", "KeyF", True)
        )
        k = ("block1", i, False)
        assert resps[i]["browser_time"] > stims[k]["probe"]["browser_time"]
        lit, probe, lit_off = stims[k]["lit"], stims[k]["probe"], offs[k]["lit"]
        assert abs(f - lit["payload"]["frame_ms"]) < 0.2
        for what, begin_ms, end, want in (
            ("fixation", p["fixation_browser_ms"], lit["browser_time"], p["gap_ms"] + 500),
            ("lit", lit["browser_time"], lit_off["browser_time"], 1000),
            ("blank", lit_off["browser_time"], probe["browser_time"], 2000),
        ):
            span = end - begin_ms
            # The nearest whole number of frames; when `want` lies within 1 ms (the bound below) of
            # half-way between two frame counts (gap_ms 825, 875, 925 ... at 60 Hz), both are
            # nearest, and the frame timestamps' jitter decides which one the page takes.
            nearest = {math.floor(want / f + 0.5)}
            if abs(want / f % 1 - 0.5) * f <= 1:
                nearest = {math.floor(want / f), math.ceil(want / f)}
            assert any(abs(span - n * f) <= 1 for n in nearest), (
                i, what, span, sorted(nearest), f, frame_probe.around(page, end)
            )

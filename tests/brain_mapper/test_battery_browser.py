"""The whole battery: every task module registered, and one participant through all of them.

``test_the_default_flow_plays_every_round_with_its_task`` reads the real default flow from the
server (``/api/flow``) and the real registry (``web/js/tasks/index.js``) in Chromium, and resolves
each step the way ``app.js`` does. ``test_a_participant_plays_the_whole_battery`` starts
``brain-mapper`` on a pseudo-terminal device with ``flows/battery.json`` (every task, one short
block each, practice included, then delayed recall) and a scripted participant who presses F or
J while a task waits for a key and types words remembered from the study list. Nothing waits on
a fixed sleep for an assertion.
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from brain_mapper import flow, scores
from test_engine import events_of, markers_of, session_folder_of
from test_shell_browser import SCREEN, begin, press, score_text, server_state
from test_tasks_browser import wait_for_file

BATTERY = Path(__file__).parent / "flows" / "battery.json"
#: The rounds of the battery, in its order, and the task that logs each one's trials.
TASKS = ["words", "perception", "nback", "flanker", "spatial", "recall"]

#: Resolves every step of the server's flow to a module of the registry, by the rule of
#: ``taskOf`` in ``app.js``: steps of kind task, video and recall; ``module`` names the module
#: (the step id when the flow leaves it out); null or an unregistered name keeps the placeholder.
RESOLVE = """async () => {
  const tasks = await import('/static/js/tasks/index.js');
  const steps = await (await fetch('/api/flow')).json();
  const kinds = ['task', 'video', 'recall'];
  const out = steps.map((step) => {
    const name = step.module === undefined ? step.id : step.module;
    const registered = kinds.includes(step.kind) && typeof name === 'string'
      && Object.prototype.hasOwnProperty.call(tasks, name);
    const m = registered ? tasks[name] : null;
    return {
      id: step.id, kind: step.kind, module: registered ? name : null,
      seconds: step.blocks.map((b) => b.seconds),
      api: m ? ['mount', 'update', 'tick', 'unmount', 'instructions']
        .filter((f) => typeof m[f] === 'function') : [],
    };
  });
  return {steps: out, registry: Object.keys(tasks).sort()};
}"""


def test_the_default_flow_plays_every_round_with_its_task(app, pty_device, page):
    a = app(port=pty_device.path)  # no --flow: the default flow
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    found = page.evaluate(RESOLVE)
    assert found["registry"] == [
        "example", "flanker", "nback", "perception", "recall", "spatial", "timing", "words",
    ]
    steps = found["steps"]
    assert [s["id"] for s in steps] == [s.id for s in flow.default_flow()]
    expected = {
        "words": "words",
        "perception": "perception",
        "nback": "nback",
        "flanker": "flanker",
        "spatial": "spatial",
        "video": None,  # not a task module: the video screen (screens/video.js) draws it
        "recall": "recall",
    }
    played = {s["id"]: s["module"] for s in steps if s["kind"] in ("task", "video", "recall")}
    assert played == expected
    for s in steps:
        if s["module"] is not None:
            assert s["api"] == ["mount", "update", "tick", "unmount", "instructions"], s
    # Rest steps keep their own screens; every other step is setup or the summary.
    rest = [s["id"] for s in steps if s["kind"] == "rest"]
    assert rest == ["rest_closed", "rest_open"]
    assert all(s["module"] is None for s in steps if s["kind"] in ("setup", "rest", "summary"))
    assert next(s for s in steps if s["id"] == "recall")["seconds"] == [125.0]


# -- the whole battery ------------------------------------------------------------------------------

#: What the driver needs to know about the page at one moment.
LOOK = """() => {
  const s = document.getElementById('screen').dataset;
  const area = document.getElementById('task-area');
  const shown = (sel) => { const e = document.querySelector(sel); return !!(e && e.offsetParent); };
  const advance = document.querySelector('#advance');
  const recall = document.getElementById('recall-input');
  return {
    screen: s.screen || '', step: s.step || '', phase: s.phase || '',
    mode: area ? area.dataset.mode : null,
    awaiting: area ? area.dataset.awaiting === 'true' : false,
    trial: area ? area.dataset.trial : null,
    practice: shown('#practice'),
    // A task that starts on Enter: what Enter does now ('practice', 'block' or '').
    ready: area ? area.dataset.ready || '' : '',
    advance: !!(advance && advance.offsetParent && !advance.disabled),
    recall: !!(recall && recall.offsetParent && !recall.disabled),
  };
}"""


#: Records, every frame, the practice feedback on screen: per task its words and how it is drawn
#: (relative to the stimulus panel).
RECORD_FEEDBACK = """
window.__feedback = {};
const look = () => {
  const node = document.querySelector('.task-feedback:not([hidden])');
  const area = document.getElementById('task-area');
  const panel = document.getElementById('task-panel');
  if (node && area && panel && node.textContent) {
    const s = getComputedStyle(node), r = node.getBoundingClientRect(), p = panel.getBoundingClientRect();
    const seen = (window.__feedback[area.dataset.task] ||= {words: [], style: null});
    if (!seen.words.includes(node.textContent)) seen.words.push(node.textContent);
    seen.style ||= {
      size: Math.round(1000 * parseFloat(s.fontSize) / p.width) / 1000,
      color: s.color, background: s.backgroundColor, family: s.fontFamily, weight: s.fontWeight,
      dx: Math.round(r.left + r.width / 2 - (p.left + p.width / 2)),
      dy: Math.round(r.top + r.height / 2 - (p.top + p.height / 2)),
    };
  }
  requestAnimationFrame(look);
};
requestAnimationFrame(look);
"""


#: Records, on every frame after the task area changed, the color of everything drawn on a task
#: screen and the color of the ground it is drawn on (the nearest ancestor with a background, else
#: the page): text (instructions, legend, words, letters, crosses, feedback, the recall field),
#: the flanker arrows (``fill``), the spatial outlines (``border``) and filled squares, and the
#: 2-back cross (the stimulus area's ``::after``). Also how the stimulus area itself is drawn.
RECORD_COLORS = """
window.__colors = {};
window.__areas = {};
let dirty = true;
new MutationObserver(() => { dirty = true; }).observe(document, {subtree: true, childList: true,
  attributes: true, characterData: true});
const opaque = (c) => c && c !== 'transparent' && !/^rgba\\(.*,\\s*0\\)$/.test(c);
const groundOf = (node) => {
  for (let e = node.parentElement; e; e = e.parentElement) {
    const b = getComputedStyle(e).backgroundColor;
    if (opaque(b)) return b;
  }
  return getComputedStyle(document.body).backgroundColor;
};
const shown = (e) => {
  const r = e.getBoundingClientRect();
  if (!(r.width > 0 && r.height > 0)) return false;
  for (let x = e; x && x.nodeType === 1; x = x.parentElement) {
    const s = getComputedStyle(x);
    if (s.visibility !== 'visible' || s.display === 'none' || s.opacity === '0') return false;
  }
  return true;
};
const note = (task, kind, what, color, ground) => {
  const seen = (window.__colors[task] ||= {});
  seen[`${kind}|${what}|${color}|${ground}`] = {kind, what, color, ground};
};
const look = () => {
  const area = document.getElementById('task-area');
  if (area && dirty) {
    dirty = false;
    const task = area.dataset.task;
    for (const e of area.querySelectorAll('*')) {
      if (e.closest('button, .leave') || !shown(e)) continue;
      const s = getComputedStyle(e);
      const name = e.id ? `#${e.id}` : `.${String(e.getAttribute('class') || e.tagName).split(' ')[0]}`;
      const text = Array.from(e.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
      if (text || e.tagName === 'INPUT') {
        note(task, 'text', name, s.color, opaque(s.backgroundColor) ? s.backgroundColor : groundOf(e));
      }
      if (e.classList.contains('flanker-ink')) note(task, 'fill', name, s.fill, groundOf(e));
      if (e.classList.contains('spatial-cell')) {
        note(task, 'outline', name, s.borderTopColor, groundOf(e));
        if (e.dataset.lit === 'true') note(task, 'fill', name, s.backgroundColor, groundOf(e));
      }
    }
    const panel = document.getElementById('task-panel');
    if (panel) {
      const s = getComputedStyle(panel);
      window.__areas[task] = [s.backgroundColor, s.backgroundImage, s.borderTopWidth, s.boxShadow];
      const after = getComputedStyle(panel, '::after');
      if (after.content && after.content !== 'none' && after.content !== 'normal') {
        note(task, 'text', '#task-panel::after', after.color, groundOf(panel.firstChild || panel));
      }
    }
  }
  requestAnimationFrame(look);
};
requestAnimationFrame(look);
"""


def relative_luminance(color: str) -> float:
    """WCAG 2 relative luminance of an ``rgb()`` / opaque ``rgba()`` color string."""
    parts = [float(x) for x in color[color.index("(") + 1 : color.index(")")].split(",")]
    assert len(parts) == 3 or parts[3] == 1, color

    def linear(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in parts[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    hi, lo = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_ratio_matches_the_wcag_examples():
    assert contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(21)
    assert contrast_ratio("rgb(118, 118, 118)", "rgb(255, 255, 255)") == pytest.approx(4.54, abs=0.01)


def remembered(folder: Path) -> list[str]:
    """The words studied so far, as the page logged them."""
    return [
        e["payload"]["word"]
        for e in events_of(folder)
        if e["name"] == "words.stim" and e["payload"].get("part") == "word"
    ]


def samples_of(folder: Path) -> dict[int, int]:
    return {
        int(m["label"].rsplit("#", 1)[1]): m["sample"] for m in markers_of(folder / "eeg")
    }


def test_a_participant_plays_the_whole_battery(app, pty_device, page):
    received: list[dict] = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda f: received.append(json.loads(f))))
    console: list[str] = []
    page.on(
        "console",
        lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("warning", "error") else None,
    )
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
    page.add_init_script(RECORD_FEEDBACK)
    page.add_init_script(RECORD_COLORS)
    a = app(port=pty_device.path, flow=BATTERY)
    plan = flow.load(BATTERY)
    rounds = [s for s in plan if s.blocks]
    assert [s.id for s in rounds] == TASKS
    begin(a, page)
    folder = None
    chooser = random.Random(7)
    practised: set[str] = set()
    typed: dict[str, list[str]] = {}
    seen_rounds: list[str] = []
    progress = points = None
    while True:
        look = page.evaluate(LOOK)
        if look["phase"] == "done":
            break
        if look["screen"] == "task" and look["step"] not in seen_rounds:
            seen_rounds.append(look["step"])
        try:
            if look["screen"] == "summary":
                progress = page.inner_text("#hud-progress-text")
                points = page.inner_text("#hud-points")
                page.click("#advance", timeout=2000)
                page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
            elif look["recall"]:
                if folder is None:
                    folder = session_folder_of(a)
                mine = typed.setdefault(look["step"], [])
                studied = remembered(folder)
                # Three words of the list and one that was not on it, then nothing more.
                want = [w for w in studied if w not in mine][:3] if len(mine) < 3 else []
                if not want and "anchovy" not in mine:
                    want = ["anchovy"]
                if want:
                    page.keyboard.type(want[0])
                    page.keyboard.press("Enter")
                    mine.append(want[0])
                else:
                    page.wait_for_timeout(100)
            elif look["screen"] == "task" and look["awaiting"]:
                # A plausible answer: some time after the stimulus, F or J.
                page.wait_for_timeout(chooser.randint(250, 600))
                page.keyboard.press(chooser.choice(["f", "j"]))
                page.wait_for_function(
                    f"(() => {{ const a = document.getElementById('task-area');"
                    f" return !a || a.dataset.awaiting !== 'true' || a.dataset.trial !== '{look['trial']}'; }})()",
                    timeout=5000,
                )
            elif look["screen"] == "task" and look["practice"] and look["step"] not in practised:
                practised.add(look["step"])
                page.click("#practice", timeout=2000)
            elif look["screen"] == "task" and look["ready"]:
                # The F/J tasks start on Enter: the practice, then each block.
                if look["ready"] == "practice":
                    practised.add(look["step"])
                page.keyboard.press("Enter")
                page.wait_for_function(
                    "(() => { const a = document.getElementById('task-area'); return !a || !a.dataset.ready; })()",
                    timeout=5000,
                )
            elif look["advance"] and look["phase"] in ("waiting", "between") or (
                look["advance"] and look["screen"] in ("participant", "between")
            ):
                press(page, timeout=2000)
                if look["screen"] == "participant":
                    page.wait_for_function(
                        "document.getElementById('screen').dataset.screen !== 'participant'",
                        timeout=10_000,
                    )
            else:
                page.wait_for_timeout(30)
        except PlaywrightTimeout:
            pass  # the screen changed under the action; look again
    assert seen_rounds == TASKS
    # Practice was offered and played before the first block of every task that has it.
    assert practised == {"perception", "nback", "flanker", "spatial"}
    # Practice feedback: the same words and the same look in every task, white on the page's
    # navy ground at 0.06 of the stimulus area, centered in it.
    feedback = page.evaluate("window.__feedback")
    assert sorted(feedback) == ["flanker", "nback", "perception", "spatial"]
    for task, seen in feedback.items():
        assert set(seen["words"]) <= {"Correct", "Incorrect", "No response"}, (task, seen)
        assert seen["style"] == feedback["flanker"]["style"], (task, seen, feedback["flanker"])
    style = feedback["flanker"]["style"]
    assert style["size"] == pytest.approx(0.06, abs=0.002)
    assert (style["color"], style["background"]) == ("rgb(255, 255, 255)", "rgb(18, 24, 38)")
    # Every task draws straight on the page: the stimulus area has no ground, border or shadow.
    areas = page.evaluate("window.__areas")
    assert sorted(areas) == sorted(set(TASKS))
    for task, drawn in areas.items():
        assert drawn == ["rgba(0, 0, 0, 0)", "none", "0px", "none"], (task, drawn)
    # Everything drawn on a task screen, on the ground it is drawn on, is at least 7:1; the
    # stimuli themselves are white.
    colors = page.evaluate("window.__colors")
    assert sorted(colors) == sorted(set(TASKS))
    ratios = {}
    for task, seen in colors.items():
        for item in seen.values():
            ratio = contrast_ratio(item["color"], item["ground"])
            ratios[(task, item["kind"], item["what"])] = round(ratio, 2)
            assert ratio >= 7, (task, item, ratio)
    print("contrast ratios:", json.dumps({" ".join(k): v for k, v in sorted(ratios.items())}))
    stimuli = {
        "words": ["#words-word", "#words-fixation"],
        "nback": [".nback-letter", "#task-panel::after"],
        "flanker": [".flanker-arrow"],
        "spatial": [".spatial-cell", "#spatial-fixation"],
    }
    for task, names in stimuli.items():
        for name in names:
            found = [i for i in colors[task].values() if i["what"] == name]
            assert found, (task, name)
            assert {i["color"] for i in found} == {"rgb(255, 255, 255)"}, (task, name, found)
    # The key labels are white on the navy; while a key is pressed (the 150 ms sign that the press
    # was taken) its label is navy on white. (Before the night of 2026-11-25 the legend was only
    # white.)
    legend = [i for i in colors["flanker"].values() if i["what"] in (".KBD", ".SPAN")]
    white, navy = "rgb(255, 255, 255)", "rgb(18, 24, 38)"
    assert legend and {(i["color"], i["ground"]) for i in legend} <= {(white, navy), (navy, white)}, legend
    assert any(i["color"] == white for i in legend), legend
    assert abs(style["dx"]) <= 1 and abs(style["dy"]) <= 1
    # Every event the page sent was stored and acknowledged; none refused or dropped.
    page.wait_for_function(
        "async () => (await import('/static/js/tasks/runner.js')).pendingEvents() === 0",
        timeout=15_000,
    )
    assert page.evaluate(
        "Object.keys(sessionStorage).filter((k) => k.startsWith('brain-mapper-task/outbox/'))"
    ) == []
    # Before the merge with the engine's block_finished, the server answers that command (sent
    # when no more trials fit in a block) with an error the page does not show; nothing else.
    assert [m for m in received if m.get("t") == "error"
            and "block_finished" not in m.get("msg", "")] == []
    acks = [m for m in received if m.get("t") == "ack"]
    assert [m for m in acks if "refused" in m or "dropped" in m or "late" in m] == []
    folder = session_folder_of(a)
    events = events_of(folder)
    acked = {m["cid"] for m in acks}
    sent = [e for e in events if "cid" in e["payload"]]
    assert sent and all(e["payload"]["cid"] in acked for e in sent)
    assert not any(e["payload"].get("queued") for e in sent)
    # Trial events for every task, and a behavior file with one row per trial event.
    for task in TASKS:
        trials = [e for e in events if e["name"] == f"{task}.trial"]
        assert trials, task
        if task not in ("words", "recall"):
            assert any(t["payload"]["practice"] for t in trials), task
            assert any(
                t["payload"]["scored"] and t["payload"]["response"] is not None for t in trials
            ), task
        path = folder / "behavior" / f"{task}.csv"
        wait_for_file(path)
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert [int(r["seq"]) for r in rows] == [t["seq"] for t in trials], task
    words = [e for e in events if e["name"] == "words.trial"][0]["payload"]
    recall = [e for e in events if e["name"] == "recall.trial"][0]["payload"]
    assert words["status"] == "done" and recall["status"] == "done"
    assert len(words["matched"]) == 3 and len(recall["matched"]) == 3
    assert [i["text"] for i in recall["intrusions_tagged"]] == ["anchovy"]
    # Every stimulus event has a marker, and the samples rise with seq.
    samples = samples_of(folder)
    stims = [e for e in events if e["name"].split(".", 1)[-1] in ("stim", "stim_off")]
    assert {e["name"].split(".")[0] for e in stims} == {"words", "perception", "nback", "flanker", "spatial", "recall"}
    assert all(e["seq"] in samples for e in stims)
    ordered = [samples[e["seq"]] for e in sorted(events, key=lambda e: e["seq"]) if e["seq"] in samples]
    assert ordered == sorted(ordered) and ordered[-1] > ordered[0]
    stim_samples = [samples[e["seq"]] for e in stims]
    assert stim_samples == sorted(stim_samples)
    # Points and progress: every block of the flow done, its points awarded once.
    total = sum(b.points for s in rounds for b in s.blocks)
    count = sum(len(s.blocks) for s in rounds)
    assert progress == f"{count} of {count} blocks done"
    awards = [json.loads(x) for x in (folder / "points.jsonl").read_text().splitlines()]
    assert [(w["step"], w["reason"]) for w in awards] == [
        (s.id, "block complete") for s in rounds for _ in s.blocks
    ]
    assert awards[-1]["total"] == total
    state = server_state(a)
    assert state["failure"] is None and not state["stopped"] and state["points_total"] == total
    # The HUD on the summary showed the session's score (earned of possible), the one the event
    # log gives.
    assert points == score_text(state)
    assert state["scores"] == scores.score_folder(folder)
    assert console == []


def test_every_test_flow_names_a_module_that_is_registered_or_none():
    # The task flows play their own module; the short flow keeps every placeholder on purpose
    # (``"module": null``), which the shell and skip tests rely on.
    registered = {"example", "flanker", "nback", "perception", "recall", "spatial", "timing", "words"}
    for path in sorted((Path(__file__).parent / "flows").glob("*.json")):
        for step in flow.load(path):
            if step.kind not in ("task", "video", "recall"):
                continue
            if path.name == "short.json":
                assert step.module is None, (path.name, step.id)
            else:
                assert step.module in registered, (path.name, step.id, step.module)


def test_the_registry_file_lists_one_module_per_line_in_alphabetical_order():
    index = Path(flow.__file__).parent / "web" / "js" / "tasks" / "index.js"
    lines = [x for x in index.read_text().splitlines() if x.startswith("export")]
    names = [x.split(" as ")[1].split()[0] if " as " in x else x.split("{")[1].split("}")[0].strip() for x in lines]
    assert names == sorted(names) == [
        "example", "flanker", "nback", "perception", "recall", "spatial", "timing", "words",
    ]

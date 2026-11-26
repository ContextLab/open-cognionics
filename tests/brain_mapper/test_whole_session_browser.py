"""The merged app as a whole: device step, calibration, every task, the zip, with an onlooker.

Every test starts the real ``brain-mapper`` command on the simulated headset (a pseudo-terminal,
``tests/python/pty_device.py``) and, where a page is involved, drives it in a real Chromium.
``test_the_default_flow_from_device_step_to_zip`` plays a shortened copy of ``default_flow()``
(the same steps and modules, each task block cut to the length the task tests use: words 64 s,
since its study alone takes 25 s, the others 10 to 12 s; video and rest 2 s; calibration eyes
open and closed 3 s) with a scripted participant while an onlooker window is open. The video
round is the real video screen with the player's host blocked in the browser (the offline path
of test_video_browser.py, so the test needs no internet): the page says the video could not be
loaded and the participant skips the round. The others
check the wiring of the merge: the behaviour files go into the zip however a session ends, one
bind path, and the gates in front of a task block. Nothing waits on a fixed sleep for an
assertion.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout

import open_cognionics as oc
from brain_mapper import flow, package
from test_battery_browser import LOOK, remembered
from test_calibration_browser import ALLOWED, contact_stream
from test_engine import events_of, markers_of, next_state, open_ws, send, session_folder_of
from test_package import wait_packaged, zip_contents
from test_participants import request
from test_shell_browser import SCREEN, begin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

PACKAGE = Path(flow.__file__).resolve().parent
LABEL = "JOINQ7"
#: The block length of each round in the shortened default flow (the task tests' lengths).
SHORT_SECONDS = {"words": 64.0, "perception": 10.0, "nback": 12.0, "flanker": 10.0, "spatial": 12.0, "recall": 12.0}
OTHER_SECONDS = 2.0
CALIBRATION = {"eyes_open": 3.0, "eyes_closed": 3.0}
TASKS = ["words", "perception", "nback", "flanker", "spatial", "recall"]


def contact_device() -> PtyDevice:
    """Answers 0x11 and 0x12 with the check-on or the check-off recording, like the headset."""
    return PtyDevice(onhead_stream(), config_reply(), contact_stream=contact_stream(), contact_on=True)


def shortened_default_flow(tmp_path: Path) -> Path:
    steps = []
    for step in flow.default_flow():
        item = {
            "id": step.id,
            "kind": step.kind,
            "title": step.title,
            "module": step.module,
            "blocks": [
                {"id": b.id, "seconds": SHORT_SECONDS.get(step.id, OTHER_SECONDS), "points": b.points}
                for b in step.blocks
            ],
        }
        if step.calibration is not None:
            item["calibration"] = CALIBRATION
        if step.may_finish_early:
            item["may_finish_early"] = True
        steps.append(item)
    path = tmp_path / "default-shortened.json"
    path.write_text(json.dumps(steps))
    return path


def shape(steps, video_module=True) -> list[tuple]:
    """``video_module=False`` leaves the module of the video step out (see ``shortened_default_flow``)."""
    return [
        (s.id, s.kind, s.title, s.module if video_module or s.kind != "video" else None,
         [b.id for b in s.blocks], s.calibration is not None, s.may_finish_early)
        for s in steps
    ]  # fmt: skip


#: Records every step and phase the onlooker's root element shows, as it changes.
ONLOOKER_WATCH = """
window.__seen = [];
const watch = () => {
  const root = document.getElementById('ol-root');
  if (!root) { requestAnimationFrame(watch); return; }
  const note = () => {
    const d = root.dataset;
    const last = window.__seen[window.__seen.length - 1];
    if (d.step && (!last || last[0] !== d.step || last[1] !== d.phase)) window.__seen.push([d.step, d.phase]);
  };
  new MutationObserver(note).observe(root, { attributes: true });
  note();
};
watch();
"""


def open_watched_onlooker(browser, a):
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script(ONLOOKER_WATCH)
    look = context.new_page()
    frames: list[str] = []
    sent: list[str] = []
    urls: list[str] = []
    def watch(ws):
        ws.on("framereceived", lambda payload: frames.append(payload))
        ws.on("framesent", lambda payload: sent.append(payload))

    look.on("websocket", watch)
    look.on("request", lambda r: urls.append(r.url))
    look.goto(a.url + "/onlooker?test=1")
    look.wait_for_function("window.__onlooker && window.__onlooker.ready", timeout=120_000)
    return context, look, frames, sent, urls


def block_the_player(page) -> list[str]:
    """Aborts every request of the page's browser context to the video player's host, as when
    there is no internet (the offline path of test_video_browser.py); returns the URLs refused."""
    refused: list[str] = []

    def refuse(route):
        refused.append(route.request.url)
        route.abort("internetdisconnected")

    page.context.route(lambda url: urlsplit(url).netloc == "player.vimeo.com", refuse)
    return refused


def from_the_player(message) -> bool:
    """A console message about a request to the blocked player (the browser's own report)."""
    url = (message.location or {}).get("url", "")
    return urlsplit(url).netloc == "player.vimeo.com" or "player.vimeo.com" in message.text


#: What each task's trial events give, counted by hand from the documented rules (docs,
#: "Points"; scores.py is not used here): ``(earned, possible)``.
def hand_counted(events: list[dict]) -> dict:
    tasks: dict = {}
    for e in events:
        name = e["name"]
        if not name.endswith(".trial"):
            continue
        task, p = name[: -len(".trial")], e["payload"]
        if task not in ("perception", "flanker", "spatial", "nback", "words", "recall"):
            continue
        entry = tasks.setdefault(task, [0, 0])
        if p.get("scored") is not True:
            continue
        if task in ("perception", "flanker", "spatial"):
            entry[0] += p.get("correct") is True
            entry[1] += 1
        elif task == "nback":
            if p.get("needs_response") is True and not p.get("tail") and not p.get("after_resume") and p.get("window_ms", 2400) >= 2400:
                entry[0] += p.get("correct") is True
                entry[1] += 1
        else:
            lists = [p["words"]] if task == "words" else p["lists"]
            studied = {w.strip().lower() for words in lists for w in words} - {""}
            typed = {w.strip().lower() for w in p["typed"]}
            entry[0] += len(studied & typed)
            entry[1] += len(studied)
    return {task: tuple(v) for task, v in tasks.items()}


def click_found(page, selector: str) -> bool:
    """Clicks the element found now (never one looked up again after the screen moved on)."""
    button = page.query_selector(selector)
    if button is None:
        return False
    button.click(timeout=2000)
    return True


def drive(page, a, done, label=LABEL, seed=7, timeout=900.0) -> dict:
    """A scripted participant: types the label, presses Continue and Start, plays practice, answers
    F or J while a task waits for a key, and types words remembered from the study lists, until
    ``done(look)``. Returns what was seen."""
    chooser = random.Random(seed)
    seen = {"rounds": [], "practised": set(), "screens": [], "video_skipped": False}
    typed: dict[str, list[str]] = {}
    deadline = time.monotonic() + timeout
    while True:
        assert time.monotonic() < deadline, f"the scripted participant got stuck: {seen['screens'][-5:]}"
        look = page.evaluate(LOOK)
        if done(look):
            return seen
        where = (look["screen"], look["step"], look["phase"])
        if not seen["screens"] or seen["screens"][-1] != where:
            seen["screens"].append(where)
        if look["screen"] == "task" and look["step"] not in seen["rounds"]:
            seen["rounds"].append(look["step"])
        try:
            if look["screen"] == "participant":
                field = page.query_selector("#participant-label")
                if field is not None and not field.input_value():
                    field.fill(label)
                if look["advance"] and click_found(page, "#advance:not([disabled])"):
                    page.wait_for_function(
                        "document.getElementById('screen').dataset.screen !== 'participant'", timeout=30_000
                    )
            elif look["screen"] == "video" and page.locator("#video-problem").is_visible():
                # The player could not be loaded: the participant skips the round.
                if click_found(page, "#skip-round"):
                    page.click("#leave-yes", timeout=5000)
                    seen["video_skipped"] = True
                    page.wait_for_function(
                        "document.getElementById('screen').dataset.step !== 'video'", timeout=30_000
                    )
            elif look["recall"]:
                mine = typed.setdefault(look["step"], [])
                studied = remembered(session_folder_of(a))
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
                page.wait_for_timeout(chooser.randint(250, 600))
                page.keyboard.press(chooser.choice(["f", "j"]))
                page.wait_for_function(
                    "(() => { const a = document.getElementById('task-area');"
                    f" return !a || a.dataset.awaiting !== 'true' || a.dataset.trial !== '{look['trial']}'; }})()",
                    timeout=5000,
                )
            elif look["screen"] == "task" and look["practice"] and look["step"] not in seen["practised"]:
                seen["practised"].add(look["step"])
                click_found(page, "#practice")
            elif look["screen"] == "task" and look.get("ready"):
                # The F/J tasks start on Enter (the practice, then each block).
                if look["ready"] == "practice":
                    seen["practised"].add(look["step"])
                page.keyboard.press("Enter")
                page.wait_for_function(
                    "(() => { const a = document.getElementById('task-area'); return !a || !a.dataset.ready; })()",
                    timeout=5000,
                )
            elif look["advance"] and (look["phase"] in ("waiting", "between") or look["screen"] == "summary"):
                click_found(page, "#advance:not([disabled])")
                page.wait_for_timeout(50)
            else:
                page.wait_for_timeout(30)
        except PlaywrightTimeout:
            pass  # the screen changed under the action; look again


def recorded_samples(eeg: Path) -> int:
    return sum(chunk.samples for chunk in oc.replay(eeg))


def zip_matches_folder(zipped: Path, folder: Path) -> None:
    """The zip holds what the folder holds now (``package.members``: every file, and
    ``points.jsonl`` once more as ``behavior/points.jsonl``), byte for byte, but for the one key
    added to ``session.json`` once the zip was verified (``zip``, its name)."""
    assert package.verify(zipped, package.members(folder).files, crc_only=("session.json",))
    meta = json.loads((folder / "session.json").read_text())
    assert meta.pop("zip") == zipped.name
    assert json.loads(zip_contents(zipped)["session.json"]) == meta


def behaviour_rows(content: dict[str, bytes], task: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(content[f"behavior/{task}.csv"].decode("utf-8"))))


# -- c: the default flow, end to end --------------------------------------------------------------


def test_the_default_flow_from_device_step_to_zip(app, page, browser, tmp_path):
    path = shortened_default_flow(tmp_path)
    plan = flow.load(path)
    assert shape(plan) == shape(flow.default_flow())  # the same steps, modules and blocks
    assert next(s for s in plan if s.kind == "video").module == "video"  # the real video screen
    blocked = block_the_player(page)
    device = contact_device()
    try:
        a = app(port=device.path, flow=path)
        console: list[str] = []
        page.on(
            "console",
            lambda m: console.append(f"{m.type}: {m.text}")
            if m.type == "error" and not from_the_player(m)
            else None,
        )
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
        begin(a, page)
        context, look, frames, sent, urls = open_watched_onlooker(browser, a)
        try:
            seen = drive(page, a, lambda seen_now: seen_now["phase"] == "done")
            folder = session_folder_of(a)
            zipped = wait_packaged(folder, timeout=120)
            page.wait_for_selector(f'{SCREEN}[data-phase="done"]')
            look.wait_for_function(
                "document.getElementById('ol-root').dataset.phase === 'done'", timeout=30_000
            )
            page.wait_for_selector("#summary-scores li", timeout=30_000)
            summary_rows = page.evaluate(
                """() => {
                  const out = {video: null};
                  for (const li of document.querySelectorAll('#summary-scores li[data-step]')) {
                    out[li.dataset.step] = li.querySelector('.how').textContent;
                  }
                  return out;
                }"""
            )
            onlooker_seen = look.evaluate("window.__seen")
            onlooker_text = look.inner_text("body")
        finally:
            context.close()
        written = device.written()
    finally:
        device.hang_up()

    # Every round resolved to its own task module, in order; the video round showed the video
    # screen, which said the video could not be loaded (the player's host is blocked), and was
    # skipped.
    assert seen["rounds"] == ["words", "perception", "nback", "flanker", "spatial", "recall"]
    assert ("video", "video", "waiting") in seen["screens"]
    assert seen["video_skipped"] is True
    assert blocked and all(urlsplit(u).netloc == "player.vimeo.com" for u in blocked)
    assert not any(s[0] == "placeholder" for s in seen["screens"])
    assert {"rest", "summary", "device", "calibration"} <= {s[0] for s in seen["screens"]}
    assert set(written) <= ALLOWED, written.hex()
    assert console == []

    meta = json.loads((folder / "session.json").read_text())
    assert meta["complete"] is True and meta["ending"] == "finished" and meta["ended_early"] is False
    assert meta["hook_errors"] == [] and meta["not_zipped"] == []
    assert json.loads((folder / "progress.json").read_text())["status"] == "finished"
    # One recording, holding every block and calibration part.
    assert meta["recordings"] == ["eeg"] and "recordings_set_aside" not in meta
    assert not list(folder.glob("eeg-lost-*"))
    events = events_of(folder)
    marks = {m["label"]: m["sample"] for m in markers_of(folder / "eeg")}
    total = recorded_samples(folder / "eeg")
    # Every block but the video's ran (the video round was skipped before its block began).
    blocks = [(s.id, b.id) for s in plan for b in s.blocks if s.kind != "video"]
    skipped = [e for e in events if e["name"] == "skipped"]
    assert [(e["step"], e["payload"]["block"]) for e in skipped] == [("video", None)]
    assert [e["payload"]["reason"] for e in events if e["name"] == "video.error"] == ["not_ready"]
    assert json.loads((folder / "progress.json").read_text())["steps"][
        [s.id for s in plan].index("video")
    ]["outcome"] == "skipped"
    for name in ("block_start", "block_end"):
        found = [(e["step"], e["block"]) for e in events if e["name"] == name]
        assert found == blocks, name
    for e in events:
        if e["name"] in ("block_start", "block_end") or e["step"] == "calibration":
            label = f"{e['step']}/{e['name']}#{e['seq']}"
            assert label in marks and 0 <= marks[label] <= total, label
    assert not any(e["name"] in ("link_lost", "link_back", "source_ended") for e in events)
    # Events of every task, each with a marker, at samples that rise with seq.
    by_seq = {int(label.rsplit("#", 1)[1]): sample for label, sample in marks.items()}
    for task in TASKS:
        task_events = [e for e in events if e["name"].startswith(f"{task}.")]
        assert any(e["name"] == f"{task}.trial" for e in task_events), task
        assert all(e["seq"] in by_seq for e in task_events), task
    ordered = [by_seq[e["seq"]] for e in sorted(events, key=lambda e: e["seq"]) if e["seq"] in by_seq]
    assert ordered == sorted(ordered) and ordered[0] < ordered[-1]
    # calibration.json, from the measured contact check.
    calibration = json.loads((folder / "calibration.json").read_text())
    assert len(calibration["states"]) == 20
    names = [e["name"] for e in events if e["step"] == "calibration"]
    for name in ("contact_start", "contact_end", "settling_start", "eyes_open_start", "eyes_closed_end", "blinks_end"):
        assert name in names, name
    assert names.count("blink_cue") == 5
    # Each rest: Start, the start tone, the period, the end tone, in that order, each with a marker.
    for rest in ("rest_closed", "rest_open"):
        order = [e["name"] for e in events if e["step"] == rest and e["name"] in ("block_start", "block_end", "rest.tone_start", "rest.tone_end")]
        assert order == ["block_start", "rest.tone_start", "block_end", "rest.tone_end"], (rest, order)
        tones = [e for e in events if e["step"] == rest and e["name"].startswith("rest.tone_")]
        assert [t["payload"]["hz"] for t in tones] == [440, 660] and all(t["payload"]["ms"] == 300 for t in tones)
        assert all(f"{rest}/{t['name']}#{t['seq']}" in marks for t in tones)

    # The zip: verified against the folder, holding all of it.
    zip_matches_folder(zipped, folder)
    content = zip_contents(zipped)
    # Scores: session.json and behavior/scores.json give what the trial events give by hand; the
    # video round has no score.
    counted = hand_counted(events)
    assert set(counted) == set(TASKS)
    for scores in (meta["scores"], json.loads(content["behavior/scores.json"])):
        assert {t: (v["earned"], v["possible"]) for t, v in scores["tasks"].items()} == counted
        assert (scores["total"]["earned"], scores["total"]["possible"]) == (
            sum(v[0] for v in counted.values()),
            sum(v[1] for v in counted.values()),
        )
        assert "video" not in scores["tasks"]  # the player never reported: nothing was watched
    # The video round is scored (a point for each full 10 s watched) and has its row: a dash, skipped.
    assert summary_rows["video"] == "—"
    for task in TASKS:
        earned, possible = counted[task]
        assert summary_rows[task] == (f"{earned} of {possible}" if possible else "—"), (task, summary_rows)
    for name in ("session.json", "progress.json", "events.jsonl", "points.jsonl", "calibration.json",
                 "eeg/raw.bin", "eeg/meta.json", "eeg/markers.jsonl", "behavior/columns.json"):  # fmt: skip
        assert name in content, name
    for task in TASKS:
        rows = behaviour_rows(content, task)
        trials = [e["seq"] for e in events if e["name"] == f"{task}.trial"]
        assert [int(r["seq"]) for r in rows] == trials, task
        assert (folder / "behavior" / f"{task}.csv").read_bytes() == content[f"behavior/{task}.csv"]

    # The onlooker followed every step, and was never told the label or given the zip.
    steps_seen = [s for s, _ in onlooker_seen]
    for step in [s.id for s in plan]:
        assert step in steps_seen, (step, onlooker_seen)
    assert [s for s in dict.fromkeys(steps_seen)] == [s.id for s in plan]
    assert LABEL not in onlooker_text
    assert all(LABEL not in f for f in frames)
    assert all(LABEL not in u for u in urls)
    parsed = [json.loads(f) for f in frames]
    assert not any(m.get("t") == "package" or "download" in m for m in parsed)
    assert all(json.loads(s).get("cmd") == "display" for s in sent)


# -- a: the behaviour files are in the zip however the session ends ---------------------------------


def one_task_flow(tmp_path: Path, task: str, seconds: float, blocks: int = 1) -> Path:
    path = tmp_path / f"{task}-flow.json"
    path.write_text(
        json.dumps(
            [
                {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
                {"id": "device", "kind": "setup", "title": "Device setup", "blocks": []},
                {
                    "id": task,
                    "kind": "task",
                    "title": task,
                    "blocks": [{"id": f"block{i + 1}", "seconds": seconds, "points": 100} for i in range(blocks)],
                },
                {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
            ]
        )
    )
    return path


def trials_answered(page, a, task: str, count: int) -> None:
    """Drives the participant until ``count`` scored trials of ``task`` are in the event log."""

    def enough(look) -> bool:
        try:
            folder = session_folder_of(a)
        except (AssertionError, FileNotFoundError):
            return False
        scored = [e for e in events_of(folder) if e["name"] == f"{task}.trial" and not e["payload"]["practice"]]
        return len(scored) >= count and look["phase"] == "running"

    drive(page, a, enough, timeout=120)


def to_between(page, a) -> None:
    """Plays the first block (practice included) up to the between-blocks screen, where "Stop
    here" is offered (it is hidden while a block runs)."""
    drive(page, a, lambda look: look["phase"] == "between" and look["screen"] == "between", timeout=120)
    page.wait_for_selector("#stop-here", state="visible")


def test_a_stopped_session_zips_its_behaviour_files(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=one_task_flow(tmp_path, "flanker", 10, blocks=2))
    begin(a, page)
    to_between(page, a)
    page.click("#stop-here")
    page.click("#leave-yes")
    folder = session_folder_of(a)
    zipped = wait_packaged(folder)
    meta = json.loads((folder / "session.json").read_text())
    assert meta["ending"] == "stopped" and meta["hook_errors"] == []
    content = zip_contents(zipped)
    trials = [e["seq"] for e in events_of(folder) if e["name"] == "flanker.trial"]
    assert [int(r["seq"]) for r in behaviour_rows(content, "flanker")] == trials
    assert isinstance(json.loads(content["behavior/columns.json"]), dict)
    zip_matches_folder(zipped, folder)


def test_a_lost_link_in_a_task_block_pauses_it_and_a_stop_then_zips_its_behaviour_files(app, page, tmp_path):
    """A link lost after the device step pauses the block (since 2026-11-25; until
    then the session ended early here, with ``source_ended``); "Stop here" on the pause overlay then
    ends the session in the ordinary way, and the trials played are kept."""
    device = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=device.path, flow=one_task_flow(tmp_path, "nback", 60))
        begin(a, page)
        trials_answered(page, a, "nback", 2)
        device.hang_up()
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=30_000)
        page.wait_for_selector("#fs-stop-here", state="visible")
        page.click("#fs-stop-here")
        page.click("#fs-leave-yes")
        folder = session_folder_of(a)
        zipped = wait_packaged(folder)
    finally:
        device.hang_up()
    meta = json.loads((folder / "session.json").read_text())
    assert meta["ending"] == "stopped" and meta["complete"] is False and meta["hook_errors"] == []
    events = events_of(folder)
    names = [e["name"] for e in events]
    assert "link_lost" in names and "source_ended" not in names
    assert [e["payload"]["reason"] for e in events if e["name"] == "paused"] == ["link_lost"]
    content = zip_contents(zipped)
    trials = [e["seq"] for e in events if e["name"] == "nback.trial"]
    assert len(trials) >= 2
    assert [int(r["seq"]) for r in behaviour_rows(content, "nback")] == trials
    assert "behavior/columns.json" in content
    zip_matches_folder(zipped, folder)


def test_behaviour_files_that_cannot_be_written_do_not_stop_the_zip(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=one_task_flow(tmp_path, "flanker", 10, blocks=2))
    begin(a, page)
    to_between(page, a)
    folder = session_folder_of(a)
    (folder / "behavior").write_text("a file where the folder would go")
    page.click("#stop-here")
    page.click("#leave-yes")
    zipped = wait_packaged(folder)
    meta = json.loads((folder / "session.json").read_text())
    # Both hooks write into behavior/: the behaviour files and the scores file both fail.
    assert [h["hook"] for h in meta["hook_errors"]] == ["before_zip", "write_before_zip"]
    assert all(LABEL not in h["error"] and str(folder) not in h["error"] for h in meta["hook_errors"])
    content = zip_contents(zipped)
    assert content["behavior"] == b"a file where the folder would go"
    assert "events.jsonl" in content and "eeg/raw.bin" in content
    zip_matches_folder(zipped, folder)
    # The participant page is told, in words, that the step before the zip failed.
    page.wait_for_function(
        "document.body.innerText.includes('A step before the zip')", timeout=30_000
    )


# -- b: one bind path ---------------------------------------------------------------------------------


def test_the_server_binds_its_port_in_one_place_only_after_the_loopback_probe():
    """Every socket the package makes, binds or listens on is in ``cli.bind``, which probes the
    port (``served``) before it binds 127.0.0.1; uvicorn is given that socket, never a host or port."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("bind", "listen", "create_server", "run", "serve"):
                        found.append((path.name, function.name, node.func.attr))
                    if node.func.attr == "socket" and getattr(node.func.value, "id", "") == "socket":
                        found.append((path.name, function.name, "socket"))
                    if node.func.attr == "Config" and getattr(node.func.value, "id", "") == "uvicorn":
                        assert not {k.arg for k in node.keywords} & {"host", "port", "fd", "uds"}
    servers = [f for f in found if f[2] in ("bind", "listen", "create_server")]
    assert servers == [("cli.py", "bind", "bind"), ("cli.py", "bind", "listen")], found
    sockets = sorted({(f, fn) for f, fn, what in found if what == "socket"})
    assert sockets == [("cli.py", "bind"), ("cli.py", "served")], found
    source = (PACKAGE / "cli.py").read_text(encoding="utf-8")
    bind = source[source.index("def bind(") : source.index("\ndef ", source.index("def bind("))]
    assert bind.index("served(port)") < bind.index("sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)") < bind.index('sock.bind(("127.0.0.1", port))')
    assert "sockets=[sock]" in source


# -- d: the gates in front of a task block --------------------------------------------------------


def post_choice(a, **body) -> tuple[int, dict]:
    return request(a, "POST", "/api/participant", {"label": LABEL, **body})


def test_one_round_and_from_a_round_on_pass_through_device_and_calibration(app, tmp_path):
    for choice, rounds in ((dict(only="flanker"), ["flanker"]), (dict(start_at="spatial"), ["spatial", "video", "rest_closed", "rest_open", "recall"])):  # fmt: skip
        device = contact_device()
        try:
            a = app(port=device.path, data_dir=tmp_path / next(iter(choice.values())))
            with open_ws(a, "participant") as ws:
                next_state(ws)
                status, body = post_choice(a, **choice)
                assert status == 200, body
                state = next_state(ws, predicate=lambda m: m["rev"] > 0)
                assert state["plan"] == ["participant", "device", "calibration", *rounds, "summary"]
                send(ws, cmd="advance")
                next_state(ws, predicate=lambda m: m["step"] == "device")
                # The task cannot be reached from the device step by a skip.
                send(ws, cmd="skip")
                refused = json.loads(ws.recv(timeout=10))
                while refused.get("t") != "error":
                    refused = json.loads(ws.recv(timeout=10))
                assert refused["kind"] == "refused"
                folder = session_folder_of(a)
                deadline = time.monotonic() + 30
                while not (folder / "eeg" / "raw.bin").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                send(ws, cmd="advance")
                state = next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "contact")
                assert state["step"] == "calibration"
                send(ws, cmd="skip")
                state = next_state(ws, timeout=20, predicate=lambda m: m["step"] == rounds[0])
                assert state["phase"] == "waiting"
                # The block starts once the data have shown the contact check off for the
                # settling time (``_refuse_while_check_goes_off``); until then Start is refused.
                deadline = time.monotonic() + 30
                while True:
                    assert time.monotonic() < deadline, "the block never started"
                    send(ws, cmd="advance")
                    try:
                        next_state(ws, timeout=1, predicate=lambda m: m["phase"] == "running")
                        break
                    except AssertionError:
                        continue
            starts = [e["step"] for e in events_of(folder) if e["name"] == "step_start"]
            assert starts[:4] == ["participant", "device", "calibration", rounds[0]]
            block = next(e for e in events_of(folder) if e["name"] == "block_start")
            assert any(m["label"] == f"{rounds[0]}/block_start#{block['seq']}" for m in markers_of(folder / "eeg"))
        finally:
            device.hang_up()


def test_a_task_block_cannot_start_while_the_link_is_down(app, tmp_path):
    device = contact_device()
    try:
        a = app(port=device.path)
        with open_ws(a, "participant") as ws:
            next_state(ws)
            status, body = post_choice(a, only="nback")
            assert status == 200, body
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m: m["step"] == "device")
            folder = session_folder_of(a)
            deadline = time.monotonic() + 30
            while not (folder / "eeg" / "raw.bin").exists():
                assert time.monotonic() < deadline
                time.sleep(0.05)
            device.hang_up()  # the dongle is unplugged before the device step is passed
            deadline = time.monotonic() + 30
            while "link_lost" not in [e["name"] for e in events_of(folder)]:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            for command in ("advance", "skip"):
                send(ws, cmd=command)
                message = json.loads(ws.recv(timeout=10))
                while message.get("t") != "error":
                    message = json.loads(ws.recv(timeout=10))
                assert message["kind"] == "refused", message
            state = next_state(ws)
            assert state["step"] == "device" and state["phase"] == "waiting"
        names = [e["name"] for e in events_of(folder)]
        assert "block_start" not in names and "nback" not in [e["step"] for e in events_of(folder)]
    finally:
        device.hang_up()

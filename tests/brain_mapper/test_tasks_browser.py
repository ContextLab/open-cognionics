"""The task framework (``web/js/tasks/``) and the behavior files, in a real Chromium.

Each test starts ``brain-mapper`` on a pseudo-terminal device (or the real server in this process,
where a connection has to be cut) with a flow that plays the example task, drives the page with
the keyboard the way a person would, and reads the DOM, the server's state messages, the session's
``events.jsonl``, the recording's markers and ``behavior/example.csv``. Nothing waits on a fixed
sleep: the page (``#task-area`` carries ``data-trial``, ``data-practice``, ``data-awaiting`` and
``data-mode``) and the files say when something has happened.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from brain_mapper import behavior, flow, package
from brain_mapper.server import Client, Hub, Settings
from test_engine import SHORT, accept, events_of, markers_of, next_state, open_ws, send, session_folder_of
from test_participants import request, stop
from test_progress import device  # noqa: F401  (a fixture)
from test_shell_browser import SCREEN, advance_until, begin, free_port, screen_of, server_state
from test_shell_browser import inprocess  # noqa: F401  (a fixture)

EXAMPLE = Path(__file__).parent / "flows" / "example.json"
AREA = "#task-area"


def example_flow(tmp_path: Path, seconds: float, blocks: int = 1) -> Path:
    """The example flow with ``blocks`` blocks of ``seconds``."""
    raw = json.loads(EXAMPLE.read_text())
    raw[1]["blocks"] = [
        {"id": f"block{i + 1}", "seconds": seconds, "points": 100}
        for i in range(blocks)
    ]
    path = tmp_path / "example.json"
    path.write_text(json.dumps(raw))
    return path


def to_instructions(a, page) -> None:
    begin(a, page)
    advance_until(page, "example", "waiting")
    page.wait_for_selector("#task-instructions", state="visible")


def awaiting(page, trial: int, practice: bool, timeout: float = 15_000) -> None:
    page.wait_for_selector(
        f'{AREA}[data-trial="{trial}"][data-practice="{str(practice).lower()}"][data-awaiting="true"]',
        timeout=timeout,
    )


def answer(page, trial: int, practice: bool) -> None:
    """Waits for trial ``trial`` to await a key, then presses the space bar."""
    awaiting(page, trial, practice)
    page.keyboard.press("Space")


def practise(page) -> None:
    page.click("#practice")
    for i in range(2):
        answer(page, i, True)
    page.wait_for_selector("#advance", state="visible")


def wait_until(check, what: str, timeout: float = 10.0):
    """Polls ``check()`` until it is true: the server writes the behavior file and its log line
    just after it sends the state that ends the session."""
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, f"never: {what}"
        time.sleep(0.05)


def wait_for_file(path: Path) -> None:
    wait_until(path.is_file, str(path.name))


def named(folder: Path, suffix: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == f"example.{suffix}"]


def samples_of(folder: Path) -> dict[int, int]:
    """``seq`` to sample, over every recording of the session (``eeg-lost-<n>``, then ``eeg``)."""
    found = {}
    for rec in sorted(folder.glob("eeg-lost-*"), key=lambda p: int(p.name.rsplit("-", 1)[1])) + [folder / "eeg"]:
        if (rec / "markers.jsonl").is_file():
            for m in markers_of(rec):
                found[int(m["label"].rsplit("#", 1)[1])] = m["sample"]
    return found


def key_of(event: dict) -> tuple:
    p = event["payload"]
    return (p["block"], p["trial"], p["practice"])


def check_clocks(events: list[dict]) -> None:
    """Every line has the three times; within one server run the monotonic time never goes back
    as ``seq`` goes up (the wall clock may)."""
    by_run: dict[str, list[dict]] = {}
    for event in sorted(events, key=lambda e: e["seq"]):
        assert isinstance(event["server_mono_ns"], int) and isinstance(event["server_wall_ns"], int)
        assert event["unix_time"] == event["server_wall_ns"] / 1e9
        if event["browser_time"] is None:
            assert event["page_unix_ms"] is None
        else:
            assert event["page_unix_ms"] == pytest.approx(event["time_origin"] + event["browser_time"], abs=1e-6)
        by_run.setdefault(event["server_run"], []).append(event)
    for run in by_run.values():
        monos = [e["server_mono_ns"] for e in run]
        assert monos == sorted(monos)


def check_timing(folder: Path) -> None:
    """Every example event has a marker; the samples follow ``seq``; a reaction time is the
    response's time minus the stimulus's, both as the page sent them."""
    events = events_of(folder)
    samples = samples_of(folder)
    check_clocks(events)
    ours = [e for e in events if e["name"].startswith("example.")]
    assert ours
    for event in ours:
        if event["payload"].get("no_recording") is True:
            # Logged while the headset link was lost (no recording open): no marker, said so.
            assert event["payload"]["marked"] is False and event["seq"] not in samples, event
            continue
        assert event["seq"] in samples, event
    ordered = [samples[e["seq"]] for e in events if e["seq"] in samples]
    assert ordered == sorted(ordered)
    assert ordered[-1] > ordered[0]
    stims = {key_of(e): e for e in ours if e["name"] == "example.stim"}
    offs = {key_of(e): e for e in ours if e["name"] == "example.stim_off"}
    resps = {key_of(e): e for e in ours if e["name"] == "example.resp"}
    for stim in stims.values():
        assert stim["payload"]["part"] == "stim"
    for trial in (e for e in ours if e["name"] == "example.trial"):
        key = key_of(trial)
        p = trial["payload"]
        assert (
            p["param_response"] == "space bar"
        )  # the parameter, kept apart from the measure
        assert p["param_cid"] == "square"  # ... and apart from the server's client id
        assert isinstance(p["cid"], str) and p["cid"] != "square" and "payload_cid" not in p
        if key in stims:
            stim = stims[key]
            assert p["stim_browser_ms"] == stim["browser_time"]
            assert p["stim_unix_ms"] == pytest.approx(
                stim["time_origin"] + stim["browser_time"]
            )
            if key in offs:  # the offset is painted (and logged) in a later frame
                assert offs[key]["browser_time"] > stim["browser_time"]
                assert offs[key]["seq"] > stim["seq"]
        if key in resps:
            stim, resp = stims[key], resps[key]
            assert resp["seq"] > stim["seq"]
            assert p["rt_ms"] == resp["browser_time"] - stim["browser_time"]
            assert resp["payload"]["rt_ms"] == p["rt_ms"]
            # the task's key map: the answer in `response`, the code in `key`, in both records
            assert (p["response"], p["key"]) == ("seen", "Space")
            assert (resp["payload"]["response"], resp["payload"]["key"]) == ("seen", "Space")
            assert p["resp_browser_ms"] == resp["browser_time"]
            assert p["rt_ms"] > 0
        if p["status"] == "done":
            assert (
                key in offs
            )  # a finished trial's stimulus was taken off and that was logged


#: The largest stamp lag (arrival at the server minus the page's time of the event) accepted for
#: an event sent at once; measured on this machine at a few ms (the largest seen: see the report).
NORMAL_LAG_MS = 250


def check_behavior(folder: Path) -> None:
    trials = named(folder, "trial")
    samples = samples_of(folder)
    events = events_of(folder)
    path = folder / "behavior" / "example.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    header, body = rows[0], rows[1:]
    timing = ["stim_seq", "stim_sample", "stim_recording", "stim_lag_ms", "stim_off_ms"]
    timing += ["resp_seq", "resp_sample", "resp_recording", "resp_lag_ms", "queued"]
    fixed = behavior.FIRST + timing
    keys = sorted({k for t in trials for k in t["payload"]} - {"queued"})
    # block, trial and practice are in every trial payload and equal the fixed columns: no copies
    same = {"block", "trial", "practice"}
    assert same <= set(keys)
    assert header == fixed + [f"payload_{k}" if k in fixed else k for k in keys if k not in same]
    assert not {"payload_block", "payload_trial", "payload_practice"} & set(header)
    assert len(body) == len(trials)
    by_seq = {e["seq"]: e for e in events}
    for row, event in zip(body, trials):
        cells = dict(zip(header, row))
        assert int(cells["seq"]) == event["seq"]
        assert cells["sample"] == str(samples.get(event["seq"], ""))
        assert cells["block"] == event["payload"]["block"]
        assert cells["trial"] == str(event["payload"]["trial"])
        assert cells["practice"] == json.dumps(event["payload"]["practice"])
        assert cells["status"] == event["payload"]["status"]
        assert cells["param_response"] == "space bar"
        if cells["stim_seq"]:
            stim = by_seq[int(cells["stim_seq"])]
            assert (stim["name"], key_of(stim)) == ("example.stim", key_of(event))
            assert cells["stim_sample"] == str(samples[stim["seq"]])
            lag = float(cells["stim_lag_ms"])
            assert lag == pytest.approx(
                stim["unix_time"] * 1000 - stim["time_origin"] - stim["browser_time"],
                abs=0.01,
            )
            if cells["queued"] == "false":
                assert -50 < lag < NORMAL_LAG_MS
        if cells["resp_seq"]:
            resp = by_seq[int(cells["resp_seq"])]
            assert resp["name"] == "example.resp" and key_of(resp) == key_of(event)
            assert cells["resp_sample"] == str(samples[resp["seq"]])
            assert int(cells["resp_sample"]) >= int(cells["stim_sample"])
            if cells["queued"] == "false":
                assert -50 < float(cells["resp_lag_ms"]) < NORMAL_LAG_MS
        if event["payload"]["status"] == "done":
            assert float(cells["stim_off_ms"]) > 0
    before = path.read_bytes()
    assert behavior.write(folder) == [
        path
    ]  # written again from the same files: the same rows
    assert path.read_bytes() == before
    normal = [
        float(r[header.index("stim_lag_ms")])
        for r in body
        if r[header.index("stim_lag_ms")]
    ]
    normal = [x for x, r in zip(normal, body) if r[header.index("queued")] == "false"]
    if normal:
        print(
            f"stamp lag of stimuli sent at once: largest {max(normal):.1f} ms of {len(normal)}"
        )


def check_delivery(folder: Path) -> None:
    """Every event of the page was written once and in the order the page made it: each carries
    its client id (page load + counter); no id twice; within a page load the counters rise with
    ``seq``; no trial has two ``<task>.trial`` lines."""
    ours = [e for e in events_of(folder) if "cid" in e["payload"]]
    assert ours
    cids = [e["payload"]["cid"] for e in ours]
    assert len(cids) == len(set(cids))
    by_load: dict[str, list[int]] = {}
    for event in sorted(ours, key=lambda e: e["seq"]):
        load, counter = event["payload"]["cid"].rsplit("-", 1)
        by_load.setdefault(load, []).append(int(counter))
    for counters in by_load.values():
        # in order and none missing (only events refused once the session was over could be)
        assert counters == list(range(1, len(counters) + 1))
    trials = [key_of(e) for e in ours if e["name"] == "example.trial"]
    assert len(trials) == len(set(trials))


# -- the registry, the seed, the random numbers -----------------------------------------------------


def test_the_state_carries_the_session_seed_of_progress_json(app, pty_device, page):
    a = app(port=pty_device.path, flow=EXAMPLE)
    assert server_state(a)["seed"] is None  # no session yet
    to_instructions(a, page)
    progress = json.loads((session_folder_of(a) / "progress.json").read_text())
    seed = server_state(a)["seed"]
    assert isinstance(seed, int) and seed == progress["seed"]
    assert (
        page.evaluate("document.getElementById('task-area').dataset.task") == "example"
    )


def test_a_task_step_without_a_registered_task_shows_the_placeholder(
    app, pty_device, page
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    assert screen_of(page)[0] == "placeholder"
    assert page.locator(AREA).count() == 0


def test_the_module_field_chooses_the_task_or_keeps_the_placeholder(
    app, pty_device, page, tmp_path
):
    # "example" is a registered task. Here the step called example keeps the placeholder
    # (module null); the other steps name it, so they show it: a task step with another id, a
    # recall step and (ignored there) a rest step, which keeps its own screen.
    block = [{"id": "block1", "seconds": 2, "points": 100}]
    steps = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {
            "id": "example",
            "kind": "task",
            "title": "Held",
            "module": None,
            "blocks": block,
        },
        {
            "id": "other",
            "kind": "task",
            "title": "Other",
            "module": "example",
            "blocks": block,
        },
        {
            "id": "recall",
            "kind": "recall",
            "title": "Recall",
            "module": "example",
            "blocks": block,
        },
        {
            "id": "rest_open",
            "kind": "rest",
            "title": "Rest",
            "module": "example",
            "blocks": block,
        },
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "modules.json"
    path.write_text(json.dumps(steps))
    a = app(port=pty_device.path, flow=path)
    served = json.loads(page.request.get(a.url + "/api/flow").text())
    assert [s["module"] for s in served] == [
        "participant",
        None,
        "example",
        "example",
        "example",
        "summary",
    ]
    begin(a, page)
    expected = [
        ("example", "placeholder"),
        ("other", "task"),
        ("recall", "task"),
        ("rest_open", "rest"),
    ]
    for step, screen in expected:
        advance_until(page, step, "waiting")
        assert screen_of(page)[0] == screen, step
        if screen == "task":
            assert page.get_attribute(AREA, "data-task") == "example"
            page.wait_for_selector("#task-instructions", state="visible")
        page.click("#skip-round")
        page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')


@pytest.mark.parametrize("module", [3, "", "a-b", "1abc", "x" * 65, ["example"]])
def test_a_flow_file_with_a_bad_module_is_refused_in_words(tmp_path, module):
    raw = json.loads(EXAMPLE.read_text())
    raw[1]["module"] = module
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="module must be null or 1 to 64 of"):
        flow.load(path)


def test_a_step_without_a_module_field_uses_its_id():
    assert all(s.module == s.id for s in flow.default_flow())
    held = [s for s in flow.load(SHORT) if s.kind in ("task", "video", "recall")]
    assert held and all(s.module is None for s in held)


def reference_stream(seed, *labels):
    """The generator of ``rng.js`` in Python: FNV-1a (32 bits) of the UTF-8 JSON text of
    ``[seed, ...labels]``, then mulberry32."""
    mask = 0xFFFFFFFF
    text = json.dumps([seed, *labels], separators=(",", ":"), ensure_ascii=False)
    state = 0x811C9DC5
    for byte in text.encode("utf-8"):
        state = ((state ^ byte) * 0x01000193) & mask

    def nxt() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & mask
        t = ((state ^ (state >> 15)) * (1 | state)) & mask
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & mask)) & mask) ^ t
        return ((t ^ (t >> 14)) & mask) / 4294967296

    return nxt


def test_the_seeded_streams_give_fixed_values_in_the_browser(app, pty_device, page):
    a = app(port=pty_device.path, flow=EXAMPLE)
    page.goto(a.url)
    result = page.evaluate(
        """async () => {
          const { rng } = await import('/static/js/tasks/rng.js');
          const take = (r) => Array.from({ length: 5 }, () => r.int(1000));
          const s = rng(123, 'nback', 'block1');
          return {
            a: take(rng(123, 'nback', 'block1')),
            again: take(rng(123, 'nback', 'block1')),
            other: take(rng(123, 'nback', 'block2')),
            wide: take(rng(2147483647, 'wörds', 3)),
            shuffled: rng(7, 'flanker').shuffle([1, 2, 3, 4, 5, 6]),
            picked: rng(7, 'flanker').pick(['a', 'b', 'c', 'd']),
            floats: [s.next(), s.next()],
          };
        }"""
    )
    assert result["a"] == result["again"] == [798, 670, 413, 96, 334]
    assert result["other"] == [577, 699, 835, 296, 979]
    assert result["wide"] == [13, 241, 343, 0, 837]
    for labels, values in (
        ((123, "nback", "block1"), result["a"]),
        ((123, "nback", "block2"), result["other"]),
        ((2147483647, "wörds", 3), result["wide"]),
    ):
        ref = reference_stream(*labels)
        assert [int(ref() * 1000) for _ in range(5)] == values
    ref = reference_stream(123, "nback", "block1")
    assert result["floats"] == [ref(), ref()]
    ref = reference_stream(7, "flanker")
    items = [1, 2, 3, 4, 5, 6]
    for i in range(len(items) - 1, 0, -1):
        j = int(ref() * (i + 1))
        items[i], items[j] = items[j], items[i]
    assert result["shuffled"] == items
    assert (
        result["picked"]
        == ["a", "b", "c", "d"][int(reference_stream(7, "flanker")() * 4)]
    )


# -- instructions, practice, trials, endings, behavior file -----------------------------------------


@pytest.mark.parametrize("ending", ["finished", "stopped", "hang_up"])
def test_instructions_practice_and_trials_are_logged_with_markers_and_written_out(
    app, pty_device, page, tmp_path, ending
):
    # The finished session plays both blocks of the example flow (6 s and 2 s); the others are cut
    # off inside a 30 s block, long enough never to end first.
    a = app(
        port=pty_device.path,
        flow=EXAMPLE if ending == "finished" else example_flow(tmp_path, 30),
    )
    to_instructions(a, page)
    assert page.evaluate("document.activeElement.id") == "task-area"
    assert "Space" in page.inner_text("#task-legend")
    assert "space bar" in page.inner_text("#task-instructions")
    assert page.locator("#advance").count() == 0  # practice comes first
    practise(page)
    folder = session_folder_of(a)
    practice = named(folder, "trial")
    assert [t["payload"]["trial"] for t in practice] == [0, 1]
    assert all(
        t["payload"]["practice"] and not t["payload"]["scored"] for t in practice
    )
    assert all(t["payload"]["status"] == "done" for t in practice)
    assert (
        server_state(a)["phase"] == "waiting"
    )  # practice runs before the block's timer
    page.click("#advance")
    for i in range(2):
        answer(page, i, False)
    awaiting(page, 2, False)  # trial 2 is in progress (not answered) ...
    if ending == "finished":
        page.wait_for_selector(
            f'{SCREEN}[data-screen="between"]', timeout=15_000
        )  # ... when block1 ends
        assert "space bar" in page.inner_text("#between-instructions")
        page.click("#advance")  # block2 starts from the between-blocks screen
        page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]')
        page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
        assert page.inner_text("#between-instructions") == ""  # no block follows
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
        page.click("#advance")
    elif ending == "stopped":
        page.evaluate("document.exitFullscreen()")
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
        page.click("#fs-stop-here")
        page.click("#fs-leave-yes")
    else:
        # A lost link pauses the block (since 2026-11-25; until then it ended the
        # session with trial 2 unlogged); Stop here then ends the session.
        pty_device.hang_up()
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=30_000)
        page.click("#fs-stop-here")
        page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    state = server_state(a)
    assert state["failure"] is None and state["stopped"] == (ending != "finished")
    check_timing(folder)
    trials = named(folder, "trial")
    scored = [t for t in trials if not t["payload"]["practice"]]
    assert [t["payload"]["status"] for t in scored[:2]] == ["done", "done"]
    assert all(
        t["payload"]["scored"] and t["payload"]["correct"] is True for t in scored[:2]
    )
    if ending == "finished":
        # The trial in progress when each block ended is cut short and not scored.
        cut = [t for t in scored if t["payload"]["status"] == "cut_short"]
        assert [t["payload"]["block"] for t in cut] == ["block1", "block2"]
        assert not any(t["payload"]["scored"] for t in cut)
        for block, last in (("block1", cut[0]), ("block2", cut[1])):
            mine = [t for t in scored if t["payload"]["block"] == block]
            assert (
                mine[-1] is last
            )  # the last trial of the block, the earlier ones done
            assert all(t["payload"]["status"] == "done" for t in mine[:-1])
        assert cut[0]["payload"]["trial"] >= 2
    elif ending == "stopped":
        last = scored[-1]
        assert (last["payload"]["trial"], last["payload"]["status"]) == (2, "abandoned")
        assert (
            last["payload"]["reason"] == "fullscreen_lost"
            and not last["payload"]["scored"]
        )
        assert len(scored) == 3
    else:
        # The link was lost in trial 2: it is abandoned (the pause) and logged without a sample.
        last = scored[-1]
        assert (last["payload"]["trial"], last["payload"]["status"]) == (2, "abandoned")
        assert last["payload"]["reason"] == "link_lost" and not last["payload"]["scored"]
        assert last["payload"]["marked"] is False and last["payload"]["no_recording"] is True
        assert len(scored) == 3
    check_behavior(folder)
    check_delivery(folder)


def test_a_pause_abandons_the_trial_and_resume_starts_a_fresh_one_and_keys_are_filtered(
    app, pty_device, page, tmp_path
):
    # A pause abandons the block it is in (since 2026-11-25): Resume goes to the between screen
    # and the next block, which starts at its own first trial. Three blocks, so that a round left
    # after two pauses is partly played.
    a = app(port=pty_device.path, flow=example_flow(tmp_path, 30, blocks=3))
    to_instructions(a, page)
    practise(page)
    page.click("#advance")
    awaiting(page, 0, False)
    page.keyboard.down("Space")  # trial 0 is answered
    awaiting(page, 1, False, timeout=5000)
    page.keyboard.down(
        "Space"
    )  # held down: an auto-repeat, ignored (no response, no stray key)
    page.keyboard.up("Space")
    page.keyboard.press(
        "x"
    )  # a key that is not the task's: one stray key for trial 1 ...
    page.keyboard.press("x")  # ... and no second one
    answer(page, 1, False)
    awaiting(page, 2, False)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_function(
        "document.getElementById('task-area').dataset.awaiting === 'false'"
    )
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
    page.click("#advance")  # block 2: a fresh trial list from its first trial
    answer(page, 0, False)
    awaiting(page, 1, False)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    folder = session_folder_of(a)
    scored = [t for t in named(folder, "trial") if not t["payload"]["practice"]]
    status = {(t["block"], t["payload"]["trial"]): t["payload"]["status"] for t in scored}
    assert status == {
        ("block1", 0): "done",
        ("block1", 1): "done",
        ("block1", 2): "abandoned",
        ("block2", 0): "done",
        ("block2", 1): "abandoned",
    }
    for t in scored:
        assert t["payload"]["scored"] == (t["payload"]["status"] == "done")
        if t["payload"]["status"] == "abandoned":
            assert t["payload"]["reason"] == "fullscreen_lost"
    # Each block's trial list and its history: each trial saw the trials done before it in its
    # block (the abandoned ones are not in the history); block 2 starts with none.
    before = {(t["block"], t["payload"]["trial"]): t["payload"]["done_before"] for t in scored}
    assert before == {("block1", 0): 0, ("block1", 1): 1, ("block1", 2): 2, ("block2", 0): 0, ("block2", 1): 1}
    resps = [e for e in named(folder, "resp") if not e["payload"]["practice"]]
    assert [(r["block"], r["payload"]["trial"]) for r in resps] == [("block1", 0), ("block1", 1), ("block2", 0)]
    strays = named(folder, "stray")
    assert [(s["payload"]["trial"], s["payload"]["key"]) for s in strays] == [
        (1, "KeyX")
    ]
    ends = [e for e in events_of(folder) if e["name"] == "block_end"]
    assert [(e["block"], e["payload"].get("ended_by")) for e in ends] == [("block1", "interrupted"), ("block2", "interrupted")]
    check_timing(folder)
    state = server_state(a)
    # Before: "skipped" (one block, paused twice). Now two of three blocks were ended by a pause,
    # which counts them as played: the round is partly played.
    assert state["outcomes"]["example"] == "partial" and state["phase"] == "waiting"
    page.click("#advance")  # Finish: the session ends and the behavior file is written
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]')
    wait_for_file(folder / "behavior" / "example.csv")


def test_skip_and_stop_are_offered_on_the_instruction_screen(app, pty_device, page):
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    assert (
        page.locator("#skip-round").is_visible()
        and page.locator("#stop-here").is_visible()
    )
    page.click("#skip-round")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert server_state(a)["outcomes"]["example"] == "skipped"


def test_no_event_is_lost_while_the_connection_is_down(
    inprocess,  # noqa: F811  (the fixture imported from test_shell_browser)
    page,
    tmp_path,
):
    # Two blocks: the pause the drop causes abandons block 1 and Resume goes on with block 2.
    a = inprocess(example_flow(tmp_path, 30, blocks=2))
    to_instructions(a, page)
    practise(page)
    page.click("#advance")
    answer(page, 0, False)
    awaiting(page, 1, False)
    dropped = time.time()
    a.drop()
    page.wait_for_selector("#conn-lost", state="visible")
    assert a.state()["phase"] == "running"  # nothing could tell the server yet
    page.wait_for_function(
        "document.getElementById('task-area').dataset.awaiting === 'false'"
    )
    restored = time.time()
    a.restore()
    paused = a.wait_state(lambda s: s["phase"] == "paused")
    assert paused["reason"] == "connection_lost"
    page.wait_for_selector("#fs-lost", state="visible")
    assert "connection" in page.inner_text("#fs-lost-why")
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
    page.click("#advance")
    answer(page, 0, False)
    folder = a.hub.folder
    page.wait_for_function(
        "() => document.getElementById('task-area').dataset.trial === '1'"
    )
    scored = {
        (t["block"], t["payload"]["trial"]): t
        for t in named(folder, "trial")
        if not t["payload"]["practice"]
    }
    assert scored[("block1", 1)]["payload"]["status"] == "abandoned"
    assert scored[("block1", 1)]["payload"]["reason"] == "connection_lost"
    assert (
        scored[("block1", 1)]["payload"]["queued"] is True
    )  # kept while the socket was closed, sent after
    assert scored[("block2", 0)]["payload"]["status"] == "done"
    # The abandoned trial's summary reached the server before the pause it caused.
    paused_seq = next(e["seq"] for e in events_of(folder) if e["name"] == "paused")
    assert scored[("block1", 1)]["seq"] < paused_seq
    # Its stamp lag is the outage: it was made when the page saw the socket close (just after the
    # drop) and stamped when it arrived (after the restore).
    queued = scored[("block1", 1)]
    lag = behavior.lag_ms(queued)
    outage_ms = (restored - dropped) * 1000
    arrived_ms = (queued["unix_time"] - dropped) * 1000
    assert outage_ms - 1000 < lag <= arrived_ms + 5, (lag, outage_ms, arrived_ms)
    check_delivery(folder)
    check_timing(folder)


# -- the behavior file on its own ---------------------------------------------------------------------


def test_an_existing_behavior_file_with_more_rows_is_kept(
    app, pty_device, page, tmp_path
):
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    practise(page)
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    folder = session_folder_of(a)
    path = folder / "behavior" / "example.csv"
    wait_for_file(path)
    full = path.read_bytes()
    assert full.count(b"\n") == 3  # the header and the two practice trials
    copy = tmp_path / "copy"
    shutil.copytree(folder, copy)
    lines = (copy / "events.jsonl").read_text().splitlines(keepends=True)
    last = max(i for i, x in enumerate(lines) if '"example.trial"' in x)
    (copy / "events.jsonl").write_text("".join(lines[:last] + lines[last + 1 :]))
    assert behavior.write(copy) == []
    assert (copy / "behavior" / "example.csv").read_bytes() == full
    (copy / "behavior" / "example.csv").unlink()
    assert behavior.write(copy) == [copy / "behavior" / "example.csv"]
    assert (copy / "behavior" / "example.csv").read_bytes().count(b"\n") == 2


def test_a_behavior_file_that_cannot_be_written_leaves_the_session_and_its_files_whole(
    inprocess,  # noqa: F811  (the fixture imported from test_shell_browser)
    page,
    tmp_path,
    caplog,
):
    a = inprocess(EXAMPLE)
    to_instructions(a, page)
    practise(page)
    folder = a.hub.folder
    (folder / "behavior").write_text("a file where the folder would go")
    caplog.set_level(logging.ERROR, logger="brain_mapper")
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    state = a.wait_state(lambda s: s["phase"] == "done")
    assert state["stopped"] and state["failure"] is None and not state["ended_early"]
    wait_until(
        lambda: any(
            "behavior files could not be written" in r.getMessage()
            for r in caplog.records
        ),
        "the log line",
    )
    assert not any(
        folder.name in r.getMessage() for r in caplog.records
    )  # no label in the log
    events = events_of(folder)
    assert events[-1]["name"] == "stopped"
    assert len(named(folder, "trial")) == 2
    check_timing(folder)


# -- delivery: acknowledgements, a reload --------------------------------------------------------------


def drained(client) -> list[dict]:
    """The messages the server queued for ``client`` (it offers them straight into the queue)."""
    found = []
    while not client.messages.empty():
        text = client.messages.get_nowait()[0]
        if text is not None:
            found.append(json.loads(text))
    return found


def test_an_event_sent_twice_is_written_once_and_acknowledged_each_time(
    pty_device, tmp_path
):
    import asyncio

    settings = Settings(
        data_dir=tmp_path, port=pty_device.path, replay=None, flow=EXAMPLE, http_port=1
    )
    hub = Hub(settings)
    hub.start()
    loop = asyncio.new_event_loop()
    try:
        assert hub.ready.wait(20), "the source did not open"
        hub.accept("P001")
        page = Client(loop, "participant", "page-1")
        assert hub.connect(page)
        drained(page)
        command = {
            "cmd": "event",
            "name": "example.probe",
            "payload": {"n": 1},
            "browser_time": 5.0,
            "time_origin": time.time() * 1000 - 5,
            "cid": "abc-1",
        }
        hub.command(page, dict(command))
        hub.command(
            page, dict(command)
        )  # sent again: its acknowledgement was lost, say
        acks = [m for m in drained(page) if m["t"] == "ack"]
        assert len(acks) == 2 and acks[0] == acks[1]
        assert acks[0]["cid"] == "abc-1" and isinstance(acks[0]["seq"], int)
        written = [e for e in events_of(hub.folder) if e["name"] == "example.probe"]
        assert len(written) == 1
        assert written[0]["seq"] == acks[0]["seq"]
        assert written[0]["payload"] == {"n": 1, "cid": "abc-1"}
        for bad in ("a b", "", "x" * 65, 5):
            with pytest.raises(ValueError, match="cid"):
                hub.command(page, {**command, "cid": bad})
        # Once the session is over an event is refused for good: the page is told why and stops
        # sending it.
        hub.session.advance()  # the participant ID is done: the session can be stopped
        hub.session.stop()
        drained(page)
        hub.command(page, {**command, "cid": "abc-2"})
        acks = [m for m in drained(page) if m["t"] == "ack"]
        assert acks == [{"t": "ack", "cid": "abc-2", "refused": "the session is over"}]
        assert [
            e["payload"].get("cid")
            for e in events_of(hub.folder)
            if e["name"] == "example.probe"
        ] == ["abc-1"]
    finally:
        hub.stop()
        loop.close()


def answer_next(page, practice: bool = False) -> int:
    """Waits for the next trial to await a key, presses the space bar, returns the trial's number."""
    page.wait_for_selector(
        f'{AREA}[data-practice="{str(practice).lower()}"][data-awaiting="true"]',
        timeout=15_000,
    )
    number = int(page.get_attribute(AREA, "data-trial"))
    page.keyboard.press("Space")
    page.wait_for_function(
        f"document.getElementById('task-area').dataset.trial !== '{number}'"
        " || document.getElementById('task-area').dataset.awaiting === 'false'"
    )
    return number


def test_a_reload_loses_no_event_and_goes_on_with_the_trial_list_and_its_history(
    app, pty_device, page, tmp_path
):
    # Two blocks: the pause of the reload abandons block 1; Resume goes on with block 2, whose
    # trial list starts again (the reloaded page rebuilds its state from sessionStorage).
    a = app(port=pty_device.path, flow=example_flow(tmp_path, 60, blocks=2))
    to_instructions(a, page)
    practise(page)
    page.click("#advance")
    answer(page, 0, False)
    answer(page, 1, False)
    awaiting(page, 2, False)
    page.reload()
    page.wait_for_selector("#fs-lost", state="visible")
    wait_until(
        lambda: server_state(a)["phase"] == "paused", "the pause of the reloaded page"
    )
    assert server_state(a)["reason"] == "page_reloaded"
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
    page.click("#advance")
    answer(page, 0, False)  # block 2 from its first trial: trial 2 of block 1 was abandoned
    answer(page, 1, False)
    awaiting(page, 2, False)
    folder = session_folder_of(a)

    def scored():
        return {
            (t["block"], t["payload"]["trial"]): t["payload"]
            for t in named(folder, "trial")
            if not t["payload"]["practice"]
        }

    wait_until(lambda: len(scored()) == 5, "five trial records")
    found = scored()
    assert {k: p["status"] for k, p in found.items()} == {
        ("block1", 0): "done",
        ("block1", 1): "done",
        ("block1", 2): "abandoned",
        ("block2", 0): "done",
        ("block2", 1): "done",
    }
    assert found[("block1", 2)]["reason"] == "page_closed"
    # Each block's history: block 1's trials saw those done before them; block 2 starts with none.
    assert {k: p["done_before"] for k, p in found.items()} == {
        ("block1", 0): 0,
        ("block1", 1): 1,
        ("block1", 2): 2,
        ("block2", 0): 0,
        ("block2", 1): 1,
    }
    check_delivery(folder)
    loads = {
        e["payload"]["cid"].rsplit("-", 1)[0]
        for e in events_of(folder)
        if "cid" in e["payload"]
    }
    assert len(loads) == 2  # two page loads, each with its own client ids
    assert page.evaluate(
        "Object.keys(sessionStorage).filter((k) => k.startsWith('brain-mapper-task/outbox/'))"
    ) == []  # every event acknowledged: no outbox left
    check_timing(folder)


# -- keys on buttons, practice and full screen -------------------------------------------------------


def test_a_key_on_a_button_presses_it_and_is_no_response_and_practice_stops_when_full_screen_closes(
    app, pty_device, page
):
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    page.click("#practice")
    awaiting(page, 0, True)
    page.click(
        "#skip-round"
    )  # the question opens while practice trial 0 waits for a key
    page.wait_for_selector("#leave-ask", state="visible")
    page.focus("#leave-no")
    page.keyboard.press("Space")  # presses "Keep going" ...
    page.wait_for_selector("#leave-ask", state="hidden")
    assert page.get_attribute(AREA, "data-awaiting") == "true"  # ... and is no response
    page.focus(AREA)
    page.keyboard.press("Space")
    awaiting(page, 1, True)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    page.wait_for_function(
        "document.getElementById('task-area').dataset.mode === 'instructions'"
    )
    page.focus("#fs-return")
    page.keyboard.press("Space")  # presses "Return to full screen"
    page.wait_for_selector("#fs-lost", state="hidden")
    assert page.evaluate("document.fullscreenElement !== null")
    assert page.locator(
        "#practice"
    ).is_visible()  # practice starts again from the instructions
    folder = session_folder_of(a)
    wait_until(lambda: len(named(folder, "trial")) == 2, "two practice records")
    trials = [t["payload"] for t in named(folder, "trial")]
    assert [(t["trial"], t["status"], t.get("reason")) for t in trials] == [
        (0, "done", None),
        (1, "abandoned", "fullscreen_lost"),
    ]
    assert [r["payload"]["trial"] for r in named(folder, "resp")] == [0]
    assert named(folder, "stray") == []
    assert server_state(a)["phase"] == "waiting"


# -- the behavior file from hand-made session files -----------------------------------------------------


ORIGIN = 1_700_000_000_000.0


def page_event(seq: int, name: str, payload: dict, browser: float, lag: float) -> dict:
    return {
        "seq": seq,
        "step": name.split(".")[0],
        "block": "b1",
        "name": name,
        "payload": payload,
        "browser_time": browser,
        "time_origin": ORIGIN,
        "unix_time": (ORIGIN + browser + lag) / 1000,
    }


def session_files(folder: Path, events: list[dict]) -> None:
    (folder / "eeg").mkdir(parents=True)
    (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    markers = [
        {
            "label": f"{e['step']}/{e['name']}#{e['seq']}",
            "sample": 1000 + 10 * e["seq"],
            "unix_time": e["unix_time"],
        }
        for e in events
    ]
    (folder / "eeg" / "markers.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in markers)
    )


def trial_events(
    task: str, trial: int, first_seq: int, queued_resp: bool = False, **extra
) -> list[dict]:
    where = {"block": "b1", "trial": trial, "practice": False}
    t0 = 1000.0 * (trial + 1)
    resp = {"key": "Space", "rt_ms": 300.0, **where}
    if queued_resp:
        resp["queued"] = True
    return [
        page_event(first_seq, f"{task}.stim", {"part": "stim", **where}, t0, 7.0),
        page_event(
            first_seq + 1, f"{task}.stim_off", {"part": "stim", **where}, t0 + 50, 4.0
        ),
        page_event(
            first_seq + 2, f"{task}.stim", {"part": "probe", **where}, t0 + 100, 6.0
        ),
        page_event(
            first_seq + 3,
            f"{task}.resp",
            resp,
            t0 + 300,
            30_000.0 if queued_resp else 9.0,
        ),
        page_event(
            first_seq + 4,
            f"{task}.trial",
            {**extra, **where, "status": "done", "rt_ms": 300.0, "response": "Space"},
            t0 + 310,
            5.0,
        ),
    ]


def test_the_behaviour_file_joins_the_timing_guards_formulas_and_renames_colliding_keys(
    tmp_path,
):
    folder = tmp_path / "session"
    extra = {
        "word": "=SUM(A1)",
        "note": "-3",
        "minus": -3,
        "plus": "+1",
        "at": "@x",
        "seq": "mine",
        "stim_lag_ms": 1,
    }
    session_files(
        folder,
        trial_events("t", 0, 1, **extra)
        + trial_events("t", 1, 6, queued_resp=True, **extra),
    )
    assert behavior.write(folder) == [folder / "behavior" / "t.csv"]
    with (folder / "behavior" / "t.csv").open(newline="") as stream:
        rows = list(csv.reader(stream))
    timing = [
        f"{p}_{f}"
        for p in ("stim", "probe")
        for f in ("seq", "sample", "recording", "lag_ms", "off_ms")
    ]
    timing += ["resp_seq", "resp_sample", "resp_recording", "resp_lag_ms", "queued"]
    payload = [  # block, trial and practice equal the fixed columns in every row: no copies
        "at",
        "minus",
        "note",
        "plus",
        "response",
        "rt_ms",
        "seq",
        "status",
        "stim_lag_ms",
        "word",
    ]
    fixed = behavior.FIRST + timing
    assert rows[0] == fixed + [f"payload_{k}" if k in fixed else k for k in payload]
    first, second = (dict(zip(rows[0], r)) for r in rows[1:])
    assert (first["seq"], first["sample"]) == ("5", "1050")
    assert (
        first["stim_seq"],
        first["stim_sample"],
        first["probe_seq"],
        first["resp_seq"],
    ) == ("1", "1010", "3", "4")
    assert float(first["stim_lag_ms"]) == pytest.approx(7.0, abs=0.01)
    assert float(first["probe_lag_ms"]) == pytest.approx(6.0, abs=0.01)
    assert float(first["resp_lag_ms"]) == pytest.approx(9.0, abs=0.01)
    assert float(first["stim_off_ms"]) == 50.0 and first["probe_off_ms"] == ""
    assert first["queued"] == "false" and second["queued"] == "true"
    assert float(second["resp_lag_ms"]) == pytest.approx(30_000.0, abs=0.01)
    # A text cell that a spreadsheet would read as a formula gets a quote; a number does not.
    assert (first["word"], first["note"], first["plus"], first["at"]) == (
        "'=SUM(A1)",
        "'-3",
        "'+1",
        "'@x",
    )
    assert first["minus"] == "-3"
    # Payload keys named like a fixed column keep their own column.
    assert (first["payload_seq"], first["payload_stim_lag_ms"]) == ("mine", "1")


def test_one_task_whose_file_cannot_be_written_does_not_stop_the_others(
    tmp_path, caplog
):
    folder = tmp_path / "session"
    session_files(folder, trial_events("a", 0, 1) + trial_events("b", 0, 6))
    (folder / "behavior" / "a.csv").mkdir(
        parents=True
    )  # a folder where a's file would go
    caplog.set_level(logging.ERROR, logger="brain_mapper")
    assert behavior.write(folder) == [folder / "behavior" / "b.csv"]
    assert (folder / "behavior" / "b.csv").read_text().count("\n") == 2
    assert any("task a could not be written" in r.getMessage() for r in caplog.records)
    assert not list((folder / "behavior").glob(".*.tmp"))


# -- frame timing ----------------------------------------------------------------------------------


FRAME_CHECKS = """
async ({series, dues}) => {
  const f = await import('/static/js/tasks/frames.js');
  const period = f.framePeriod(series);
  const picks = dues.map((due) => {
    const i = f.pickFrame(series, due, period);
    return {due, i, ts: series[i], before: i > 0 ? series[i - 1] : null,
            late: f.lateFrames(series[i], due, period)};
  });
  return {period, picks, frames200: f.framesFor(200, period), short: f.framePeriod(series.slice(0, 30))};
}
"""

RECORD_FRAMES = """
(count) => new Promise((resolve) => {
  const times = [];
  const step = (ts) => { times.push(ts); if (times.length < count) requestAnimationFrame(step); else resolve(times); };
  requestAnimationFrame(step);
})
"""


def check_frame_series(page, series: list[float], tolerance: float) -> dict:
    """Runs the runner's frame arithmetic on ``series`` for due times spread over it (none in the
    last frames, which have no next frame to compare with) and checks each pick."""
    span = series[-1] - series[0]
    dues = [series[0] + 20 + i * span / 97.3 for i in range(90)]
    dues = [d for d in dues if d < series[-3]]
    out = page.evaluate(FRAME_CHECKS, {"series": series, "dues": dues})
    period = out["period"]
    assert out["short"] is None  # fewer than 30 intervals: not measured yet
    for pick in out["picks"]:
        # the nearest frame: no more than half a frame (plus the jitter of the series) from the due
        # time, and no farther than the frame before it
        assert abs(pick["ts"] - pick["due"]) <= period / 2 + tolerance, pick
        if pick["before"] is not None:
            assert abs(pick["before"] - pick["due"]) >= abs(pick["ts"] - pick["due"]) - tolerance, pick
        assert pick["late"] == 0, pick
    return out


MISSED = """async (cases) => {
  const f = await import('/static/js/tasks/frames.js');
  return cases.map(([prev, ts, due, p]) => [f.missedFrames(prev, ts, due, p), f.lateFrames(ts, due, p)]);
}"""


def test_a_change_due_half_way_between_two_frames_is_not_late_unless_a_frame_was_dropped(
    app, pty_device, page
):
    # Measured in Chromium (60 Hz): a change due exactly half a frame between two frames (the lit
    # squares of spatial with gap_ms 825 or 875, 79.5 or 82.5 frames after the trial's first
    # frame) is put on the later frame in about 2 % of cases, when the interval between the two
    # frames is 16.8 ms instead of 16.7 (the runner's period): the earlier frame is then 8.36 ms
    # early, just more than half a frame, and the later one 8.4 ms late, 0.503 of a frame, which
    # `lateFrames` rounds to 1. No frame was dropped, so `missedFrames` (the runner's measure when
    # it saw the frame before) counts 0; a frame dropped between them counts 1.
    a = app(port=pty_device.path, flow=EXAMPLE)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    p = 16.7
    due = 1000.0
    cases = [
        [due - 8.4, due + 8.4, due, p],  # the tie, with a 16.8 ms interval: nothing dropped
        [due - 8.3, due + 8.4, due, p],  # (the earlier frame would have been due: not a real case)
        [due - 8.4, due + 8.4 + p, due, p],  # one frame dropped after the earlier frame: 1 late
        [due - 30.0, due + 3.4 + p, due, p],  # two dropped, the first not yet due: 1 late
        [due - 3 * p, due - 2 * p, due, p],  # not due yet (never painted): nothing missed
    ]
    assert page.evaluate(MISSED, cases) == [[0, 1], [0, 1], [1, 2], [1, 1], [0, 0]]


def test_the_frame_arithmetic_picks_the_nearest_frame_at_60_120_and_144_hz(app, pty_device, page):
    a = app(port=pty_device.path, flow=EXAMPLE)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    # A series recorded from this Chromium (its display runs at 60 Hz here) ...
    recorded = page.evaluate(RECORD_FRAMES, 241)
    out = check_frame_series(page, recorded, tolerance=2.0)
    assert out["period"] == pytest.approx(1000 / 60, abs=0.5)
    assert out["frames200"] == 12
    # ... and exact series for 120 and 144 Hz, which this machine cannot produce.
    for hz, frames in ((120, 24), (144, 29)):
        start = 1234.5
        series = [start + i * 1000 / hz for i in range(hz * 2 + 1)]
        out = check_frame_series(page, series, tolerance=1e-9)
        assert out["period"] == pytest.approx(1000 / hz, abs=1e-9)
        assert out["frames200"] == frames
    # A missed frame: a change due at a frame that never came goes on the next one, one frame late.
    period = 1000 / 60
    gap = [i * period for i in range(40)]
    del gap[20]
    late = page.evaluate(
        """async ({series, due}) => {
          const f = await import('/static/js/tasks/frames.js');
          const p = f.framePeriod(series);
          const i = f.pickFrame(series, due, p);
          return {ts: series[i], late: f.lateFrames(series[i], due, p)};
        }""",
        {"series": gap, "due": 20 * period},
    )
    assert late == {"ts": pytest.approx(21 * period), "late": 1}
    # The scoring helper keeps only the records with scored === true.
    kept = page.evaluate(
        """async () => {
          const r = await import('/static/js/tasks/runner.js');
          return r.scoredRecords([{trial: 0, scored: true}, {trial: 1, scored: false},
                                  {trial: 2}, null, {trial: 3, scored: "true"}, {trial: 4, scored: true}]);
        }"""
    )
    assert [r["trial"] for r in kept] == [0, 4]


def timing_flow(tmp_path: Path) -> Path:
    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {
            "id": "flashes",
            "kind": "task",
            "title": "Flashes",
            "module": "timing",
            "blocks": [{"id": "block1", "seconds": 95, "points": 100}],
        },
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "timing.json"
    path.write_text(json.dumps(raw))
    return path


def test_thirty_200_ms_stimuli_last_whole_frames_and_anchored_onsets_do_not_drift(
    app, pty_device, page, tmp_path
):
    a = app(port=pty_device.path, flow=timing_flow(tmp_path))
    begin(a, page)
    advance_until(page, "flashes", "waiting")
    page.click("#advance")  # no practice: Start
    folder = session_folder_of(a)

    def offs():
        return [e for e in events_of(folder) if e["name"] == "timing.stim_off"]

    wait_until(lambda: len(offs()) == 30, "30 offsets", timeout=110)
    stims = [e for e in events_of(folder) if e["name"] == "timing.stim"]
    assert [s["payload"]["flash"] for s in stims] == list(range(30))
    off = offs()
    first = stims[0]["browser_time"]
    durations, errors = [], []
    for k, (on, gone) in enumerate(zip(stims, off)):
        f = gone["payload"]["frame_ms"]
        assert f == pytest.approx(1000 / 60, abs=1.0)  # measured on the page
        frames = gone["payload"]["frames"]
        assert frames == round(200 / f)  # the nearest whole number of frames
        duration = gone["browser_time"] - on["browser_time"]
        durations.append(duration)
        assert abs(duration - 200) <= f, (k, duration, f)
        assert gone["payload"]["due_ms"] == pytest.approx(on["browser_time"] + frames * f)
        if k:
            due = first + k * 2500
            assert on["payload"]["due_ms"] == pytest.approx(due)
            error = on["browser_time"] - due
            errors.append(error)
            assert abs(error) <= on["payload"]["frame_ms"], (k, error)
            assert on["payload"]["late_frames"] == 0, (k, on["payload"])
    # no drift: the onset errors of the last flashes are no larger than those of the first
    assert max(abs(e) for e in errors[-5:]) <= on["payload"]["frame_ms"] / 2 + 2
    print(
        f"200 ms stimuli: {min(durations):.2f} to {max(durations):.2f} ms; "
        f"onset error {min(errors):.2f} to {max(errors):.2f} ms"
    )


# -- what stays on the screen ----------------------------------------------------------------------


def test_an_abandoned_trial_takes_its_stimulus_off_the_stage_at_once(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=example_flow(tmp_path, 30))
    to_instructions(a, page)
    page.click("#practice")
    awaiting(page, 0, True)  # the square is on the panel
    assert page.locator("#task-panel .example-square").count() == 1
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0
    page.click("#fs-return")
    page.click("#practice")
    answer(page, 1, True)
    answer(page, 2, True)
    page.wait_for_selector("#advance", state="visible")
    page.click("#advance")
    awaiting(page, 0, False)
    assert page.locator("#task-panel .example-square").count() == 1
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_selector("#fs-lost", state="visible")
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0
    folder = session_folder_of(a)
    wait_until(lambda: len(named(folder, "stim_off")) >= 2, "the offsets")
    offs = {(o["payload"]["trial"], o["payload"]["practice"]) for o in named(folder, "stim_off")}
    assert {(0, True), (0, False)} <= offs  # each abandoned stimulus's offset was logged


# -- the stimulus area --------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(1024, 768), (1920, 1080)])
def test_the_stimulus_area_is_a_centered_square_on_the_page_ground_above_the_legend(
    app, pty_device, page, tmp_path, size
):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    page.click("#practice")
    awaiting(page, 0, True)
    panel = page.locator("#task-panel").bounding_box()
    legend = page.locator("#task-legend").bounding_box()
    width, height = page.evaluate("[innerWidth, innerHeight]")
    assert panel["width"] == pytest.approx(panel["height"], abs=0.5)
    assert panel["width"] >= 0.5 * min(width, height)
    assert abs(panel["x"] + panel["width"] / 2 - width / 2) <= 1
    assert panel["y"] >= 0 and panel["y"] + panel["height"] <= height
    assert legend["y"] >= panel["y"] + panel["height"]  # the legend is below it ...
    assert legend["y"] + legend["height"] <= height  # ... and on the screen
    colors = page.evaluate(
        """() => {
          const s = getComputedStyle(document.getElementById('task-panel'));
          const q = getComputedStyle(document.querySelector('#task-panel .example-square'));
          return [s.backgroundColor, s.color, q.backgroundColor, getComputedStyle(document.body).backgroundColor];
        }"""
    )
    # No ground of its own: white stimuli straight on the page's navy.
    assert colors == ["rgba(0, 0, 0, 0)", "rgb(255, 255, 255)", "rgb(255, 255, 255)", "rgb(18, 24, 38)"]
    frame = page.evaluate(
        """() => {
          const s = getComputedStyle(document.getElementById('task-panel'));
          return [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth, s.borderLeftWidth,
                  s.boxShadow, s.outlineStyle, s.backgroundImage];
        }"""
    )
    assert frame == ["0px", "0px", "0px", "0px", "none", "none", "none"]


# -- storage per session, the seed of recall alone -------------------------------------------------------


def words_and_recall_flow(tmp_path: Path) -> Path:
    def step(id_, kind, title):
        return {
            "id": id_,
            "kind": kind,
            "title": title,
            "module": "example",
            "blocks": [{"id": "block1", "seconds": 4, "points": 100}],
        }

    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        step("words", "task", "Words"),
        step("recall", "recall", "Delayed free recall"),
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "words_recall.json"
    path.write_text(json.dumps(raw))
    return path


def play_block(page) -> None:
    """Practice, Start, one answered trial, then the block ends by itself."""
    practise(page)
    page.click("#advance")
    answer(page, 0, False)
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)


def test_a_later_session_in_the_same_tab_starts_clean_and_recall_alone_takes_the_studied_seed(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    port = free_port()
    flow_path = words_and_recall_flow(tmp_path)
    first = app(port=device().path, flow=flow_path, http_port=port)
    begin(first, page)
    advance_until(page, "words", "waiting")
    play_block(page)
    advance_until(page, "recall", "waiting")
    play_block(page)  # practice of the recall step is done in this session
    state_a = server_state(first)
    seed, session_a = state_a["seed"], state_a["session_id"]
    assert isinstance(session_a, str) and len(session_a) >= 16
    assert "P001" not in session_a
    saved = json.loads((session_folder_of(first) / "session.json").read_text())
    assert saved["session_id"] == session_a
    keys = page.evaluate("Object.keys(sessionStorage)")
    runner_keys = [
        k for k in keys if k.startswith("brain-mapper-task/") and not k.startswith("brain-mapper-task/outbox/")
    ]
    assert runner_keys and all(k.startswith(f"brain-mapper-task/{session_a}/") for k in runner_keys)
    stop(first)

    # The same tab, the same address: recall alone, in a new session.
    second = app(port=device().path, flow=flow_path, http_port=port, data_dir=first.data_dir)
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="recall"]')
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    advance_until(page, "recall", "waiting")
    state_b = server_state(second)
    assert state_b["seed"] == seed  # the words the participant studied
    assert state_b["session_id"] != session_a
    # Nothing of the first session's progress: practice is offered again and trials start at 0.
    page.wait_for_selector("#practice", state="visible")
    page.click("#practice")
    awaiting(page, 0, True)
    # ... and the first session's stored values are gone from the tab (its outbox would be sent)
    keys = page.evaluate("Object.keys(sessionStorage)")
    assert not [k for k in keys if k.startswith(f"brain-mapper-task/{session_a}/")]


def test_recall_alone_takes_the_seed_of_the_newest_session_with_the_words_studied(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    tmp_path,
):
    flow_path = words_and_recall_flow(tmp_path)
    seeds = []
    data_dir = tmp_path / "data"
    for _ in range(2):  # two sessions that study the words, each with its own seed
        a = app(port=device().path, flow=flow_path, data_dir=data_dir)
        accept(a)
        with open_ws(a) as ws:
            state = next_state(ws)
            while not (state["step"] == "words" and state["phase"] == "waiting"):
                send(ws, cmd="advance")
                state = next_state(ws, predicate=lambda m, s=state: m["rev"] > s["rev"])
            send(ws, cmd="advance")
            next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "between")
            send(ws, cmd="advance")  # on to the recall step: the words round is finished
            state = next_state(ws, predicate=lambda m: m["step"] == "recall")
            assert state["outcomes"]["words"] == "finished"
            seeds.append(state["seed"])
            send(ws, cmd="stop")
            next_state(ws, predicate=lambda m: m["stopped"])
        stop(a)
    # Two random 31-bit seeds are equal with probability 2^-31; then the test could not tell them apart.
    assert seeds[0] != seeds[1]
    b = app(port=device().path, flow=flow_path, data_dir=data_dir)
    status, _ = request(b, "POST", "/api/participant", {"label": "P001", "only": "recall"})
    assert status == 200
    assert server_state(b)["seed"] == seeds[1]  # the newer of the two
    stop(b)
    # From recall on (start_at): recall is played without the words before it, so the same rule.
    c = app(port=device().path, flow=flow_path, data_dir=data_dir)
    status, _ = request(c, "POST", "/api/participant", {"label": "P001", "start_at": "recall"})
    assert status == 200
    assert server_state(c)["plan"] == ["participant", "recall", "summary"]
    assert server_state(c)["seed"] == seeds[1]
    stop(c)
    # A whole route plays the words itself first: a new seed of its own.
    d = app(port=device().path, flow=flow_path, data_dir=data_dir)
    assert request(d, "POST", "/api/participant", {"label": "P001"})[0] == 200
    assert server_state(d)["seed"] not in seeds


# -- payload keys and the CSV ----------------------------------------------------------------------------


def next_message(ws, kind: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"no {kind} message"
        try:
            message = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        if message.get("t") == kind:
            return message


def test_any_payload_key_or_part_is_stored_as_sent_and_its_csv_column_is_neutral_and_guarded(app, pty_device):
    a = app(port=pty_device.path, flow=EXAMPLE)
    accept(a)
    origin = time.time() * 1000 - 100
    where = {"block": "block1", "trial": 0, "practice": False}

    def event(ws, cid, name, payload):
        send(ws, cmd="event", name=name, payload=payload, browser_time=50.0, time_origin=origin, cid=cid)

    with open_ws(a) as ws:
        next_state(ws)
        send(ws, cmd="advance")  # the participant step is done: events go to the example step
        next_state(ws, predicate=lambda m: m["step"] == "example")
        event(ws, "c-1", "example.stim", {"part": "+x", **where})
        assert next_message(ws, "ack")["cid"] == "c-1"
        payload = {
            "delayMs": 300,
            "=cmd": 1,
            "x_delayms": "a key that the neutral name of delayMs would take",
            "cid": "the task's own",
            "tabbed": "\tcmd",
            "returned": "\r=1",
            "nested": ["=HYPERLINK(1)", {"k": "@x"}],
            "obj": {"a": "-1"},
            "status": "done",
            **where,
        }
        event(ws, "c-2", "example.trial", payload)
        ack = next_message(ws, "ack")
        assert ack["cid"] == "c-2" and isinstance(ack["seq"], int) and "refused" not in ack
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["phase"] == "done")
    folder = session_folder_of(a)
    ours = [e for e in events_of(folder) if e["name"].startswith("example.")]
    assert [e["name"] for e in ours] == ["example.stim", "example.trial"]
    assert ours[0]["payload"] == {"part": "+x", **where, "cid": "c-1"}  # verbatim
    stored = dict(payload)
    stored["payload_cid"] = stored.pop("cid")  # the task's own `cid` is kept apart
    assert ours[1]["payload"] == {**stored, "cid": "c-2"}
    path = folder / "behavior" / "example.csv"
    wait_for_file(path)
    with path.open(newline="") as stream:
        rows = list(csv.reader(stream))
    header = rows[0]
    cells = dict(zip(header, rows[1]))
    # neutral names: x_ and the name in lower case, other characters as _; collisions get _2
    assert "x__x_seq" in header and "x__x_lag_ms" in header  # the part +x
    assert cells["x_delayms"] == "a key that the neutral name of delayMs would take"
    assert cells["x_delayms_2"] == "300" and cells["x__cmd"] == "1"
    assert cells["payload_cid"] == "the task's own" and cells["cid"] == "c-2"
    assert cells["tabbed"] == "'\tcmd" and cells["returned"] == "'\r=1"
    assert json.loads(cells["nested"]) == ["=HYPERLINK(1)", {"k": "@x"}]  # JSON text, starting with [
    assert json.loads(cells["obj"]) == {"a": "-1"}
    assert not any(c.startswith(behavior.FORMULA) for c in header)
    assert all(behavior.NAME.fullmatch(c) for c in header)
    names = json.loads((folder / "behavior" / "columns.json").read_text())
    assert names["example.csv"] == {
        "x__x_seq": "+x_seq",
        "x__x_sample": "+x_sample",
        "x__x_recording": "+x_recording",
        "x__x_lag_ms": "+x_lag_ms",
        "x__x_off_ms": "+x_off_ms",
        "x__cmd": "=cmd",
        "x_delayms_2": "delayMs",
    }


def test_column_names_from_any_event_log_are_neutral_and_guarded(tmp_path):
    folder = tmp_path / "session"
    events = trial_events("t", 0, 1)
    events[2]["payload"]["part"] = "=probe"
    events[4]["payload"]["\tkey"] = 1
    events[4]["payload"]["+sum"] = "x"
    session_files(folder, events)
    behavior.write(folder)
    with (folder / "behavior" / "t.csv").open(newline="") as stream:
        header = next(csv.reader(stream))
    assert "x__probe_seq" in header and "x__key" in header and "x__sum" in header
    assert not any(h.startswith(behavior.FORMULA) for h in header)
    names = json.loads((folder / "behavior" / "columns.json").read_text())["t.csv"]
    assert names["x__key"] == "\tkey" and names["x__sum"] == "+sum" and names["x__probe_seq"] == "=probe_seq"


def test_each_sample_column_says_which_recording_it_belongs_to(tmp_path):
    """A drop splits a trial: its stimulus marker is in the recording set aside (``eeg-lost-1``),
    its response and trial markers in the new one (``eeg``). Sample numbers start at 0 in each,
    so every sample column has a recording column beside it."""
    folder = tmp_path / "session"
    events = trial_events("t", 0, 1)
    session_files(folder, events)
    lost = folder / "eeg-lost-1"
    lost.mkdir()
    marks = [json.loads(x) for x in (folder / "eeg" / "markers.jsonl").read_text().splitlines()]
    (lost / "markers.jsonl").write_text("".join(json.dumps(m) + "\n" for m in marks[:3]))
    (folder / "eeg" / "markers.jsonl").write_text(
        "".join(json.dumps({**m, "sample": 5 + i}) + "\n" for i, m in enumerate(marks[3:]))
    )
    behavior.write(folder)
    with (folder / "behavior" / "t.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    (cells,) = rows
    assert cells["stim_recording"] == "eeg-lost-1" and cells["stim_sample"] == "1010"
    assert cells["probe_recording"] == "eeg-lost-1" and cells["probe_sample"] == "1030"
    assert cells["resp_recording"] == "eeg" and cells["resp_sample"] == "5"
    assert cells["recording"] == "eeg" and cells["sample"] == "6"
    header = list(rows[0])
    for part in ("stim", "probe", "resp"):
        assert header.index(f"{part}_recording") == header.index(f"{part}_sample") + 1


# -- waits after a response --------------------------------------------------------------------------


def orders_flow(tmp_path: Path) -> Path:
    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {
            "id": "orders",
            "kind": "task",
            "title": "Orders",
            "module": "timing",
            "blocks": [
                {"id": "wait_then_key", "seconds": 8, "points": 100},
                {"id": "key_then_wait", "seconds": 8, "points": 100},
            ],
        },
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(raw))
    return path


def test_a_wait_after_a_response_counts_from_the_response_in_both_orders(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=orders_flow(tmp_path))
    begin(a, page)
    advance_until(page, "orders", "waiting")
    page.click("#advance")
    for trial in (0, 1):
        awaiting(page, trial, False)
        page.wait_for_timeout(400)  # a key well after the wait before it ended
        page.keyboard.press("Space")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=20_000)
    page.click("#advance")
    for trial in (0, 1):
        awaiting(page, trial, False)
        page.wait_for_timeout(250)
        page.keyboard.press("Space")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=20_000)
    folder = session_folder_of(a)
    events = [e for e in events_of(folder) if e["name"].startswith("timing.")]
    checked = 0
    for block, wait_ms in (("wait_then_key", 500), ("key_then_wait", 300)):
        for trial in (0, 1):
            mine = [e for e in events if e["block"] == block and e["payload"].get("trial") == trial]
            stim = next(e for e in mine if e["name"] == "timing.stim")
            resp = next(e for e in mine if e["name"] == "timing.resp")
            off = next(e for e in mine if e["name"] == "timing.stim_off")
            f = off["payload"]["frame_ms"]
            onset = stim["browser_time"]
            key_on_grid = onset + round((resp["browser_time"] - onset) / f) * f
            frames = round(wait_ms / f)
            assert off["payload"]["frames"] == frames
            assert off["payload"]["due_ms"] == pytest.approx(key_on_grid + frames * f, abs=0.01)
            assert off["payload"]["late_frames"] == 0, off["payload"]
            assert abs(off["browser_time"] - off["payload"]["due_ms"]) <= f / 2 + 1
            checked += 1
    assert checked == 4


def test_a_response_window_that_times_out_ends_on_the_frame_grid(app, pty_device, page, tmp_path):
    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {
            "id": "orders",
            "kind": "task",
            "title": "Orders",
            "module": "timing",
            "blocks": [{"id": "timeouts", "seconds": 45, "points": 100}],
        },
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    flow = tmp_path / "timeouts.json"
    flow.write_text(json.dumps(raw))
    a = app(port=pty_device.path, flow=flow)
    begin(a, page)
    advance_until(page, "orders", "waiting")
    page.click("#advance")
    folder = session_folder_of(a)

    def offs():
        return [e for e in events_of(folder) if e["name"] == "timing.stim_off"]

    wait_until(lambda: len(offs()) >= 20, "20 timeouts", timeout=60)
    trials = {e["payload"]["trial"]: e for e in events_of(folder) if e["name"] == "timing.trial"}
    stims = {e["payload"]["trial"]: e for e in events_of(folder) if e["name"] == "timing.stim"}
    for off in offs()[:20]:
        number = off["payload"]["trial"]
        assert trials[number]["payload"]["response"] is None  # the window timed out
        assert off["payload"]["due_ms"] == pytest.approx(stims[number]["browser_time"] + 1500)
        assert off["payload"]["late_frames"] == 0, off["payload"]
        assert off["browser_time"] - off["payload"]["due_ms"] < off["payload"]["frame_ms"] / 2 + 0.01


# -- a persistent layer ------------------------------------------------------------------------------------


WATCH_CROSS = """
() => {
  window.__crossRemoved = 0;
  window.__framesWithout = 0;
  window.__framesWith = 0;
  const panel = document.getElementById('task-panel');
  new MutationObserver((records) => {
    for (const r of records) for (const n of r.removedNodes) {
      if (n.nodeType === 1 && (n.matches('.example-cross') || n.querySelector('.example-cross'))) window.__crossRemoved += 1;
    }
  }).observe(panel, {childList: true, subtree: true});
  let seen = false;
  const sample = () => {
    const here = !!panel.querySelector('.example-cross');
    if (here) { seen = true; window.__framesWith += 1; } else if (seen) window.__framesWithout += 1;
    if (!window.__stopSampling) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
}
"""


def test_the_background_layer_stays_across_trial_boundaries_and_goes_on_a_pause(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=example_flow(tmp_path, 30))
    to_instructions(a, page)
    page.evaluate(WATCH_CROSS)
    page.click("#practice")
    answer(page, 0, True)
    answer(page, 1, True)  # trial 0 ended and trial 1 began under the same cross
    page.wait_for_selector("#advance", state="visible")
    page.evaluate("window.__stopSampling = true")
    assert page.evaluate("window.__crossRemoved") == 0
    assert page.evaluate("window.__framesWithout") == 0
    assert page.evaluate("window.__framesWith") > 30
    folder = session_folder_of(a)
    backgrounds = named(folder, "background")
    assert [(b["payload"]["trial"], b["payload"]["practice"]) for b in backgrounds] == [(0, True)]  # drawn once
    page.click("#advance")
    awaiting(page, 0, False)
    assert page.locator("#task-panel .example-cross").count() == 1
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    assert page.evaluate("document.getElementById('task-panel').childElementCount") == 0


# -- events kept for an earlier session --------------------------------------------------------------------


def lose_a_trial_with_the_server(app, device, page, tmp_path, port):  # noqa: F811
    """Session A on a server that then stops in a block: the trial in progress is abandoned while
    no server is there, so its record waits in the page's outbox. Returns (server, session id)."""
    a = app(port=device().path, flow=example_flow(tmp_path, 60), http_port=port)
    to_instructions(a, page)
    practise(page)
    page.click("#advance")
    answer(page, 0, False)
    awaiting(page, 1, False)
    session_id = server_state(a)["session_id"]
    stop(a)
    page.wait_for_selector("#conn-lost", state="visible")
    page.wait_for_function(
        f"Object.keys(sessionStorage).some((k) => k === 'brain-mapper-task/outbox/{session_id}')"
    )
    return a, session_id


def outbox_sizes(page) -> list[int]:
    return page.evaluate(
        "Object.keys(sessionStorage).filter((k) => k.startsWith('brain-mapper-task/outbox/'))"
        ".map((k) => JSON.parse(sessionStorage.getItem(k)).length)"
    )


def test_events_kept_for_a_session_whose_folder_is_here_are_appended_to_it_late(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    port = free_port()
    first, session_id = lose_a_trial_with_the_server(app, device, page, tmp_path, port)
    folder = session_folder_of(first)
    before = events_of(folder)
    second = app(port=device().path, flow=EXAMPLE, http_port=port, data_dir=first.data_dir)
    wait_until(lambda: len(events_of(folder)) > len(before), "the late events", timeout=30)
    wait_until(lambda: outbox_sizes(page) == [], "an empty outbox", timeout=30)
    late = [e for e in events_of(folder) if e["payload"].get("late") is True]
    assert late and all(e["seq"] > before[-1]["seq"] for e in late)
    assert {e["server_run"] for e in late}.isdisjoint({e["server_run"] for e in before})  # another process
    check_clocks(events_of(folder))
    trial = next(e for e in late if e["name"] == "example.trial")
    assert (trial["payload"]["trial"], trial["payload"]["status"]) == (1, "abandoned")
    assert trial["payload"]["reason"] == "connection_lost" and trial["step"] is None
    labels = [m["label"] for m in markers_of(folder / "eeg")]
    assert not any(label.endswith(f"#{trial['seq']}") for label in labels)  # no sample: no marker
    seqs = [e["seq"] for e in events_of(folder)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    rows = (folder / "behavior" / "example.csv").read_text().splitlines()
    assert len(rows) - 1 == len(named(folder, "trial"))  # the behavior file took it in
    assert page.locator("#notice").is_hidden()
    assert server_state(second)["session_id"] is None  # nothing else happened on the new server


def test_late_events_of_a_zipped_session_are_kept_and_an_additional_zip_is_made_at_the_next_stop(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    """Every session is zipped as it ends (Ctrl-C included), so the late events of the test above
    reach a zipped folder. Nothing is lost: they are appended and counted in session.json, the
    first zip is left exactly as it is, and the next packaging (here the stop of the second
    server) makes an additional zip with its own name, verified like any other."""
    port = free_port()
    first, session_id = lose_a_trial_with_the_server(app, device, page, tmp_path, port)
    folder = session_folder_of(first)
    meta = json.loads((folder / "session.json").read_text())
    first_zip = folder.parent / meta["zip"]
    before_bytes = first_zip.read_bytes()
    before = events_of(folder)
    second = app(port=device().path, flow=EXAMPLE, http_port=port, data_dir=first.data_dir)
    wait_until(lambda: len(events_of(folder)) > len(before), "the late events", timeout=30)
    wait_until(lambda: outbox_sizes(page) == [], "an empty outbox", timeout=30)
    late = [e for e in events_of(folder) if e["payload"].get("late") is True]
    noted = json.loads((folder / "session.json").read_text())["late_events_after_zip"]
    assert noted["count"] == len(late)
    assert noted["first_unix"] == pytest.approx(late[0]["unix_time"], abs=1.0)
    assert noted["last_unix"] == pytest.approx(late[-1]["unix_time"], abs=1.0)
    stop(second)
    record = json.loads((folder / "session.json").read_text())
    [extra] = record["late_zips"]
    assert extra["events"] == len(late) and extra["hook_errors"] == []
    extra_zip = folder.parent / extra["zip"]
    assert extra_zip.name == f"{folder.name}-late.zip" and extra_zip != first_zip
    assert first_zip.read_bytes() == before_bytes  # the first zip is never touched
    assert package.verify(extra_zip, package.members(folder).files, crc_only=("session.json",))
    with zipfile.ZipFile(extra_zip) as z:
        lines = z.read("events.jsonl").decode().splitlines()
        assert len(lines) == len(events_of(folder))
        rows = z.read("behavior/example.csv").decode().splitlines()
        assert len(rows) - 1 == len(named(folder, "trial"))


def test_events_kept_for_a_session_that_is_not_here_are_reported_once_in_the_next_session(
    app,
    device,  # noqa: F811  (the fixture imported from test_progress)
    page,
    tmp_path,
):
    port = free_port()
    first, session_id = lose_a_trial_with_the_server(app, device, page, tmp_path, port)
    kept = page.evaluate(f"JSON.parse(sessionStorage.getItem('brain-mapper-task/outbox/{session_id}'))")
    assert kept
    elsewhere = tmp_path / "elsewhere"  # another data folder: the earlier session is not on this server
    second = app(port=device().path, flow=EXAMPLE, http_port=port, data_dir=elsewhere)
    wait_until(lambda: outbox_sizes(page) == [], "an empty outbox", timeout=30)
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    advance_until(page, "example", "waiting")
    folder = session_folder_of(second)

    def reports():
        return [e for e in events_of(folder) if e["name"] == "events_dropped"]

    wait_until(lambda: len(reports()) == 1, "one report", timeout=20)
    report = reports()[0]["payload"]
    assert report["session_id"] == session_id and report["count"] == len(kept)
    times = [c["browser_time"] for c in kept]
    assert report["oldest_browser_ms"] == pytest.approx(min(times))
    assert report["newest_browser_ms"] == pytest.approx(max(times))
    assert page.locator("#notice").is_hidden()
    assert not (elsewhere / "sessions" / session_folder_of(first).name).exists()


# -- storage that is full ----------------------------------------------------------------------------------


FILL_STORAGE = """
() => {
  const chunk = 'x'.repeat(256 * 1024);
  let i = 0;
  try { for (;;) { sessionStorage.setItem('filler-' + i, chunk); i += 1; } } catch (e) {}
  let small = 0;
  try { for (;;) { sessionStorage.setItem('filler-small-' + small, 'x'.repeat(1024)); small += 1; } } catch (e) {}
  return i;
}
"""


def test_a_full_session_storage_is_reported_once_and_trial_numbers_never_repeat(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=example_flow(tmp_path, 30))
    to_instructions(a, page)
    assert page.evaluate(FILL_STORAGE) > 0
    practise(page)
    page.click("#advance")
    for trial in range(3):
        answer(page, trial, False)
    awaiting(page, 3, False)
    folder = session_folder_of(a)
    wait_until(lambda: len(named(folder, "trial")) >= 5, "the trial records")
    failures = [e for e in events_of(folder) if e["name"] == "storage_failed"]
    assert len(failures) == 1 and failures[0]["payload"]["error"] == "QuotaExceededError"
    numbers = [(t["payload"]["practice"], t["payload"]["trial"]) for t in named(folder, "trial")]
    assert numbers == [(True, 0), (True, 1), (False, 0), (False, 1), (False, 2)]
    check_delivery(folder)


def test_a_payload_block_that_differs_from_the_block_column_is_kept_and_logged(tmp_path, caplog):
    folder = tmp_path / "session"
    events = trial_events("t", 0, 1) + trial_events("t", 1, 6)
    # A trial payload without its block: the block column takes the event's, so the payload
    # column (empty there) is not a copy of it.
    events[-1]["payload"] = {k: v for k, v in events[-1]["payload"].items() if k != "block"}
    session_files(folder, events)
    with caplog.at_level("WARNING", logger="brain_mapper.behavior"):
        behavior.write(folder)
    with (folder / "behavior" / "t.csv").open(newline="") as stream:
        rows = list(csv.reader(stream))
    header = rows[0]
    assert "payload_block" in header  # kept: they differ in one row
    assert "payload_trial" not in header and "payload_practice" not in header
    assert [dict(zip(header, r))["payload_block"] for r in rows[1:]] == ["b1", ""]
    assert "payload block differs" in caplog.text


# -- the four F/J tasks: Enter, lead-in, the fixed response window, feedback, key labels -------------

#: The F/J tasks that start on Enter.
ENTER_TASKS = ("perception", "nback", "flanker", "spatial")


def one_task(tmp_path: Path, task: str, seconds: list[float]) -> Path:
    """A flow with the participant step, ``task`` with blocks of ``seconds``, and the summary."""
    path = tmp_path / f"{task}-enter.json"
    path.write_text(
        json.dumps(
            [
                {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
                {
                    "id": task,
                    "kind": "task",
                    "title": task,
                    "blocks": [{"id": f"block{i + 1}", "seconds": s, "points": 100} for i, s in enumerate(seconds)],
                },
                {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
            ]
        )
    )
    return path


def task_events(folder: Path, task: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"].startswith(f"{task}.")]


def frames_of(ms: float, frame: float) -> float:
    """``ms`` as the runner counts it: the nearest whole number of frames, in ms."""
    return round(ms / frame) * frame


def wait_events(a, task: str, check, what: str, timeout: float = 30.0) -> list[dict]:
    found: list[dict] = []

    def look() -> bool:
        found[:] = task_events(session_folder_of(a), task)
        return check(found)

    wait_until(look, what, timeout)
    return found


TWO_FRAMES = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"


@pytest.mark.parametrize("task", ENTER_TASKS)
def test_nothing_starts_before_enter_then_a_two_second_lead_in_then_the_first_trial(app, pty_device, page, tmp_path, task):
    a = app(port=pty_device.path, flow=one_task(tmp_path, task, [12, 12]))
    begin(a, page)
    advance_until(page, task, "waiting")
    page.wait_for_selector(f'{AREA}[data-ready="practice"]')
    assert page.inner_text("#task-enter") == "Put your fingers on F and J.\nPress Enter to start."
    assert page.locator("#practice, #advance").count() == 0  # no button: Enter is the start
    # Keys other than Enter do nothing: no trial, no event, the same screen.
    for key in ("f", "j", "Space", "a"):
        page.keyboard.press(key)
    page.evaluate(TWO_FRAMES)
    assert page.get_attribute(AREA, "data-mode") == "instructions"
    assert task_events(session_folder_of(a), task) == []
    page.keyboard.press("Enter")  # the practice
    page.wait_for_selector(f'{AREA}[data-ready="block"]', timeout=60_000)  # it ran by itself, unanswered
    assert server_state(a)["phase"] == "waiting"  # practice runs before the block's clock
    assert page.inner_text("#task-enter") == "Put your fingers on F and J.\nPress Enter to start."
    for key in ("f", "j", "Space"):
        page.keyboard.press(key)
    page.evaluate(TWO_FRAMES)
    assert server_state(a)["phase"] == "waiting"  # nothing but Enter starts the block
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{SCREEN}[data-step="{task}"][data-phase="running"]')
    # Block 1 ends; the next block waits for Enter on the same screen, with its instructions.
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="between"]', timeout=30_000)
    page.wait_for_selector(f'{AREA}[data-ready="block"]')
    assert page.inner_text("#task-block") == "Block 2 of 2"
    assert page.inner_text("#task-instructions").startswith("Same as before")
    before = len(task_events(session_folder_of(a), task))
    page.keyboard.press("j")
    page.evaluate(TWO_FRAMES)
    assert server_state(a)["phase"] == "between"
    assert len(task_events(session_folder_of(a), task)) == before
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{SCREEN}[data-step="{task}"][data-phase="running"]')
    events = wait_events(
        a, task, lambda es: sum(e["name"] == f"{task}.lead_in_end" for e in es) == 3, "three lead-ins ended"
    )
    leads = [e for e in events if e["name"] == f"{task}.lead_in"]
    ends = [e for e in events if e["name"] == f"{task}.lead_in_end"]
    assert [(e["payload"]["block"], e["payload"]["practice"]) for e in leads] == [
        ("block1", True),
        ("block1", False),
        ("block2", False),
    ]
    # Enter comes first: the lead-in is the task's first event.
    assert events[0]["name"] == f"{task}.lead_in"
    for lead, end in zip(leads, ends):
        f = lead["payload"]["frame_ms"]
        assert lead["payload"]["duration_ms"] == 2000
        shown = end["browser_time"] - lead["browser_time"]
        # Frame-exact: the frame nearest 2000 ms after the cross's own frame (as every other part).
        assert end["payload"]["due_ms"] == pytest.approx(lead["payload"]["due_ms"])
        assert lead["payload"]["frames"] == round(2000 / f)
        assert abs(shown - 2000) <= f / 2 + 1, shown
        assert end["payload"]["late_frames"] == 0
        assert end["payload"]["trial"] == 0
        # The first trial's first change is made in the frame that ends the lead-in.
        first_change = next(
            e
            for e in events
            if e["name"] in (f"{task}.stim", f"{task}.background", f"{task}.stim_off")
            and e["payload"].get("block") == lead["payload"]["block"]
            and e["payload"].get("practice") == lead["payload"]["practice"]
        )
        assert first_change["browser_time"] == end["browser_time"]
    # The block's lead-in comes after the server started the block.
    log = events_of(session_folder_of(a))
    starts = [e["seq"] for e in log if e["name"] == "block_start"]
    block_leads = [e["seq"] for e in log if e["name"] == f"{task}.lead_in" and not e["payload"]["practice"]]
    assert starts[0] < block_leads[0] and starts[1] < block_leads[1]


WATCH_SIGNS = """() => {
  window.__signs = [];
  window.__hud = [];
  for (const side of document.querySelectorAll('.task-side')) {
    new MutationObserver(() => window.__signs.push(
      [side.dataset.key, side.classList.contains('is-pressed'), performance.now()]
    )).observe(side, { attributes: true, attributeFilter: ['class'] });
  }
  const hud = document.getElementById('hud-points');
  new MutationObserver(() => window.__hud.push([hud.textContent, performance.now()]))
    .observe(hud, { childList: true, characterData: true, subtree: true });
}"""


#: The number of children of the stimulus area when `#task-area` first turns to data-mode "over"
#: (in `window.__over`; null before).
WATCH_OVER = """() => {
  window.__over = null;
  const area = document.getElementById('task-area');
  new MutationObserver(() => {
    if (window.__over === null && area.dataset.mode === 'over')
      window.__over = document.getElementById('task-panel').children.length;
  }).observe(area, { attributes: true, attributeFilter: ['data-mode'] });
}"""


def test_every_press_in_the_window_is_logged_the_last_counts_feedback_follows_and_the_score_updates_each_trial(
    app, pty_device, page, tmp_path
):
    from brain_mapper import scores
    from test_task_flanker import KEY, OTHER, awaiting_selector, practise, start_block

    a = app(port=pty_device.path, flow=one_task(tmp_path, "flanker", [14]))
    begin(a, page)
    advance_until(page, "flanker", "waiting")
    practise(page, ["miss"] * 6)
    page.evaluate(WATCH_SIGNS)
    page.evaluate(WATCH_OVER)
    start_block(page)
    # Trial 0: the wrong key, then the right one: the last press is the answer.
    page.wait_for_selector(awaiting_selector(0, False), timeout=15_000)
    target = page.get_attribute("#flanker-field", "data-target")
    page.keyboard.press(KEY[OTHER[target]])
    page.keyboard.press(KEY[target])
    page.wait_for_selector('#task-feedback[data-trial="0"][data-practice="false"]', timeout=5_000)
    assert page.inner_text("#task-feedback") == "Correct"
    # The header has the score of trial 0 once it is over (its record goes out at its end, after
    # the gap), before trial 1's arrows.
    page.wait_for_function("document.getElementById('hud-points').textContent === '1 of 1'", timeout=5_000)
    assert page.locator(awaiting_selector(1, False)).count() == 0
    # Trial 1: one press, then a press after the window (during the feedback): stray.
    page.wait_for_selector(awaiting_selector(1, False), timeout=15_000)
    target1 = page.get_attribute("#flanker-field", "data-target")
    page.keyboard.press(KEY[target1])
    page.wait_for_selector('#task-feedback[data-trial="1"][data-practice="false"]', timeout=5_000)
    page.keyboard.press(KEY[OTHER[target1]])
    page.wait_for_function("document.getElementById('hud-points').textContent === '2 of 2'", timeout=5_000)
    # Trial 2: nothing pressed.
    page.wait_for_selector('#task-feedback[data-trial="2"][data-practice="false"]', timeout=15_000)
    assert page.inner_text("#task-feedback") == "No response"
    page.wait_for_function("document.getElementById('hud-points').textContent === '2 of 3'", timeout=5_000)
    # No fourth trial fits in 14 s (2 s lead-in + 3 x 3.2-3.6 s): the stage goes blank. The page
    # then sends block_finished and the engine ends the block at once (ended_by "trials_done"),
    # so the blank stage can last less than a frame: it is seen by the observer of WATCH_OVER.
    page.wait_for_function("window.__over !== null", timeout=10_000)
    assert page.evaluate("window.__over") == 0
    page.wait_for_selector(f'{SCREEN}[data-phase="done"], {SCREEN}[data-screen="between"]', timeout=15_000)
    assert page.locator("#notice:not([hidden])").count() == 0  # the unknown command is not shown
    folder = session_folder_of(a)
    events = task_events(folder, "flanker")
    trials = [e["payload"] for e in events if e["name"] == "flanker.trial" and not e["payload"]["practice"]]
    assert [t["trial"] for t in trials] == [0, 1, 2]
    assert [t["status"] for t in trials] == ["done"] * 3  # none cut short
    [end] = [e["payload"] for e in events_of(folder) if e["name"] == "block_end"]
    assert end["ended_by"] == "trials_done"
    t0, t1, t2 = trials
    assert (t0["response"], t0["correct"], t0["responses"], t0["changed"]) == (target, True, 2, True)
    assert t0["rt_first_ms"] < t0["rt_last_ms"] == t0["rt_ms"]
    assert (t1["response"], t1["correct"], t1["responses"], t1["changed"]) == (target1, True, 1, False)
    assert t1["rt_first_ms"] == t1["rt_last_ms"] == t1["rt_ms"]
    assert (t2["response"], t2["key"], t2["rt_ms"], t2["correct"]) == (None, None, None, False)
    assert (t2["responses"], t2["changed"], t2["rt_first_ms"], t2["rt_last_ms"]) == (0, False, None, None)
    assert [t["feedback"] for t in trials] == ["Correct", "Correct", "No response"]
    # Both presses of trial 0 are logged, in order, with their index and time.
    resps = [e for e in events if e["name"] == "flanker.resp" and not e["payload"]["practice"]]
    assert [(e["payload"]["trial"], e["payload"]["index"]) for e in resps] == [(0, 0), (0, 1), (1, 0)]
    assert [e["payload"]["response"] for e in resps[:2]] == [OTHER[target], target]
    assert resps[1]["browser_time"] == t0["resp_browser_ms"]  # the scored press is the last
    strays = [e["payload"] for e in events if e["name"] == "flanker.stray" and not e["payload"]["practice"]]
    assert [(s["trial"], s["key"], s["awaiting"]) for s in strays] == [(1, f"Key{KEY[OTHER[target1]].upper()}", False)]
    # The fixed timeline: arrows for the whole 1500 ms window, the feedback in the frame it ends
    # for 500 ms, whether a key was pressed or not.
    for t in trials:
        mine = [e for e in events if e["payload"].get("trial") == t["trial"] and not e["payload"]["practice"]]
        stim = {e["payload"]["part"]: e for e in mine if e["name"] == "flanker.stim"}
        off = {e["payload"]["part"]: e for e in mine if e["name"] == "flanker.stim_off"}
        f = stim["arrows"]["payload"]["frame_ms"]
        arrows = stim["arrows"]["browser_time"]
        assert abs(off["arrows"]["browser_time"] - arrows - 1500) <= f / 2 + 1
        assert stim["feedback"]["browser_time"] == off["arrows"]["browser_time"]
        assert stim["feedback"]["payload"]["feedback"] == t["feedback"]
        assert stim["feedback"]["payload"]["late_frames"] == 0
        assert abs(off["feedback"]["browser_time"] - stim["feedback"]["browser_time"] - 500) <= f / 2 + 1
        assert off["feedback"]["payload"]["late_frames"] == 0
    # The server's score counts the last press of each trial.
    assert scores.score_folder(folder)["tasks"]["flanker"]["earned"] == 2
    assert scores.score_folder(folder)["tasks"]["flanker"]["possible"] == 3
    # Each press in a window lit its key for 150 ms, the same sign for either key; the stray did not.
    signs = page.evaluate("window.__signs")
    lit = [(k, on) for k, on, _ in signs]
    keys = [f"Key{KEY[OTHER[target]].upper()}", f"Key{KEY[target].upper()}", f"Key{KEY[target1].upper()}"]
    assert [k for k, on in lit if on] == keys
    for code in set(keys):
        times = [(on, ts) for k, on, ts in signs if k == code]
        for (on, start), (off_, end) in zip(times[::2], times[1::2]):
            assert on and not off_
            assert 150 <= end - start < 250, end - start
    # The header changed only after trial events: dash, then 1 of 1, 2 of 2, 2 of 3.
    hud = [text for text, _ in page.evaluate("window.__hud")]
    assert [x for i, x in enumerate(hud) if i == 0 or hud[i - 1] != x] == ["1 of 1", "2 of 2", "2 of 3"]


MEASURE = """() => {
  const box = (sel) => { const r = document.querySelector(sel).getBoundingClientRect();
    return { x: r.x, y: r.y, w: r.width, h: r.height }; };
  const kbd = getComputedStyle(document.querySelector('#task-side-left kbd'));
  const s = document.scrollingElement;
  return { vw: innerWidth, vh: innerHeight, panel: box('#task-panel'), left: box('#task-side-left'),
    right: box('#task-side-right'), intro: box('.task-intro'), leave: box('#task-area .leave'), prompt: document.getElementById('task-enter').hidden ? null : box('#task-enter'),
    letter: parseFloat(kbd.fontSize), scrollW: s.scrollWidth, scrollH: s.scrollHeight,
    leftText: document.getElementById('task-side-left').innerText,
    rightText: document.getElementById('task-side-right').innerText,
    legend: document.querySelectorAll('#task-legend').length };
}"""

SIDES = {
    "perception": ("F\ntilted left", "J\ntilted right"),
    "nback": ("F\ndifferent", "J\nsame"),
    "flanker": ("F\nleft", "J\nright"),
    "spatial": ("F\nnot filled", "J\nfilled"),
}


def check_sides(look: dict, task: str, running: bool) -> None:
    vw, vh, panel, left, right = look["vw"], look["vh"], look["panel"], look["left"], look["right"]
    assert (look["leftText"], look["rightText"]) == SIDES[task]
    assert look["legend"] == 0  # the labels replace the legend
    assert look["scrollW"] <= vw and look["scrollH"] <= vh  # no scroll
    assert panel["w"] == pytest.approx(panel["h"], abs=0.5)
    assert abs(panel["x"] + panel["w"] / 2 - vw / 2) <= 1  # centered
    for side in (left, right):
        assert side["x"] >= 0 and side["x"] + side["w"] <= vw and side["y"] >= 0 and side["y"] + side["h"] <= vh
        # Level with the middle of the stimulus area.
        assert abs(side["y"] + side["h"] / 2 - (panel["y"] + panel["h"] / 2)) <= 1
    assert left["x"] + left["w"] <= panel["x"] and right["x"] >= panel["x"] + panel["w"]  # outside the area
    # Near the center: the labels' outer edges inside the middle 60% of the width.
    assert left["x"] >= 0.2 * vw - 1 and right["x"] + right["w"] <= 0.8 * vw + 1
    assert look["letter"] == pytest.approx(max(28, 0.08 * panel["w"]), abs=0.6)
    if look["prompt"]:
        # The prompt: between the labels, below the instructions, above the leave controls.
        p = look["prompt"]
        assert left["x"] + left["w"] <= p["x"] and p["x"] + p["w"] <= right["x"]
        assert p["y"] >= look["intro"]["y"] + look["intro"]["h"]
        assert p["y"] + p["h"] <= look["leave"]["y"]
    if running and (vw, vh) == (3840, 1600):
        assert 0.65 * vh <= panel["h"] <= 0.71 * vh  # about 0.7 of the height on a 3840x1600 screen


@pytest.mark.parametrize("size", [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)])
def test_key_labels_sit_just_outside_the_stimulus_area_near_the_center_at_four_sizes(app, pty_device, page, tmp_path, size):
    from test_skip_browser import answer_yes, ask

    page.set_viewport_size({"width": size[0], "height": size[1]})
    steps = [{"id": "participant", "kind": "setup", "title": "Participant", "blocks": []}]
    for task in ENTER_TASKS:
        steps.append({"id": task, "kind": "task", "title": task, "blocks": [{"id": "block1", "seconds": 20, "points": 100}]})
    steps.append({"id": "summary", "kind": "summary", "title": "Summary", "blocks": []})
    path = tmp_path / "four.json"
    path.write_text(json.dumps(steps))
    a = app(port=pty_device.path, flow=path)
    begin(a, page)
    advance_until(page, ENTER_TASKS[0], "waiting")
    for task in ENTER_TASKS:
        page.wait_for_selector(f'{SCREEN}[data-step="{task}"] {AREA}[data-ready="practice"]')
        check_sides(page.evaluate(MEASURE), task, running=False)
        page.keyboard.press("Enter")
        page.wait_for_selector("#task-leadin")
        check_sides(page.evaluate(MEASURE), task, running=True)
        ask(page, "skip-round")
        answer_yes(page)


INSTRUCTION_SIZES = """() => {
  const px = (sel) => parseFloat(getComputedStyle(document.querySelector(sel)).fontSize);
  const r = document.querySelector('#task-panel').getBoundingClientRect();
  return { letter: px('#task-side-left kbd'), meaning: px('#task-side-left'),
    prompt: document.getElementById('task-enter').hidden ? null : px('#task-enter'),
    body: px('#task-instructions'), panel: r.height };
}"""


def test_key_labels_and_prompt_on_the_instruction_screen_are_as_large_as_during_trials_at_3840x1600(
    app, pty_device, page, tmp_path
):
    """At 3840 x 1600 the instruction screen's stimulus area was the room left under the
    instructions, so the key letters (0.08 of the area) and the Enter prompt came out tiny there
    and only grew once the block ran. The area is now as large on the instruction screen as during
    trials: the same letter size, and the prompt at least the size of the instruction text."""
    from shots import ShotsDir
    from test_skip_browser import answer_yes, ask

    shots = ShotsDir("instruction-keys-3840")
    shots.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 3840, "height": 1600})
    steps = [{"id": "participant", "kind": "setup", "title": "Participant", "blocks": []}]
    for task in ENTER_TASKS:
        steps.append({"id": task, "kind": "task", "title": task, "blocks": [{"id": "block1", "seconds": 20, "points": 100}]})
    steps.append({"id": "summary", "kind": "summary", "title": "Summary", "blocks": []})
    path = tmp_path / "four.json"
    path.write_text(json.dumps(steps))
    a = app(port=pty_device.path, flow=path)
    begin(a, page)
    advance_until(page, ENTER_TASKS[0], "waiting")
    for task in ENTER_TASKS:
        page.wait_for_selector(f'{SCREEN}[data-step="{task}"] {AREA}[data-ready="practice"]')
        before = page.evaluate(INSTRUCTION_SIZES)
        page.screenshot(path=str(shots / f"{task}-instructions.png"))
        page.keyboard.press("Enter")
        page.wait_for_selector("#task-leadin")
        during = page.evaluate(INSTRUCTION_SIZES)
        page.screenshot(path=str(shots / f"{task}-leadin.png"))
        assert before["letter"] == pytest.approx(during["letter"], rel=0.05), (task, before, during)
        assert before["letter"] >= 0.05 * 1600, (task, before)
        assert before["meaning"] == pytest.approx(during["meaning"], rel=0.05), (task, before, during)
        assert before["prompt"] is not None and before["prompt"] >= 0.95 * before["body"], (task, before)
        ask(page, "skip-round")
        answer_yes(page)


def test_a_pause_abandons_the_block_its_trials_never_go_on_and_the_next_block_starts_from_its_prompt(
    app, pty_device, page, tmp_path
):
    from test_task_flanker import awaiting_selector, practise, start_block

    a = app(port=pty_device.path, flow=one_task(tmp_path, "flanker", [20, 12]))
    begin(a, page)
    advance_until(page, "flanker", "waiting")
    practise(page, ["miss"] * 6)
    start_block(page)
    page.wait_for_selector(awaiting_selector(1, False), timeout=15_000)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_function("document.getElementById('task-panel').children.length === 0")
    paused_at = len(events_of(session_folder_of(a)))
    page.click("#fs-return")
    # A server that resumes the same block: the page starts no trial in it; one that goes to the
    # next block: its prompt. Either way the next block starts from its prompt and instructions.
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="between"] {AREA}[data-ready="block"]', timeout=30_000)
    assert page.inner_text("#task-block") == "Block 2 of 2"
    assert page.inner_text("#task-instructions").startswith("Same as before")
    after = events_of(session_folder_of(a))[paused_at:]
    assert not [e for e in after if e["name"] in ("flanker.stim", "flanker.lead_in", "flanker.background")]
    block1 = [e["payload"] for e in events_of(session_folder_of(a)) if e["name"] == "flanker.trial" and e["payload"]["block"] == "block1" and not e["payload"]["practice"]]
    assert [(t["trial"], t["status"]) for t in block1] == [(0, "done"), (1, "abandoned")]
    start_block(page)
    page.wait_for_selector(awaiting_selector(0, False), timeout=15_000)  # block 2 begins at its first trial
    events = task_events(session_folder_of(a), "flanker")
    assert [e["payload"]["block"] for e in events if e["name"] == "flanker.lead_in"] == ["block1", "block1", "block2"]

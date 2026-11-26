"""A headset link lost after the device step pauses the session; it does not end it (a drop
is treated as a pause).

Every test runs the real ``brain-mapper`` command on the simulated headset (a pseudo-terminal,
``tests/python/pty_device.py``) reached through a symbolic link, so that "unplugging" is
``hang_up()`` and "plugging in again" is pointing the link at a new simulated headset. The browser
tests drive a real Chromium; the others speak the participant's websocket. Nothing waits on a
fixed sleep for an assertion.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from pathlib import Path

import open_cognionics as oc
import pytest
from test_device_browser import point
from test_engine import events_of, markers_of, next_state, open_ws, send, session_folder_of
from test_package import wait_packaged, zip_contents
from test_shell_browser import SCREEN, begin
from test_video_browser import START_S, after, headed, net_page, to_video, video_flow, wait_events, wait_ready  # noqa: F401
from test_whole_session_browser import contact_device, drive, one_task_flow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402
from shots import ShotsDir  # noqa: E402

ALLOWED = {0x11, 0x12, 0x14}


def until(check, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while True:
        found = check()
        if found:
            return found
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def names(folder: Path) -> list[str]:
    return [e["name"] for e in events_of(folder)]


def samples_in(recording: Path) -> int:
    return sum(chunk.samples for chunk in oc.replay(recording))


class Plug:
    """A headset port that can be unplugged and plugged in again (a symbolic link)."""

    def __init__(self, tmp_path: Path, make=lambda: PtyDevice(onhead_stream(), config_reply())):
        self.make = make
        self.link = tmp_path / "headset-port"
        self.devices = [make()]
        self.link.symlink_to(self.devices[0].path)

    @property
    def device(self) -> PtyDevice:
        return self.devices[-1]

    def unplug(self) -> None:
        self.device.hang_up()

    def plug_in(self) -> None:
        self.devices.append(self.make())
        point(self.link, self.device.path)

    def written(self) -> bytes:
        return b"".join(d.written() for d in self.devices)

    def close(self) -> None:
        for d in self.devices:
            d.hang_up()


def scored(folder: Path, task: str) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == f"{task}.trial" and not e["payload"].get("practice")]


def wait_paused(page) -> None:
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=30_000)
    page.wait_for_selector("#fs-lost", state="visible")


def resume_click(page) -> None:
    page.click("#fs-return")


def check_drop_and_back(page, a, plug: Plug, folder: Path, drop: int) -> None:
    """One drop in the running block: paused, ``link_lost`` logged, the old recording kept and
    readable; Resume refused before the link is back and accepted after."""
    before = len(scored(folder, "flanker"))
    plug.unplug()
    wait_paused(page)
    assert "headset connection was lost" in page.inner_text("#fs-lost-why")
    assert page.locator(f'{SCREEN}[data-screen="ended"]').count() == 0
    lost = until(lambda: [e for e in events_of(folder) if e["name"] == "link_lost"][drop - 1 :], "link_lost")[0]
    assert lost["payload"]["earlier_recording"] == f"eeg-lost-{drop}"
    assert isinstance(lost["payload"]["last_sample"], int) and lost["payload"]["last_sample"] > 0
    paused = [e for e in events_of(folder) if e["name"] == "paused" and e["seq"] > lost["seq"] - 2]
    assert any(e["payload"]["reason"] == "link_lost" for e in paused)
    aside = folder / f"eeg-lost-{drop}"
    assert samples_in(aside) > 0  # whole, closed and readable
    # Resume is grayed out while no recording is open, and a
    # click on it sends nothing; the engine's gate still refuses a resume (tested over the socket).
    page.wait_for_selector("#fs-return[disabled]", timeout=10_000)
    page.click("#fs-return", force=True)
    page.wait_for_timeout(300)
    assert page.get_attribute(SCREEN, "data-phase") == "paused"
    # Plugged in again: a new recording, link_back logged; the block stays paused until Resume.
    plug.plug_in()
    back = until(lambda: [e for e in events_of(folder) if e["name"] == "link_back"][drop - 1 :], "link_back", 40)[0]
    assert back["payload"]["recording"] == "eeg" and back["payload"]["first_sample"] == 0
    until(lambda: (folder / "eeg" / "raw.bin").is_file(), "the new recording")
    assert page.get_attribute(SCREEN, "data-phase") == "paused"
    deadline = time.monotonic() + 20
    while page.get_attribute(SCREEN, "data-phase") == "paused":
        assert time.monotonic() < deadline, "Resume was never accepted after the link came back"
        if page.locator("#fs-return").is_visible():
            resume_click(page)
        page.wait_for_timeout(300)
    resumed = [e for e in events_of(folder) if e["name"] == "resumed" and e["seq"] > back["seq"]]
    assert resumed, "no resumed event after link_back"
    # The pause ended the block: Resume leads to the between-blocks
    # screen, and the next block starts from there; the trials before the drop are kept.
    assert resumed[0]["payload"] == {"to": "between"}
    ended = [e for e in events_of(folder) if e["name"] == "block_end" and e["payload"].get("ended_by") == "interrupted"]
    assert len(ended) == drop and ended[-1]["payload"]["reason"] == "link_lost"
    assert len(scored(folder, "flanker")) >= before


def play_a_flanker_block_with_drops(app, page, tmp_path, drops: int) -> tuple[Path, Plug]:
    plug = Plug(tmp_path)
    # One block per drop and one more: each drop ends its block, the next block follows.
    a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 40, blocks=drops + 1))
    begin(a, page)
    folder = None
    for drop in range(1, drops + 1):
        want = 2 * drop

        def enough(look, want=want) -> bool:
            try:
                f = session_folder_of(a)
            except (AssertionError, FileNotFoundError):
                return False
            return len(scored(f, "flanker")) >= want and look["phase"] == "running"

        drive(page, a, enough, timeout=120)
        folder = session_folder_of(a)
        check_drop_and_back(page, a, plug, folder, drop)
    # The last block runs its time; then the session is finished.
    drive(page, a, lambda look: look["phase"] == "done", timeout=180)
    return folder, plug


def check_both_sides(folder: Path, drops: int) -> None:
    events = events_of(folder)
    meta = json.loads((folder / "session.json").read_text())
    assert meta["ending"] == "finished" and meta["ended_early"] is False
    ends = [e for e in events if e["name"] == "block_end"]
    assert len(ends) == drops + 1
    # The last block is not cut: its page ends it when no further trial fits (trials_done).
    assert [e["payload"].get("ended_by") for e in ends] == ["interrupted"] * drops + ["trials_done"]
    lost = [e for e in events if e["name"] == "link_lost"]
    back = [e for e in events if e["name"] == "link_back"]
    assert len(lost) == len(back) == drops
    trials = scored(folder, "flanker")
    before = [t for t in trials if t["seq"] < lost[0]["seq"]]
    after = [t for t in trials if t["seq"] > back[-1]["seq"]]
    assert before and after, "trials on both sides of the drop"
    # Events that came while no recording was open are kept, with no sample, and say so.
    for e in events:
        if lost[0]["seq"] < e["seq"] < back[0]["seq"] and e["name"] not in ("link_lost", "link_back"):
            assert e["payload"].get("marked") is False and e["payload"].get("no_recording") is True, e
    # Scores and behavior files take in the trials of every recording.
    done = [t for t in trials if t["payload"].get("scored") is True]
    entry = meta["scores"]["tasks"]["flanker"]
    assert entry["possible"] == len(done)
    assert entry["earned"] == sum(1 for t in done if t["payload"].get("correct") is True)
    rows = list(csv.DictReader(io.StringIO((folder / "behavior" / "flanker.csv").read_text())))
    assert [int(r["seq"]) for r in rows] == [t["seq"] for t in events if t["name"] == "flanker.trial"]
    by_seq = {int(r["seq"]): r for r in rows}
    assert by_seq[before[0]["seq"]]["recording"] == "eeg-lost-1" and by_seq[before[0]["seq"]]["sample"] != ""
    assert by_seq[after[-1]["seq"]]["recording"] == "eeg" and by_seq[after[-1]["seq"]]["sample"] != ""
    # session.json lists every recording, with its times and the blocks it covers.
    cover = meta["recording_coverage"]
    assert [c["folder"] for c in cover] == [*(f"eeg-lost-{n}" for n in range(1, drops + 1)), "eeg"]
    for c in cover:
        assert c["start_unix"] <= c["end_unix"]
        assert ["flanker", "block1"] in c["blocks"] or c["folder"] != "eeg-lost-1"
    assert ["flanker", f"block{drops + 1}"] in cover[-1]["blocks"]
    # The zip holds them all.
    content = zip_contents(wait_packaged(folder))
    for n in range(1, drops + 1):
        assert f"eeg-lost-{n}/raw.bin" in content
    assert "eeg/raw.bin" in content


def test_a_drop_in_a_flanker_block_pauses_it_and_it_resumes_after_the_link_is_back(app, page, tmp_path):
    folder, plug = play_a_flanker_block_with_drops(app, page, tmp_path, drops=1)
    try:
        check_both_sides(folder, drops=1)
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


def test_two_drops_in_one_block(app, page, tmp_path):
    folder, plug = play_a_flanker_block_with_drops(app, page, tmp_path, drops=2)
    try:
        check_both_sides(folder, drops=2)
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- without a browser ------------------------------------------------------------------------------


def refused(ws, command: str) -> dict:
    send(ws, cmd=command)
    while True:
        message = json.loads(ws.recv(timeout=10))
        if message.get("t") == "error":
            return message


def to_running_block(a, ws, plug_folder=None) -> Path:
    next_state(ws)
    from test_participants import request

    status, body = request(a, "POST", "/api/participant", {"label": "LINKP1"})
    assert status == 200, body
    send(ws, cmd="advance")
    next_state(ws, predicate=lambda m: m["step"] == "device")
    folder = session_folder_of(a)
    until(lambda: (folder / "eeg" / "raw.bin").exists(), "the recording")
    send(ws, cmd="advance")
    next_state(ws, timeout=20, predicate=lambda m: m["step"] == "flanker" and m["phase"] == "waiting")
    send(ws, cmd="advance")
    next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "running")
    return folder


def test_stop_while_the_link_is_lost_ends_the_session_with_what_was_recorded(app, tmp_path):
    plug = Plug(tmp_path)
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 60))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            plug.unplug()
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "paused")
            assert state["reason"] == "link_lost" and state["ended_early"] is False
            assert refused(ws, "resume")["kind"] == "refused"
            send(ws, cmd="stop")
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "done")
            assert state["stopped"] is True and state["failure"] is None
        zipped = wait_packaged(folder)
        meta = json.loads((folder / "session.json").read_text())
        assert meta["ending"] == "stopped" and meta["hook_errors"] == []
        content = zip_contents(zipped)
        assert "eeg-lost-1/raw.bin" in content and "eeg/raw.bin" not in content
        assert samples_in(folder / "eeg-lost-1") > 0
        stopped = [e for e in events_of(folder) if e["name"] == "stopped"][0]
        assert stopped["payload"]["marked"] is False and stopped["payload"]["no_recording"] is True
    finally:
        plug.close()


def calibration_flow(tmp_path: Path) -> Path:
    path = tmp_path / "calibration-flow.json"
    path.write_text(
        json.dumps(
            [
                {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
                {"id": "device", "kind": "setup", "title": "Device setup", "blocks": []},
                {
                    "id": "calibration",
                    "kind": "setup",
                    "title": "Calibration",
                    "blocks": [],
                    "calibration": {"eyes_open": 30.0, "eyes_closed": 2.0},
                },
                {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
            ]
        )
    )
    return path


def test_a_drop_in_the_contact_check_and_in_eyes_open_restarts_that_part(app, tmp_path):
    plug = Plug(tmp_path, make=contact_device)
    try:
        a = app(port=str(plug.link), flow=calibration_flow(tmp_path))
        with open_ws(a, "participant") as ws:
            next_state(ws)
            from test_participants import request

            status, body = request(a, "POST", "/api/participant", {"label": "LINKP2"})
            assert status == 200, body
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m: m["step"] == "device")
            folder = session_folder_of(a)
            until(lambda: (folder / "eeg" / "raw.bin").exists(), "the recording")
            send(ws, cmd="advance")
            next_state(ws, timeout=20, predicate=lambda m: m.get("substep") == "contact")
            # A drop during the contact check: the check starts again from its beginning.
            plug.unplug()
            until(lambda: "link_lost" in names(folder), "link_lost")
            starts = until(
                lambda: [e for e in events_of(folder) if e["name"] == "contact_start"][1:], "a new contact_start"
            )
            assert starts[0]["payload"]["restarted"] == "link_lost"
            plug.plug_in()
            until(lambda: "link_back" in names(folder), "link_back", 40)
            # Leaving the contact check needs a measure of the check that is on now.
            deadline = time.monotonic() + 40
            while True:
                assert time.monotonic() < deadline, "the contact check was never measured after the link came back"
                send(ws, cmd="advance")
                try:
                    next_state(ws, timeout=1, predicate=lambda m: m.get("substep") in ("settling", "signal", "eyes_open"))
                    break
                except AssertionError:
                    continue
            next_state(ws, timeout=40, predicate=lambda m: m.get("substep") == "signal_done")
            send(ws, cmd="advance")
            next_state(ws, timeout=30, predicate=lambda m: m.get("substep") == "eyes_open" and m["phase"] == "running")
            first_open = [e for e in events_of(folder) if e["name"] == "eyes_open_start"]
            assert len(first_open) == 1
            # A drop during eyes open: paused; after Resume the part starts again from its beginning.
            plug.unplug()
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "paused")
            assert state["reason"] == "link_lost"
            assert refused(ws, "resume")["kind"] == "refused"
            plug.plug_in()
            until(lambda: names(folder).count("link_back") == 2, "the second link_back", 40)
            send(ws, cmd="resume")
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "running")
            assert state["substep"] == "eyes_open" and state["remaining"] >= 29
            restarted = [e for e in events_of(folder) if e["name"] == "part_restarted"]
            assert [(e["payload"]["part"], e["payload"]["reason"]) for e in restarted] == [("eyes_open", "link_lost")]
            opens = [e for e in events_of(folder) if e["name"] == "eyes_open_start"]
            assert len(opens) == 2 and opens[1]["seq"] > restarted[0]["seq"]
            marks = {m["label"] for m in markers_of(folder / "eeg")}
            assert f"calibration/eyes_open_start#{opens[1]['seq']}" in marks
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


@pytest.mark.network
def test_a_drop_during_the_video_pauses_it_and_resume_plays_on_from_the_same_position(
    app, net_page, tmp_path  # noqa: F811  (the fixture imported from test_video_browser)
):
    """The real Vimeo player (headed Chromium, internet): the drop pauses the block and the video;
    after the link is back, Resume plays on from where it was held, not from the start."""
    page = net_page
    plug = Plug(tmp_path)
    try:
        a = app(port=str(plug.link), flow=video_flow(tmp_path, 60))
        to_video(a, page)
        wait_ready(page)
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
        folder = session_folder_of(a)
        start = next(e for e in events_of(folder) if e["name"] == "block_start" and e["step"] == "video")
        wait_events(a, lambda ev: len(after(ev, start["seq"], "video.progress")) >= 2, "two progress events")
        plug.unplug()
        wait_paused(page)
        lost = until(lambda: [e for e in events_of(folder) if e["name"] == "link_lost"], "link_lost")[0]
        events = wait_events(a, lambda ev: after(ev, lost["seq"], "video.pause"), "no video.pause after the drop")
        held = after(events, lost["seq"], "video.pause")[0]["payload"]["seconds"]
        assert held > START_S  # it had played some of the part
        plug.plug_in()
        until(lambda: [e for e in events_of(folder) if e["name"] == "link_back"], "link_back", 40)
        page.wait_for_function("document.getElementById('fs-return').offsetParent !== null")
        resume_click(page)
        page.wait_for_selector(f'{SCREEN}[data-phase="running"]', timeout=20_000)
        resumed = next(e for e in events_of(folder) if e["name"] == "resumed")
        events = wait_events(
            a,
            lambda ev: after(ev, resumed["seq"], "video.play") and after(ev, resumed["seq"], "video.progress"),
            "no video.play and progress after the resume",
        )
        between = [e for e in events if lost["seq"] < e["seq"] < resumed["seq"] and e["name"] == "video.progress"]
        assert all(e["payload"]["seconds"] <= held + 0.5 for e in between), (held, between)
        again = after(events, resumed["seq"], "video.progress")[0]["payload"]["seconds"]
        assert held - 0.5 <= again <= held + 3.0, (held, again)  # the same place, not the part's start
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- the gate of a round's Start, and the blink cues after a drop --------------------------------------


NOTICES = """() => {
  window.__notices = [];
  window.__waiting = [];
  new MutationObserver(() => {
    const n = document.getElementById('notice');
    if (n && !n.hidden && n.textContent) window.__notices.push(n.textContent);
    const b = document.getElementById('advance');
    if (b && b.dataset.waiting === 'true') window.__waiting.push(b.textContent);
  }).observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
}"""


def test_start_while_the_link_is_down_says_so_and_the_round_never_starts_by_itself(app, page, tmp_path):
    """After a calibration (the settling gate is in use) the link drops on a round's instructions.
    Start then says the headset link is not live (not "Starting in a moment"), and once the link is
    back the round waits for a fresh press of Start."""
    plug = Plug(tmp_path, make=contact_device)
    try:
        a = app(port=str(plug.link))
        page.goto(a.url)
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
        page.click('.route-pick[data-step="rest_open"]')  # a round whose Start comes at once
        page.wait_for_selector("#play-only", state="visible")
        page.click("#play-only")
        page.click("#fs-enter")
        page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
        page.fill("#participant-label", "LINKP3")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
        page.wait_for_selector("#skip-round", state="visible", timeout=30_000)
        page.click("#skip-round")
        page.click("#leave-yes")
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="waiting"]', timeout=30_000)
        folder = session_folder_of(a)
        plug.unplug()
        until(lambda: "link_lost" in names(folder), "link_lost")
        page.evaluate(NOTICES)
        page.click("#advance")
        page.wait_for_function(
            "window.__notices.some((n) => n.includes('not live yet'))", timeout=10_000
        )
        page.wait_for_selector("#advance:not([disabled])", timeout=5_000)
        assert page.evaluate("window.__waiting") == []
        assert page.get_attribute("#advance", "data-waiting") != "true"
        plug.plug_in()
        back = until(lambda: [e for e in events_of(folder) if e["name"] == "link_back"], "link_back", 40)[0]
        # Long enough for the settling time after link-up and several of the page's retries.
        page.wait_for_timeout(5_000)
        assert page.get_attribute(SCREEN, "data-phase") == "waiting"
        assert "block_start" not in names(folder)
        page.click("#advance")  # a fresh press
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="running"]', timeout=20_000)
        starts = [e for e in events_of(folder) if e["name"] == "block_start"]
        assert len(starts) == 1 and starts[0]["seq"] > back["seq"]
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


def test_a_drop_during_the_blinks_shows_and_logs_all_five_cues_again(app, page, tmp_path):
    from test_calibration_browser import to_calibration

    steps = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {"id": "device", "kind": "setup", "title": "Device setup", "blocks": []},
        {
            "id": "calibration",
            "kind": "setup",
            "title": "Calibration",
            "blocks": [],
            "calibration": {"eyes_open": 2.0, "eyes_closed": 2.0},
        },
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "blinks-flow.json"
    path.write_text(json.dumps(steps))
    plug = Plug(tmp_path, make=contact_device)
    try:
        a = app(port=str(plug.link), flow=path)
        to_calibration(a, page)
        folder = session_folder_of(a)
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        page.click("#advance")
        # the signal check after settling: Continue
        page.wait_for_selector(f'{SCREEN}[data-substep="signal_done"]', timeout=40_000)
        page.wait_for_selector("#advance:not([disabled])", timeout=10_000)
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-substep="blinks"]', timeout=40_000)
        until(lambda: names(folder).count("blink_cue") >= 2, "two blink cues", 20)
        plug.unplug()
        wait_paused(page)
        cues_before = [e["payload"]["n"] for e in events_of(folder) if e["name"] == "blink_cue"]
        assert cues_before[:2] == [1, 2], cues_before
        plug.plug_in()
        until(lambda: "link_back" in names(folder), "link_back", 40)
        deadline = time.monotonic() + 20
        while page.get_attribute(SCREEN, "data-phase") == "paused":
            assert time.monotonic() < deadline, "Resume was never accepted after the link came back"
            if page.locator("#fs-return").is_visible():
                resume_click(page)
            page.wait_for_timeout(300)
        page.wait_for_selector(f'{SCREEN}[data-substep="done"]', timeout=60_000)
        events = events_of(folder)
        restarted = [e for e in events if e["name"] == "part_restarted"]
        assert [e["payload"]["part"] for e in restarted] == ["blinks"]
        again = [e for e in events if e["name"] == "blink_cue" and e["seq"] > restarted[0]["seq"]]
        assert [e["payload"]["n"] for e in again] == [1, 2, 3, 4, 5], again
        assert all(e["payload"]["shown"] is True for e in again), again
        marks = {m["label"] for m in markers_of(folder / "eeg")}
        assert all(f"calibration/blink_cue#{e['seq']}" in marks for e in again)
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- Resume is grayed out while no samples flow, and only a press resumes ---------------------------------

SHOTS_NIGHT = ShotsDir("shots-night")


def resume_disabled(page) -> bool:
    return page.evaluate(
        """() => { const b = document.getElementById('fs-return');
                   return b.disabled && b.getAttribute('aria-disabled') === 'true'; }"""
    )


@pytest.mark.parametrize("how", ["hang_up", "silence", "paused_by_hand_then_hang_up"])
def test_resume_is_disabled_while_no_samples_flow_and_only_a_press_resumes(app, page, tmp_path, how):
    """Lost samples pause the block as the participant's own pause
    does; Resume is grayed out until samples flow into a new recording; nothing resumes by
    itself, not at link-up and not 10 s later; one press resumes."""
    page.set_viewport_size({"width": 900, "height": 900})
    plug = Plug(tmp_path)
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 60))
        begin(a, page)

        def enough(look) -> bool:
            try:
                f = session_folder_of(a)
            except (AssertionError, FileNotFoundError):
                return False
            return len(scored(f, "flanker")) >= 1 and look["phase"] == "running"

        drive(page, a, enough, timeout=120)
        folder = session_folder_of(a)
        if how == "paused_by_hand_then_hang_up":
            page.evaluate("document.exitFullscreen()")
            page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
            page.wait_for_selector("#fs-return:not([disabled])")
        device = plug.device
        if how == "silence":
            device.go_silent()
        else:
            plug.unplug()
        until(lambda: [e for e in events_of(folder) if e["name"] == "link_lost"], "link_lost", 10)
        page.wait_for_selector("#fs-return[disabled]", timeout=10_000)
        assert resume_disabled(page)
        assert page.inner_text("#fs-wait").strip() == "Waiting for the headset"
        assert page.is_disabled("#fs-check")  # the headset check needs data from the headset
        assert page.locator("#fs-wait").is_visible()
        assert page.get_attribute(SCREEN, "data-phase") == "paused"
        if how != "paused_by_hand_then_hang_up":
            assert "headset connection was lost" in page.inner_text("#fs-lost-why")
        SHOTS_NIGHT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS_NIGHT / f"paused-{how}-waiting.png"))
        # A click on the grayed-out button does nothing: no resume is sent.
        page.click("#fs-return", force=True)
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        assert page.get_attribute(SCREEN, "data-phase") == "paused"
        assert "not live yet" not in page.inner_text("#notice")
        if how == "silence":
            device.speak_again()
        else:
            plug.plug_in()
        back = until(lambda: [e for e in events_of(folder) if e["name"] == "link_back"], "link_back", 40)[0]
        page.wait_for_selector("#fs-return:not([disabled])", timeout=5_000)
        enabled_after = time.time() - back["unix_time"]
        print(f"{how}: Resume enabled {enabled_after:.3f} s after link_back")
        assert enabled_after <= 1.0, enabled_after
        assert page.get_attribute("#fs-return", "aria-disabled") == "false"
        assert not page.locator("#fs-wait").is_visible()
        page.screenshot(path=str(SHOTS_NIGHT / f"paused-{how}-ready.png"))
        # Nothing resumes by itself.
        page.wait_for_timeout(10_000)
        assert page.get_attribute(SCREEN, "data-phase") == "paused"
        assert not [e for e in events_of(folder) if e["name"] == "resumed" and e["seq"] > back["seq"]]
        resume_click(page)  # one press
        # The pause ended the block: Resume leads to the between-blocks screen (the next block, or
        # here, with one block, the next round, starts from there).
        page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=10_000)
        resumed = [e for e in events_of(folder) if e["name"] == "resumed" and e["seq"] > back["seq"]]
        assert len(resumed) == 1
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()

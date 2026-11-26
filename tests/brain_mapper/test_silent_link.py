"""A SILENT headset link is a lost link: the headset switched off, run flat or out of range while
its dongle stays plugged in (the port stays open, reads just find nothing).

When no sample has come for ``source.SILENT_SECONDS`` (3.0 s) on an open link, the link is ended
through the same path as a hang-up: the recording is closed and kept (``eeg-lost-<n>``),
``link_lost`` is logged with the reason ``silent``, a running block is paused, the headset is
tried again every 2 s, a new recording starts at link-up (which needs samples to flow), and the
block goes on only after Resume.

Every test runs the real ``brain-mapper`` on the simulated headset ``tests/python/pty_device.py``
(``go_silent`` / ``speak_again``: the pty stays open, the device just stops sending). The browser
tests drive a real Chromium. Nothing waits on a fixed sleep for an assertion; the one wait of
1.5 s is the length of a stall the test makes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import open_cognionics as oc
import pytest
from brain_mapper.source import RETRY_SECONDS, SILENT, SILENT_SECONDS, LiveSource
from test_device_browser import LINK, to_device, wait_link, wait_tip
from test_engine import SHORT, events_of, next_state, open_ws, send, session_folder_of
from test_link_pause import (
    ALLOWED,
    Plug,
    check_both_sides,
    refused,
    resume_click,
    samples_in,
    scored,
    to_running_block,
    until,
    wait_paused,
)
from test_package import wait_packaged, zip_contents
from test_shell_browser import SCREEN, begin
from test_whole_session_browser import drive, one_task_flow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PACKET_LEN, PtyDevice, config_reply, onhead_stream  # noqa: E402

#: The time from the last sample to the pause (3 s of silence, then at
#: most 1 s to close the recording, set it aside and pause the block).
PAUSED_WITHIN = (SILENT_SECONDS - 0.1, SILENT_SECONDS + 1.0)


def lost_events(folder: Path) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == "link_lost"]


def back_events(folder: Path) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == "link_back"]


def check_kept_up_to_the_silence(aside: Path, device: PtyDevice, lost: dict) -> None:
    """The set-aside recording is closed, readable, and holds every sample the device sent up to
    the silence: its ``raw.bin`` is one unbroken run of the bytes played after the configuration
    reply, ending within a packet of the last byte played."""
    raw = (aside / "raw.bin").read_bytes()
    played = device.played_after_config()
    assert raw and len(raw) % PACKET_LEN == 0
    at = played.find(raw)
    assert at >= 0, "raw.bin is not one run of what the device played"
    assert len(played) - (at + len(raw)) < PACKET_LEN, "samples sent before the silence are missing"
    recorded = oc.read_recording(aside)
    n = recorded.data.shape[0]
    # Rows of the recording: the samples received and the lost ones (filled in), in order.
    assert n == sum(c.samples + c.lost for c in oc.replay(aside)) and samples_in(aside) > 0
    # ``last_sample`` comes from the samples decoded while it was open (informational: the start
    # index is read just after the recording is registered, so it may be a few packets off).
    assert abs(lost["payload"]["last_sample"] - (n - 1)) <= 5, (lost["payload"]["last_sample"], n)


def some_recorded(folder: Path) -> None:
    """Waits until the open recording holds a second of samples (500 packets)."""
    raw = folder / "eeg" / "raw.bin"
    until(lambda: raw.is_file() and raw.stat().st_size >= 500 * PACKET_LEN, "a second of recording")


# -- in a running block, with the browser ------------------------------------------------------------


def test_silence_in_a_flanker_block_pauses_it_and_it_resumes_when_samples_return(app, page, tmp_path):
    plug = Plug(tmp_path)
    device = plug.device
    try:
        # Two blocks: the drop ends the first, the second follows.
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 40, blocks=2))
        begin(a, page)

        def enough(look) -> bool:
            try:
                f = session_folder_of(a)
            except (AssertionError, FileNotFoundError):
                return False
            return len(scored(f, "flanker")) >= 2 and look["phase"] == "running"

        drive(page, a, enough, timeout=120)
        folder = session_folder_of(a)
        silent_at = device.go_silent()
        wait_paused(page)
        took = time.monotonic() - silent_at
        print(f"last sample to paused (page): {took:.3f} s")
        assert PAUSED_WITHIN[0] <= took <= PAUSED_WITHIN[1], took
        assert "headset connection was lost" in page.inner_text("#fs-lost-why")
        # The operator's tip says what to check (the dropped-link tip).
        page.wait_for_selector('#fs-lost-tip[data-tip="dropped"]:not([hidden])')
        lost = until(lambda: lost_events(folder), "link_lost")[0]
        assert lost["payload"]["reason"] == SILENT and lost["payload"]["silent_s"] == SILENT_SECONDS
        assert lost["payload"]["earlier_recording"] == "eeg-lost-1"
        paused = [e for e in events_of(folder) if e["name"] == "paused" and e["seq"] > lost["seq"]]
        assert [e["payload"]["reason"] for e in paused] == ["link_lost"]
        check_kept_up_to_the_silence(folder / "eeg-lost-1", device, lost)
        # Resume is grayed out while the headset is silent: the dongle is there, but no link is up.
        page.wait_for_selector("#fs-return[disabled]", timeout=10_000)
        assert page.inner_text("#fs-wait").strip() == "Waiting for the headset"
        assert page.get_attribute(SCREEN, "data-phase") == "paused"
        # Tries while it stays silent are failed tries: nothing new is set aside, no link_back.
        asked = device.written().count(0x14)
        until(lambda: device.written().count(0x14) > asked, "a try while the headset is silent", 15)
        assert back_events(folder) == [] and not (folder / "eeg").exists()
        device.speak_again()
        back = until(lambda: back_events(folder), "link_back", 40)[0]
        assert back["payload"]["recording"] == "eeg" and back["payload"]["first_sample"] == 0
        assert not (folder / "eeg-lost-2").exists()
        until(lambda: (folder / "eeg" / "raw.bin").is_file(), "the new recording")
        assert page.get_attribute(SCREEN, "data-phase") == "paused"  # not before Resume
        deadline = time.monotonic() + 20
        while page.get_attribute(SCREEN, "data-phase") == "paused":
            assert time.monotonic() < deadline, "Resume was never accepted after the link came back"
            if page.locator("#fs-return").is_visible():
                resume_click(page)
            page.wait_for_timeout(300)
        drive(page, a, lambda look: look["phase"] == "done", timeout=180)
        check_both_sides(folder, drops=1)
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- without a browser ------------------------------------------------------------------------------


def test_a_short_stall_does_not_count_and_shows_in_the_data_then_silence_pauses_within_the_limit(app, tmp_path):
    plug = Plug(tmp_path)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 120))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            # A stall of 1.5 s (under the limit): nothing is lost, the block keeps running.
            stalled_at = device.go_silent()
            time.sleep(1.5)  # the length of the stall, not an assertion
            device.speak_again()
            deadline = stalled_at + SILENT_SECONDS + 2.0
            while time.monotonic() < deadline:
                try:
                    message = json.loads(ws.recv(timeout=max(0.01, deadline - time.monotonic())))
                except TimeoutError:
                    break
                if message.get("t") == "state":
                    assert message["phase"] == "running", message
            assert lost_events(folder) == []
            assert not (folder / "eeg-lost-1").exists()
            # Now a real silence: paused within the limit, measured from the last sample sent.
            silent_at = device.go_silent()
            state = next_state(ws, timeout=10, predicate=lambda m: m["phase"] == "paused")
            took = time.monotonic() - silent_at
            print(f"last sample to paused: {took:.3f} s")
            assert PAUSED_WITHIN[0] <= took <= PAUSED_WITHIN[1], took
            assert state["reason"] == "link_lost"
            lost = until(lambda: lost_events(folder), "link_lost")[0]
            assert lost["payload"]["reason"] == SILENT
            assert refused(ws, "resume")["kind"] == "refused"
            # Stop here while it is still silent: the session ends with what was recorded.
            send(ws, cmd="stop")
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "done")
            assert state["stopped"] is True and state["failure"] is None
        aside = folder / "eeg-lost-1"
        check_kept_up_to_the_silence(aside, device, lost)
        # The stall is in the data, not hidden: the samples due during it are counted as lost
        # (by the packet counter, so modulo 128), and events.jsonl notes the gap and its length.
        chunks = list(oc.replay(aside))
        assert sum(c.lost for c in chunks) == device.last_skip % 128 > 0
        stalls = [e for e in events_of(folder) if e["name"] == "link_stall"]
        assert len(stalls) == 1 and 1.5 <= stalls[0]["payload"]["seconds"] < SILENT_SECONDS, stalls
        zipped = wait_packaged(folder)
        meta = json.loads((folder / "session.json").read_text())
        assert meta["ending"] == "stopped"
        content = zip_contents(zipped)
        assert content["eeg-lost-1/raw.bin"] == (aside / "raw.bin").read_bytes()
        assert "eeg/raw.bin" not in content
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


def test_ctrl_c_during_a_silence_saves_and_zips_what_was_recorded(app, tmp_path):
    plug = Plug(tmp_path)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 120))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            some_recorded(folder)
            device.go_silent()
            next_state(ws, timeout=10, predicate=lambda m: m["phase"] == "paused")
        a.process.send_signal(signal.SIGINT)
        assert a.process.wait(timeout=60) == 0
        zipped = wait_packaged(folder, timeout=10)
        meta = json.loads((folder / "session.json").read_text())
        assert meta["ending"] == "ended_early"
        content = zip_contents(zipped)
        assert content["eeg-lost-1/raw.bin"] == (folder / "eeg-lost-1" / "raw.bin").read_bytes()
        assert samples_in(folder / "eeg-lost-1") > 0
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


def test_ctrl_c_in_the_first_seconds_of_a_silence_saves_and_zips_what_was_recorded(app, tmp_path):
    """Before the limit is reached nothing is set aside: the recording is closed as ``eeg``."""
    plug = Plug(tmp_path)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 120))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            some_recorded(folder)
            device.go_silent()
        a.process.send_signal(signal.SIGINT)
        assert a.process.wait(timeout=60) == 0
        zipped = wait_packaged(folder, timeout=10)
        content = zip_contents(zipped)
        assert content["eeg/raw.bin"] == (folder / "eeg" / "raw.bin").read_bytes()
        played = device.played_after_config()
        raw = content["eeg/raw.bin"]
        assert played.find(raw) >= 0 and len(played) - (played.find(raw) + len(raw)) < PACKET_LEN
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


def two_block_flow(tmp_path: Path, seconds: float) -> Path:
    path = tmp_path / "two-blocks.json"
    path.write_text(
        json.dumps(
            [
                {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
                {"id": "device", "kind": "setup", "title": "Device setup", "blocks": []},
                {
                    "id": "flanker",
                    "kind": "task",
                    "title": "flanker",
                    "blocks": [{"id": f"block{i + 1}", "seconds": seconds, "points": 100} for i in range(2)],
                },
                {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
            ]
        )
    )
    return path


def silence_and_back(ws, device: PtyDevice, folder: Path, n: int) -> None:
    """Silence until ``link_lost`` number ``n`` (reason silent), then samples again until the
    matching ``link_back``; the phase is never changed by either."""
    device.go_silent()
    lost = until(lambda: lost_events(folder)[n - 1 :], f"link_lost {n}", 10)[0]
    assert lost["payload"]["reason"] == SILENT
    assert (folder / f"eeg-lost-{n}").is_dir()
    device.speak_again()
    until(lambda: back_events(folder)[n - 1 :], f"link_back {n}", 40)


def test_silence_while_paused_between_blocks_and_on_the_summary(app, tmp_path):
    plug = Plug(tmp_path)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=two_block_flow(tmp_path, 6))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            # Paused by hand (full screen closed), then silent: still paused with its own reason.
            send(ws, cmd="pause", reason="fullscreen_lost")
            next_state(ws, predicate=lambda m: m["phase"] == "paused")
            device.go_silent()
            until(lambda: lost_events(folder), "link_lost", 10)
            assert refused(ws, "resume")["kind"] == "refused"
            paused = [e for e in events_of(folder) if e["name"] == "paused"]
            assert [e["payload"]["reason"] for e in paused] == ["fullscreen_lost"]
            device.speak_again()
            until(lambda: back_events(folder), "link_back", 40)
            # The pause ended the block: Resume leads to the
            # between-blocks screen.
            send(ws, cmd="resume")
            next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "between")
            # Between blocks: the next block is refused while the link is down.
            device.go_silent()
            until(lambda: lost_events(folder)[1:], "link_lost 2", 10)
            assert refused(ws, "advance")["kind"] == "refused"
            device.speak_again()
            until(lambda: back_events(folder)[1:], "link_back 2", 40)
            send(ws, cmd="advance")
            next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "running")
            next_state(ws, timeout=30, predicate=lambda m: m["phase"] == "between")
            send(ws, cmd="advance")
            next_state(ws, timeout=20, predicate=lambda m: m["step"] == "summary")
            # On the summary: logged, nothing else changes.
            silence_and_back(ws, device, folder, 3)
            send(ws, cmd="advance")
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "done")
            assert state["failure"] is None and state["stopped"] is False
        wait_packaged(folder)
        meta = json.loads((folder / "session.json").read_text())
        assert meta["ending"] == "finished"
        assert [c["folder"] for c in meta["recording_coverage"]] == ["eeg-lost-1", "eeg-lost-2", "eeg-lost-3", "eeg"]
        assert [e["payload"]["reason"] for e in lost_events(folder)] == [SILENT] * 3
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()


# -- at connect ---------------------------------------------------------------------------------------


def test_a_headset_silent_from_the_start_never_goes_live_and_the_tip_says_why(app, page, tmp_path):
    """Silent from before brain-mapper starts (the dongle there, the headset off): every try is a
    failed try with the no-data tip; once it sends, the link comes up."""
    device = PtyDevice(onhead_stream(), config_reply())
    device.go_silent()
    try:
        a = app(port=device.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_data")
        until(lambda: int(page.locator(LINK).get_attribute("data-attempt") or 0) >= 2, "a second try", 20)
        assert page.locator(LINK).get_attribute("data-state") == "connecting"
        assert page.locator("#advance").is_disabled()
        folder = session_folder_of(a)
        assert not (folder / "eeg").exists() and not (folder / "eeg-lost-1").exists()
        device.speak_again()
        wait_link(page, "live", timeout=30_000)
        until(lambda: (folder / "eeg" / "raw.bin").is_file(), "the recording")
        assert set(device.written()) <= ALLOWED
    finally:
        device.hang_up()


def test_a_headset_that_answers_but_sends_no_samples_is_a_failed_try_not_a_link(app, page, tmp_path):
    """The configuration reply comes but no sample follows: the link does not count as up (no
    recording, no live state) and the no-data tip is shown; when samples flow it comes up."""
    device = PtyDevice(onhead_stream(), config_reply())
    device.pause()  # answers requests, plays no stream
    try:
        a = app(port=device.path, flow=SHORT)
        to_device(a, page)
        wait_tip(page, "no_data")
        until(lambda: int(page.locator(LINK).get_attribute("data-attempt") or 0) >= 2, "a second try", 30)
        assert page.locator(LINK).get_attribute("data-state") == "connecting"
        folder = session_folder_of(a)
        assert not (folder / "eeg").exists()
        assert device.written().count(0x14) >= 2
        assert set(device.written()) <= ALLOWED
    finally:
        device.hang_up()


def _fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def _thread_count() -> int:
    """Threads of this process as the operating system counts them (Rust threads included)."""
    out = subprocess.run(["ps", "-M", "-p", str(os.getpid())], capture_output=True, text=True, check=True)
    return len(out.stdout.strip().splitlines()) - 1


def test_fifty_silent_tries_leave_no_file_handle_or_thread_behind(tmp_path):
    """25 tries on a headset that never answers (``NoData`` from the library) and 25 on one that
    answers but sends no sample (the wait for the first sample): each fails within its limit and
    leaves nothing open."""
    quiet = PtyDevice(onhead_stream(), config_reply())
    quiet.go_silent()
    mute = PtyDevice(onhead_stream(), config_reply())
    mute.pause()
    try:
        sources = {quiet: LiveSource(quiet.path), mute: LiveSource(mute.path)}
        before = (_fd_count(), _thread_count(), threading.active_count())
        for n in range(50):
            device = quiet if n % 2 == 0 else mute
            source = sources[device]
            began = time.monotonic()
            with pytest.raises(oc.NoData):
                source.open_streaming()
            took = time.monotonic() - began
            assert took <= (2.5 if device is quiet else 0.5 + SILENT_SECONDS + 1.0), took
            assert source.released and source.ended is None
        after = (_fd_count(), _thread_count(), threading.active_count())
        assert after == before, (before, after)
        print(f"50 silent tries: fds, OS threads, Python threads before {before}, after {after}")
        for source in sources.values():
            source.stop()
    finally:
        quiet.hang_up()
        mute.hang_up()
    assert RETRY_SECONDS == 2.0

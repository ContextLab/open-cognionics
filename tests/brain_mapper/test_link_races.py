"""Drops of the headset link that coincide with a label or with link-up, and what the hub keeps.

Real hub, real `LiveSource`, pty devices. The interleavings are forced by holding the hub's
``_rec_lock`` (which the hub thread needs to set a drop aside) while the device hangs up: the
reader has then ended the link and the hub thread has not yet reacted, which is the window
between "the link is up" and "the recording starts".
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from brain_mapper import contact, flow
from brain_mapper.engine import CommandRefused, Session
import open_cognionics as oc
from brain_mapper.server import Hub, ParticipantRefused, Settings
from brain_mapper.source import LinkLost, LiveSource
from test_engine import SHORT, events_of, run_until

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import FIXTURES, PtyDevice, config_reply, onhead_stream  # noqa: E402


def until(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def samples_in_recording(source) -> int:
    """Samples decoded since the open recording started (0 while none is open), the figure the
    source notes when the recording closes. Read under the source's recording lock:
    ``start_recording`` sets the recording and the sample it starts from under that lock, so
    the two are never seen half set."""
    with source._rec_lock:
        if source._recording is None:
            return 0
        return source.decoded - source._rec_from


def point(link: Path, target: str) -> None:
    """Moves the symlink the hub opens, as a headset plugged in later would appear."""
    tmp = link.with_name(link.name + ".new")
    tmp.symlink_to(target)
    tmp.replace(link)


def make_hub(tmp_path: Path, link: Path) -> Hub:
    hub = Hub(
        Settings(data_dir=tmp_path / "data", port=str(link), replay=None, flow=SHORT, http_port=1, ready_wait=10)
    )
    hub.start()
    return hub


@pytest.fixture
def two_devices(tmp_path):
    first = PtyDevice(onhead_stream(), config_reply())
    second = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    yield first, second, link
    first.hang_up()
    second.hang_up()


def to_device_step(hub: Hub) -> None:
    assert hub.session.state()["step"] == "participant"
    hub.session.advance()
    assert hub.session.state()["step"] == "device"


def test_a_label_accepted_at_the_moment_of_a_drop_waits_for_the_link_and_nothing_goes_stale(
    tmp_path, two_devices
):
    first, second, link = two_devices
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        with hub._rec_lock:  # the hub thread cannot react to the drop yet
            first.hang_up()
            until(lambda: hub.source.ended is not None, what="the link to end")
            assert hub._link_up  # the hub has not noticed: this is the window
            hub.accept("P001")  # start_recording raises LinkLost: a drop, not a failure
            folder = hub.folder
            assert not hub.source.recording and not (folder / "eeg").exists()
            assert hub._pending is not None
            assert hub.session.state()["failure"] is None
            to_device_step(hub)
            with pytest.raises(CommandRefused):  # engine: not on a link that is down
                hub.session.advance()
            # a start that comes at link-up, with the link already gone, is the same drop
            hub._begin_recording()
            assert hub._pending is not None and hub.session.state()["failure"] is None
        until(lambda: not hub._link_up, what="the hub to set the drop aside")
        assert hub.session.state()["phase"] != "done" and hub.session.state()["step"] == "device"
        point(link, second.path)
        until(lambda: hub.source.recording, 30, "the recording to start on the new link")
        assert (folder / "eeg" / "raw.bin").is_file()
        assert hub.session.state()["failure"] is None
        hub.session.advance()  # the link is live and recording: the device step can be passed
        assert hub.session.state()["step"] != "device"
        names = [e["name"] for e in events_of(folder)]
        assert names.count("link_lost") == 1 and names.count("link_back") == 1
        lost = next(e for e in events_of(folder) if e["name"] == "link_lost")
        assert lost["payload"]["earlier_recording"] is None  # nothing had been recorded yet
        assert not list(folder.glob("eeg-lost-*"))
    finally:
        hub.stop()


def test_a_start_that_comes_after_the_reader_finished_is_refused_as_a_lost_link_and_reset_clears_it(
    tmp_path,
):
    first = PtyDevice(onhead_stream(), config_reply())
    second = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "port"
    link.symlink_to(first.path)
    source = LiveSource(str(link))
    try:
        source.open()
        source.subscribe(4096)
        first.hang_up()
        until(lambda: source.ended is not None, what="the end")
        with pytest.raises(LinkLost):
            source.start_recording(tmp_path / "rec1", "P001", "u")
        assert not source.recording and not (tmp_path / "rec1").exists()
        source.reset()
        assert not source.recording and source.last_recording_decoded is None
        point(link, second.path)
        source.open()
        source.subscribe(4096)
        source.start_recording(tmp_path / "rec2", "P001", "u")  # not "already recording"
        assert source.recording
    finally:
        source.stop()
        first.hang_up()
        second.hang_up()


def test_a_stopped_source_refuses_a_recording_as_a_lost_link(tmp_path, pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    source.stop()
    with pytest.raises(LinkLost):
        source.start_recording(tmp_path / "rec", "P001", "u")


class ContactCheck:
    """What the hub does for the contact check, on a bare ``Session``: the real bytes (``0x11``
    when the session wants the check on, ``0x12`` when it wants it off), the real
    ``contact.ContactMonitor`` on the chunks of the source, and the real ``contact.OffCounter``
    for the settling gate. The device answers ``0x11`` by playing the recording made with the
    check on (``impedance_on_2s.bin``)."""

    def __init__(self, session: Session, source: LiveSource) -> None:
        self.session, self.source = session, source
        self.inbox = source.subscribe(4096)
        self.monitor = contact.ContactMonitor(source.config)
        self.quiet = contact.OffCounter()
        self.measuring: int | None = None
        self.written: int | None = None
        self.done = threading.Event()
        session.contact_states = lambda seq: self.monitor.states() if seq == self.measuring else None
        session.contact_quiet = lambda: (self.quiet.quiet_samples(), float(source.config.sample_rate))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.done.is_set():
            wanted = self.session.state()["contact_check"]
            if wanted != self.written:
                self.source.set_contact_check(wanted is not None)
                self.written = wanted
                self.monitor.reset()
                self.measuring = wanted
            try:
                chunk = self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            if not hasattr(chunk, "impedance_on"):
                return  # the source ended
            self.quiet.push(chunk.index, chunk.samples, chunk.impedance_on)
            if self.measuring is not None:
                self.monitor.push(chunk)

    def stop(self) -> None:
        self.done.set()
        self.thread.join(5)


@pytest.mark.parametrize("leave", ["measured", "skip"])
def test_no_round_can_start_without_a_recording_whatever_the_flow_looks_like(tmp_path, leave):
    """A flow with no device step. The calibration step is left the way a session leaves it:
    by a measured contact check and its timed parts (``measured``; the times are shortened), or
    by the skip command (``skip``). Either way the round after it is refused while nothing is
    recording, the session goes on unharmed, and the round is allowed once a recording is open."""
    from brain_mapper.points import Ledger

    steps = [s for s in flow.default_flow() if s.id != "device"]  # no device step at all
    if leave == "measured":
        short = flow.Calibration(eyes_open=0.5, eyes_closed=0.5, blink_cues=1, blink_gap=0.5, blink_lead=0.5)
        steps = [replace(s, calibration=short) if s.calibration is not None else s for s in steps]
    check_on = (FIXTURES / "impedance_on_2s.bin").read_bytes()
    device = PtyDevice(onhead_stream(), config_reply(), contact_stream=check_on)
    source = LiveSource(device.path)
    source.open()
    source.subscribe(4096)
    folder = tmp_path / "session"
    ledger = Ledger(folder / "points.jsonl")
    session = Session(steps, source, ledger, folder=folder)
    check = ContactCheck(session, source)
    try:
        assert session.state()["step"] == "participant"
        session.advance()
        assert session.state()["step"] == "calibration"
        if leave == "measured":
            until(lambda: session.contact_states(session.state()["contact_check"]), 20, "a measure")
            session.advance()  # Continue on the measured contact check
            run_until(session, lambda s: s["substep"] == "done", timeout=30)
            leave_calibration = session.advance
        else:
            leave_calibration = lambda: session.skip("calibration")  # noqa: E731
        # the next step has blocks and nothing is recording
        with pytest.raises(CommandRefused, match="not live yet"):
            leave_calibration()
        state = session.state()
        assert state["step"] == "calibration" and state["failure"] is None and not state["ended_early"]
        assert not source.recording
        assert all(e["payload"].get("marked") is False for e in events_of(folder))
        source.start_recording(folder / "eeg", "P001", "u")
        leave_calibration()  # now it is allowed
        assert session.state()["step"] == "words"
        markers = [json.loads(x) for x in (folder / "eeg" / "markers.jsonl").read_text().splitlines()]
        assert markers and all(m["label"].startswith(("words/", "calibration/")) for m in markers)
        assert set(device.written()) <= {0x11, 0x12, 0x14}
    finally:
        check.stop()
        session.close()
        ledger.close()
        source.stop()
        device.hang_up()


def test_a_link_that_keeps_dropping_is_not_tried_again_at_once(tmp_path, two_devices):
    first, second, link = two_devices
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        first.hang_up()
        dropped = time.monotonic()
        until(lambda: not hub._link_up, 10, "the drop to be noticed")
        point(link, second.path)  # a headset is there at once; the wait is the hub's
        until(lambda: hub._link_up, 30, "the link to come back")
        assert hub._opened_at - dropped >= 1.9, hub._opened_at - dropped
    finally:
        hub.stop()


def test_a_set_aside_recording_is_noted_in_events_and_session_json(tmp_path):
    first = PtyDevice(onhead_stream(), config_reply())
    second = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        hub.accept("P001")
        folder = hub.folder
        # More than 100 samples decoded while the recording is open (``decoded`` counts from the
        # link's start, and the recording starts some samples later).
        until(lambda: samples_in_recording(hub.source) > 100, 20, "samples in the first recording")
        first.hang_up()
        until(lambda: not hub._link_up, 10, "the first drop")
        point(link, second.path)
        until(lambda: hub.source.recording, 30, "the second recording")
        meta = json.loads((folder / "session.json").read_text())
        assert [r["folder"] for r in meta["recordings"]] == ["eeg-lost-1"]
        assert meta["recordings"][0]["decoded_incl_lost"] > 50  # a few packets short at most
        assert (folder / "eeg-lost-1" / "raw.bin").is_file()
        events = events_of(folder)
        lost = next(e for e in events if e["name"] == "link_lost")
        back = next(e for e in events if e["name"] == "link_back")
        assert lost["payload"]["earlier_recording"] == "eeg-lost-1"
        assert back["payload"]["recording"] == "eeg" and back["payload"]["earlier_recordings"] == ["eeg-lost-1"]
        assert "marked" not in lost["payload"] and lost["seq"] < back["seq"]
    finally:
        hub.stop()
        first.hang_up()
        second.hang_up()


def _tree_hashes(folder: Path) -> dict[str, str]:
    import hashlib

    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _hub_thread_is_in(hub: Hub, name: str) -> bool:
    frame = sys._current_frames().get(hub._thread.ident)
    while frame is not None:
        if frame.f_code.co_name == name:
            return True
        frame = frame.f_back
    return False


def test_a_drop_during_a_stop_sets_nothing_aside_and_starts_nothing(tmp_path, two_devices):
    first, second, link = two_devices
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        hub.accept("P001")
        folder = hub.folder
        until(lambda: hub.source.recording and hub.source.decoded > 50, 20, "samples")
        with hub._rec_lock:
            first.hang_up()
            until(lambda: _hub_thread_is_in(hub, "_link_lost"), 10, "the hub thread to be in _link_lost")
            hub.session.advance()  # participant -> device
            hub.session.stop()  # the session ends and closes its recording
            assert hub.session.state()["phase"] == "done"
            closed = _tree_hashes(folder / "eeg")
        until(lambda: not hub._thread.is_alive() or hub._state.get("link", "").count('"ended"'), 10, "the link to end")
        assert not list(folder.glob("eeg-lost-*"))  # nothing was set aside
        assert _tree_hashes(folder / "eeg") == closed  # the closed recording is untouched
        assert hub._pending is None and not hub.source.recording
        point(link, second.path)
        time.sleep(0.2)
        assert not hub.source.recording  # nothing is started for a session that is over
        assert "link_lost" not in [e["name"] for e in events_of(folder)]
        assert json.loads(hub._state["link"])["state"] == "ended"
    finally:
        hub.stop()


def test_a_link_that_flaps_five_times_leaves_five_folders_with_their_files_intact(tmp_path):
    # Each device is made just before it is plugged in: one that streams for long into a pty
    # nobody reads stalls the first read (see the notes of this round).
    devices = [PtyDevice(onhead_stream(), config_reply())]
    link = tmp_path / "headset-port"
    link.symlink_to(devices[0].path)
    hub = make_hub(tmp_path, link)
    seen: dict[str, dict[str, str]] = {}
    try:
        assert hub.ready.wait(20)
        hub.accept("P001")
        folder = hub.folder
        for n in range(1, 6):
            until(lambda: hub.source.recording and hub.source.decoded > 50, 40, f"samples on link {n}")
            devices[n - 1].hang_up()
            name = f"eeg-lost-{n}"
            until(lambda: (folder / name).is_dir() and not hub._link_up, 20, f"{name} to be set aside")
            seen[name] = _tree_hashes(folder / name)  # whole and closed from here on
            assert "raw.bin" in seen[name] and "meta.json" in seen[name]
            devices.append(PtyDevice(onhead_stream(), config_reply()))
            point(link, devices[n].path)
        until(lambda: hub.source.recording, 40, "the sixth recording")
        assert sorted(p.name for p in folder.glob("eeg-lost-*")) == [f"eeg-lost-{n}" for n in range(1, 6)]
        for name, hashes in seen.items():  # nothing was touched by the later drops
            assert _tree_hashes(folder / name) == hashes, name
        meta = json.loads((folder / "session.json").read_text())
        assert [r["folder"] for r in meta["recordings"]] == list(seen)
        assert all(r["decoded_incl_lost"] and r["decoded_incl_lost"] > 0 for r in meta["recordings"])
        names = [e["name"] for e in events_of(folder)]
        assert names.count("link_lost") == 5 and names.count("link_back") == 5
    finally:
        hub.stop()
        for d in devices:
            d.hang_up()


def test_a_drop_with_no_recording_does_not_carry_the_count_of_an_earlier_link(tmp_path, two_devices):
    first, second, link = two_devices
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        hub.accept("P001")
        until(lambda: hub.source.decoded > 50, 20, "samples")
        first.hang_up()
        until(lambda: not hub._link_up, 10, "the first drop")
        assert hub.source.last_recording_decoded is None  # reset by the drop
    finally:
        hub.stop()


def test_a_headset_that_disconnects_while_the_recording_starts_is_a_lost_link(tmp_path):
    """The branch of ``start_recording`` for an ``oc.Disconnected`` raised inside
    ``Headset.record``: the source has no reader here (never subscribed), so it has not
    flagged the end itself; the library has seen the hang-up."""
    dev = PtyDevice(onhead_stream(), config_reply())
    source = LiveSource(dev.path)
    try:
        source.open()
        dev.hang_up()
        error = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and error is None:
            try:
                source.start_recording(tmp_path / "rec", "P001", "u")
                source.stop_recording()
            except LinkLost as lost:
                error = lost
            time.sleep(0.05)
        assert error is not None, "the library never reported the hang-up"
        assert isinstance(error.__cause__, oc.Disconnected)
        assert not source.recording and source.last_recording_decoded is None
    finally:
        source.stop()
        dev.hang_up()


def test_stop_during_an_open_attempt_waits_it_out_and_leaves_nothing_behind(tmp_path):
    dev = PtyDevice(onhead_stream(), None)  # streams, never sends a configuration: about 6 s
    hub = make_hub(tmp_path, Path(dev.path))
    try:
        until(lambda: bytes([0x14]) in dev.written(), 10, "the attempt to ask for the configuration")
        hub.stop()  # waits for the attempt (it gives up by itself after about 6 s)
        assert hub._thread is not None and not hub._thread.is_alive()  # the product's own join succeeded
        assert hub.source.attempts and hub.source.attempts[-1][1] >= hub.source.attempts[-1][0]
        assert hub.source.released
    finally:
        dev.hang_up()


def test_a_recording_promised_at_a_label_is_not_started_or_failed_during_shutdown(tmp_path, two_devices):
    first, second, link = two_devices
    hub = make_hub(tmp_path, link)
    try:
        assert hub.ready.wait(20)
        with hub._rec_lock:
            first.hang_up()
            until(lambda: hub.source.ended is not None)
            hub.accept("P001")
            hub._stopped.set()
            hub._begin_recording()
            assert hub._pending is not None  # kept, not started
            assert hub.session.state()["failure"] is None
    finally:
        hub.stop()
    assert "ended" not in [e["name"] for e in events_of(hub.folder)]


class FaultyHub(Hub):
    """A hub whose thread fails inside ``_serve``, as a bug would.

    This is an injected fault, kept on purpose: every input that reaches the hub thread from the
    outside (a missing or silent or mute port, a hang-up, an unwritable data folder, a device
    that loses samples) is caught where it happens and reported another way, so no real cause
    reaches the ``except`` at the top of the thread; that path exists for bugs in this code, and
    only a bug can trigger it.
    """

    def _serve(self):
        raise ValueError("boom")


def test_after_a_fault_of_the_hub_thread_a_label_is_refused_with_the_fault_message(tmp_path):
    dev = PtyDevice(onhead_stream(), config_reply())
    hub = FaultyHub(Settings(data_dir=tmp_path / "data", port=dev.path, replay=None, flow=SHORT, http_port=1, ready_wait=10))
    hub.start()
    try:
        until(lambda: hub._failed.is_set(), 20, "the fault")
        with pytest.raises(ParticipantRefused) as refused:
            hub.accept("P001")
        assert refused.value.code == "source_not_ready"
        assert "error inside brain-mapper" in str(refused.value) and "boom" in str(refused.value)
        assert hub.session is None
        sessions = tmp_path / "data" / "sessions"
        assert not sessions.exists() or not list(sessions.iterdir())  # nothing was made
    finally:
        hub.stop()
        dev.hang_up()
    link = json.loads(hub._state["link"])
    assert link["state"] == "ended" and link["fault"] is True and "boom" in link["reason"]


def test_a_fault_with_a_session_already_waiting_ends_it_once_with_a_failure(tmp_path):
    """Injected fault (see ``FaultyHub``): ``_may_retry`` is the first thing the hub thread
    calls after a link ends, so a bug there reaches the top-level handler."""
    first = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"  # nothing there: the first attempt fails and is retried

    class LateFault(Hub):
        def _may_retry(self):
            raise ValueError("boom")

    hub = LateFault(
        Settings(data_dir=tmp_path / "data", port=str(link), replay=None, flow=SHORT, http_port=1, ready_wait=3)
    )
    hub.start()
    try:
        hub.accept("P001")  # no link: the recording is promised
        assert hub.session.state()["failure"] is None
        point(link, first.path)
        until(lambda: hub.source.recording, 30, "the link")
        first.hang_up()  # the hub thread now asks _may_retry, which fails
        until(lambda: hub.session.state()["phase"] == "done", 20, "the session to end")
        state = hub.session.state()
        assert "error inside brain-mapper" in state["failure"]
        names = [e["name"] for e in events_of(hub.folder)]
        assert names.count("ended") == 1
    finally:
        hub.stop()
        first.hang_up()


def test_the_check_command_prints_a_fail_line_for_an_argument_error(tmp_path):
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "brain_mapper.cli", "--check", "--port", "x", "--replay", str(tmp_path)],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 1
    assert out.stdout.startswith("FAIL  arguments: ")
    bad = subprocess.run(
        [sys.executable, "-m", "brain_mapper.cli", "--check", "--nonsense"],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert bad.returncode == 2
    assert bad.stdout.startswith("FAIL  arguments: ")

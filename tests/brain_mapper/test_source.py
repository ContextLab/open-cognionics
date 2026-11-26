"""LiveSource and ReplaySource against a real pty device and a real recording."""

from __future__ import annotations

import json
import queue
import threading
import shutil
import time
import zipfile

import numpy as np
import pytest

import open_cognionics as oc
from brain_mapper.source import LiveSource, ReplaySource, SourceEnded


def collect(subscription, timeout=60.0):
    """Chunks up to and including the ``SourceEnded`` that closes the subscription."""
    chunks = []
    deadline = time.monotonic() + timeout
    while True:
        item = subscription.get(timeout=max(0.1, deadline - time.monotonic()))
        if isinstance(item, SourceEnded):
            return chunks, item
        chunks.append(item)


def take_chunks(subscription, seconds):
    chunks = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            item = subscription.get(timeout=0.2)
        except queue.Empty:
            continue
        assert not isinstance(item, SourceEnded), item
        chunks.append(item)
    return chunks


def assert_contiguous(chunks):
    for before, after in zip(chunks, chunks[1:]):
        assert after.index == before.index + before.samples + after.lost


def test_live_source_yields_contiguous_chunks_of_20_channels(pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    try:
        assert source.kind == "live"
        assert source.config.labels[:3] == ["F7", "Fp1", "Fp2"]
        assert len(source.config.labels) == 20
        sub = source.subscribe(4096)
        chunks = take_chunks(sub, 2.0)
        assert chunks
        assert all(c.data.shape[1] == 20 for c in chunks)
        assert_contiguous(chunks)
        assert 800 <= sum(c.samples for c in chunks) <= 1300
        assert source.position >= chunks[-1].index + chunks[-1].samples - 1
    finally:
        source.stop()


def test_recording_through_the_live_source_opens_with_the_mark(pty_device, tmp_path):
    out = tmp_path / "session"
    source = LiveSource(pty_device.path)
    source.open()
    try:
        source.subscribe(4096)
        source.start_recording(out, "P007", "uuid-7")
        time.sleep(1.0)
        source.mark("hello")
        time.sleep(1.0)
    finally:
        source.stop()
    recorded = oc.read_recording(out)
    assert recorded.data.shape[0] >= 900
    assert recorded.data.shape[1] == 20
    assert [m.label for m in recorded.markers] == ["hello"]
    assert recorded.meta["participant"] == "P007"
    assert recorded.meta["participant_uuid"] == "uuid-7"
    assert recorded.meta["finished"] is True


def test_mark_without_a_recording_is_an_error(pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    try:
        with pytest.raises(RuntimeError):
            source.mark("x")
    finally:
        source.stop()


def test_only_the_three_known_bytes_are_ever_written(pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    try:
        time.sleep(0.3)
        assert pty_device.written() == bytes([0x14])
        source.set_contact_check(True)
        time.sleep(0.3)
        source.set_contact_check(False)
        time.sleep(0.3)
        assert pty_device.written() == bytes([0x14, 0x11, 0x12])
    finally:
        source.stop()
    assert set(pty_device.written()) <= {0x11, 0x12, 0x14}


def test_a_subscriber_that_never_reads_loses_chunks_and_a_reader_does_not(pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    try:
        idle = source.subscribe(2)
        busy = source.subscribe(4096)
        seen = take_chunks(busy, 2.0)
        assert len(seen) > 4
        assert_contiguous(seen)
        held = []
        while True:
            try:
                held.append(idle.get_nowait())
            except queue.Empty:
                break
        assert 0 < len(held) <= 2
        assert len(held) < len(seen)
    finally:
        source.stop()


def test_a_hang_up_delivers_source_ended_disconnected(pty_device):
    source = LiveSource(pty_device.path)
    source.open()
    try:
        sub = source.subscribe(4096)
        take_chunks(sub, 0.5)
        pty_device.hang_up()
        chunks, ended = collect(sub, timeout=15)
        assert isinstance(ended, SourceEnded)
        assert ended.reason.startswith("disconnected: ")
    finally:
        source.stop()


def test_live_source_without_a_headset_raises_the_library_error(tmp_path):
    source = LiveSource(str(tmp_path / "no-such-device"))
    with pytest.raises(oc.OpenError):
        source.open()


def make_zip(folder, target):
    """A session zip with the recording under ``eeg/``."""
    with zipfile.ZipFile(target, "w") as z:
        for path in sorted(folder.iterdir()):
            z.write(path, f"eeg/{path.name}")
        z.writestr("session.json", "{}")
    return target


@pytest.mark.parametrize("form", ["folder", "zip", "raw"])
def test_replay_source_plays_the_whole_recording_in_real_time(form, recorded_session, tmp_path):
    expected = oc.read_recording(recorded_session).data.shape[0]
    if form == "folder":
        path = recorded_session
    elif form == "zip":
        path = make_zip(recorded_session, tmp_path / "session.zip")
    else:
        path = recorded_session / "raw.bin"
    source = ReplaySource(path)
    source.open()
    try:
        assert source.kind == "replay"
        assert len(source.config.labels) == 20
        seconds = expected / source.config.sample_rate
        sub = source.subscribe(100_000)
        start = time.monotonic()
        chunks, ended = collect(sub, timeout=seconds + 30)
        elapsed = time.monotonic() - start
        assert sum(c.samples + c.lost for c in chunks) == expected
        assert all(c.data.shape[1] == 20 for c in chunks)
        assert_contiguous(chunks)
        assert 0.8 * seconds <= elapsed <= seconds + 5
        assert ended.reason == "end of recording"
        assert source.position == expected
    finally:
        source.stop()


def test_a_bare_raw_bin_is_refused_with_a_message(recorded_session, tmp_path):
    lone = tmp_path / "bare"
    lone.mkdir()
    shutil.copy(recorded_session / "raw.bin", lone / "raw.bin")
    with pytest.raises(ValueError, match="meta.json"):
        ReplaySource(lone / "raw.bin").open()


def test_a_path_that_is_not_a_recording_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        ReplaySource(tmp_path / "missing").open()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="raw.bin"):
        ReplaySource(empty).open()


def test_replay_recording_copies_the_source_files_and_writes_new_marks(recorded_session, tmp_path):
    out = tmp_path / "copy"
    source = ReplaySource(recorded_session)
    source.open()
    try:
        sub = source.subscribe(100_000)
        take_chunks(sub, 2.0)
        source.start_recording(out, "P009", "uuid-9")
        before = source.position
        assert before > 0
        source.mark("one")
        time.sleep(0.5)
        source.mark("two")
        after = source.position
    finally:
        source.stop()
    for name in ("raw.bin", "meta.json", "clock.jsonl", "recording.bdf"):
        assert (out / name).read_bytes() == (recorded_session / name).read_bytes()
    lines = [json.loads(line) for line in (out / "markers.jsonl").read_text().splitlines()]
    assert [m["label"] for m in lines] == ["one", "two"]
    assert before <= lines[0]["sample"] < lines[1]["sample"] <= after
    assert all(isinstance(m["unix_time"], float) for m in lines)
    assert oc.read_recording(out).data.shape[0] == oc.read_recording(recorded_session).data.shape[0]


def test_replay_contact_check_changes_nothing(recorded_session):
    source = ReplaySource(recorded_session)
    source.open()
    try:
        source.set_contact_check(True)
        source.set_contact_check(False)
        assert "does nothing" in ReplaySource.set_contact_check.__doc__.lower()
    finally:
        source.stop()


def test_replay_chunks_hold_finite_volt_values(recorded_session):
    source = ReplaySource(recorded_session)
    source.open()
    try:
        sub = source.subscribe(1000)
        chunks = take_chunks(sub, 1.0)
        data = np.concatenate([c.data for c in chunks])
        assert np.isfinite(data).all()
        assert np.abs(data).max() < 2.5 / source.config.gain
    finally:
        source.stop()


def test_a_lost_link_closes_the_recording_and_releases_the_port(pty_device, tmp_path):
    out = tmp_path / "session"
    source = LiveSource(pty_device.path)
    source.open()
    try:
        sub = source.subscribe(4096)
        source.start_recording(out, "P010", "uuid-10")
        time.sleep(1.5)
        source.mark("before")
        time.sleep(0.5)
        pty_device.hang_up()
        _, ended = collect(sub, timeout=15)
        assert ended.reason.startswith("disconnected: ")
        # Nothing was closed by hand: the source closed both when the link ended.
        assert source.recording is False
        assert source.released is True
        with pytest.raises(RuntimeError):
            source.mark("after")
        meta = json.loads((out / "meta.json").read_text())
        assert meta["finished"] is True
        raw = (out / "raw.bin").read_bytes()
        played = pty_device.played_after_config()
        at = played.find(raw)
        assert at >= 0 and len(raw) > 0
        assert len(played) - (at + len(raw)) < 75  # at most a packet cut by the hang-up
        recorded = oc.read_recording(out)
        assert [m.label for m in recorded.markers] == ["before"]
        assert recorded.data.shape[0] >= 900
        assert b"disconnected: " in (out / "recording.bdf").read_bytes()
    finally:
        source.stop()


def test_the_end_of_a_replay_closes_the_recording(recorded_session, tmp_path):
    out = tmp_path / "copy"
    source = ReplaySource(recorded_session)
    source.open()
    try:
        sub = source.subscribe(100_000)
        source.start_recording(out, "P011", "uuid-11")
        source.mark("one")
        _, ended = collect(sub, timeout=60)
        assert ended.reason == "end of recording"
        assert source.recording is False
        with pytest.raises(RuntimeError):
            source.mark("two")
        lines = (out / "markers.jsonl").read_text().splitlines()
        assert [json.loads(line)["label"] for line in lines] == ["one"]
        assert (out / "raw.bin").read_bytes() == (recorded_session / "raw.bin").read_bytes()
    finally:
        source.stop()


def test_stop_leaves_a_complete_recording_of_what_the_reader_had_received(pty_device, tmp_path):
    out = tmp_path / "session"
    source = LiveSource(pty_device.path)
    source.open()
    source.subscribe(4096)
    source.start_recording(out, "P012", "uuid-12")
    time.sleep(2.0)
    source.stop()
    assert source.recording is False
    assert source.released is True
    assert json.loads((out / "meta.json").read_text())["finished"] is True
    raw = (out / "raw.bin").read_bytes()
    played = pty_device.played_after_config()
    at = played.find(raw)
    assert at >= 0
    assert len(raw) % 75 == 0
    # What the device had played when the recording ended is in the file, but
    # for the bytes still in flight (a read is 20 ms of data at most).
    assert len(played) - (at + len(raw)) < 0.25 * 37_500


def test_stop_during_open_releases_the_port_when_open_returns(tmp_path):
    from pty_device import PtyDevice, config_reply, onhead_stream

    # The first request for the configuration is not answered, so open() waits
    # for the resend (1.5 s) and then succeeds.
    device = PtyDevice(onhead_stream(), config_reply(), ignore_requests=1)
    try:
        source = LiveSource(device.path)
        outcome = []

        def opener():
            try:
                source.open()
                outcome.append("opened")
            except Exception as error:
                outcome.append(error)

        thread = threading.Thread(target=opener)
        thread.start()
        deadline = time.monotonic() + 5
        while device.written() == b"" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert device.written() == bytes([0x14])
        started = time.monotonic()
        source.stop()
        assert time.monotonic() - started < 1.0  # stop does not wait for open
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert len(outcome) == 1 and isinstance(outcome[0], RuntimeError)
        assert source.released is True
        # The port is free again: a new connection to the same device works.
        with oc.connect(device.path) as headset:
            assert headset.config.n_eeg == 20
    finally:
        device.hang_up()


def eeg_counts(stream: bytes, packet: int, channel: int) -> int:
    """Raw count of one EEG channel of one packet, from the byte layout in docs/protocol.md."""
    at = packet * 75 + 2 + 3 * channel
    b0, b1, b2 = stream[at : at + 3]
    value = ((b0 << 24) | (b1 << 17) | (b2 << 10)) & 0xFFFFFFFF
    if value >= 1 << 31:
        value -= 1 << 32
    return value >> 8


def test_a_recording_started_between_the_two_parts_of_a_packet_starts_at_the_first_whole_packet(tmp_path):
    """The fixture writes whole packets now, so this is the product proof that a split packet is
    handled: a packet that arrives in two reads, with the recording started between them,
    leaves its second part at the start of raw.bin (the bytes exactly as received) and the
    decoder starts at the next, whole packet (sample 0), as docs/recordings.md says."""
    from pty_device import PtyDevice, config_reply, onhead_stream

    stream = onhead_stream()
    device = PtyDevice(stream, config_reply())
    out = tmp_path / "session"
    source = LiveSource(device.path)
    try:
        source.open()
        source.subscribe(4096)
        device.hold_next_packet(17)  # 17 bytes of a packet now, the other 58 later
        assert device.wait_held(), "the first part was not read"
        # The pty queue being empty only says the reader took the bytes from the port. The
        # reader's own counter says it has dealt with everything before them: every whole
        # packet played ahead of the held one (the stream after the configuration reply) has
        # been decoded, so the 17 bytes are what it is holding when the recording starts.
        # (the device played packets before the configuration reply; the library drops those)
        whole_before = (
            (len(device.played()) - len(config_reply()) - 17) // 75 - device.packets_before_config
        )
        deadline = time.monotonic() + 10
        while source.decoded < whole_before and time.monotonic() < deadline:
            time.sleep(0.001)
        assert source.decoded == whole_before, (source.decoded, whole_before)
        source.start_recording(out, "P012", "uuid-12")
        source.mark("between the parts")
        device.release_held_packet()
        time.sleep(1.5)
    finally:
        source.stop()
        device.hang_up()
    held = device.held_packet
    assert held is not None
    raw = (out / "raw.bin").read_bytes()
    # the stray leading bytes are kept, exactly as received: the rest of the held packet
    assert raw[:58] == stream[held * 75 + 17 : held * 75 + 75]
    recorded = oc.read_recording(out)
    # sample 0 is the first whole packet, the one after the held packet
    assert recorded.data.shape[0] == (len(raw) - 58) // 75
    assert recorded.data.shape[0] >= 500
    scale = recorded.meta["eeg_volts_per_count"]
    for row in (0, 1, 2, 50, 300, recorded.data.shape[0] - 1):
        packet = (held + 1 + row) % (len(stream) // 75)
        for channel in range(20):
            expected = eeg_counts(stream, packet, channel) * scale
            assert abs(recorded.data[row, channel] - expected) < 1e-12, (row, channel)
    assert [(m.label, m.sample) for m in recorded.markers] == [("between the parts", 0)]


# -- trying again until the headset opens ------------------------------------------------------------


def _retry_in_thread(source, reports):
    outcome = []

    def run():
        outcome.append(source.connect_with_retry(lambda problem, n: reports.append((time.monotonic(), problem, n))))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def _fds_open_on(path) -> int:
    """How many of this process's file descriptors are open on ``path`` (a port).

    Counts only handles on that one path, so another thread of the test process opening or
    closing something else (a browser, a log file) cannot change the number.
    """
    import fcntl
    import os

    target = os.path.realpath(path)
    count = 0
    for name in os.listdir("/dev/fd"):
        try:
            if hasattr(fcntl, "F_GETPATH"):  # macOS
                found = fcntl.fcntl(int(name), fcntl.F_GETPATH, bytes(1024)).split(b"\0")[0].decode()
            else:
                found = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if found == target:
            count += 1
    return count


def _assert_attempts_two_seconds_apart(attempts, at_least):
    """The attempts' own timestamps (``LiveSource.attempts``, ``time.monotonic()`` of the product):
    each attempt starts 2 s after the one before ended, so start to start it is at least 1.9 s and
    at most 2.0 s plus how long the earlier attempt took plus 0.5 s. A retry period of 30 s, or of
    0, fails this whatever the load; how long the test waited for the attempts does not matter."""
    assert len(attempts) >= at_least, attempts
    for (start, end), (next_start, _) in zip(attempts, attempts[1:]):
        spacing = next_start - start
        assert 1.9 <= spacing <= 2.0 + (end - start) + 0.5, (spacing, end - start)
        assert next_start - end >= 1.9, (next_start - end)  # never overlapping, never at once


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_port_that_cannot_be_opened_is_tried_every_two_seconds_not_in_a_tight_loop(tmp_path):
    source = LiveSource(str(tmp_path / "no-such-port"))
    reports = []
    thread, outcome = _retry_in_thread(source, reports)
    _wait_for(lambda: len(reports) >= 3, 90)
    started = time.monotonic()
    source.stop()
    thread.join(timeout=5)
    assert time.monotonic() - started < 1.0  # the wait between attempts ends at once
    assert not thread.is_alive() and outcome == [False]
    _assert_attempts_two_seconds_apart(source.attempts, at_least=3)
    assert [n for _, _, n in reports][:3] == [1, 2, 3]
    assert {p.code for _, p, _ in reports} == {"open_error"}
    assert reports[0][1].kind == "OpenError" and "No such file" in reports[0][1].msg


def test_two_attempts_never_run_at_once_and_a_silent_headset_is_not_asked_again_at_once():
    from pty_device import PtyDevice

    device = PtyDevice.silent_device()
    held = _fds_open_on(device.path)  # the device's own handle on its pty
    # The counter does see a handle: while a source holds the port, the number is one higher.
    probe = LiveSource(device.path)
    try:
        import open_cognionics as oc

        try:
            probe.open()
        except oc.NoData:
            pass
        assert _fds_open_on(device.path) == held  # a failed open leaves nothing behind
        holder = open(device.path, "rb", buffering=0)
        try:
            assert _fds_open_on(device.path) == held + 1  # a leak would show like this
        finally:
            holder.close()
        assert _fds_open_on(device.path) == held
    finally:
        probe.stop()
    source = LiveSource(device.path)
    reports = []
    try:
        thread, outcome = _retry_in_thread(source, reports)
        _wait_for(lambda: len(reports) >= 2, 90)
        # each attempt writes one configuration request and takes about 1.8 s to give up; the
        # next starts 2 s after that, so the requests are 3.8 s or more apart (never overlapping)
        _assert_attempts_two_seconds_apart(source.attempts, at_least=2)
        assert all(end - start >= 1.5 for start, end in source.attempts[:-1])  # each waited for data
        assert device.written().count(bytes([0x14])) <= len(reports) + 1
        assert {p.code for _, p, _ in reports} == {"no_data"}
        source.stop()
        thread.join(timeout=10)
        assert not thread.is_alive() and outcome == [False]
        assert source.released is True
        assert _fds_open_on(device.path) == held  # this source's handles on the port are all closed
        assert len(source.attempts) >= len(reports)
    finally:
        device.hang_up()


def test_stop_while_an_attempt_is_in_flight_ends_the_retrying_and_leaves_no_port_open():
    from pty_device import PtyDevice

    device = PtyDevice.silent_device()
    source = LiveSource(device.path)
    reports = []
    try:
        thread, outcome = _retry_in_thread(source, reports)
        _wait_for(lambda: device.written() == bytes([0x14]), 5)  # the first attempt is waiting
        source.stop()
        thread.join(timeout=10)
        assert not thread.is_alive() and outcome == [False]
        assert source.released is True
        # the port is free: a new connection to the same device works once it answers
    finally:
        device.hang_up()


def test_stop_during_an_attempt_on_a_device_that_never_takes_the_request_ends_within_the_limit():
    from pty_device import PtyDevice, config_reply, onhead_stream

    # The device streams but never reads what is written to it (as when the dongle's flow
    # control holds the computer's output). The attempt cannot be interrupted, but it gives up
    # within the library's limit of 8 s (docs/device.md), and stop leaves no port open.
    device = PtyDevice(onhead_stream(), config_reply(), deaf=True)
    held = _fds_open_on(device.path)  # the device's own handle on its pty
    source = LiveSource(device.path)
    reports = []
    try:
        started = time.monotonic()
        thread, outcome = _retry_in_thread(source, reports)
        _wait_for(device.host_bytes_waiting, 5)  # the request is waiting
        source.stop()
        thread.join(timeout=8.0 + 5)
        assert not thread.is_alive()
        assert time.monotonic() - started < 8.0 + 1.0
        assert outcome == [False]
        assert source.released is True
        assert _fds_open_on(device.path) == held
    finally:
        device.hang_up()


def test_a_headset_that_appears_later_is_found_by_the_retries(tmp_path, pty_device):

    link = tmp_path / "port"
    source = LiveSource(str(link))
    reports = []
    try:
        thread, outcome = _retry_in_thread(source, reports)
        _wait_for(lambda: reports, 10)
        link.symlink_to(pty_device.path)  # plugged in
        thread.join(timeout=15)
        assert outcome == [True]
        assert reports[0][1].code == "open_error"
        assert source.config.n_eeg == 20
        sub = source.subscribe(4096)
        assert take_chunks(sub, 1.0)
    finally:
        source.stop()


def test_an_ended_live_source_can_be_reset_and_opened_again(tmp_path):
    from pty_device import PtyDevice, config_reply, onhead_stream

    first = PtyDevice(onhead_stream(), config_reply())
    second = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "port"
    link.symlink_to(first.path)
    source = LiveSource(str(link))
    try:
        source.open()
        sub = source.subscribe(4096)
        assert take_chunks(sub, 0.5)
        with pytest.raises(RuntimeError):
            source.reset()  # still running
        first.hang_up()
        _, ended = collect(sub, timeout=10)
        assert ended.reason.startswith("disconnected")
        source.reset()
        assert source.ended is None and source.position == 0 and source.released is True
        link.unlink()
        link.symlink_to(second.path)
        source.open()
        assert take_chunks(source.subscribe(4096), 1.0)
    finally:
        source.stop()
        first.hang_up()
        second.hang_up()
    with pytest.raises(RuntimeError):
        source.reset()  # stopped

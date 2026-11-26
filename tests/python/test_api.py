"""The Python API against a real pseudo-terminal playing recorded headset bytes.

No test here opens a real serial port: the headset dongle may be plugged in.
"""

import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import psutil
import pytest

import open_cognionics as oc
from pty_device import PACKET_LEN, REAL_RATE, PtyDevice, config_reply, onhead_stream

HERE = Path(__file__).resolve().parent


def take(iterator, n):
    out = []
    for item in iterator:
        out.append(item)
        if len(out) == n:
            break
    return out


def test_version():
    assert oc.__version__ == "0.0.1"


def test_connect_reads_the_configuration(device):
    with oc.connect(device.path) as h:
        config = h.config
        assert config.labels[:3] == ["F7", "Fp1", "Fp2"]
        assert config.sample_rate == 500.0
        assert config.n_eeg == 20
        assert config.n_ext == 0
        assert config.n_acc == 3
        assert config.gain == 3
        assert config.eeg_volts_per_count == 5.0 / (3 * 2**24)
        assert config.channel_names == config.labels + ["ACC_X", "ACC_Y", "ACC_Z"]
        assert isinstance(config.impedance_at_power_on, bool)
        assert h.port == device.path
    assert device.written() == bytes([0x14])


def test_first_chunks(device):
    with oc.connect(device.path) as h:
        before = time.time()
        chunks = take(h.stream(), 3)
        assert h.sample_index >= chunks[-1].index + chunks[-1].samples
        gain = h.config.gain
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.data.shape == (chunk.samples, 20)
        assert chunk.data.dtype == np.float64
        assert chunk.data.flags["C_CONTIGUOUS"]
        # Volts, within the converter's input range (+-2.5 V / gain). The
        # recording has one electrode (O1) at -0.75 V, so 0.5 V is too low.
        assert np.abs(chunk.data).max() < 2.5 / gain
        assert np.abs(chunk.data).max() > 1e-4
        assert chunk.acc.shape == (chunk.samples, 3)
        assert chunk.ext.shape == (chunk.samples, 0)
        assert chunk.triggers.shape == (chunk.samples,)
        assert chunk.lost == 0
        assert 3.0 < chunk.battery_volts < 5.0
        assert chunk.impedance_on is False
        # Host time of the first sample, within the generous bounds of a loaded machine.
        assert before - 5.0 < chunk.time < time.time() + 1.0
    for a, b in zip(chunks, chunks[1:]):
        assert b.index == a.index + a.samples


def test_chunk_values_match_the_recorded_bytes(device):
    stream = onhead_stream()
    with oc.connect(device.path) as h:
        chunk = take(h.stream(), 1)[0]
        vpc = h.config.eeg_volts_per_count
    # Every row must be one of the recorded packets, scaled exactly.
    packets = {}
    for k in range(len(stream) // PACKET_LEN):
        p = stream[k * PACKET_LEN : (k + 1) * PACKET_LEN]
        packets.setdefault(p[2:-4], p)
    row = chunk.data[0]
    acc = chunk.acc[0]
    matches = []
    for body in packets:
        counts = []
        for ch in range(23):
            b0, b1, b2 = body[3 * ch : 3 * ch + 3]
            value = ((b0 << 24) | (b1 << 17) | (b2 << 10)) & 0xFFFFFFFF
            if value >= 1 << 31:
                value -= 1 << 32
            counts.append(value >> 8)
        if np.array_equal(np.array(counts[:20]) * vpc, row) and list(acc) == counts[20:]:
            matches.append(body)
    assert matches


def test_record_mark_read_and_replay(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P001", participant_uuid="u-1") as rec:
            assert rec.directory == out
            time.sleep(1.0)
            rec.mark("x")
            time.sleep(1.0)
        h.impedance_check(True)
        h.impedance_check(False)
        time.sleep(0.2)
    assert device.written() == bytes([0x14, 0x11, 0x12])

    recorded = oc.read_recording(out)
    n = recorded.data.shape[0]
    assert n >= 900
    assert recorded.data.shape == (n, 20)
    assert recorded.acc.shape == (n, 3)
    assert not np.isnan(recorded.data).any()
    assert np.abs(recorded.data).max() < 2.5 / recorded.config.gain
    assert [m.label for m in recorded.markers] == ["x"]
    assert 300 <= recorded.markers[0].sample <= n
    assert recorded.meta["participant"] == "P001"
    assert recorded.meta["participant_uuid"] == "u-1"
    assert recorded.meta["finished"] is True
    assert recorded.meta["port"] == device.path
    assert recorded.config.labels[:3] == ["F7", "Fp1", "Fp2"]
    assert recorded.times.shape == (n,)
    # Times come from clock.jsonl: host times of the samples, one sample
    # period apart. The first read after record() was called can hold
    # samples digitised a little before the call.
    assert len(recorded.clock) >= 2
    start = recorded.meta["start_unix"]
    assert start - 0.1 <= recorded.times[0] < start + 1.0
    assert np.allclose(np.diff(recorded.times), 1 / 500, rtol=0, atol=1e-4)
    assert recorded.times[-1] < time.time()

    replay = oc.replay(out)
    assert [m.label for m in replay.markers] == ["x"]
    assert replay.config.sample_rate == 500.0
    chunks = list(replay)
    assert sum(c.samples + c.lost for c in chunks) == n
    assert np.array_equal(np.concatenate([c.data for c in chunks]), recorded.data)

    # A bare raw.bin needs the configuration.
    with pytest.raises(ValueError):
        oc.replay(out / "raw.bin")
    bare = oc.replay(out / "raw.bin", config=recorded.config)
    assert sum(c.samples for c in bare) == n


def test_recording_keeps_every_byte_without_a_reader(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        # Subscribed, never iterated: its queue fills after a few chunks.
        unread = h.stream(queue=4)
        with h.record(out, participant="P002"):
            time.sleep(2.0)
        del unread
    raw = (out / "raw.bin").read_bytes()
    played = device.played_after_config()
    # A contiguous run of what the device sent: no byte dropped or added.
    assert raw in played
    assert len(raw) >= 0.8 * 2 * REAL_RATE
    recorded = oc.read_recording(out)
    assert not np.isnan(recorded.data).any()
    assert recorded.data.shape[0] >= 0.8 * 2 * 500


def test_lost_samples_are_counted(lossy_device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(lossy_device.path) as h:
        with h.record(out, participant="P003"):
            chunks = take(h.stream(queue=4096), 400)
    lost = [c for c in chunks if c.lost]
    assert lost, "the device skips a counter value every 100 packets"
    assert all(c.lost == 1 for c in lost)
    for a, b in zip(chunks, chunks[1:]):
        assert b.index == a.index + a.samples + b.lost
    recorded = oc.read_recording(out)
    nan_rows = np.isnan(recorded.data).all(axis=1)
    assert nan_rows.sum() >= 1
    assert np.isnan(recorded.data).any(axis=1).sum() == nan_rows.sum()
    assert np.isnan(recorded.acc[nan_rows]).all()


def test_min_samples_coalesces_chunks(lossy_device):
    with oc.connect(lossy_device.path) as h:
        chunks = take(h.stream(min_samples=50, queue=4096), 12)
    for a, b in zip(chunks, chunks[1:]):
        assert b.index == a.index + a.samples + b.lost
    # A chunk is shorter than 50 samples only where a lost sample ends it.
    for a, b in zip(chunks, chunks[1:]):
        if a.samples < 50:
            assert b.lost > 0
    assert any(c.samples >= 50 for c in chunks)
    assert any(c.lost for c in chunks)
    for c in chunks:
        assert c.data.shape == (c.samples, 20)
        assert c.triggers.shape == (c.samples,)


def test_hang_up_raises_disconnected(device):
    with oc.connect(device.path) as h:
        stream = h.stream()
        take(stream, 2)
        device.hang_up()
        with pytest.raises(oc.Disconnected):
            for _ in stream:
                pass
        with pytest.raises(oc.Disconnected):
            h.impedance_check(True)


def test_recording_through_a_hang_up(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P005"):
            time.sleep(0.5)
            device.hang_up()
            time.sleep(0.5)
    raw = (out / "raw.bin").read_bytes()
    assert raw and raw in device.played_after_config()
    meta = json.loads((out / "meta.json").read_text())
    assert meta["finished"] is True
    assert b"disconnected: " in (out / "recording.bdf").read_bytes()
    assert oc.read_recording(out).data.shape[0] >= 100


def test_stream_timeout(device):
    with oc.connect(device.path) as h:
        stream = h.stream(timeout=0.5)
        take(stream, 1)
        device.pause()
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            for _ in stream:
                pass
        assert time.monotonic() - started < 10.0


def test_ctrl_c_interrupts_a_waiting_stream(device):
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    with oc.connect(device.path) as h:
        stream = h.stream()
        take(stream, 1)
        device.pause()
        time.sleep(0.3)
        old = signal.signal(signal.SIGALRM, interrupt)
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.3)
            started = time.monotonic()
            with pytest.raises(KeyboardInterrupt):
                for _ in stream:
                    pass
            assert time.monotonic() - started < 5.0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)


def test_closing_ends_the_stream_and_the_reader_thread(device):
    process = psutil.Process()
    baseline = process.num_threads()
    h = oc.connect(device.path)
    assert process.num_threads() > baseline
    stream = h.stream()
    take(stream, 3)
    h.close()
    # Chunks already queued are still delivered, then the stream ends.
    started = time.monotonic()
    rest = list(stream)
    assert time.monotonic() - started < 5.0
    for a, b in zip(rest, rest[1:]):
        assert b.index == a.index + a.samples + b.lost
    deadline = time.monotonic() + 5.0
    while process.num_threads() > baseline and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process.num_threads() == baseline
    with pytest.raises(ValueError):
        h.stream()
    h.close()  # closing twice is allowed


def test_process_exits_after_breaking_out_of_stream(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(HERE)!r})
            import open_cognionics as oc
            from pty_device import PtyDevice, config_reply, onhead_stream

            device = PtyDevice(onhead_stream(), config_reply())
            h = oc.connect(device.path)
            for n, chunk in enumerate(h.stream()):
                if n == 5:
                    break
            h.close()
            device.hang_up()
            print("closed", flush=True)
            """
        )
    )
    child = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    try:
        line = child.stdout.readline()
        assert line.strip() == "closed"
        closed = time.monotonic()
        child.wait(timeout=30)
        assert time.monotonic() - closed < 2.0
        assert child.returncode == 0
    finally:
        child.kill()


def test_errors_share_a_base_class():
    for name in [
        "NoHeadset",
        "SeveralHeadsets",
        "OpenError",
        "NoData",
        "NoConfiguration",
        "ConfigError",
        "Disconnected",
    ]:
        error = getattr(oc, name)
        assert issubclass(error, oc.Error), name
    assert issubclass(oc.Error, Exception)


def test_connect_to_a_missing_port():
    with pytest.raises(oc.OpenError):
        oc.connect("/dev/does-not-exist")


def test_select_headset():
    with pytest.raises(oc.NoHeadset):
        oc.select_headset([])
    a = oc.PortInfo("/dev/cu.usbserial-A", "Cognionics Quick-20", "A1")
    b = oc.PortInfo("/dev/cu.usbserial-B", "Cognionics Quick-20", None)
    assert oc.select_headset([a]).path == a.path
    with pytest.raises(oc.SeveralHeadsets) as info:
        oc.select_headset([a, b])
    assert a.path in str(info.value)
    assert b.path in str(info.value)


def test_find_headsets_lists_headset_ports_once_and_opens_nothing():
    # Lists the machine's real serial ports; opens none of them. How many
    # headsets there are depends on the machine.
    process = psutil.Process()
    fds = process.num_fds()
    found = oc.find_headsets()
    assert process.num_fds() == fds
    paths = [port.path for port in found]
    assert len(paths) == len(set(paths))
    for port in found:
        assert isinstance(port, oc.PortInfo)
        assert "Cognionics" in port.description or "Quick" in port.description
        assert port.path.startswith("/dev/")
        assert not port.path.startswith("/dev/tty."), port
        assert port.serial_number is None or isinstance(port.serial_number, str)


def test_silent_device_raises_no_data():
    device = PtyDevice.silent_device()
    try:
        started = time.monotonic()
        with pytest.raises(oc.NoData):
            oc.connect(device.path)
        elapsed = time.monotonic() - started
    finally:
        device.hang_up()
    # One request, then NoData once 1.5 s pass with no byte at all.
    assert 1.5 <= elapsed < 4.0
    assert device.written() == bytes([0x14])


def test_unanswered_requests_raise_no_configuration():
    device = PtyDevice(onhead_stream(), config_reply(), ignore_requests=4)
    try:
        with pytest.raises(oc.NoConfiguration) as info:
            oc.connect(device.path)
    finally:
        device.hang_up()
    assert "4" in str(info.value)
    assert set(device.written()) == {0x14}


def test_recorded_meta_is_the_file(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P004"):
            time.sleep(0.5)
    assert oc.read_recording(out).meta == json.loads((out / "meta.json").read_text())


def drain_until_disconnected(stream, within=10.0):
    """Iterate until the stream raises Disconnected; fails after `within` s."""
    started = time.monotonic()
    with pytest.raises(oc.Disconnected):
        for _ in stream:
            assert time.monotonic() - started < within
    return time.monotonic() - started


def test_stream_after_a_disconnect_raises_at_once(device):
    with oc.connect(device.path) as h:
        take(h.stream(), 1)
        watcher = h.stream(queue=100_000)
        device.hang_up()
        drain_until_disconnected(watcher)
        started = time.monotonic()
        with pytest.raises(oc.Disconnected):
            for _ in h.stream(timeout=30):
                pass
        assert time.monotonic() - started < 1.0


def test_record_after_a_disconnect_raises_and_creates_nothing(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        watcher = h.stream(queue=100_000)
        device.hang_up()
        drain_until_disconnected(watcher)
        with pytest.raises(oc.Disconnected):
            h.record(out, participant="P006")
    assert not out.exists()


@pytest.mark.parametrize("close_first", [False, True])
def test_dropped_link_is_reported_when_the_queue_was_full(device, close_first):
    h = oc.connect(device.path)
    try:
        # Never read until after the hang-up: its one slot holds a chunk, so
        # the Disconnected event cannot be queued.
        full = h.stream(queue=1)
        watcher = h.stream(queue=100_000)
        time.sleep(0.3)
        device.hang_up()
        drain_until_disconnected(watcher)
        if close_first:
            h.close()
        drain_until_disconnected(full, within=5.0)
    finally:
        h.close()


def test_user_close_ends_a_full_stream_normally(device):
    h = oc.connect(device.path)
    full = h.stream(queue=1)
    time.sleep(0.3)
    h.close()
    assert len(list(full)) <= 1


@pytest.mark.parametrize("timeout", [float("inf"), 1e300, 1e18])
def test_huge_timeouts_mean_no_timeout(device, timeout):
    with oc.connect(device.path) as h:
        assert len(take(h.stream(timeout=timeout), 2)) == 2


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("-inf")])
def test_invalid_timeouts_are_rejected(device, timeout):
    with oc.connect(device.path) as h:
        with pytest.raises(ValueError):
            h.stream(timeout=timeout)


def test_replay_accounts_for_every_sample(lossy_device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(lossy_device.path) as h:
        with h.record(out, participant="P007"):
            time.sleep(1.5)
    raw = out / "raw.bin"
    data = bytearray(raw.read_bytes())
    # Damage the last three packets' worth of bytes.
    for k in range(1, 3 * PACKET_LEN + 1):
        data[-k] = 0x00
    raw.write_bytes(bytes(data))
    rows = oc.read_recording(out).data.shape[0]
    replay = oc.replay(out)
    assert replay.lost_at_end == 0
    chunks = list(replay)
    assert any(c.lost for c in chunks)
    total = sum(c.samples + c.lost for c in chunks) + replay.lost_at_end
    assert total == rows


def test_stub_covers_the_public_names():
    import ast

    package = Path(oc.__file__).parent
    stub = ast.parse((package / "_core.pyi").read_text())
    init = ast.parse((package / "__init__.py").read_text())

    def defined(tree):
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(t.id for t in targets if isinstance(t, ast.Name))
        return names

    in_stub = defined(stub)
    in_init = defined(init)
    imported = {
        alias.name
        for node in init.body
        if isinstance(node, ast.ImportFrom) and node.module == "_core"
        for alias in node.names
    }
    # Everything taken from _core is described in the stub, and every public
    # name is either in the stub or defined (annotated) in __init__.py.
    assert imported <= in_stub, imported - in_stub
    for name in oc.__all__:
        assert name in in_stub or name in in_init, name
    for name in in_stub - {"__version__"}:
        assert hasattr(oc._core, name), name


SESSION_FILES = ["clock.jsonl", "markers.jsonl", "meta.json", "raw.bin", "recording.bdf"]


def read_bdf_counts(path: Path):
    """Exact 24-bit values of every data signal, (samples, signals), and the
    (onset, text) annotations of a BDF+ file, read here byte by byte."""
    raw = path.read_bytes()
    header_len = int(raw[184:192])
    records = int(raw[236:244])
    signals = int(raw[252:256])
    base = 256 + signals * 216
    spr = [int(raw[base + 8 * i : base + 8 * i + 8]) for i in range(signals)]
    record_len = sum(spr) * 3
    assert len(raw) == header_len + records * record_len
    body = np.frombuffer(raw[header_len:], dtype=np.uint8).reshape(records, record_len)
    columns = []
    for s in range(signals - 1):
        at = sum(spr[:s]) * 3
        b = body[:, at : at + spr[s] * 3].reshape(records, spr[s], 3).astype(np.int64)
        v = b[..., 0] | (b[..., 1] << 8) | (b[..., 2] << 16)
        columns.append(np.where(v >= 1 << 23, v - (1 << 24), v).reshape(-1))
    annotations = []
    at = sum(spr[:-1]) * 3
    for r in range(records):
        for tal in bytes(body[r, at:]).split(b"\x00"):
            if not tal:
                continue
            parts = tal.split(b"\x14")
            onset = float(parts[0].split(b"\x15")[0])
            annotations.extend((onset, t.decode()) for t in parts[1:-1] if t)
    return np.stack(columns, axis=1), annotations


def first_whole_packet(raw: bytes) -> int:
    """Byte offset of the first whole packet in ``raw``."""
    for i in range(len(raw) - PACKET_LEN):
        if raw[i] == 0xFF and raw[i + PACKET_LEN] == 0xFF:
            return i
    raise AssertionError("no whole packet")


def test_record_refuses_a_directory_that_holds_a_recording(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P010"):
            time.sleep(0.5)
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        assert sorted(before) == SESSION_FILES
        with pytest.raises(FileExistsError):
            h.record(out, participant="P011")
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_recording_started_mid_stream_counts_from_its_first_whole_packet(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        take(h.stream(), 20)
        assert h.sample_index > 0
        with h.record(out, participant="P008") as rec:
            time.sleep(1.0)
            rec.mark("m")
            time.sleep(1.0)
    recorded = oc.read_recording(out)
    n = recorded.data.shape[0]
    marker = recorded.markers[0]
    assert 0 < marker.sample < n
    vpc = recorded.config.eeg_volts_per_count
    eeg = np.rint(recorded.data / vpc).astype(np.int64)
    assert np.array_equal(eeg * vpc, recorded.data)

    # Row 0 is the first whole packet in raw.bin.
    raw = (out / "raw.bin").read_bytes()
    at = first_whole_packet(raw)
    b0, b1, b2 = raw[at + 2 : at + 5]
    value = ((b0 << 24) | (b1 << 17) | (b2 << 10)) & 0xFFFFFFFF
    assert eeg[0, 0] == (value - (1 << 32) if value >= 1 << 31 else value) >> 8

    # The BDF holds the same samples with the same indices, and the marker's
    # annotation is at its sample.
    counts, annotations = read_bdf_counts(out / "recording.bdf")
    assert np.array_equal(counts[:n, :20], eeg)
    assert np.array_equal(counts[:n, 20:], recorded.acc.astype(np.int64))
    onsets = [onset for onset, text in annotations if text == "m"]
    assert len(onsets) == 1
    assert round(onsets[0] * 500) == marker.sample
    assert np.array_equal(counts[marker.sample, :20], eeg[marker.sample])


def test_clock_lines_are_monotone_and_about_a_second_apart(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P012"):
            time.sleep(3.5)
    lines = [json.loads(line) for line in (out / "clock.jsonl").read_text().splitlines()]
    samples = [line["sample"] for line in lines]
    times = [line["unix_time"] for line in lines]
    recorded = oc.read_recording(out)
    assert samples[0] == 0
    assert samples[-1] == recorded.data.shape[0]
    assert len(lines) >= 4
    assert all(b > a for a, b in zip(samples, samples[1:]))
    assert all(b >= a for a, b in zip(times, times[1:]))
    # 500 samples apart, so 1 s plus the change of the clock offset between
    # the lines. The offset moves up by at most MAX_DRIFT (500 ppm) per 2 s
    # window, about 1 ms here; it moves down at once when a read arrives
    # sooner than any before, by as much as reads were late earlier (up to
    # 38 ms seen with the machine loaded).
    for k in range(len(lines) - 2):
        assert samples[k + 1] - samples[k] == 500
        step = times[k + 1] - times[k]
        assert 0.9 < step <= 1.0 + 0.002, (k, step)
    assert recorded.clock == list(zip(samples, times))
    assert np.allclose(recorded.times[samples[:-1]], times[:-1], rtol=0, atol=1e-6)


def test_sample_at_a_marker_time_is_the_sample_due_then(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P009") as rec:
            time.sleep(2.0)
            rec.mark("m")
            time.sleep(1.0)
    recorded = oc.read_recording(out)
    marker = recorded.markers[0]

    # Independently of the clock model: the device plays stream packet g at
    # wall_start + g / 500. Recording sample 0 is the first whole packet of
    # raw.bin, which is a run of what was played after the configuration reply.
    raw = (out / "raw.bin").read_bytes()
    played = device.played_after_config()
    offset = played.index(raw[: 10 * PACKET_LEN])
    first = -(-(offset + first_whole_packet(raw)) // PACKET_LEN)
    g0 = device.packets_before_config + first
    due = (marker.unix_time - device.wall_start) * 500 - g0

    at = recorded.sample_at(marker.unix_time)
    assert abs(at - due) < 5, (at, due)
    # Marker.sample counts only what had been read: it is never later.
    assert marker.sample <= at + 5
    # times and sample_at are inverses.
    assert abs(recorded.sample_at(recorded.times[1000]) - 1000) < 1e-3


def test_a_recording_without_clock_jsonl_reads_with_nominal_times(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P013") as rec:
            time.sleep(1.0)
            rec.mark("m")
            time.sleep(0.5)
    # As a recording made before clock.jsonl was written.
    (out / "clock.jsonl").unlink()
    recorded = oc.read_recording(out)
    n = recorded.data.shape[0]
    assert recorded.clock == []
    start = recorded.meta["start_unix"]
    assert recorded.times[0] == start
    # Doubles near 1.8e9 resolve about 2.4e-7 s, so compare offsets from the start.
    offsets = recorded.times - start
    assert np.allclose(offsets, np.arange(n) / 500, rtol=0, atol=1e-6)
    assert recorded.sample_at(start + 1.0) == pytest.approx(500.0, abs=1e-3)
    assert [m.label for m in recorded.markers] == ["m"]
    assert sum(c.samples + c.lost for c in oc.replay(out)) == n


def test_a_clock_file_cut_short_still_reads_and_replays(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P014"):
            time.sleep(1.5)
    whole = oc.read_recording(out)
    assert whole.clock_error is None
    n = whole.data.shape[0]
    replayed = sum(c.samples + c.lost for c in oc.replay(out))
    with open(out / "clock.jsonl", "ab") as f:
        f.write(b'{"sample":99999999,"unix_ti')
    assert oc.replay(out).clock_error is not None
    recorded = oc.read_recording(out)
    assert isinstance(recorded.clock_error, str)
    assert "cut short" in recorded.clock_error
    assert recorded.clock == whole.clock
    assert recorded.times.shape == (n,)
    assert (np.diff(recorded.times) >= 0).all()
    assert sum(c.samples + c.lost for c in oc.replay(out)) == replayed


def test_a_layout_without_eeg_channels_reads(tmp_path):
    # 1 EXT + 3 ACC channels, no EEG: 3 * 4 + 6 = 18-byte packets.
    session = tmp_path / "session"
    session.mkdir()
    config = {
        "layout": {"eeg": 0, "ext": 1, "acc": 3},
        "sample_rate": 500.0,
        "gain": 3,
        "labels": [],
        "impedance_at_power_on": False,
        "compressed": False,
    }
    meta = {
        "participant": "P014",
        "participant_uuid": None,
        "start_unix": 1_800_000_000.0,
        "config": config,
        "port": "none",
        "software_version": "test",
        "finished": True,
    }
    (session / "meta.json").write_text(json.dumps(meta))
    packets = b"".join(
        bytes([0xFF, k]) + bytes([0, 2 * k, 4] * 4) + bytes([0, 100, 0, 0]) for k in range(10)
    )
    (session / "raw.bin").write_bytes(packets)
    recorded = oc.read_recording(session)
    assert recorded.data.shape == (10, 0)
    assert recorded.ext.shape == (10, 1)
    assert recorded.acc.shape == (10, 3)
    expected = [(((2 * k) << 17) | (4 << 10)) >> 8 for k in range(10)]
    assert recorded.acc[:, 0].tolist() == expected
    assert recorded.times.shape == (10,)

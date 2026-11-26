"""Read a recording written by the Rust BDF+ writer back with MNE.

Run from the repository root:

    uv run --with mne --with pytest pytest tests/python/test_bdf_readback.py -q
"""

import json
import subprocess
from pathlib import Path

import mne
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "onhead_10s.bin"
PACKET = 75
LOST_PACKET = 100  # the example removes this packet
VOLTS_PER_COUNT = 5.0 / (3 * 2**24)


EEG_NAMES = [
    "F7", "Fp1", "Fp2", "F8", "F3", "Fz", "F4", "C3", "Cz", "P8",
    "P7", "Pz", "P4", "T3", "P3", "O1", "O2", "C4", "T4", "A2",
]  # fixed by the headset (docs/channel-order-check.md), not read from our parser


def run_example(tmp_path_factory, *extra):
    out = tmp_path_factory.mktemp("rec")
    subprocess.run(
        ["cargo", "run", "--quiet", "--example", "record_fixture", "--", str(out), *extra],
        cwd=ROOT,
        check=True,
    )
    return out


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    return run_example(tmp_path_factory)


@pytest.fixture(scope="module")
def disconnected(tmp_path_factory):
    return run_example(tmp_path_factory, "disconnect")


def counts(packet_index: int, channel: int) -> int:
    """Raw count of one channel in one packet (docs/protocol.md section 2)."""
    data = FIXTURE.read_bytes()
    at = packet_index * PACKET + 2 + 3 * channel
    b0, b1, b2 = data[at : at + 3]
    value = ((b0 << 24) | (b1 << 17) | (b2 << 10)) & 0xFFFFFFFF
    if value >= 1 << 31:
        value -= 1 << 32
    return value >> 8


def test_meta_has_exact_scale(recording):
    meta = json.loads((recording / "meta.json").read_text())
    assert meta["finished"] is True
    assert meta["participant"] == "P001"
    assert meta["eeg_volts_per_count"] == VOLTS_PER_COUNT


def test_mne_reads_the_file(recording):
    raw = mne.io.read_raw_bdf(recording / "recording.bdf", preload=True, verbose="error")
    assert raw.info["sfreq"] == 500
    assert raw.ch_names[:20] == EEG_NAMES
    assert raw.ch_names[20:] == ["ACC_X", "ACC_Y", "ACC_Z"]
    assert raw.n_times == 5000
    assert raw.times[-1] + 1 / 500 == pytest.approx(10.0)

    n = LOST_PACKET  # samples 0..100 are packets 0..100, before the removed one
    # The header stores physical limits in 8 characters, so the offset and gain
    # are rounded; each sample agrees within 1 uV and differences within 1e-9 V.
    for ch in range(20):
        data = raw.get_data(picks=[ch])[0]
        raw_counts = np.array([counts(i, ch) for i in range(n)])
        expected = raw_counts * VOLTS_PER_COUNT
        assert np.allclose(data[:n], expected, atol=1e-6, rtol=0), EEG_NAMES[ch]
        assert np.allclose(np.diff(data[:n]), np.diff(expected), atol=1e-9, rtol=0), EEG_NAMES[ch]
    assert abs(raw.get_data(picks=[0])[0][0] - counts(0, 0) * VOLTS_PER_COUNT) < 1e-6

    # Accelerometer channels are raw counts, exactly (no scaling: dimension "count").
    for k in range(3):
        acc = raw.get_data(picks=[20 + k])[0][:n]
        expected = np.array([counts(i, 20 + k) for i in range(n)])
        assert np.array_equal(np.round(acc).astype(int), expected), raw.ch_names[20 + k]
        assert np.allclose(acc, expected, atol=1e-6, rtol=0)
    data = raw.get_data(picks=[0])[0]

    # The lost sample repeats the one before it.
    assert data[LOST_PACKET] == data[LOST_PACKET - 1]


def test_annotations(recording):
    raw = mne.io.read_raw_bdf(recording / "recording.bdf", verbose="error")
    found = {(a["description"], round(a["onset"], 6)) for a in raw.annotations}
    assert ("first", 1.0) in found
    assert ("second", 5.0) in found
    assert ("lost 1", LOST_PACKET / 500) in found
    assert len(raw.annotations) == 3


def test_recording_that_ends_in_a_disconnect(disconnected):
    meta = json.loads((disconnected / "meta.json").read_text())
    assert meta["finished"] is True
    raw = mne.io.read_raw_bdf(disconnected / "recording.bdf", preload=True, verbose="error")
    # 1200 samples, padded to three whole one-second records.
    assert raw.n_times == 1500
    assert raw.ch_names[:20] == EEG_NAMES
    found = {(a["description"], round(a["onset"], 6)) for a in raw.annotations}
    assert ("disconnected: link lost", 2.4) in found
    assert ("padding", 2.4) in found
    assert ("lost 1", LOST_PACKET / 500) in found
    assert ("first", 1.0) in found
    # "second" (5.0 s) lies beyond the end of the data, so MNE does not list it,
    # but it was not dropped: it is in the file after the data.
    assert b"+5\x14second\x14" in (disconnected / "recording.bdf").read_bytes()

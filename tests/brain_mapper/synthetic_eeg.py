"""Session folders with synthetic EEG of known content, for the tests of ``brain_mapper.analysis``.

The EEG is written as the headset's own packets (``docs/protocol.md`` section 2: 75 bytes, a
0xFF header, the packet counter, 20 EEG channels of 3 bytes, then the rest copied from a packet
of the recorded fixture), so the analysis reads it with ``open_cognionics.read_recording`` exactly
as it reads a real recording. Nothing here is product code.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FS = 500
LABELS = ["F7", "Fp1", "Fp2", "F8", "F3", "Fz", "F4", "C3", "Cz", "P8", "P7", "Pz", "P4", "T3", "P3", "O1", "O2", "C4", "T4", "A2"]
VOLTS_PER_COUNT = 5.0 / (3 * 2**24)
#: Added to every count so the first byte of a sample is never 0xFF (the header byte).
OFFSET = 200_000


def ch(name: str) -> int:
    return LABELS.index(name)


def narrowband(rng: np.random.Generator, n: int, centre: float, width: float = 2.0) -> np.ndarray:
    """Gaussian noise band-limited to ``centre +- width/2`` Hz (random phase), unit variance."""
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / FS)
    spectrum[np.abs(freqs - centre) > width / 2] = 0
    x = np.fft.irfft(spectrum, n)
    return x / x.std()


def lagged(x: np.ndarray, centre: float, degrees: float = 90.0) -> np.ndarray:
    """``x`` delayed by ``degrees`` of a cycle at ``centre`` Hz."""
    shift = int(round(degrees / 360.0 / centre * FS))
    return np.roll(x, shift)


def encode(data_uv: np.ndarray, drop: np.ndarray | None = None) -> bytes:
    """Packets for ``data_uv`` (samples by 20 channels, microvolts); the samples at ``drop`` are
    left out, so the headset's packet counter skips them (lost samples)."""
    template = np.frombuffer((FIXTURES / "onhead_10s.bin").read_bytes()[:75], dtype=np.uint8)
    counts = np.round(data_uv * 1e-6 / VOLTS_PER_COUNT).astype(np.int64) + OFFSET
    v = (counts << 8) & 0xFFFFFFFF
    packets = np.tile(template, (len(counts), 1))
    packets[:, 1] = np.arange(len(counts)) % 128
    row = np.stack([(v >> 24) & 0xFF, (v >> 17) & 0x7F, (v >> 10) & 0x7F], axis=2).astype(np.uint8)
    packets[:, 2 : 2 + 60] = row.reshape(len(counts), 60)
    if drop is not None:
        packets = np.delete(packets, drop, axis=0)
    return packets.tobytes()


def write_session(
    root: Path, conditions: list[tuple[str, str, np.ndarray]], name: str = "SYN01_20261125_000000", drop: np.ndarray | None = None
) -> Path:
    """A session folder holding ``conditions`` (``(step, block, data_uv)``) one after the other,
    with 2 s of quiet between them; ``session.json`` has the timeline the analysis reads. The
    samples at ``drop`` (indices into the whole recording) are lost."""
    folder = root / name
    (folder / "eeg").mkdir(parents=True)
    rng = np.random.default_rng(7)
    gap = rng.standard_normal((2 * FS, 20)) * 5
    pieces, timeline, flow, cursor = [gap], [], [], len(gap)
    for step, block, data in conditions:
        timeline.append(
            {"step": step, "block": block, "start_sample": cursor, "end_sample": cursor + len(data), "outcome": "finished"}
        )
        if step not in [f["id"] for f in flow]:
            flow.append({"id": step, "kind": "task", "title": step.replace("_", " ").capitalize(), "blocks": []})
        pieces += [data, gap]
        cursor += len(data) + len(gap)
    data = np.concatenate(pieces)
    (folder / "eeg" / "raw.bin").write_bytes(encode(data, drop))
    meta = {
        "participant": "SYN01",
        "participant_uuid": None,
        "start_unix": 1795600000.0,
        "config": {
            "layout": {"eeg": 20, "ext": 0, "acc": 3},
            "sample_rate": 500.0,
            "gain": 3,
            "labels": LABELS,
            "impedance_at_power_on": True,
            "compressed": False,
        },
        "port": "synthetic",
        "software_version": "0.0.1",
        "eeg_volts_per_count": VOLTS_PER_COUNT,
        "finished": True,
    }
    (folder / "eeg" / "meta.json").write_text(json.dumps(meta))
    (folder / "eeg" / "markers.jsonl").write_text("")
    (folder / "events.jsonl").write_text("")
    session = {
        "session": folder.name,
        "label": "SYN01",
        "uuid": "00000000-0000-4000-8000-000000000000",
        "started_unix": 1795600000.0,
        "flow": flow,
        "timeline": timeline,
    }
    (folder / "session.json").write_text(json.dumps(session))
    return folder


def noise(rng: np.random.Generator, seconds: float, sd: float = 10.0) -> np.ndarray:
    """Independent noise on each channel with a 1/f power spectrum (0.5 to 250 Hz), ``sd`` uV."""
    n = int(seconds * FS)
    spectrum = np.fft.rfft(rng.standard_normal((n, 20)), axis=0)
    freqs = np.fft.rfftfreq(n, 1 / FS)
    spectrum *= np.where(freqs >= 0.5, 1 / np.sqrt(np.maximum(freqs, 0.5)), 0.0)[:, None]
    x = np.fft.irfft(spectrum, n, axis=0)
    return x / x.std(axis=0) * sd

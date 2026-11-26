# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.26", "scipy>=1.11"]
# ///
"""Summarize a guided recording made by the web session server.

    uv run scripts/analyze_guided.py captures/session-<unix>

Reads `raw.bin` and `markers.jsonl` and prints, per stream channel: signal
size with eyes open, alpha (8-13 Hz) with eyes closed against eyes open, and
the size of the deflection that follows each blink tick.
"""
import json
import pathlib
import sys

import numpy as np
from scipy import signal

FS = 500.0
N_CH, N_EEG = 23, 20
PACKET = 3 * N_CH + 6
UV_PER_COUNT = 5.0 / (3 * 16777216.0) * 1e6
LABELS = "F7 Fp1 Fp2 F8 F3 Fz F4 C3 Cz P8 P7 Pz P4 T3 P3 O1 O2 C4 T4 A2".split()


def decode(raw: bytes):
    """Return (samples x channels in microvolts with NaN rows for lost packets, lost count)."""
    data = np.frombuffer(raw, dtype=np.uint8)
    rows, lost, last, pos = [], 0, None, 0
    while pos + PACKET < len(data):
        if data[pos] != 0xFF or data[pos + PACKET] != 0xFF:
            pos += 1
            continue
        counter = int(data[pos + 1])
        if last is not None:
            gap = (counter - last - 1) % 128
            lost += gap
            rows.extend([np.full(N_EEG, np.nan)] * gap)
        last = counter
        b = data[pos + 2 : pos + 2 + 3 * N_EEG].astype(np.int64).reshape(N_EEG, 3)
        top = np.where(b[:, 0] >= 128, b[:, 0] - 256, b[:, 0])
        rows.append(((top << 16) | (b[:, 1] << 9) | (b[:, 2] << 2)) * UV_PER_COUNT)
        pos += PACKET
    return np.array(rows), lost


def segment(x, marks, name):
    return x[marks[f"{name}_start"][0] : marks[f"{name}_end"][0]]


def band_power(x, lo, hi):
    f, p = signal.welch(x, FS, nperseg=1024, axis=0)
    keep = (f >= lo) & (f < hi)
    return p[keep].sum(axis=0) * (f[1] - f[0]), f[keep][p[keep].mean(axis=1).argmax()]


def main() -> int:
    session = pathlib.Path(sys.argv[1])
    x, lost = decode((session / "raw.bin").read_bytes())
    marks: dict[str, list[int]] = {}
    for line in (session / "markers.jsonl").read_text().splitlines():
        m = json.loads(line)
        marks.setdefault(m["label"], []).append(m["sample"])
    print(f"samples {len(x)} ({len(x) / FS:.1f} s), lost {lost}")
    for name in ("settle", "eyes_open", "eyes_closed", "blinks"):
        a, b = marks[f"{name}_start"][0], marks[f"{name}_end"][0]
        print(f"  {name}: samples {a}-{b} ({(b - a) / FS:.1f} s)")
    if np.isnan(x).any():
        x = np.where(np.isnan(x), np.nanmedian(x, axis=0), x)

    sos = signal.butter(4, [1, 45], btype="band", fs=FS, output="sos")
    wide = signal.sosfiltfilt(sos, x, axis=0)
    eo, ec = segment(wide, marks, "eyes_open"), segment(wide, marks, "eyes_closed")
    alpha_eo, _ = band_power(eo, 8, 13)
    alpha_ec, peak = band_power(ec, 8, 13)
    mains, _ = band_power(segment(x - x.mean(axis=0), marks, "eyes_open"), 58, 62)

    slow = signal.sosfiltfilt(signal.butter(2, [0.5, 8], btype="band", fs=FS, output="sos"), x, axis=0)
    ticks = [t for t in marks.get("blink_tick", []) if t + int(1.5 * FS) < len(x)]
    epochs = np.array([slow[t - 100 : t + int(1.5 * FS)] for t in ticks])
    epochs -= epochs[:, :100].mean(axis=1, keepdims=True)
    evoked = epochs.mean(axis=0)
    blink = evoked.max(axis=0) - evoked.min(axis=0)
    sign = np.sign(evoked[np.abs(evoked).argmax(axis=0), np.arange(N_EEG)])

    print(f"\nblink ticks used: {len(ticks)}; eyes-closed alpha peak near {peak:.1f} Hz (all channels pooled)")
    print("ch label  rms_open_uV  alpha_open  alpha_closed  closed/open  mains60_uV  blink_p2p_uV  blink_sign")
    for c in range(N_EEG):
        print(
            f"{c:2d} {LABELS[c]:4s} {eo[:, c].std():11.1f} {np.sqrt(alpha_eo[c]):11.2f} {np.sqrt(alpha_ec[c]):13.2f}"
            f" {alpha_ec[c] / alpha_eo[c]:12.2f} {np.sqrt(mains[c]):11.1f} {blink[c]:13.1f} {sign[c]:+11.0f}"
        )
    order = np.argsort(-blink)
    print("\nlargest blink deflection:", ", ".join(f"{LABELS[c]} (ch {c}) {blink[c]:.0f}" for c in order[:6]))
    order = np.argsort(-(alpha_ec / alpha_eo))
    print("largest alpha increase:  ", ", ".join(f"{LABELS[c]} (ch {c}) x{alpha_ec[c] / alpha_eo[c]:.1f}" for c in order[:6]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

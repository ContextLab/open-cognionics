# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.26", "scipy>=1.11"]
# ///
"""Which channels carry blinks in the last N seconds of a session recording?

    uv run scripts/analyze_recent_blinks.py captures/session-<unix> [seconds]

Finds blink-like events as peaks in the first principal component of the
0.5-8 Hz signal over well-behaved channels, then prints the average
deflection of every channel around those events.
"""
import pathlib
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from analyze_guided import FS, LABELS, decode  # noqa: E402


def main() -> int:
    session = pathlib.Path(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    raw = (session / "raw.bin").read_bytes()
    x, lost = decode(raw[-int((seconds + 2) * FS) * 75 :])
    x = np.nan_to_num(x)[int(FS) :]
    print(f"samples {len(x)} ({len(x) / FS:.1f} s), lost {lost}")

    slow = signal.sosfiltfilt(signal.butter(2, [0.5, 8], btype="band", fs=FS, output="sos"), x, axis=0)
    wide = signal.sosfiltfilt(signal.butter(4, [1, 45], btype="band", fs=FS, output="sos"), x, axis=0)
    f, p = signal.welch(x - x.mean(0), FS, nperseg=1024, axis=0)
    mains = np.sqrt(p[(f >= 58) & (f < 62)].sum(0) * (f[1] - f[0]))
    spread = np.percentile(slow, 99, axis=0) - np.percentile(slow, 1, axis=0)
    rms = np.median(np.abs(wide), axis=0) * 1.4826

    usable = spread <= np.sort(spread)[len(spread) // 2]  # the quieter half
    print("slow spread (uV):", " ".join(f"{LABELS[c]}:{v:.0f}" for c, v in enumerate(spread)))
    u, s, vt = np.linalg.svd(slow[:, usable] - slow[:, usable].mean(0), full_matrices=False)
    pc = u[:, 0] * s[0]
    if abs(pc.min()) > abs(pc.max()):
        pc = -pc
    peaks, _ = signal.find_peaks(pc, height=0.4 * np.percentile(pc, 99.5), distance=int(0.4 * FS))
    peaks = peaks[(peaks > 150) & (peaks < len(x) - 250)]
    print(f"blink-like events found: {len(peaks)} at s:", " ".join(f"{t / FS:.1f}" for t in peaks))
    if len(peaks) == 0:
        return 1
    epochs = np.array([slow[t - 150 : t + 250] for t in peaks])
    epochs -= epochs[:, :50].mean(axis=1, keepdims=True)
    evoked = epochs.mean(0)
    p2p = evoked.max(0) - evoked.min(0)
    sign = np.sign(evoked[np.abs(evoked).argmax(0), np.arange(len(LABELS))])

    print("label  blink_uV sign  slow_spread_uV  robust_rms_uV  mains60_uV")
    for c in np.argsort(-p2p):
        print(f"{LABELS[c]:5s} {p2p[c]:9.0f} {sign[c]:+4.0f} {spread[c]:15.0f} {rms[c]:14.1f} {mains[c]:11.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

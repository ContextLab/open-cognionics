# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy>=1.26", "scipy>=1.11"]
# ///
"""Analyse a channel-order check recorded from the /order page.

    uv run scripts/analyze_order_check.py captures/session-<unix>

For every prompted electrode, prints the three channels whose movement signal
grew most, and the three whose 125 Hz contact tone changed most, between the
"get ready" window and the pressing window. Electrodes without all three
prompt markers (a check that was stopped early) are listed as not prompted.

Channel labels come from the session's meta.json (the built-in Quick-20 list
when there is none). Marker samples count from the first whole packet in
raw.bin, as `decode` does; sessions written by the server before the fix of
2026-11-24 (final review C1) hold marker samples counted from another point
and do not line up.
"""
import json
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from analyze_guided import LABELS, decode  # noqa: E402


def session_labels(session: pathlib.Path) -> list[str]:
    """EEG labels in stream order, from meta.json if there is one."""
    meta = session / "meta.json"
    if meta.exists():
        return list(json.loads(meta.read_text())["config"]["labels"])
    print("no meta.json: using the built-in Quick-20 labels")
    return list(LABELS)


def main() -> int:
    session = pathlib.Path(sys.argv[1])
    labels = session_labels(session)
    x, lost = decode((session / "raw.bin").read_bytes())
    x = np.nan_to_num(x)
    marks = {}
    for line in (session / "markers.jsonl").read_text().splitlines():
        m = json.loads(line)
        marks[m["label"].split(" ")[0]] = m["sample"]
    print("samples", len(x), "lost", lost)

    # Movement: 4-sample mean (cancels the 125 Hz tone), then a 4-sample difference.
    kernel = np.ones(4) / 4
    smooth = np.apply_along_axis(lambda v: np.convolve(v, kernel, "same"), 0, x)
    diff = smooth[4:] - smooth[:-4]
    phase = np.exp(-1j * np.pi * np.arange(len(x)) / 2)

    def tone(a, b):
        return 2 * np.abs((x[a:b] * phase[a:b, None]).mean(0))

    def move(a, b):
        return np.sqrt(np.mean(diff[a:b] ** 2, 0))

    prompted = []
    for label in labels:
        names = [f"order_{label}_{k}" for k in ("ready", "start", "end")]
        if not all(name in marks for name in names):
            print(f"{label:4s} not prompted")
            continue
        prompted.append(label)
        r, t0, t1 = (marks[name] for name in names)
        rm = move(t0 + 250, t1) / (move(r + 250, t0) + 0.05)
        rt = tone(t0 + 250, t1) / (tone(r + 250, t0) + 0.05)
        top_m = np.argsort(-rm)[:3]
        top_t = np.argsort(-np.abs(np.log(rt)))[:3]
        print(
            f"{label:4s} move: "
            + ", ".join(f"{labels[c]} x{rm[c]:.1f}" for c in top_m)
            + "   | tone: "
            + ", ".join(f"{labels[c]} x{rt[c]:.2f}" for c in top_t)
        )

    if prompted:
        r = marks[f"order_{prompted[0]}_ready"]
        print("\ntone amplitude at rest (uV):", " ".join(f"{labels[c]}:{v:.0f}" for c, v in enumerate(tone(r, r + 1000))))
        print("movement at rest (uV):      ", " ".join(f"{labels[c]}:{v:.1f}" for c, v in enumerate(move(r, r + 1000))))
    if "order_check_start" in marks and "order_check_end" in marks:
        a, b = marks["order_check_start"], marks["order_check_end"]
        span = np.abs(x[a:b] - np.median(x[a:b], 0)).max(0)
        print("largest excursion (uV):     ", " ".join(f"{labels[c]}:{v:.0f}" for c, v in enumerate(span)))
    else:
        print("no order_check_start / order_check_end pair: the check was stopped early")
    return 0


if __name__ == "__main__":
    sys.exit(main())

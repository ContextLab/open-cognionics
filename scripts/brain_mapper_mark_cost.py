"""Time ``LiveSource.mark`` through a real recording of 2000 markers from the pty device.

Run from the repository root: ``uv run python scripts/brain_mapper_mark_cost.py``. Prints the
milliseconds one mark took at markers 10, 500 and 2000 (the median of the 9 marks around each),
and checks that every marker's returned sample is the one in ``markers.jsonl``. It opens only the
pseudo-terminal device of ``tests/python/pty_device.py`` and writes into a temporary folder.
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "python"))

from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

from brain_mapper.source import LiveSource  # noqa: E402

COUNT = 2000


def main() -> None:
    dev = PtyDevice(onhead_stream(), config_reply())
    source = LiveSource(dev.path)
    try:
        source.open()
        source.subscribe(4096)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "eeg"
            source.start_recording(rec, "MARKCOST", "uuid-mark-cost")
            costs: list[float] = []
            samples: list[int] = []
            for i in range(COUNT):
                start = time.perf_counter()
                samples.append(source.mark(f"test/mark#{i + 1}"))
                costs.append((time.perf_counter() - start) * 1000)
            source.stop_recording()
            written = [json.loads(x) for x in (rec / "markers.jsonl").read_text().splitlines()]
            assert [m["sample"] for m in written] == samples, "returned samples differ from the file"
            assert [m["label"] for m in written] == [f"test/mark#{i + 1}" for i in range(COUNT)]
            for at in (10, 500, 2000):
                window = costs[max(0, at - 5) : at + 4]
                print(f"mark {at}: {statistics.median(window):.3f} ms")
    finally:
        source.stop()
        dev.hang_up()


if __name__ == "__main__":
    main()

"""Runs in a subprocess of test_progress.py: a skip whose next step cannot be entered.

The file size limit (RLIMIT_FSIZE) is set here, on this process only, never in the pytest
process: it applies to every file the process writes. Usage:

    python fsize_driver.py <pty path> <folder>

It makes a session on the pty device (streaming slowly, so there is a recording to damage and
it does not outgrow the log), pads ``events.jsonl`` until it is far larger than every other file
of the session, so that the limit can sit just above it and only the event log is refused, skips
the round that is waiting with a limit that lets the "skipped" line in and refuses the line after
it, and prints one JSON line: what the session said afterwards, the limit, and the lengths of the
two lines (so that a test can show that ``events.jsonl`` is the file that reached the limit).
"""

from __future__ import annotations

import json
import resource
import signal
import sys
import time
from pathlib import Path

from brain_mapper import flow
from brain_mapper.engine import Session
from brain_mapper.points import Ledger
from brain_mapper.source import LiveSource

SHORT = Path(__file__).parent / "flows" / "short.json"


def make(path: str, folder: Path):
    source = LiveSource(path)
    source.open()
    source.subscribe(4096)
    source.start_recording(folder / "eeg", "P001", "uuid-1")
    ledger = Ledger(folder / "points.jsonl")
    session = Session(
        flow.load(SHORT),
        source,
        ledger,
        folder=folder,
        meta={"session": folder.name, "uuid": "uuid-1", "seed": 7, "continues": None},
    )
    return source, ledger, session


def to_words_waiting(session) -> None:
    while session.state()["step"] != "words":
        session.advance()


def tree_sizes(folder: Path) -> dict[str, int]:
    return {str(p.relative_to(folder)): p.stat().st_size for p in folder.rglob("*") if p.is_file()}


def main() -> None:
    path, base = sys.argv[1], Path(sys.argv[2])
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # the default would end this process
    # a control run without the limit gives the length of the two lines that follow a skip
    source, ledger, session = make(path, base / "control")
    to_words_waiting(session)
    session.skip()
    lines = (base / "control" / "events.jsonl").read_text().splitlines()
    skipped_len, next_len = len(lines[-2]) + 1, len(lines[-1]) + 1
    session.close()
    ledger.close()
    source.stop()

    folder = base / "limited"
    source, ledger, session = make(path, folder)
    to_words_waiting(session)
    time.sleep(1.0)  # about a second of the stream is recorded
    # Long events make the log the largest file by far: the BDF file grows by about 4 kB for each
    # marker whatever the event holds, so closing the recording writes nothing the limit refuses.
    for i in range(100):
        session.event("padding", {"i": i, "text": "x" * 16000}, 1.0 + i, 1.0)
    before = tree_sizes(folder)
    old = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = before["events.jsonl"] + skipped_len + next_len // 2
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, old[1]))
    try:
        session.skip()
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, old)
    state = session.state()
    session.close()
    ledger.close()
    source.stop()
    print(
        json.dumps(
            {
                "failure": state["failure"],
                "phase": state["phase"],
                "ended_early": state["ended_early"],
                "step": state["step"],
                "outcomes": state["outcomes"],
                "limit": limit,
                "skipped_len": skipped_len,
                "next_len": next_len,
                "before": before,
                "after": tree_sizes(folder),
                "time": time.time(),
            }
        )
    )


if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""End-to-end check of capture_raw.py through a real pseudo-terminal.

    uv run scripts/test_capture_raw.py

A writer thread sends legacy-format packets (20 EEG + 4 EXT channels) into one
end of an OS pseudo-terminal while capture_raw reads the other end through
pyserial. This exercises the real read / write-to-disk / summarize path; it
does not involve a headset.
"""
import os
import pathlib
import sys
import tempfile
import threading
import time
import tty

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import capture_raw  # noqa: E402

CHANNELS = 24
PACKETS = 400


def packet(counter: int) -> bytes:
    body = bytes([0x01, counter % 128, 0x02]) * CHANNELS
    return bytes([0xFF, counter % 128]) + body + bytes([0x10, 100, 0, 0])


def main() -> None:
    master, slave = os.openpty()
    tty.setraw(slave)
    port = os.ttyname(slave)
    sent = b"".join(packet(c) for c in range(PACKETS))

    def writer() -> None:
        time.sleep(0.3)
        for start in range(0, len(sent), 390):
            os.write(master, sent[start : start + 390])
            time.sleep(0.005)

    thread = threading.Thread(target=writer)
    thread.start()
    with tempfile.TemporaryDirectory() as tmp:
        path = capture_raw.capture(port, 2.0, 115200, pathlib.Path(tmp), "pty")
        thread.join()
        got = path.read_bytes()
        rows = (pathlib.Path(tmp) / "pty.chunks.csv").read_text().strip().splitlines()
    assert got == sent, f"captured {len(got)} bytes, sent {len(sent)}"
    assert rows[0] == "unix_time,monotonic,offset,nbytes"
    assert sum(int(r.split(",")[3]) for r in rows[1:]) == len(sent)

    summary = capture_raw.summarize(got)
    assert summary["packet_len"] == 3 * CHANNELS + 6, summary
    assert summary["channels"] == CHANNELS, summary
    assert summary["triple_sync_runs"] == 0, summary
    assert summary["counter_steps"][0] == (1, PACKETS - 2), summary
    print("ok:", summary)


if __name__ == "__main__":
    main()

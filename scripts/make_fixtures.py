# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Cut the test fixtures in tests/fixtures/ out of the recordings in captures/.

    uv run scripts/make_fixtures.py

Every cut is made on a packet boundary (packet length 75, sync byte 0xFF, and
the next packets' sync bytes and counters present). Bytes are copied exactly as
recorded; nothing is repaired.
"""
import json
import pathlib
import struct
import sys

PACKET = 75
SYNC = 0xFF
CONFIG_START = b"\xff\xff\xff\xff"
CONFIG_END = 1234567890
CHECK_PACKETS = 8

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "captures"
FIXTURES = ROOT / "tests" / "fixtures"


def is_boundary(data: bytes, pos: int) -> bool:
    """True if `pos` starts a run of CHECK_PACKETS packets with sync and counting counters."""
    if pos + PACKET * CHECK_PACKETS > len(data):
        return False
    for k in range(CHECK_PACKETS):
        at = pos + k * PACKET
        if data[at] != SYNC:
            return False
        if k and data[at + 1] != (data[at - PACKET + 1] + 1) % 128:
            return False
    return data[pos + 1] < 128


def first_boundary(data: bytes, start: int = 0) -> int:
    for pos in range(start, len(data)):
        if is_boundary(data, pos):
            return pos
    raise ValueError("no packet boundary found")


def config_cut(data: bytes) -> bytes:
    marker = data.find(CONFIG_START)
    if marker < 0:
        raise ValueError("no configuration start marker")
    # end word, found by walking big-endian words after the marker
    end = None
    for i in range(marker + 4, len(data) - 3, 4):
        if struct.unpack(">I", data[i : i + 4])[0] == CONFIG_END:
            end = i + 4
            break
    if end is None:
        raise ValueError("no configuration end word")
    grid = first_boundary(data)
    last_before = grid + ((marker - grid) // PACKET) * PACKET
    # The recording starts only a few packets before the block, so take as many
    # whole packets as exist, up to 150.
    begin = max(last_before - 150 * PACKET, grid)
    print(f"config_reply: {(last_before - begin) // PACKET} whole packets before the block")
    resume = first_boundary(data, end)
    stop = resume + 150 * PACKET
    if stop > len(data):
        raise ValueError("fewer than 150 packets after the configuration block")
    return data[begin:stop]


def marker_cut(session: pathlib.Path, label: str, packets: int) -> bytes:
    data = (session / "raw.bin").read_bytes()
    sample = None
    for line in (session / "markers.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            if obj["label"] == label:
                sample = obj["sample"]
                break
    if sample is None:
        raise ValueError(f"marker {label} not found")
    begin = first_boundary(data) + sample * PACKET
    chunk = data[begin : begin + packets * PACKET]
    if len(chunk) != packets * PACKET:
        raise ValueError("recording too short")
    return chunk


def index_cut(path: pathlib.Path, first: int, packets: int) -> bytes:
    data = path.read_bytes()
    begin = first_boundary(data) + first * PACKET
    chunk = data[begin : begin + packets * PACKET]
    if len(chunk) != packets * PACKET:
        raise ValueError("recording too short")
    return chunk


def read_timing(path: pathlib.Path) -> bytes:
    """Per-read log without the wall-clock column: monotonic clock, offset, size."""
    lines = path.read_text().splitlines()
    rows = [line.split(",") for line in lines[1:] if line.strip()]
    text = "monotonic,offset,nbytes\n" + "".join(f"{r[1]},{r[2]},{r[3]}\n" for r in rows)
    return text.encode()


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    outputs = {
        "read_timing_10s.csv": read_timing(CAPTURES / "first-3M.chunks.csv"),
        "config_reply.bin": config_cut((CAPTURES / "config-01.bin").read_bytes()),
        "onhead_10s.bin": marker_cut(CAPTURES / "session-1795543289", "eyes_closed_start", 5000),
        "impedance_on_2s.bin": index_cut(CAPTURES / "onhead-01-impon.bin", 1000, 1000),
    }
    for name, blob in outputs.items():
        (FIXTURES / name).write_bytes(blob)
        print(f"wrote tests/fixtures/{name} ({len(blob)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

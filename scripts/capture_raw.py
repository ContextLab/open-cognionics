# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Record the raw byte stream from a Cognionics / CGX headset, without sending anything to it.

    uv run scripts/capture_raw.py --list
    uv run scripts/capture_raw.py /dev/cu.usbserial-XXXX --seconds 20

Writes `captures/<name>.bin` (the bytes exactly as received) and
`captures/<name>.chunks.csv` (host time, byte offset and size of every read),
then prints a summary of the framing it sees. The headset is only read from:
this script never writes a byte to the port.
"""
import argparse
import collections
import csv
import pathlib
import sys
import time

import serial
from serial.tools import list_ports

SYNC = 0xFF


def summarize(data: bytes) -> dict:
    """Describe the framing visible in a capture."""
    positions = [i for i, b in enumerate(data) if b == SYNC]
    gaps = collections.Counter(b - a for a, b in zip(positions, positions[1:]))
    triple = sum(1 for i in range(len(data) - 2) if data[i] == data[i + 1] == data[i + 2] == SYNC)
    common = gaps.most_common(5)
    summary = {
        "bytes": len(data),
        "sync_bytes": len(positions),
        "triple_sync_runs": triple,
        "common_sync_spacings": common,
        "packet_len": None,
        "channels": None,
        "counter_steps": None,
    }
    if not common:
        return summary
    length = common[0][0]
    summary["packet_len"] = length
    if length > 6 and (length - 6) % 3 == 0:
        summary["channels"] = (length - 6) // 3
    counters = [data[p + 1] for p in positions if p + length < len(data) and data[p + length] == SYNC]
    steps = collections.Counter((b - a) % 128 for a, b in zip(counters, counters[1:]))
    summary["counter_steps"] = steps.most_common(4)
    return summary


def capture(
    port: str, seconds: float, baud: int, out_dir: pathlib.Path, name: str, rtscts: bool = False
) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    bin_path = out_dir / f"{name}.bin"
    csv_path = out_dir / f"{name}.chunks.csv"
    offset = 0
    with serial.Serial(port, baudrate=baud, timeout=0.05, rtscts=rtscts) as link, open(bin_path, "wb") as raw, open(
        csv_path, "w", newline=""
    ) as chunks:
        log = csv.writer(chunks)
        log.writerow(["unix_time", "monotonic", "offset", "nbytes"])
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            block = link.read(link.in_waiting or 1)
            if not block:
                continue
            log.writerow([f"{time.time():.6f}", f"{time.monotonic():.6f}", offset, len(block)])
            raw.write(block)
            offset += len(block)
    return bin_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("port", nargs="?", help="serial port of the paired headset")
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--baud", type=int, default=3000000, help="FTDI dongle: 3000000 or 1000000")
    parser.add_argument("--no-rtscts", action="store_true", help="disable RTS/CTS flow control")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("captures"))
    parser.add_argument("--name", default=time.strftime("capture-%Y%m%d-%H%M%S"))
    parser.add_argument("--summarize", type=pathlib.Path, help="summarize an existing .bin and exit")
    args = parser.parse_args()

    if args.list:
        for p in list_ports.comports():
            print(f"{p.device}\t{p.description}")
        return 0
    if args.summarize:
        path = args.summarize
    elif args.port:
        path = capture(args.port, args.seconds, args.baud, args.out, args.name, not args.no_rtscts)
        print(f"wrote {path}")
    else:
        parser.error("give a port, --list or --summarize")
    for key, value in summarize(path.read_bytes()).items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

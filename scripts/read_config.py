# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial>=3.5"]
# ///
"""Ask a Cognionics / CGX headset for its configuration and save the reply.

    uv run scripts/read_config.py /dev/cu.usbserial-XXXX

Writes exactly one byte to the headset: 0x14, the "dump configuration"
command (docs/protocol.md section 5). Everything received afterwards is saved
to `captures/<name>.bin`, and the configuration words found in it are printed.
"""
import argparse
import pathlib
import struct
import sys
import time

import serial

DUMP_CONFIG = 0x14
CONFIG_START = b"\xff\xff\xff\xff"
CONFIG_END = 1234567890


def extract_words(data: bytes) -> list[int] | None:
    """Return the configuration words after the start marker, or None if incomplete."""
    start = data.find(CONFIG_START)
    if start < 0:
        return None
    body = data[start + len(CONFIG_START) :]
    words = []
    for i in range(0, len(body) - 3, 4):
        (word,) = struct.unpack(">I", body[i : i + 4])
        words.append(word)
        if word == CONFIG_END:
            return words
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("port", nargs="?")
    parser.add_argument("--baud", type=int, default=3000000)
    parser.add_argument("--seconds", type=float, default=6.0, help="how long to keep reading")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("captures"))
    parser.add_argument("--name", default=time.strftime("config-%Y%m%d-%H%M%S"))
    parser.add_argument("--parse", type=pathlib.Path, help="parse a saved reply and exit")
    args = parser.parse_args()

    if args.parse:
        data = args.parse.read_bytes()
    elif args.port:
        args.out.mkdir(parents=True, exist_ok=True)
        received = bytearray()
        with serial.Serial(args.port, baudrate=args.baud, timeout=0.05, rtscts=True) as link:
            flush_until = time.monotonic() + 0.5
            while time.monotonic() < flush_until:
                link.read(65536)
            link.write(bytes([DUMP_CONFIG]))
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                received += link.read(4096)
        data = bytes(received)
        path = args.out / f"{args.name}.bin"
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")
    else:
        parser.error("give a port or --parse")

    words = extract_words(data)
    if words is None:
        print("no complete configuration block found")
        return 1
    print(f"{len(words)} words (including the end marker)")
    for i, word in enumerate(words):
        print(f"W[{i}] = {word}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

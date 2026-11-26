"""Run a simulated headset on a pseudo-terminal until interrupted.

    uv run python scripts/run_pty_device.py [seconds-before-unplug]

Prints the device path on the first line (flushed), then plays the recorded
Quick-20 stream. With a number of seconds it hangs up after that long, as when
the dongle is unplugged; otherwise it runs until killed. Point the server at
the printed path: `open-cognionics <path>`. Never touches a real serial port.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "python"))

from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402


def main() -> None:
    device = PtyDevice(onhead_stream(), config_reply())
    print(device.path, flush=True)
    if len(sys.argv) > 1:
        time.sleep(float(sys.argv[1]))
        device.hang_up()
        print("hung up", flush=True)
        time.sleep(3600)
    else:
        time.sleep(10**6)


if __name__ == "__main__":
    main()

"""Make the session zip of an earlier version of brain-mapper, for the import tests.

    uv run python scripts/brain_mapper_import_fixture.py [out.zip]

Runs the installed ``brain-mapper`` command (whatever version is checked out) on a simulated
headset (a pseudo-terminal, ``tests/python/pty_device.py``) with the short flow
(``tests/brain_mapper/flows/short.json``), accepts the label ``P001``, plays every step through
to the end over the websocket, waits for the session zip and copies it to ``out.zip`` (default
``tests/brain_mapper/fixtures/previous-version-session.zip``). It prints the commit it was made on.

The headset stream is the recorded one of the tests with every channel sample set to zero (the
sync byte, the counter, the contact, battery and trigger bytes are kept), so the zip holds no
recorded EEG and compresses to a few kilobytes. No real serial port is opened.

The committed fixture was made on commit b13a896 (branch feat-import), before ``sample_point``,
``previous_sessions`` and ``imported_from`` existed.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "python"))

from pty_device import PACKET_LEN, PtyDevice, config_reply, onhead_stream  # noqa: E402

COMMAND = Path(sys.executable).parent / "brain-mapper"
FLOW = ROOT / "tests" / "brain_mapper" / "flows" / "short.json"
OUT = ROOT / "tests" / "brain_mapper" / "fixtures" / "previous-version-session.zip"
#: Bytes 2 .. 2 + 3 x 23 of a packet are the samples of the 20 EEG and 3 motion channels.
SAMPLES = slice(2, 2 + 3 * 23)


def zero_stream() -> bytes:
    """The test stream with every channel sample set to zero."""
    data = bytearray(onhead_stream())
    for at in range(0, len(data) - PACKET_LEN + 1, PACKET_LEN):
        data[at + SAMPLES.start : at + SAMPLES.stop] = bytes(SAMPLES.stop - SAMPLES.start)
    return bytes(data)


def post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Origin": base},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def play_through(base: str) -> None:
    host_port = base.removeprefix("http://")
    with connect(f"ws://{host_port}/ws?role=participant&page={uuid.uuid4().hex}", origin=base) as ws:
        deadline = time.monotonic() + 180
        last_rev = -1
        while True:
            assert time.monotonic() < deadline, "the session did not end in time"
            message = json.loads(ws.recv(timeout=30))
            if message.get("t") != "state" or message["rev"] <= last_rev:
                continue
            last_rev = message["rev"]
            if message["phase"] == "done":
                return
            if message["phase"] in ("waiting", "between"):
                ws.send(json.dumps({"cmd": "advance"}))


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="brain-mapper-fixture-"))
    device = PtyDevice(zero_stream(), config_reply())
    process = None
    try:
        args = [str(COMMAND), "--no-browser", "--data-dir", str(work), "--http-port", "0"]
        args += ["--port", device.path, "--flow", str(FLOW)]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            if line.startswith("brain-mapper at "):
                base = line.removeprefix("brain-mapper at ").strip()
                break
        else:
            raise SystemExit("brain-mapper did not start")
        post(base, "/api/participant", {"label": "P001"})
        play_through(base)
        (folder,) = [p for p in (work / "sessions").iterdir() if p.is_dir()]
        deadline = time.monotonic() + 60
        while True:
            meta = json.loads((folder / "session.json").read_text())
            if meta.get("zip"):
                break
            assert time.monotonic() < deadline, "no zip was made"
            time.sleep(0.2)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder.parent / meta["zip"], out)
        print(f"{out} ({out.stat().st_size} bytes), made on commit {commit}")
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
        device.hang_up()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

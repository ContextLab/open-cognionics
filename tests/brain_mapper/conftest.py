"""Fixtures for the brain-mapper tests.

Everything here is real: a pseudo-terminal device (``tests/python/pty_device.py``),
a recording made from it with ``Headset.record``, the ``brain-mapper`` command
as a subprocess, and a Chromium driven by Playwright. No fixture opens a real
serial port; the headset dongle may be plugged in.
"""

from __future__ import annotations

import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import open_cognionics as oc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402
import shots  # noqa: E402

#: Seconds of headset data in the recording every replay test uses.
RECORDING_SECONDS = 15
#: The command installed by the brain-mapper package, next to the interpreter.
COMMAND = Path(sys.executable).parent / "brain-mapper"
PRINTED = "brain-mapper at "


@pytest.fixture(scope="session", autouse=True)
def _shots_base(tmp_path_factory):
    """Screenshots go under pytest's temporary directory unless $BRAIN_MAPPER_SHOTS_DIR is set
    (``shots.py``)."""
    shots.BASE[:] = [tmp_path_factory.getbasetemp() / "shots"]


@pytest.fixture
def pty_device():
    dev = PtyDevice(onhead_stream(), config_reply())
    yield dev
    dev.hang_up()


@pytest.fixture(scope="session")
def recorded_session():
    """A real 15 s recording (a session folder) made from a pty device."""
    root = Path(tempfile.mkdtemp(prefix="brain-mapper-recording-"))
    out = root / "recording"
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        with oc.connect(dev.path) as headset:
            with headset.record(out, participant="P001"):
                time.sleep(RECORDING_SECONDS)
    finally:
        dev.hang_up()
    yield out
    shutil.rmtree(root, ignore_errors=True)


@dataclass
class App:
    url: str
    data_dir: Path
    process: subprocess.Popen
    #: The lines the process printed up to and including the ``brain-mapper at`` line.
    printed: list[str] = field(default_factory=list)

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.url.removeprefix("http://").partition(":")
        return host, int(port)


class _Lines:
    """Reads a process's stdout on a thread so the pipe never fills."""

    def __init__(self, stream) -> None:
        self.queue: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        self.thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self.thread.start()

    def _pump(self, stream) -> None:
        # The pipe is closed here, at its end, and not whenever the garbage collector gets to it:
        # a late close lands in some later test (test_checks compares the open file descriptors).
        with stream:
            for line in stream:
                self.queue.put(line)
        self.queue.put("")

    def wait_for(self, prefix: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no line starting {prefix!r}; saw {self.seen!r}")
            try:
                line = self.queue.get(timeout=left)
            except queue.Empty:
                continue
            if line == "":
                raise AssertionError(f"process ended; saw {self.seen!r}")
            self.seen.append(line.rstrip())
            if line.startswith(prefix):
                return line.rstrip()


@pytest.fixture
def app(tmp_path):
    """``app(port=..., replay=..., flow=..., http_port=0, data_dir=None)`` starts ``brain-mapper``.

    ``data_dir`` defaults to a new folder in the test's temporary folder; a test passes
    one to start a later run on the folder of an earlier run (or on a prepared folder).

    ``env`` (a full environment for the process, default: this one) lets a test start the app from
    a changed copy of the package. Raises unless ``port`` (a pseudo-terminal path) or ``replay``
    is given, so no test can start the app on the real dongle.
    """
    started: list[subprocess.Popen] = []
    readers: list[_Lines] = []

    def start(port=None, replay=None, flow=None, http_port=0, data_dir=None, env=None) -> App:
        if port is None and replay is None:
            raise ValueError("app() needs port (a pty path) or replay; never the real dongle")
        if data_dir is None:
            data_dir = tmp_path / f"data{len(started)}"
        args = [str(COMMAND), "--no-browser", "--data-dir", str(data_dir)]
        args += ["--http-port", str(http_port)]
        if port is not None:
            args += ["--port", str(port)]
        if replay is not None:
            args += ["--replay", str(replay)]
        if flow is not None:
            args += ["--flow", str(flow)]
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        started.append(process)
        lines = _Lines(process.stdout)
        readers.append(lines)
        line = lines.wait_for(PRINTED, timeout=30)
        return App(line.removeprefix(PRINTED), data_dir, process, lines.seen)

    yield start
    for process in started:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    for lines in readers:  # each process has ended: its pipe reaches its end and is closed
        lines.thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser():
    # Headless Chromium honours ``requestFullscreen`` from a real click and sets
    # ``document.fullscreenElement`` (checked 2026-11-25, Task 6 step 1), so the
    # browser tests run headless; ``exitFullscreen()`` is honoured the same way.
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()

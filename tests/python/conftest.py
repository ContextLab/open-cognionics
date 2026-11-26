"""Fixtures for the Python API tests.

No test here opens a real serial port: the headset dongle may be plugged in.
Every connection goes to a pseudo-terminal from ``pty_device.py``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402


@pytest.fixture
def device():
    dev = PtyDevice(onhead_stream(), config_reply())
    yield dev
    dev.hang_up()


@pytest.fixture
def lossy_device():
    """Loses one sample after every 100 packets played."""
    dev = PtyDevice(onhead_stream(), config_reply(), lose_every=100)
    yield dev
    dev.hang_up()

"""What the device screen is shown while the link is live: link figures and thinned-out traces.

Nothing here touches the recording. The hub's forwarder reads chunks from its own queue on the
source (as for any subscriber) and these classes only turn them into small messages.

``LinkStats`` keeps the figures of ``{"t":"status"}``: the rate (rows received per second over
the last ``RATE_WINDOW`` seconds), the samples lost since the link opened and in the last
``LOST_WINDOW`` seconds, the battery reading and whether the contact check is on.

``Decimator`` turns the rows of a chunk into ``DISPLAY_HZ`` rows a second for the traces: each
output row is the mean of ``fs / DISPLAY_HZ`` input rows (rows of lost samples are left out of the
mean), in microvolts rounded to 0.1. At 500 Hz and 20 channels that is 50 rows of 20 numbers a
second, about 7 kB a second, sent in messages of ``FLUSH_ROWS`` rows (every 0.1 s).
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

#: Rows a second the traces are sent at.
DISPLAY_HZ = 50
#: Rows (at ``DISPLAY_HZ``) held back to make one ``trace`` message: 0.1 s.
FLUSH_ROWS = 5
#: Seconds the rate is measured over.
RATE_WINDOW = 2.0
#: Seconds a lost sample counts for the "samples are being lost" tip.
LOST_WINDOW = 10.0


def finite(value: float) -> float | None:
    """``value``, or ``None`` if it is not a finite number (JSON has no NaN)."""
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


class LinkStats:
    """The figures of one live link, from the chunks it delivered."""

    def __init__(self) -> None:
        self.lost_total = 0
        self.battery: float | None = None
        self.impedance = False
        self._rows: deque[tuple[float, int]] = deque()
        self._lost: deque[tuple[float, int]] = deque()

    def add(self, now: float, samples: int, lost: int, battery: float, impedance: bool) -> None:
        """One chunk: ``samples`` rows (lost ones included), ``lost`` of them lost."""
        self._rows.append((now, samples - lost))
        if lost:
            self._lost.append((now, lost))
            self.lost_total += lost
        self.battery = finite(battery)
        self.impedance = bool(impedance)

    def _trim(self, now: float) -> None:
        while self._rows and now - self._rows[0][0] > RATE_WINDOW:
            self._rows.popleft()
        while self._lost and now - self._lost[0][0] > LOST_WINDOW:
            self._lost.popleft()

    def message(self, now: float, since: float) -> dict:
        """The ``status`` message. ``since`` is when the link opened (the window cannot start
        before it); ``rate`` is ``None`` for the first second."""
        self._trim(now)
        span = min(RATE_WINDOW, now - since)
        rate = sum(n for _, n in self._rows) / span if span >= 1.0 else None
        return {
            "t": "status",
            "rate": None if rate is None else round(rate, 1),
            "lost": self.lost_total,
            "lost10": sum(n for _, n in self._lost),
            "bat": self.battery,
            "imp": self.impedance,
        }


class Decimator:
    """Means of ``fs / DISPLAY_HZ`` rows, kept across chunks."""

    def __init__(self, fs: float, channels: int) -> None:
        self.factor = max(1, round(fs / DISPLAY_HZ))
        self.channels = channels
        self._carry = np.empty((0, channels))
        self._last = np.zeros(channels)
        self._out: list[list[float]] = []

    def push(self, volts: np.ndarray) -> list[list[float]] | None:
        """Adds a chunk (rows by channels, in volts); returns ``FLUSH_ROWS`` or more rows in
        microvolts once that many are ready, else ``None``."""
        rows = np.concatenate([self._carry, volts[:, : self.channels]])
        groups = len(rows) // self.factor
        self._carry = rows[groups * self.factor :]
        if groups:
            block = rows[: groups * self.factor].reshape(groups, self.factor, self.channels)
            valid = ~np.isnan(block)
            count = valid.sum(axis=1)
            total = np.where(valid, block, 0.0).sum(axis=1)
            means = total / np.maximum(count, 1)
            for i in range(groups):
                means[i] = np.where(count[i] == 0, self._last, means[i])
                self._last = means[i]
            self._out.extend(np.round(means * 1e6, 1).tolist())
        if len(self._out) >= FLUSH_ROWS:
            out, self._out = self._out, []
            return out
        return None

    def reset(self) -> None:
        self._carry = np.empty((0, self.channels))
        self._out = []

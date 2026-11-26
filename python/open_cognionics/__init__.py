"""Streaming and recording for Cognionics (CGX) EEG headsets.

``connect`` opens a headset over its USB serial dongle; ``Headset.stream``
yields ``Chunk`` objects and ``Headset.record`` writes a session directory
(``raw.bin``, ``recording.bdf``, ``markers.jsonl``, ``clock.jsonl``,
``meta.json``).
``replay`` and ``read_recording`` read a session directory back.

The only bytes this package writes to a headset are ``0x11`` (impedance
check on), ``0x12`` (off) and ``0x14`` (configuration request).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

from ._core import (
    Chunk,
    Config,
    ConfigError,
    Disconnected,
    Error,
    Headset,
    Marker,
    NoConfiguration,
    NoData,
    NoHeadset,
    OpenError,
    PortInfo,
    Recording,
    Replay,
    SeveralHeadsets,
    Stream,
    __version__,
    _read_recording,
    connect,
    find_headsets,
    replay,
    select_headset,
)

__all__ = [
    "Chunk",
    "Config",
    "ConfigError",
    "Disconnected",
    "Error",
    "Headset",
    "Marker",
    "NoConfiguration",
    "NoData",
    "NoHeadset",
    "OpenError",
    "PortInfo",
    "Recorded",
    "Recording",
    "Replay",
    "SeveralHeadsets",
    "Stream",
    "__version__",
    "connect",
    "find_headsets",
    "read_recording",
    "replay",
    "select_headset",
]


class Recorded:
    """A whole recording read into memory by ``read_recording``.

    Attributes:
        data: EEG in volts, float64, shape (samples, n_eeg). Rows of lost
            samples are NaN.
        ext: Extension (ExG) channels in volts, shape (samples, n_ext), NaN
            rows for lost samples.
        acc: Accelerometer raw counts as float64, shape (samples, n_acc), NaN
            rows for lost samples.
        times: Host time of each sample, seconds since the Unix epoch. With
            ``clock.jsonl``: linear between its points, and extended before
            the first and after the last at the nominal rate. Without it:
            ``meta["start_unix"]`` (0 without ``meta.json``) plus
            ``n / sample_rate``.
        markers: The ``Marker`` objects from ``markers.jsonl``.
        meta: The contents of ``meta.json`` as a dict, or ``None`` for a bare
            ``raw.bin``.
        config: The ``Config`` used to decode the bytes.
        clock: The ``(sample, unix_time)`` points of ``clock.jsonl``; empty
            without the file. When the last line was cut short, the whole
            lines before it.
        clock_error: ``None`` when ``clock.jsonl`` was whole or absent;
            otherwise a string such as ``"line 3: cut short"`` for a last
            line that was cut short and left out.
    """

    def __init__(
        self,
        data: np.ndarray,
        ext: np.ndarray,
        acc: np.ndarray,
        times: np.ndarray,
        markers: list[Marker],
        meta: dict[str, Any] | None,
        config: Config,
        clock: list[tuple[int, float]] | None = None,
        clock_error: str | None = None,
    ) -> None:
        self.data = data
        self.ext = ext
        self.acc = acc
        self.times = times
        self.markers = markers
        self.meta = meta
        self.config = config
        self.clock = list(clock or [])
        self.clock_error = clock_error

    def sample_at(self, unix_time: float) -> float:
        """Sample position (fractional) at host time ``unix_time``.

        The inverse of ``times``: with ``clock.jsonl``, linear between its
        points and extended at the nominal rate beyond them; without it,
        ``(unix_time - start) * sample_rate``.
        """
        rate = self.config.sample_rate
        if not self.clock:
            start = float(self.meta["start_unix"]) if self.meta else 0.0
            return (unix_time - start) * rate
        samples = np.array([s for s, _ in self.clock], dtype=np.float64)
        times = np.array([t for _, t in self.clock], dtype=np.float64)
        if unix_time <= times[0]:
            return float(samples[0] + (unix_time - times[0]) * rate)
        if unix_time >= times[-1]:
            return float(samples[-1] + (unix_time - times[-1]) * rate)
        return float(np.interp(unix_time, times, samples))

    def __repr__(self) -> str:
        return f"Recorded(samples={self.data.shape[0]}, markers={len(self.markers)})"


def _sample_times(
    n: int, start: float, rate: float, clock: list[tuple[int, float]]
) -> np.ndarray:
    """Host time of samples ``0..n``: from the clock points, else nominal."""
    index = np.arange(n, dtype=np.float64)
    if not clock:
        return start + index / rate
    samples = np.array([s for s, _ in clock], dtype=np.float64)
    times = np.array([t for _, t in clock], dtype=np.float64)
    out = np.interp(index, samples, times)
    before = index < samples[0]
    out[before] = times[0] + (index[before] - samples[0]) / rate
    after = index > samples[-1]
    out[after] = times[-1] + (index[after] - samples[-1]) / rate
    return out


def read_recording(
    path: str | os.PathLike[str], config: Config | None = None
) -> Recorded:
    """Read a session directory (or a bare ``raw.bin`` with ``config``) whole.

    Raises ``ValueError`` when a bare ``raw.bin`` is given without ``config``
    or the metadata cannot be read, and ``OSError`` when a file cannot be read.
    """
    path = Path(path)
    config, markers, clock, clock_error, data, ext, acc = _read_recording(path, config)
    meta = None
    if path.is_dir():
        meta_path = path / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
    start = float(meta["start_unix"]) if meta else 0.0
    times = _sample_times(data.shape[0], start, config.sample_rate, clock)
    return Recorded(data, ext, acc, times, markers, meta, config, clock, clock_error)

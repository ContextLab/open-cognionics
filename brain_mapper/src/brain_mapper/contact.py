"""The contact measure: the size of the headset's contact-check tone on each electrode.

With the contact check on (byte ``0x11``) the headset puts a tone at a quarter of its sample
rate on every EEG channel (``docs/protocol.md``, section 8); the poorer the contact of an
electrode, the larger the tone on its channel. ``tone_amplitude`` measures the tone over a
window of samples, ``classify`` turns the amplitudes into ``good``, ``marginal`` or ``bad`` with
``Thresholds``, and ``ContactMonitor`` does both over the last second of the chunks it is fed.

The tone is at ``tone_frequency(sample_rate)``, a quarter of the sample rate (125 Hz at the
Quick-20's 500 samples a second). At a rate whose quarter falls among the frequencies of the
brain signal and of mains pickup the measure means nothing: ``tone_frequency`` then raises
``Unmeasurable`` and the monitor says so instead of giving states.

A channel that is flat or sits at the end of the converter's range carries no tone at all
(seen in the pilot recordings for electrodes without contact); ``channel_amplitude`` gives such
a channel an infinite amplitude, so it is ``bad``.

The thresholds are provisional: they were chosen from pilot recordings (see
``docs/brain-mapper.md``, "Calibration") and are saved by version with every session.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

#: The frequency of the contact-check tone of the Quick-20 at 500 samples a second.
TONE_HZ = 125.0
#: The lowest tone that is measured: above mains pickup (50 or 60 Hz) and the brain signal.
MIN_TONE_HZ = 70.0
STATES = ("good", "marginal", "bad")
#: Share of the converter's full scale from which a sample counts as at the end of the range.
RAIL_SHARE = 0.99


@dataclass(frozen=True)
class Thresholds:
    """Tone amplitudes in volts: below ``marginal`` is good, from ``bad`` on is bad."""

    marginal: float
    bad: float
    version: str


#: Chosen from pilot recordings (see ``docs/brain-mapper.md``).
THRESHOLDS = Thresholds(marginal=1.0e-3, bad=4.0e-3, version="provisional-2026-11-25")

#: Seconds of data recorded with the contact check off, after the last sample recorded with it
#: on, before the eyes-open baseline (or a round's first block, after a contact check) starts.
#: In the pilot recordings the signal was back to its level within 1.5 s of the switch
#: (``docs/brain-mapper.md``, "Calibration"); 2 s is the settling time the thresholds left out.
#: The gate counts it in samples of the data stream: ``settle_samples``.
SETTLE_S = 2.0


def settle_samples(sample_rate: float, seconds: float = SETTLE_S) -> int:
    """``seconds`` as a number of samples at ``sample_rate``, rounded up (never shorter)."""
    return math.ceil(round(float(seconds) * float(sample_rate), 6))


class OffCounter:
    """How many samples of the data stream have come since the last one that was, or may have
    been, recorded with the contact check on.

    ``push`` takes each chunk as the stream gives it: its first sample number (lost samples
    count), its number of samples, and its ``impedance_on``, which is the flag of its LAST
    sample only. A chunk joins several reads (25 samples or more, more under load), so one
    whose last sample is off can begin with samples that were on. The counter therefore treats
    a chunk flagged off that follows one flagged on (or that comes first) as on up to its last
    sample: samples lost between the two may also have been on, and are covered the same way.
    The count is thus never larger than the true one, at the cost of at most one chunk of extra
    wait. (A stretch that goes on and off again inside one chunk between two chunks flagged off
    cannot be seen from the flags the stream gives.)
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forgets everything (a new link): nothing has been quiet yet."""
        # One tuple, assigned at once, so another thread reads a consistent pair:
        # (end of the last sample that may have been on, end of the last chunk, last flag).
        self._marks: tuple[int, int, bool] | None = None

    def push(self, index: int, samples: int, impedance_on: bool) -> None:
        end = int(index) + int(samples)
        marks = self._marks
        last_on, previous_on = (marks[0], marks[2]) if marks is not None else (int(index), True)
        if impedance_on:
            last_on = end
        elif previous_on:
            last_on = max(last_on, end - 1)  # all but its last sample (which is off) may be on
        self._marks = (last_on, end, bool(impedance_on))

    def quiet_samples(self) -> int:
        """Samples received since the last one that may have been on; 0 before any data."""
        marks = self._marks
        if marks is None:
            return 0
        return max(0, marks[1] - marks[0])


class Unmeasurable(ValueError):
    """The contact cannot be measured at this sample rate; the message says why, plainly."""


def tone_frequency(sample_rate: float) -> float:
    """The frequency of the contact-check tone: a quarter of ``sample_rate``.

    Raises ``Unmeasurable`` when the rate is not a positive number, or when its quarter is below
    ``MIN_TONE_HZ``: the tone would then be mixed with the brain signal and mains pickup.
    """
    rate = float(sample_rate)
    if not np.isfinite(rate) or rate <= 0:
        raise Unmeasurable(f"the headset reports a sample rate of {sample_rate}, so the contact cannot be measured")
    frequency = rate / 4
    if frequency < MIN_TONE_HZ:
        raise Unmeasurable(
            f"at {rate:g} samples a second the contact-check tone is at {frequency:g} Hz, among the "
            f"brain signal and mains pickup, so the contact cannot be measured"
        )
    return frequency


def window_samples(sample_rate: float) -> int:
    """About one second of samples, a multiple of 4 so that it holds whole periods of the tone."""
    return max(4, 4 * round(float(sample_rate) / 4))


def tone_amplitude(data: np.ndarray, sample_rate: float, frequency: float = TONE_HZ) -> np.ndarray:
    """The amplitude, in volts, of the sine at ``frequency`` in each channel of ``data``.

    ``data`` is samples by channels, in volts, at ``sample_rate``; rows with a missing value
    (lost samples) are left out. The mean of each channel is removed first, and the tone is
    found by complex demodulation: twice the magnitude of the mean of the samples times
    ``exp(-2 pi i f t)``, which is the amplitude of a sine of that frequency. The result is
    exact for a window of a whole number of periods (``window_samples``).
    """
    x = np.asarray(data, dtype=float)
    if x.ndim != 2:
        raise ValueError("data is samples by channels")
    if not sample_rate > 0:
        raise ValueError("the sample rate must be above 0")
    ok = np.isfinite(x).all(axis=1)
    if not ok.any():
        return np.full(x.shape[1], np.nan)
    n = np.flatnonzero(ok)
    kept = x[ok]
    kept = kept - kept.mean(axis=0)
    carrier = np.exp(-2j * np.pi * frequency * n / sample_rate)
    return 2.0 * np.abs(carrier @ kept) / len(n)


def channel_amplitude(data: np.ndarray, config) -> np.ndarray:
    """``tone_amplitude`` at the configuration's sample rate, with ``inf`` for a channel that
    is flat (no change of one count) or touches the end of the converter's range."""
    x = np.asarray(data, dtype=float)[:, : len(config.labels)]
    amplitude = tone_amplitude(x, config.sample_rate, tone_frequency(config.sample_rate))
    step = config.eeg_volts_per_count
    full = 2**23 * step  # 24-bit samples
    ok = np.isfinite(x).all(axis=1)
    if ok.any():
        kept = x[ok]
        flat = (kept.max(axis=0) - kept.min(axis=0)) < step
        railed = (np.abs(kept) >= RAIL_SHARE * full).any(axis=0)
        amplitude = np.where(flat | railed, np.inf, amplitude)
    return amplitude


#: Below this median over the channels (volts) the tone counts as not seen at all with the check
#: on: in pilot recordings the median with the check on was many times larger, and the tone with
#: it off stayed below it (``docs/brain-mapper.md``, "Calibration"). A
#: contact check that sees no tone calls every contact good; the page then says so.
TONE_SEEN_V = 50e-6


def tone_seen(amplitude: np.ndarray) -> bool:
    """Whether the tone is there at all: the median of the finite amplitudes (a flat or railed
    channel's ``inf`` counts as seen) is at least ``TONE_SEEN_V``."""
    values = np.asarray(amplitude, dtype=float)
    values = np.where(np.isinf(values), np.finfo(float).max, values)
    values = values[~np.isnan(values)]
    return bool(len(values)) and float(np.median(values)) >= TONE_SEEN_V


def classify(amplitude: np.ndarray, thresholds: Thresholds) -> list[str]:
    """``good`` below ``thresholds.marginal``, ``bad`` from ``thresholds.bad`` on (and for a value
    that is not a finite number), ``marginal`` in between."""
    out = []
    for value in np.asarray(amplitude, dtype=float):
        if not np.isfinite(value) or value >= thresholds.bad:
            out.append("bad")
        elif value >= thresholds.marginal:
            out.append("marginal")
        else:
            out.append("good")
    return out


class ContactMonitor:
    """The state of every electrode over the last second of the chunks it is fed.

    Only samples recorded with the contact check on count: a chunk recorded with it off
    empties the window, so a state never mixes the two. The first second recorded with the
    check on after that (or after ``reset``) is left out too: it holds the switch-on transient
    (``settling`` is true until it has passed). ``states()`` is ``None`` until a whole second
    (``window_samples``, lost ones included) has been fed after it. ``sample`` is the number of
    the sample after the last one fed. At a sample rate where the tone cannot be measured,
    ``unmeasurable`` says why and ``states()`` is always ``None``.
    """

    def __init__(self, config, thresholds: Thresholds = THRESHOLDS) -> None:
        self.config = config
        self.thresholds = thresholds
        self.labels = list(config.labels)
        self.unmeasurable: str | None = None
        try:
            tone_frequency(config.sample_rate)
        except Unmeasurable as error:
            self.unmeasurable = str(error)
        self.window = window_samples(config.sample_rate) if self.unmeasurable is None else 1
        self.sample: int | None = None
        self._rows: deque[np.ndarray] = deque()
        self._count = 0
        self._skip = self.window

    def reset(self) -> None:
        self._rows.clear()
        self._count = 0
        self._skip = self.window

    @property
    def settling(self) -> bool:
        """Whether the first second after the check went on is still being left out."""
        return self._skip > 0

    def push(self, chunk) -> None:
        self.sample = chunk.index + chunk.samples
        if not chunk.impedance_on:
            self.reset()
            return
        data = np.asarray(chunk.data, dtype=float)[:, : len(self.labels)]
        if self._skip:
            dropped = min(self._skip, len(data))
            self._skip -= dropped
            data = data[dropped:]
        if not len(data):
            return
        self._rows.append(data)
        self._count += len(data)
        while self._count - len(self._rows[0]) >= self.window:
            self._count -= len(self._rows.popleft())

    def amplitudes(self) -> np.ndarray | None:
        """``channel_amplitude`` of the last second, or ``None`` before there is one."""
        if self.unmeasurable is not None or self._count < self.window:
            return None
        last = np.concatenate(list(self._rows))[-self.window :]
        if np.isfinite(last).all(axis=1).sum() < self.window // 2:
            return None  # more than half of the second was lost
        return channel_amplitude(last, self.config)

    def states(self) -> dict[str, str] | None:
        """``{label: "good" | "marginal" | "bad"}`` for the last second, or ``None``."""
        amplitude = self.amplitudes()
        if amplitude is None:
            return None
        return dict(zip(self.labels, classify(amplitude, self.thresholds)))

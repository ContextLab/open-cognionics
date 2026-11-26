"""The signal check: what each EEG channel looks like with the contact-check tone off.

The contact check (``contact.py``) measures a tone the headset adds while its check is on. It
cannot tell a channel that carries no tone because its contact is good from one that carries no
tone because the tone never reached it, and it says nothing about how the channel behaves once
the tone is off. This check looks at the signal itself, over the last ``SIGNAL_S`` seconds
recorded with the tone off (after the settling wait), and gives each channel one state:

* ``clipped``: more than ``CLIP_SHARE`` (1 %) of the samples at or within 1 % of the
  converter's limit (``RAIL_SHARE``). The limit is 2^23 counts times the volts per count (the
  24-bit samples, ``docs/protocol.md``, "Scaling"; 833.3 mV at the test headset's gain of 3),
  the limit ``contact.py`` uses too.
* ``flat``: the 1 to 45 Hz signal has a robust standard deviation (1.4826 times the median
  absolute deviation) under ``FLAT_RMS_V``, 0.5 uV, five steps of the converter.
* ``noisy``: the 1 to 45 Hz signal goes over ``NOISY_PTP_V``, 200 uV, peak to peak in more than
  half (``NOISY_SHARE``) of the 1 s pieces of the window (4 or more of 6). The analysis uses
  2 s segments over a whole session; in a 6 s window one blink (about 0.3 s, often over
  200 uV at Fp1 and Fp2) would fall in 3 of the 5 overlapping 2 s segments and make the channel
  noisy, while it falls in at most 2 of the 6 one-second pieces. Two blinks can make a channel
  noisy only when each falls across the boundary of two pieces.
* ``fine``: none of these.

The three bounds are those the analysis of a recording uses to call a channel bad over a whole
session (branch ``night-analysis``, ``analysis/spectra.py`` and ``docs/brain-mapper-results.md``,
"Bad channels"), (with 1 s pieces in place of 2 s segments, see above), so a channel called noisy, clipped or flat
here is one that the analysis would leave out if it stayed like this; and 200 uV peak to peak is
also the bound from which the analysis rejects a 2 s segment. Each state is checked in that order: a clipped channel is not
also called flat or noisy.

Mains pickup (the larger of the 50 Hz and 60 Hz components, ``mains_uv``) is measured and saved
with each channel but gives no state of its own: the band-pass removes it, and no bound for it
is established.
Before the band-pass the two mains sines are fitted and taken out, so that even strong pickup
leaves nothing at the ends of the window.

The band-pass is done in the frequency domain on the window (after the line through its first
and last samples is taken out, so that the window joins up end to start): everything outside
1 to 45 Hz is set to zero. A slow drift leaves about 1 % of its size (up to about 10 % in the
first and last second), far under the noisy bound for drifts of a few millivolts, and makes no
channel noisy by itself; one large enough to reach the converter's limit makes it clipped. Only
numpy is used.

The bounds are a first version (``VERSION``), saved with every result; like the contact
thresholds they have not been checked against a recording where it is known which electrodes
were good.
"""

from __future__ import annotations

import threading

import numpy as np

#: The version of the bounds below, saved with every result.
VERSION = "signal-2026-11-26"
#: Seconds of data, with the tone off, that one signal check looks at.
SIGNAL_S = 6.0
STATES = ("fine", "noisy", "clipped", "flat")
#: The band the signal is looked at in, in Hz.
BAND = (1.0, 45.0)
#: Share of the converter's full scale from which a sample is at the limit (as in contact.py).
RAIL_SHARE = 0.99
#: More than this share of the samples at the limit makes a channel clipped.
CLIP_SHARE = 0.01
#: Below this robust standard deviation (volts) of the band-passed signal a channel is flat.
FLAT_RMS_V = 0.5e-6
#: Above this peak to peak (volts) of the band-passed signal a 1 s piece is too large.
NOISY_PTP_V = 200e-6
#: More than this share of too-large pieces makes a channel noisy.
NOISY_SHARE = 0.5
#: Pieces: 1 s long, one after the other.
SEGMENT_S, SEGMENT_STEP_S = 1.0, 1.0
#: The mains frequencies whose pickup is measured (and taken out before the band-pass).
MAINS_HZ = (50.0, 60.0)
#: Below this share of samples received (not lost) the window is not measured.
MIN_RECEIVED = 0.5


def full_scale(volts_per_count: float) -> float:
    """The converter's limit in volts: 2^23 counts of 24-bit samples (``docs/protocol.md``)."""
    return 2**23 * float(volts_per_count)


def fill_lost(data: np.ndarray) -> np.ndarray:
    """``data`` (samples by channels) with rows holding a missing value filled in, channel by
    channel, by straight lines between the received samples around them."""
    x = np.array(data, dtype=float, copy=True)
    ok = np.isfinite(x).all(axis=1)
    if ok.all() or not ok.any():
        return x
    n = np.arange(len(x))
    for c in range(x.shape[1]):
        x[~ok, c] = np.interp(n[~ok], n[ok], x[ok, c])
    return x


def band_pass(data: np.ndarray, sample_rate: float, band: tuple[float, float] = BAND) -> np.ndarray:
    """Each channel of ``data`` (samples by channels, no missing values) with everything outside
    ``band`` taken out: the line through its first and last samples is removed (so the window
    joins up end to start), then every frequency outside the band is set to zero. A slow drift
    leaves about 1 % of its size away from the ends of the window, up to about 10 % within the
    first and last second (``tests/brain_mapper/test_quality.py``)."""
    x = np.asarray(data, dtype=float)
    n = len(x)
    if n < 2:
        return np.zeros_like(x)
    ramp = np.linspace(0.0, 1.0, n)[:, None]
    x = x - (x[0] + ramp * (x[-1] - x[0]))
    spectrum = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / float(sample_rate))
    spectrum[(freqs < band[0]) | (freqs > band[1])] = 0
    return np.fft.irfft(spectrum, n=n, axis=0)


def robust_rms(x: np.ndarray) -> np.ndarray:
    """1.4826 times the median absolute deviation of each column: the RMS of Gaussian noise,
    little moved by a few large samples."""
    x = np.asarray(x, dtype=float)
    return 1.4826 * np.median(np.abs(x - np.median(x, axis=0)), axis=0)


def sine_amplitude(data: np.ndarray, sample_rate: float, frequency: float) -> np.ndarray:
    """The amplitude, in volts, of the sine at ``frequency`` in each channel (mean removed),
    by complex demodulation, as ``contact.tone_amplitude`` measures the tone."""
    x = np.asarray(data, dtype=float)
    x = x - x.mean(axis=0)
    n = np.arange(len(x))
    carrier = np.exp(-2j * np.pi * float(frequency) * n / float(sample_rate))
    return 2.0 * np.abs(carrier @ x) / len(x)


def remove_sine(data: np.ndarray, sample_rate: float, frequency: float) -> np.ndarray:
    """``data`` with the least-squares sine at ``frequency`` taken out of each channel."""
    x = np.asarray(data, dtype=float)
    n = np.arange(len(x)) * (2 * np.pi * float(frequency) / float(sample_rate))
    basis = np.stack([np.cos(n), np.sin(n)], axis=1)
    return x - basis @ np.linalg.lstsq(basis, x - x.mean(axis=0), rcond=None)[0]


def measure(data: np.ndarray, sample_rate: float, volts_per_count: float) -> dict[str, np.ndarray]:
    """The numbers the states are made from, one per channel of ``data`` (samples by channels,
    in volts; rows with a missing value are lost samples): ``rail_share`` (share of the received
    samples at the converter's limit), ``rms`` (robust standard deviation of the band-passed
    signal), ``ptp`` (median peak to peak of its 1 s pieces), ``large_share`` (share of those
    pieces over ``NOISY_PTP_V``) and ``mains`` (the larger of the 50 and 60 Hz amplitudes),
    all in volts."""
    x = np.asarray(data, dtype=float)
    if x.ndim != 2 or len(x) < 4:
        raise ValueError("data is samples by channels, at least 4 samples")
    ok = np.isfinite(x).all(axis=1)
    if ok.mean() < MIN_RECEIVED:
        raise ValueError("more than half of the samples were lost")
    limit = RAIL_SHARE * full_scale(volts_per_count)
    rail_share = (np.abs(x[ok]) >= limit).mean(axis=0)
    filled = fill_lost(x)
    mains = np.max([sine_amplitude(filled, sample_rate, hz) for hz in MAINS_HZ], axis=0)
    cleaned = filled
    for hz in MAINS_HZ:
        cleaned = remove_sine(cleaned, sample_rate, hz)
    passed = band_pass(cleaned, sample_rate)
    size = max(1, int(round(SEGMENT_S * float(sample_rate))))
    step = max(1, int(round(SEGMENT_STEP_S * float(sample_rate))))
    starts = range(0, max(1, len(passed) - size + 1), step)
    ptps = np.stack([np.ptp(passed[k : k + size], axis=0) for k in starts])
    return {
        "rail_share": rail_share,
        "rms": robust_rms(passed),
        "ptp": np.median(ptps, axis=0),
        "large_share": (ptps > NOISY_PTP_V).mean(axis=0),
        "mains": mains,
    }


def classify(measured: dict[str, np.ndarray]) -> list[str]:
    """The state of each channel from ``measure``: ``clipped``, ``flat``, ``noisy`` or ``fine``,
    checked in that order."""
    out: list[str] = []
    for c in range(len(measured["rms"])):
        if measured["rail_share"][c] > CLIP_SHARE:
            out.append("clipped")
        elif measured["rms"][c] < FLAT_RMS_V:
            out.append("flat")
        elif measured["large_share"][c] > NOISY_SHARE:
            out.append("noisy")
        else:
            out.append("fine")
    return out


def check(data: np.ndarray, labels, sample_rate: float, volts_per_count: float) -> dict:
    """The signal check of one window: ``{"states": {label: state}, "values": {label:
    {"rms_uv", "ptp_uv", "mains_uv", "large_share", "rail_share"}}, "not_fine": [labels, in
    channel order], "version", "seconds"}``."""
    labels = list(labels)
    x = np.asarray(data, dtype=float)[:, : len(labels)]
    measured = measure(x, sample_rate, volts_per_count)
    states = classify(measured)
    return {
        "states": dict(zip(labels, states)),
        "values": {
            label: {
                "rms_uv": round(float(measured["rms"][c]) * 1e6, 2),
                "ptp_uv": round(float(measured["ptp"][c]) * 1e6, 2),
                "mains_uv": round(float(measured["mains"][c]) * 1e6, 2),
                "large_share": round(float(measured["large_share"][c]), 3),
                "rail_share": round(float(measured["rail_share"][c]), 4),
            }
            for c, label in enumerate(labels)
        },
        "not_fine": [label for label, state in zip(labels, states) if state != "fine"],
        "version": VERSION,
        "seconds": round(len(x) / float(sample_rate), 3),
    }


class SignalWindow:
    """Collects the samples of the data stream recorded with the tone off, from ``begin`` on.

    The hub ``push``es every chunk. After ``begin`` the window keeps the chunks whose
    ``impedance_on`` is false; a chunk with it on empties the window (the tone came back).
    ``progress()`` gives ``(samples, needed)``; ``result()`` is ``check`` of the first ``needed``
    samples once there are that many, else ``None``. Between ``end`` (or before the first
    ``begin``) nothing is kept. Safe to use from two threads."""

    def __init__(self, config, seconds: float = SIGNAL_S) -> None:
        self.labels = list(config.labels)
        self.sample_rate = float(config.sample_rate)
        self.volts_per_count = float(config.eeg_volts_per_count)
        self.needed = max(4, int(round(seconds * self.sample_rate)))
        self._lock = threading.Lock()
        self._rows: list[np.ndarray] | None = None
        self._count = 0

    def begin(self) -> None:
        with self._lock:
            self._rows, self._count = [], 0

    def end(self) -> None:
        with self._lock:
            self._rows, self._count = None, 0

    def push(self, chunk) -> None:
        with self._lock:
            if self._rows is None or self._count >= self.needed:
                return
            if chunk.impedance_on:
                self._rows, self._count = [], 0
                return
            data = np.asarray(chunk.data, dtype=float)[:, : len(self.labels)]
            self._rows.append(data)
            self._count += len(data)

    def progress(self) -> tuple[int, int]:
        with self._lock:
            return min(self._count, self.needed), self.needed

    def result(self) -> dict | None:
        with self._lock:
            if self._rows is None or self._count < self.needed:
                return None
            data = np.concatenate(self._rows)[: self.needed]
        try:
            return check(data, self.labels, self.sample_rate, self.volts_per_count)
        except ValueError as error:
            return {"error": str(error), "states": {}, "not_fine": [], "version": VERSION}


class SignalHolder:
    """The hub's signal window across links: ``attach`` puts a new ``SignalWindow`` in place for
    the configuration of a link that came up (not begun: the session's ``begin`` starts it, and
    a part cut by a lost link is started again on Resume). Without a window nothing is measured:
    ``progress`` is ``(0, 1)`` and ``result`` ``None``."""

    def __init__(self) -> None:
        self._window: SignalWindow | None = None

    def attach(self, config) -> None:
        self._window = SignalWindow(config)

    @property
    def sample_rate(self) -> float:
        window = self._window
        return window.sample_rate if window is not None else 1.0

    def begin(self) -> None:
        if self._window is not None:
            self._window.begin()

    def end(self) -> None:
        if self._window is not None:
            self._window.end()

    def push(self, chunk) -> None:
        window = self._window
        if window is not None:
            window.push(chunk)

    def progress(self) -> tuple[int, int]:
        window = self._window
        return window.progress() if window is not None else (0, 1)

    def result(self) -> dict | None:
        window = self._window
        return window.result() if window is not None else None

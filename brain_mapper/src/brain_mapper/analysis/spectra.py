"""Preprocessing, bad channels and segments, and power spectra (numpy only).

Every step is stated here and in ``docs/brain-mapper-results.md``:

- Filter: zero-phase, in the frequency domain, applied to each analyzed span separately (its
  mean and linear trend removed first, 2 s of mirrored signal added at each end and cut off
  again). A span longer than ``CHUNK_SECONDS`` plus twice ``MARGIN_SECONDS`` is filtered in
  pieces of ``CHUNK_SECONDS``, each with ``MARGIN_SECONDS`` of its neighbors on both sides that
  are cut off again, so the memory used does not grow with the length of the round. The gain is a band-pass from 1 to 45 Hz (raised-cosine edges from 0.5 to 1 Hz and from
  45 to 50 Hz) times a notch at the mains frequency and each of its harmonics below the Nyquist
  frequency (gain 0 within 1 Hz of it, raised-cosine back to 1 at 2 Hz).
- Bad channels (whole session): at the converter's limit (99 % of full scale) in more than 1 % of
  the samples; flat (robust standard deviation below 0.5 uV after the filter); or loud (its
  peak-to-peak amplitude, after the filter and before the average reference, above 200 uV in
  more than half of the 2 s segments).
- Reference: the average of the good channels is subtracted from every channel.
- Bad segments: the data are cut into 2 s segments every 1 s; a segment is rejected when it holds
  a lost sample or any good channel's peak-to-peak amplitude in it exceeds 200 uV.
- Spectra: Welch's method on the kept segments (Hann window, 2 s, 50 % overlap, 0.5 Hz bins),
  in uV^2/Hz. Band power is the sum of the spectrum over the band's bins times 0.5 Hz.
"""

from __future__ import annotations

import numpy as np

BANDS = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}
BAND_NAMES = {
    "delta": "Delta, 1-4 Hz",
    "theta": "Theta, 4-8 Hz",
    "alpha": "Alpha, 8-13 Hz",
    "beta": "Beta, 13-30 Hz",
    "gamma": "Low gamma, 30-45 Hz",
}
PASS = (1.0, 45.0)
EDGE_LOW = 0.5
EDGE_HIGH = 50.0
NOTCH_HALF = 1.0
NOTCH_EDGE = 2.0
PAD_SECONDS = 2.0
SEGMENT_SECONDS = 2.0
STEP_SECONDS = 1.0
FLAT_UV = 0.5
LOUD_FRACTION = 0.5
CLIP_FRACTION = 0.01
PEAK_TO_PEAK_UV = 200.0
CHUNK_SECONDS = 40.0
MARGIN_SECONDS = 8.0
#: Segments handled at once (bounds the memory used).
SEGMENT_BATCH = 32


def _cosine(x: np.ndarray, start: float, stop: float) -> np.ndarray:
    """0 at and below ``start``, 1 at and above ``stop``, a raised cosine between."""
    t = np.clip((x - start) / (stop - start), 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(np.pi * t)


def gain(freqs: np.ndarray, mains: float) -> np.ndarray:
    """The filter's gain at ``freqs`` (Hz): band-pass times notches at ``mains`` and harmonics."""
    g = _cosine(freqs, EDGE_LOW, PASS[0]) * (1.0 - _cosine(freqs, PASS[1], EDGE_HIGH))
    nyquist = freqs[-1] if len(freqs) else 0.0
    harmonic = mains
    while harmonic <= nyquist:
        distance = np.abs(freqs - harmonic)
        g = g * _cosine(distance, NOTCH_HALF, NOTCH_EDGE)
        harmonic += mains
    return g


def filtered(x: np.ndarray, fs: float, mains: float) -> np.ndarray:
    """``x`` (samples by channels, no NaN) filtered as described in the module text."""
    n = x.shape[0]
    if n < 4:
        return np.zeros_like(x)
    t = np.arange(n, dtype=np.float64)
    t -= t.mean()
    centred = x - x.mean(axis=0)
    slope = (t @ centred) / max(float(t @ t), 1e-12)
    centred = centred - np.outer(t, slope)
    pad = min(int(PAD_SECONDS * fs), n - 1)
    padded = np.concatenate([centred[pad:0:-1], centred, centred[-2 : -pad - 2 : -1]], axis=0)
    spectrum = np.fft.rfft(padded, axis=0)
    spectrum *= gain(np.fft.rfftfreq(padded.shape[0], 1.0 / fs), mains)[:, None]
    return np.fft.irfft(spectrum, n=padded.shape[0], axis=0)[pad : pad + n]


def filter_in_place(x: np.ndarray, fs: float, mains: float) -> None:
    """Filter ``x`` (samples by channels, float32, no NaN) where it lies, in pieces (see the
    module text). Each piece is written back only after the next piece has been read, so every
    piece is filtered from unfiltered samples."""
    n = x.shape[0]
    core = int(CHUNK_SECONDS * fs)
    margin = int(MARGIN_SECONDS * fs)
    if n <= core + 2 * margin:
        x[:] = filtered(x.astype(np.float64), fs, mains)
        return
    pending = None
    for a in range(0, n, core):
        b = min(a + core, n)
        lo, hi = max(a - margin, 0), min(b + margin, n)
        y = filtered(x[lo:hi].astype(np.float64), fs, mains)[a - lo : b - lo].astype(x.dtype)
        if pending is not None:
            x[pending[0] : pending[1]] = pending[2]
        pending = (a, b, y)
    x[pending[0] : pending[1]] = pending[2]


def segment_view(x: np.ndarray, fs: float) -> np.ndarray:
    """The 2 s segments of ``x`` every 1 s as a view without copying: (segments, channels, samples)."""
    length = int(round(SEGMENT_SECONDS * fs))
    step = int(round(STEP_SECONDS * fs))
    if x.shape[0] < length:
        return np.zeros((0, x.shape[1], length), dtype=x.dtype)
    return np.lib.stride_tricks.sliding_window_view(x, length, axis=0)[::step]


def segment_ptp(x: np.ndarray, fs: float) -> np.ndarray:
    """Peak-to-peak amplitude of every channel in every 2 s segment: (segments, channels)."""
    view = segment_view(x, fs)
    out = np.empty(view.shape[:2], dtype=np.float64)
    for s in range(0, len(view), SEGMENT_BATCH):
        part = view[s : s + SEGMENT_BATCH]
        out[s : s + SEGMENT_BATCH] = part.max(axis=2) - part.min(axis=2)
    return out


def pass_bins(fs: float) -> tuple[np.ndarray, np.ndarray]:
    """The bins of a 2 s segment's spectrum from 1 to 45 Hz: ``(index, freqs)``."""
    freqs = np.fft.rfftfreq(int(round(SEGMENT_SECONDS * fs)), 1.0 / fs)
    index = np.flatnonzero((freqs >= PASS[0]) & (freqs <= PASS[1]))
    return index, freqs[index]


def segment_coefficients(x: np.ndarray, lost: np.ndarray, good: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """For the filtered span ``x`` (before the reference): which 2 s segments are kept (no lost
    sample, every good channel within the peak-to-peak limit after the average reference) and the
    Fourier coefficients from 1 to 45 Hz of the kept ones after the reference (Hann window, each
    segment's mean removed), as complex64 (kept segments, bins, channels)."""
    view = segment_view(x, fs)
    length = int(round(SEGMENT_SECONDS * fs))
    starts = segment_starts(x.shape[0], fs)
    index, _ = pass_bins(fs)
    w = np.hanning(length)
    kept = np.zeros(len(view), dtype=bool)
    lost_sum = np.concatenate([[0], np.cumsum(lost.astype(np.int64))])
    lost_any = (lost_sum[starts + length] - lost_sum[starts]) > 0 if len(starts) else np.zeros(0, dtype=bool)
    out = []
    for s in range(0, len(view), SEGMENT_BATCH):
        seg = view[s : s + SEGMENT_BATCH].astype(np.float64)  # (k, channels, samples)
        if good.any():
            seg -= seg[:, good, :].mean(axis=1, keepdims=True)
            ptp = seg[:, good, :].max(axis=2) - seg[:, good, :].min(axis=2)
            ok = ~(ptp > PEAK_TO_PEAK_UV).any(axis=1)
        else:
            ok = np.zeros(len(seg), dtype=bool)
        ok &= ~lost_any[s : s + SEGMENT_BATCH]
        kept[s : s + SEGMENT_BATCH] = ok
        if ok.any():
            chosen = seg[ok]
            chosen -= chosen.mean(axis=2, keepdims=True)
            coef = np.fft.rfft(chosen * w, axis=2)[:, :, index]
            out.append(np.transpose(coef, (0, 2, 1)).astype(np.complex64))
    coef = np.concatenate(out, axis=0) if out else np.zeros((0, len(index), x.shape[1]), dtype=np.complex64)
    return kept, coef


def welch_coefficients(coef: np.ndarray, fs: float) -> np.ndarray:
    """Welch's spectrum (uV^2/Hz, one-sided) from the coefficients of ``segment_coefficients``:
    (bins, channels); NaN without segments. Equal to ``welch`` at those bins."""
    w = np.hanning(int(round(SEGMENT_SECONDS * fs)))
    if len(coef) == 0:
        return np.full(coef.shape[1:], np.nan)
    power = np.zeros(coef.shape[1:])
    for s in range(0, len(coef), SEGMENT_BATCH):
        c = coef[s : s + SEGMENT_BATCH].astype(np.complex128)
        power += (c.real**2 + c.imag**2).sum(axis=0)
    return power / len(coef) / (fs * float(w @ w)) * 2.0


def robust_sd(x: np.ndarray) -> np.ndarray:
    """1.4826 times the median absolute deviation, per column."""
    med = np.median(x, axis=0)
    return 1.4826 * np.median(np.abs(x - med), axis=0)


def judge_channels(clipped: np.ndarray, sd: np.ndarray, loud: np.ndarray, labels: list[str]):
    """Bad channels from each channel's fraction of samples at the converter's limit, robust
    standard deviation (uV, after the filter) and fraction of loud 2 s segments (after the filter,
    before the reference), all over the analyzed data: ``(bad, {label: reason})``."""
    bad = np.zeros(len(labels), dtype=bool)
    reasons: dict[str, str] = {}
    for i, label in enumerate(labels):
        if clipped[i] > CLIP_FRACTION:
            bad[i], reasons[label] = True, f"at the converter's limit in {clipped[i] * 100:.1f} % of samples"
        elif sd[i] < FLAT_UV:
            bad[i], reasons[label] = True, f"flat (robust SD {sd[i]:.2f} uV)"
        elif loud[i] > LOUD_FRACTION:
            bad[i], reasons[label] = True, f"above {PEAK_TO_PEAK_UV:.0f} uV peak-to-peak in {loud[i] * 100:.0f} % of 2 s segments"
    return bad, reasons


def rereferenced(x: np.ndarray, good: np.ndarray) -> np.ndarray:
    """``x`` minus the average of the good channels at each sample."""
    if not good.any():
        return x.copy()
    return x - x[:, good].mean(axis=1, keepdims=True)


def segment_starts(n: int, fs: float) -> np.ndarray:
    length = int(round(SEGMENT_SECONDS * fs))
    step = int(round(STEP_SECONDS * fs))
    if n < length:
        return np.zeros(0, dtype=np.int64)
    return np.arange(0, n - length + 1, step, dtype=np.int64)


def segments(x: np.ndarray, fs: float) -> np.ndarray:
    """The 2 s segments of ``x`` every 1 s: shape (segments, samples, channels)."""
    length = int(round(SEGMENT_SECONDS * fs))
    starts = segment_starts(x.shape[0], fs)
    if len(starts) == 0:
        return np.zeros((0, length, x.shape[1]))
    index = starts[:, None] + np.arange(length)[None, :]
    return x[index]


def welch(seg: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """Mean one-sided power spectral density of the segments (segments, samples, channels) in
    uV^2/Hz, with a Hann window: returns ``(freqs, psd)`` with psd (freqs, channels)."""
    length = seg.shape[1]
    w = np.hanning(length)
    freqs = np.fft.rfftfreq(length, 1.0 / fs)
    if len(seg) == 0:
        return freqs, np.full((len(freqs), seg.shape[2]), np.nan)
    centred = seg - seg.mean(axis=1, keepdims=True)
    spectra = np.fft.rfft(centred * w[None, :, None], axis=1)
    power = (np.abs(spectra) ** 2).mean(axis=0) / (fs * float(w @ w))
    power[1:-1] *= 2.0  # one-sided: every bin but 0 and Nyquist holds both halves
    return freqs, power


def band_power(freqs: np.ndarray, psd: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    """Sum of the spectrum over ``band[0] <= f < band[1]`` times the bin width: uV^2 per channel."""
    df = freqs[1] - freqs[0]
    mask = (freqs >= band[0]) & (freqs < band[1])
    return psd[mask].sum(axis=0) * df


def mains_check(x: np.ndarray, fs: float) -> dict:
    """How far the spectrum at 50 and at 60 Hz stands above its neighbours (1-3 Hz away), as the
    median over channels of the peak-to-neighbour power ratio, on unfiltered data."""
    n = min(x.shape[0], int(60 * fs))
    if n < int(4 * fs):
        return {"ratio_50": None, "ratio_60": None}
    seg = x[:n] - x[:n].mean(axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    power = np.abs(np.fft.rfft(seg * np.hanning(n)[:, None], axis=0)) ** 2
    out = {}
    for f in (50.0, 60.0):
        peak = np.abs(freqs - f) <= 0.5
        side = (np.abs(freqs - f) >= 1.0) & (np.abs(freqs - f) <= 3.0)
        ratio = power[peak].max(axis=0) / np.maximum(power[side].mean(axis=0), 1e-30)
        out[f"ratio_{int(f)}"] = float(np.median(ratio))
    return out

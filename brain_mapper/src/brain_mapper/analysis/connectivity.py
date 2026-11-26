"""Connectivity: the debiased estimator of the squared weighted phase-lag index (dwPLI), and
which links stand above time-shifted copies of the same data.

Vinck M, Oostenveld R, van Wingerden M, Battaglia F, Pennartz CMA (2011). An improved index of
phase-synchronization for electrophysiological data in the presence of volume-conduction, noise
and sample-size bias. NeuroImage 55:1548-1565. https://doi.org/10.1016/j.neuroimage.2011.01.055

For channels i and j, at each frequency f, over K segments with Fourier coefficients X_k:
    I_k = Im(X_ik(f) * conj(X_jk(f)))
    dwPLI(f) = ((sum_k I_k)^2 - sum_k I_k^2) / ((sum_k |I_k|)^2 - sum_k I_k^2)
The value for a band is the mean of dwPLI(f) over the band's 0.5 Hz bins.

What it means: how consistently one channel's signal leads or lags the other's at that frequency
across segments, weighted by the size of the imaginary part of the cross-spectrum. A zero-lag
relation (the same source seen by both electrodes through volume conduction, or a common
reference) has no imaginary part and adds nothing. What it does not mean: it is not a measure of
how strongly two brain regions communicate, of direction, or of anatomical connection; at 20 scalp
channels the nodes are electrodes, not brain regions, and a lagged mixture of sources can still
show up. The estimator is near 0 without a consistent lag and can be slightly negative by chance.

How much data (``MIN_SEGMENTS``): for two independent channels of 1/f noise (true value 0), the
95th percentile of the band value over 300 simulations per number of kept 2 s segments (1 s
apart) was, at 20 and at 30 segments: delta 0.157 and 0.078, theta 0.092 and 0.070, alpha 0.104
and 0.063, beta 0.059 and 0.035, low gamma 0.060 and 0.037 (``test_the_minimum_of_clean_data``
repeats this for alpha). 30 is the fewest of the counts tried (10, 15, 20, 30, 40, 60, 90) at
which a true-zero pair stays under 0.1, a tenth of the scale, in 95 % of cases in every band.
With fewer kept segments no value is given. A sliding window is therefore 40 s long, so that
it holds 39 segments and can lose 9 to artifacts.

Which links are drawn (``surrogate_threshold``): a surrogate shifts each good channel's sequence
of kept segments by its own whole number of segments, the shifts of any two channels at least 2
apart (segments overlap by 1 s, so a difference of 0 or 1 would keep shared samples in line).
That keeps each channel's own spectrum and breaks any consistent relation between channels. For
each surrogate the largest dwPLI over all pairs of good channels is taken (pairs whose shifts
could not be kept 2 apart are left out of that surrogate); a link is drawn only if its dwPLI is
above the 95th percentile of those largest values. With no relation at all, then, a round shows
any link at all in about 5 % of cases (a rate for the whole round, not per link).
"""

from __future__ import annotations

import numpy as np

from .spectra import BANDS

#: Fewest kept 2 s segments a value is computed from (see the module text).
MIN_SEGMENTS = 30
WINDOW_SECONDS = 40.0
WINDOW_STEP_SECONDS = 5.0
#: Surrogates per round and per span of windows.
SURROGATES = 100
WINDOW_SURROGATES = 40
#: Segments whose cross-spectra are formed at once (bounds the memory used).
BATCH = 32


def band_bins(freqs: np.ndarray, band: tuple[float, float]) -> np.ndarray:
    return np.flatnonzero((freqs >= band[0]) & (freqs < band[1]))


def per_bin(coef: np.ndarray) -> np.ndarray:
    """dwPLI at every bin from Fourier coefficients (segments, bins, channels): shape (bins, ch, ch).
    The sums over segments are formed ``BATCH`` segments at a time."""
    k, nb, nc = coef.shape
    total = np.zeros((nb, nc, nc))
    absolute = np.zeros((nb, nc, nc))
    squares = np.zeros((nb, nc, nc))
    for s in range(0, k, BATCH):
        c = coef[s : s + BATCH]
        im = np.imag(c[:, :, :, None] * np.conj(c[:, :, None, :]))
        total += im.sum(axis=0)
        absolute += np.abs(im).sum(axis=0)
        squares += (im * im).sum(axis=0)
    numerator = total**2 - squares
    denominator = absolute**2 - squares
    with np.errstate(invalid="ignore", divide="ignore"):
        value = np.where(denominator > 1e-30, numerator / denominator, 0.0)
    value[:, np.arange(nc), np.arange(nc)] = 0.0
    return value


def by_band(values: np.ndarray, freqs: np.ndarray, bands=None) -> dict[str, np.ndarray]:
    """The mean over each band's bins of per-bin values (bins, ch, ch)."""
    return {name: values[band_bins(freqs, band)].mean(axis=0) for name, band in (bands or BANDS).items()}


def dwpli(coef: np.ndarray, freqs: np.ndarray, bands=None) -> dict[str, np.ndarray] | None:
    """The dwPLI matrix per band of the kept segments' coefficients; ``None`` with fewer than
    ``MIN_SEGMENTS`` segments."""
    if len(coef) < MIN_SEGMENTS:
        return None
    return by_band(per_bin(coef), freqs, bands)


def spaced_shifts(k: int, n: int, rng: np.random.Generator) -> np.ndarray:
    """``n`` circular shifts of a sequence of ``k``, any two at least 2 apart (around the circle)
    when ``k >= 2 n``; otherwise independent random shifts."""
    if k < 2 * n:
        return rng.integers(0, k, size=n)
    extra = rng.multinomial(k - 2 * n, np.full(n, 1.0 / n))
    positions = int(rng.integers(0, k)) + np.concatenate([[0], np.cumsum(2 + extra)[:-1]])
    return rng.permutation(positions % k)


def apart(shifts: np.ndarray, k: int) -> np.ndarray:
    """For every pair of channels: whether their shifts are at least 2 apart around the circle."""
    d = np.abs(shifts[:, None] - shifts[None, :]) % k
    return np.minimum(d, k - d) >= 2


def surrogate_threshold(
    coef: np.ndarray, freqs: np.ndarray, good: np.ndarray, count: int, rng: np.random.Generator, length: int | None = None, bands=None, tick=None
) -> dict[str, float | None]:
    """Per band: the 95th percentile over ``count`` surrogates of the largest dwPLI between good
    channels (see the module text). ``coef`` are the kept segments in order; with ``length``,
    each surrogate uses ``length`` consecutive segments per channel (the size of a window)."""
    bands = bands or BANDS
    index = np.flatnonzero(good)
    k = len(coef)
    use = length or k
    if len(index) < 2 or use < MIN_SEGMENTS or k < use:
        return {name: None for name in bands}
    sub = coef[:, :, index]
    upper = np.triu_indices(len(index), 1)
    maxima: dict[str, list[float]] = {name: [] for name in bands}
    rows = np.arange(use)
    for _ in range(count):
        shifts = spaced_shifts(k, len(index), rng)
        valid = apart(shifts, k)[upper]
        if not valid.any():
            continue
        take = (rows[:, None] + shifts[None, :]) % k  # (use, channels)
        shifted = np.take_along_axis(sub, take[:, None, :], axis=0)
        for name, m in by_band(per_bin(shifted), freqs, bands).items():
            maxima[name].append(float(m[upper][valid].max()))
        if tick is not None:
            tick()
    return {name: (float(np.percentile(v, 95)) if v else None) for name, v in maxima.items()}


def window_members(starts_s: np.ndarray, kept: np.ndarray, seg_seconds: float) -> tuple[np.ndarray, list[np.ndarray]]:
    """Window starts (s) every ``WINDOW_STEP_SECONDS`` over a span whose segments start at
    ``starts_s``, and for each window the positions, among the span's kept segments, of the kept
    segments lying wholly inside ``[t, t + WINDOW_SECONDS)``."""
    if len(starts_s) == 0:
        return np.zeros(0), []
    last_end = float(starts_s[-1]) + seg_seconds
    window_starts = np.arange(0.0, max(last_end - WINDOW_SECONDS, -1.0) + 1e-9, WINDOW_STEP_SECONDS)
    position = np.cumsum(kept) - 1
    members = []
    for t in window_starts:
        inside = (starts_s >= t - 1e-9) & (starts_s + seg_seconds <= t + WINDOW_SECONDS + 1e-9) & kept
        members.append(position[inside])
    return window_starts, members

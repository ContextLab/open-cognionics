"""The signal check's functions on synthetic signals (``brain_mapper.quality``)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from brain_mapper import contact, quality

FS = 500.0
VPC = 9.934107462565105e-08  # volts per count at gain 3 (docs/protocol.md, "Scaling")
N = int(quality.SIGNAL_S * FS)


def eeg_like(n=N, rms=10e-6, seed=0, channels=1):
    """Noise with most of its power at low frequencies (1/f-like), scaled to ``rms`` in 1-45 Hz."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal((n, channels))
    spectrum = np.fft.rfft(white, axis=0)
    freqs = np.fft.rfftfreq(n, 1 / FS)
    spectrum[1:] /= np.sqrt(freqs[1:])[:, None]
    spectrum[0] = 0
    x = np.fft.irfft(spectrum, n=n, axis=0)
    passed = quality.band_pass(x, FS)
    return x * rms / passed.std(axis=0)


def quantize(x):
    return np.round(x / VPC) * VPC


def states_of(x, labels=None):
    labels = labels or [f"C{k}" for k in range(x.shape[1])]
    return quality.check(quantize(x), labels, FS, VPC)


def test_full_scale_is_2_to_the_23_counts():
    assert quality.full_scale(VPC) == pytest.approx(0.8333333, rel=1e-6)


def test_clean_eeg_like_noise_is_fine():
    result = states_of(eeg_like(channels=4))
    assert set(result["states"].values()) == {"fine"}
    assert result["not_fine"] == []
    for values in result["values"].values():
        assert 5 < values["rms_uv"] < 15
        assert values["ptp_uv"] < 200


def test_one_blink_leaves_a_channel_fine():
    # a blink: 250 uV, about 0.25 s wide
    x = eeg_like()
    t = np.arange(N) / FS
    x[:, 0] += 250e-6 * np.exp(-0.5 * ((t - 3.0) / 0.1) ** 2)
    assert states_of(x)["states"] == {"C0": "fine"}


def test_saturated_channel_is_clipped():
    x = eeg_like(channels=2)
    x[:, 1] = -quality.full_scale(VPC)
    result = states_of(x)
    assert result["states"] == {"C0": "fine", "C1": "clipped"}
    assert result["not_fine"] == ["C1"]
    assert result["values"]["C1"]["rail_share"] == 1.0


def test_more_than_one_percent_at_the_limit_is_clipped_and_less_is_not():
    x = eeg_like(channels=2)
    x[:40, 0] = 0.995 * quality.full_scale(VPC)  # 1.3 % of 3000 samples
    x[:20, 1] = 0.995 * quality.full_scale(VPC)  # 0.7 %
    assert states_of(x)["states"]["C0"] == "clipped"
    assert states_of(x)["states"]["C1"] != "clipped"


def test_just_under_the_margin_is_not_clipped():
    x = eeg_like()
    x[:, 0] += 0.98 * quality.full_scale(VPC)
    assert states_of(x)["states"] == {"C0": "fine"}


def test_flat_channel_is_flat():
    x = eeg_like(channels=2)
    x[:, 1] = 0.0123  # a constant level
    result = states_of(x)
    assert result["states"]["C1"] == "flat"
    assert result["states"]["C0"] == "fine"


def test_a_channel_that_moves_by_one_count_now_and_then_is_flat():
    x = np.full((N, 1), 1000 * VPC)
    x[::50, 0] += VPC
    assert states_of(x)["states"] == {"C0": "flat"}


def test_large_slow_drift_alone_is_fine():
    t = np.arange(N) / FS
    x = eeg_like()
    x[:, 0] += 5e-3 * np.sin(2 * np.pi * 0.15 * t) + 2e-3 * t / t[-1]
    assert states_of(x)["states"] == {"C0": "fine"}


def test_drift_into_the_limit_is_clipped():
    t = np.arange(N) / FS
    x = eeg_like()
    x[:, 0] += quality.full_scale(VPC) * t / t[-1]
    assert states_of(x)["states"] == {"C0": "clipped"}


def test_strong_60_hz_is_measured_and_taken_out_so_the_channel_is_fine():
    t = np.arange(N) / FS
    x = eeg_like(channels=2)
    x[:, 1] += 5e-3 * np.sin(2 * np.pi * 60 * t + 0.3)
    result = states_of(x)
    assert result["states"] == {"C0": "fine", "C1": "fine"}
    assert result["values"]["C1"]["mains_uv"] == pytest.approx(5000, rel=0.01)
    assert result["values"]["C0"]["mains_uv"] < 5
    # what is left of it is no larger than the channel without it
    assert result["values"]["C1"]["ptp_uv"] < 1.2 * result["values"]["C0"]["ptp_uv"] + 10


def test_strong_50_hz_is_measured_too():
    t = np.arange(N) / FS
    x = eeg_like()
    x[:, 0] += 1e-3 * np.sin(2 * np.pi * 50 * t)
    result = states_of(x)
    assert result["states"] == {"C0": "fine"}
    assert result["values"]["C0"]["mains_uv"] == pytest.approx(1000, rel=0.01)


def test_large_broadband_noise_is_noisy():
    x = eeg_like(channels=2)
    x[:, 1] = eeg_like(rms=100e-6, seed=3)[:, 0]
    result = states_of(x)
    assert result["states"] == {"C0": "fine", "C1": "noisy"}
    assert result["values"]["C1"]["large_share"] == 1.0


def test_two_blinks_leave_a_channel_fine_and_four_bursts_make_it_noisy():
    t = np.arange(N) / FS
    x = eeg_like(channels=2)
    for at in (1.5, 4.5):  # each inside one piece
        x[:, 0] += 300e-6 * np.exp(-0.5 * ((t - at) / 0.1) ** 2)
    for at in (0.5, 1.5, 3.5, 5.5):
        x[:, 1] += 300e-6 * np.exp(-0.5 * ((t - at) / 0.05) ** 2)
    assert states_of(x)["states"] == {"C0": "fine", "C1": "noisy"}


def test_lost_samples_are_filled_and_too_many_are_refused():
    x = eeg_like()
    x[100:150] = np.nan
    assert states_of(x)["states"] == {"C0": "fine"}
    x[: N // 2 + 10] = np.nan
    with pytest.raises(ValueError):
        quality.check(x, ["C0"], FS, VPC)


def test_band_pass_removes_out_of_band_sines():
    t = np.arange(N) / FS
    inside = 20e-6 * np.sin(2 * np.pi * 10 * t)
    x = (inside + 1e-3 * np.sin(2 * np.pi * 0.2 * t) + 1e-4 * np.sin(2 * np.pi * 60 * t))[:, None]
    passed = quality.band_pass(x, FS)[:, 0]
    middle = slice(500, -500)
    # A drift of 1 mV leaves at most 1.5 % of its size away from the ends, 10 % near them (with 60 Hz on top).
    assert np.max(np.abs(passed[middle] - inside[middle])) < 15e-6
    assert np.max(np.abs(passed - inside)) < 100e-6
    clean = quality.band_pass(inside[:, None], FS)[:, 0]
    assert np.max(np.abs(clean - inside)) < 2e-6


def chunk(rows, on=False):
    return SimpleNamespace(data=np.asarray(rows), impedance_on=on, samples=len(rows))


def test_signal_window_keeps_only_tone_off_samples_after_begin():
    config = SimpleNamespace(labels=["A", "B"], sample_rate=FS, eeg_volts_per_count=VPC)
    window = quality.SignalWindow(config, seconds=1.0)
    x = quantize(eeg_like(n=2000, channels=2))
    window.push(chunk(x[:600]))  # before begin: not kept
    assert window.progress() == (0, 500) and window.result() is None
    window.begin()
    window.push(chunk(x[:300]))
    window.push(chunk(x[300:400], on=True))  # the tone came back: starts again
    assert window.progress() == (0, 500)
    window.push(chunk(x[400:700]))
    assert window.result() is None
    window.push(chunk(x[700:1000]))
    assert window.progress() == (500, 500)
    result = window.result()
    assert result["states"] == {"A": "fine", "B": "fine"} and result["seconds"] == 1.0
    window.end()
    assert window.result() is None and window.progress() == (0, 500)


def test_tone_seen_needs_a_median_tone_of_50_uv():
    assert contact.tone_seen(np.array([1e-3, 2e-4, 5e-5, 1e-6]))
    assert not contact.tone_seen(np.full(20, 10e-6))
    assert contact.tone_seen(np.array([np.inf, np.inf, 1e-6]))  # railed channels count as seen
    assert not contact.tone_seen(np.array([np.nan]))

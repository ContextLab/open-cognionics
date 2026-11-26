"""The contact measure (``brain_mapper.contact``) on recorded headset bytes.

The two recordings are the fixtures of ``tests/fixtures`` (see their README):
``impedance_on_2s.bin`` (headset on the head, contact check on) and ``onhead_10s.bin``
(headset on the head, eyes closed). Each is decoded with ``oc.replay`` from a session folder
made here: the fixture bytes as ``raw.bin`` beside the ``meta.json`` of a real recording
of the fixture configuration reply (``config_reply.bin``, played by the pty device).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import numpy as np
import pytest

import open_cognionics as oc
from brain_mapper import contact
from pty_device import FIXTURES, PtyDevice, config_reply, onhead_stream

#: How much larger the tone is, on every channel, with the contact check on than off.
#: Measured once over the whole of each fixture: the smallest ratio, channel by channel, was
#: 37.1. The factor asserted leaves room below that.
FACTOR = 10.0


@pytest.fixture(scope="module")
def fixture_meta(tmp_path_factory):
    """``meta.json`` of a real recording whose configuration came from ``config_reply.bin``."""
    out = tmp_path_factory.mktemp("meta") / "rec"
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        with oc.connect(dev.path) as headset:
            with headset.record(out, participant="P001"):
                time.sleep(0.5)
    finally:
        dev.hang_up()
    return json.loads((out / "meta.json").read_text())


def decode(name: str, meta: dict, tmp_path: Path):
    """The chunks of fixture ``name``, replayed from a session folder made for it."""
    folder = tmp_path / name.replace(".bin", "")
    folder.mkdir()
    shutil.copyfile(FIXTURES / name, folder / "raw.bin")
    (folder / "meta.json").write_text(json.dumps(meta))
    replay = oc.replay(folder)
    return replay.config, list(replay)


def rows(chunks) -> np.ndarray:
    return np.concatenate([np.asarray(c.data) for c in chunks])


def test_the_contact_check_is_on_in_the_impedance_fixture_and_off_in_the_onhead_one(
    fixture_meta, tmp_path
):
    _, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    _, off = decode("onhead_10s.bin", fixture_meta, tmp_path)
    assert on and all(c.impedance_on for c in on)
    assert off and not any(c.impedance_on for c in off)


def test_the_tone_is_larger_with_the_contact_check_on_on_every_channel(fixture_meta, tmp_path):
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    _, off = decode("onhead_10s.bin", fixture_meta, tmp_path)
    fs = config.sample_rate
    a_on = contact.tone_amplitude(rows(on), fs)
    a_off = contact.tone_amplitude(rows(off), fs)
    assert a_on.shape == a_off.shape == (len(config.labels),)
    assert np.all(a_on > FACTOR * a_off), np.round(a_on / a_off, 1).tolist()


def test_a_125_hz_sine_added_to_recorded_data_raises_the_result_by_its_amplitude(
    fixture_meta, tmp_path
):
    config, off = decode("onhead_10s.bin", fixture_meta, tmp_path)
    fs = config.sample_rate  # the rate of the configuration, not an assumed 500
    data = rows(off)
    base = contact.tone_amplitude(data, fs)
    amplitude = 1e-3  # volts
    n = np.arange(data.shape[0])[:, None]
    phase = np.linspace(0, np.pi, data.shape[1])[None, :]
    sine = amplitude * np.sin(2 * np.pi * 125.0 * n / fs + phase)
    raised = contact.tone_amplitude(data + sine, fs)
    assert np.all(np.abs((raised - base) - amplitude) <= 0.05 * amplitude), (raised - base).tolist()
    # Another frequency of the same size hardly moves it.
    other = amplitude * np.sin(2 * np.pi * 60.0 * n / fs)
    moved = contact.tone_amplitude(data + other, fs)
    assert np.all(np.abs(moved - base) <= 0.05 * amplitude)


def test_lost_samples_are_left_out_of_the_measure(fixture_meta, tmp_path):
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    data = rows(on)
    whole = contact.tone_amplitude(data, config.sample_rate)
    holed = data.copy()
    holed[100:110] = np.nan  # ten lost samples, as the decoder gives them
    result = contact.tone_amplitude(holed, config.sample_rate)
    assert np.all(np.isfinite(result))
    assert np.allclose(result, whole, rtol=0.05)


def test_classify_puts_values_on_either_side_of_each_threshold_in_the_right_class():
    t = contact.Thresholds(marginal=1e-3, bad=4e-3, version="test")
    values = np.array([0.0, 0.99e-3, 1.0e-3, 1.01e-3, 3.99e-3, 4.0e-3, 4.01e-3, np.inf, np.nan])
    assert contact.classify(values, t) == [
        "good", "good", "marginal", "marginal", "marginal", "bad", "bad", "bad", "bad",
    ]  # fmt: skip


def test_the_shipped_thresholds_are_ordered_and_versioned():
    t = contact.THRESHOLDS
    assert 0 < t.marginal < t.bad
    assert t.version and "provisional" in t.version


def test_the_tone_is_at_a_quarter_of_the_sample_rate():
    assert contact.tone_frequency(500) == 125.0 == contact.TONE_HZ
    assert contact.tone_frequency(1000) == 250.0
    assert contact.tone_frequency(300) == 75.0
    assert contact.window_samples(500) == 500 and contact.window_samples(1000) == 1000
    assert contact.window_samples(250.0) % 4 == 0


@pytest.mark.parametrize("rate", [250, 100, 0, -500, float("nan")])
def test_a_sample_rate_whose_quarter_is_among_the_brain_signal_cannot_be_measured(rate):
    with pytest.raises(contact.Unmeasurable, match="cannot be measured"):
        contact.tone_frequency(rate)


def test_the_monitor_says_plainly_when_the_rate_makes_the_measure_meaningless(fixture_meta, tmp_path):
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)

    class Slow:  # the recorded configuration with only its rate changed
        sample_rate = 200.0
        labels = config.labels
        eeg_volts_per_count = config.eeg_volts_per_count

    monitor = contact.ContactMonitor(Slow())
    assert "50 Hz" in monitor.unmeasurable and "cannot be measured" in monitor.unmeasurable
    for chunk in on:
        monitor.push(chunk)
    assert monitor.states() is None and monitor.amplitudes() is None
    assert contact.ContactMonitor(config).unmeasurable is None


def test_the_monitor_leaves_out_the_first_second_then_needs_one_more(fixture_meta, tmp_path):
    """The first second recorded with the check on holds the switch-on transient: it is left
    out (``settling``), and the states are those of a whole second after it."""
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    monitor = contact.ContactMonitor(config)
    second = contact.window_samples(config.sample_rate)
    assert len(rows(on)) >= 2 * second
    fed = 0
    assert monitor.settling and monitor.states() is None
    for chunk in on:
        if fed + chunk.samples >= 2 * second:
            break
        monitor.push(chunk)
        fed += chunk.samples
        assert monitor.states() is None
        assert monitor.settling == (fed < second)
        assert monitor.sample == chunk.index + chunk.samples
    assert fed < 2 * second
    for chunk in on[on.index(chunk):]:
        monitor.push(chunk)
    assert not monitor.settling
    states = monitor.states()
    assert states is not None and list(states) == config.labels
    # The states are those of the tone over the last second.
    last = rows(on)[-second:]
    expected = contact.classify(contact.tone_amplitude(last, config.sample_rate), contact.THRESHOLDS)
    assert list(states.values()) == expected
    assert set(states.values()) <= {"good", "marginal", "bad"}


def test_the_monitor_starts_again_when_the_contact_check_is_off(fixture_meta, tmp_path):
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    _, off = decode("onhead_10s.bin", fixture_meta, tmp_path)
    monitor = contact.ContactMonitor(config)
    for chunk in on:
        monitor.push(chunk)
    assert monitor.states() is not None
    monitor.push(off[0])  # a chunk recorded with the check off: the window is no use any more
    assert monitor.states() is None and monitor.settling
    for chunk in on:
        monitor.push(chunk)
    assert monitor.states() is not None
    monitor.reset()
    assert monitor.states() is None and monitor.settling


def test_a_channel_held_at_full_scale_is_bad(fixture_meta, tmp_path):
    """In the pilot recordings a channel without contact sits at the end of the converter's
    range (-833.3 mV with this configuration), where it carries no tone at all. Such a channel
    is bad, however small its tone."""
    config, on = decode("impedance_on_2s.bin", fixture_meta, tmp_path)
    data = rows(on)[-round(config.sample_rate) :].copy()
    full = 2**23 * config.eeg_volts_per_count
    data[:, 3] = -full
    data[:, 4] = 0.25  # flat, not at the rail
    amplitude = contact.channel_amplitude(data, config)
    states = contact.classify(amplitude, contact.THRESHOLDS)
    assert states[3] == "bad" and states[4] == "bad"
    assert np.isfinite(amplitude[0])


# -- the settling gate: how long the data have shown the check off, in samples ------------------


def chunked(flags: list[bool], boundaries: list[int], lost: dict[int, int] | None = None):
    """``(index, samples, impedance_on)`` of chunks cut from per-sample ``flags`` at ``boundaries``,
    as ``oc.Chunk`` gives them: the flag is that of the LAST sample of the chunk. ``lost`` maps a
    position in ``flags`` to a number of samples lost just before it (the index jumps)."""
    lost = lost or {}
    out = []
    shift = 0
    cuts = [0, *boundaries, len(flags)]
    for start, end in zip(cuts, cuts[1:]):
        shift += lost.get(start, 0)
        if end > start:
            out.append((start + shift, end - start, flags[end - 1]))
    return out


def gate_opens_at(counter: contact.OffCounter, chunks, rate: float) -> tuple[int, int]:
    """Feeds ``chunks`` and returns the end (sample number) of the chunk after which the counter
    first says the check has been off for the settling time, and the quiet it then gives."""
    need = contact.settle_samples(rate)
    for index, samples, on in chunks:
        counter.push(index, samples, on)
        if counter.quiet_samples() >= need:
            return index + samples, counter.quiet_samples()
    raise AssertionError("the gate never opened")


def test_the_settling_time_is_converted_to_samples_with_the_sample_rate():
    assert contact.settle_samples(500.0) == 1000
    assert contact.settle_samples(250.0) == 500
    assert contact.settle_samples(333.3) == 667  # rounded up: never less than the time
    assert contact.settle_samples(500.0, seconds=0.5) == 250


def test_a_chunk_whose_last_sample_is_off_can_hide_samples_with_the_check_on():
    """The case seen once in the calibration browser test (eyes open 989 samples after the last
    check-on sample): the stream joins reads into chunks of 25 samples or more, and a chunk's
    flag is that of its last sample. Here the check goes off 11 samples into the chunk that
    starts at 10000, so the last sample with the check on is 10010. Counting from the end of the
    last chunk flagged on (10000) the gate opened at 11000, 989 samples after 10011."""
    on_until = 10_011  # samples 0 .. 10010 have the check on
    flags = [i < on_until for i in range(13_000)]
    chunks = chunked(flags, list(range(25, 13_000, 25)))
    counter = contact.OffCounter()
    for index, samples, on in chunks:
        counter.push(index, samples, on)
        if index + samples == 11_000:
            break
    assert counter.quiet_samples() < 1000  # 989 samples after the last one on: not yet
    opened, _ = gate_opens_at(contact.OffCounter(), chunks, 500.0)
    assert opened - on_until >= 1000, opened - on_until


@pytest.mark.parametrize("seed", range(40))
def test_the_gate_never_opens_less_than_the_settling_time_after_the_last_sample_on(seed):
    """Random chunk sizes (1 to 120 samples, as reads under load join), random losses, the
    switch-off anywhere: the first chunk end at which the gate opens is always at least
    ``settle_samples`` after the last sample with the check on (or after the last lost sample
    that came right after one, which may have been on)."""
    rng = np.random.default_rng(seed)
    total = 6000
    on_until = int(rng.integers(1, 3000))
    flags = [i < on_until for i in range(total)]
    boundaries, at = [], 0
    while True:
        at += int(rng.integers(1, 121))
        if at >= total:
            break
        boundaries.append(at)
    lost = {b: int(rng.integers(1, 30)) for b in boundaries if rng.random() < 0.1}
    chunks = chunked(flags, boundaries, lost)
    # Sample numbers of the stream count the lost samples: a sample at position k of ``flags``
    # has the number k + (samples lost at or before k).
    def shift(k: int) -> int:
        return sum(v for b, v in lost.items() if b <= k)

    truth = on_until + shift(on_until - 1)  # just after the last sample known to be on
    if on_until in lost:  # samples lost right after it may have been on too
        truth = on_until + shift(on_until)
    opened, quiet = gate_opens_at(contact.OffCounter(), chunks, 500.0)
    assert opened - truth >= contact.settle_samples(500.0), (opened, truth, quiet)


def test_the_counter_says_nothing_has_been_quiet_before_any_data_or_after_a_reset():
    counter = contact.OffCounter()
    assert counter.quiet_samples() == 0
    counter.push(0, 25, False)
    counter.push(25, 2000, False)
    assert counter.quiet_samples() >= 1000
    counter.reset()
    assert counter.quiet_samples() == 0
    counter.push(100, 25, True)
    counter.push(125, 25, True)
    assert counter.quiet_samples() == 0

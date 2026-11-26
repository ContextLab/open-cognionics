"""The figures and the thinned-out rows the device screen is drawn from (``brain_mapper.display``).

The numbers come from a real recording's chunks where they can; the rest is arithmetic on rows
made here, with the expected values worked out by hand.
"""

from __future__ import annotations

import numpy as np
import open_cognionics as oc
import pytest

from brain_mapper import display


def test_ten_rows_at_500_hz_make_one_row_at_50_hz_in_microvolts():
    thinner = display.Decimator(500.0, 3)
    assert thinner.factor == 10
    # volts: channel 0 counts 0..9 microvolts, channel 1 is constant 5 uV, channel 2 is -1 uV
    block = np.column_stack([np.arange(10) * 1e-6, np.full(10, 5e-6), np.full(10, -1e-6)])
    out = thinner.push(np.tile(block, (5, 1)))  # 50 rows -> 5 output rows: the flush size
    assert out is not None and len(out) == display.FLUSH_ROWS
    assert out[0] == [4.5, 5.0, -1.0]


def test_rows_wait_across_chunks_until_a_whole_group_and_a_whole_message():
    thinner = display.Decimator(500.0, 1)
    ones = np.full((25, 1), 2e-6)
    assert thinner.push(ones) is None  # 2 whole groups (5 rows over): fewer than a message
    out = thinner.push(ones)  # 30 rows: 3 more groups, 5 in all
    assert out == [[2.0]] * display.FLUSH_ROWS
    assert thinner.push(ones) is None  # nothing is repeated


def test_rows_of_lost_samples_are_left_out_of_the_mean_and_a_lost_group_repeats_the_last():
    thinner = display.Decimator(500.0, 1)
    good = np.full((10, 1), 3e-6)
    half = np.full((10, 1), 4e-6)
    half[5:] = np.nan
    lost = np.full((10, 1), np.nan)
    out = thinner.push(np.vstack([good, half, lost, good, good]))
    assert out == [[3.0], [4.0], [4.0], [3.0], [3.0]]


def test_the_output_is_json_with_no_nan():
    import json

    thinner = display.Decimator(500.0, 2)
    rows = np.full((50, 2), np.nan)
    out = thinner.push(rows)
    assert json.dumps(out, allow_nan=False)


def test_real_chunks_give_50_rows_a_second_of_20_channels(recorded_session):
    replay = oc.replay(recorded_session, realtime=False)
    thinner = display.Decimator(replay.config.sample_rate, len(replay.config.labels))
    rows = 0
    samples = 0
    for chunk in replay:
        samples += chunk.samples
        out = thinner.push(chunk.data)
        if out:
            assert all(len(r) == 20 for r in out)
            rows += len(out)
        if samples >= 5000:
            break
    assert abs(rows - samples / 10) <= display.FLUSH_ROWS + 1


def test_the_rate_is_rows_received_over_the_window_and_lost_rows_do_not_count():
    stats = display.LinkStats()
    for i in range(40):  # 2 s of 50-sample chunks, every 50 ms: 25 received per chunk
        stats.add(100.0 + i * 0.05, 25, 0, 4.1, False)
    message = stats.message(102.0, since=100.0)
    assert message["t"] == "status" and message["rate"] == pytest.approx(500.0, rel=0.05)
    assert message["lost"] == 0 and message["lost10"] == 0 and message["bat"] == 4.1
    stats.add(102.0, 25, 5, 4.1, True)  # 5 of the 25 rows were lost
    message = stats.message(102.0, since=100.0)
    assert message["lost"] == 5 and message["lost10"] == 5 and message["imp"] is True


def test_the_rate_is_unknown_in_the_first_second_and_lost_samples_leave_the_ten_second_window():
    stats = display.LinkStats()
    stats.add(10.0, 25, 3, 4.1, False)
    assert stats.message(10.5, since=10.0)["rate"] is None
    assert stats.message(15.0, since=10.0)["lost10"] == 3
    after = stats.message(20.5, since=10.0)
    assert after["lost10"] == 0 and after["lost"] == 3  # the total stays; the tip goes


def test_a_battery_reading_that_is_not_a_number_is_null_not_nan():
    import json

    stats = display.LinkStats()
    stats.add(1.0, 25, 0, float("nan"), False)
    message = stats.message(3.0, since=1.0)
    assert message["bat"] is None and json.dumps(message, allow_nan=False)

"""``oc.connect`` returns or raises within its documented limit, whatever the device does.

Every connection here is made in a child process that the test kills if it outlives the
limit by a wide margin, so a connect that hangs fails the test instead of stalling the run.
The devices are real pseudo-terminals from ``pty_device.py``; no test opens a real serial port.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pty_device import (
    BACKLOG_SECONDS,
    PtyDevice,
    config_reply,
    onhead_stream,
    truncated_config_reply,
)

HERE = Path(__file__).resolve().parent
#: ``connect`` returns or raises within this many seconds of being called (docs/device.md).
CONNECT_LIMIT = 8.0
#: Time allowed on top of the limit for the child Python to start and import the library.
CHILD_START = 10.0

CHILD = textwrap.dedent(
    """
    import json, sys, time
    import open_cognionics as oc

    path, seconds = sys.argv[1], float(sys.argv[2])
    started = time.time()
    t0 = time.monotonic()
    try:
        h = oc.connect(path)
    except oc.Error as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error),
                          "elapsed": time.monotonic() - t0}), flush=True)
        sys.exit(0)
    elapsed = time.monotonic() - t0
    chunks = []
    end = time.monotonic() + seconds
    with h:
        for chunk in h.stream(timeout=2.0):
            chunks.append({"index": chunk.index, "samples": chunk.samples,
                           "lost": chunk.lost, "time": chunk.time})
            if time.monotonic() >= end:
                break
    print(json.dumps({"elapsed": elapsed, "started": started, "chunks": chunks}), flush=True)
    """
)


def connect_in_child(path: str, stream_seconds: float = 0.0) -> dict:
    """Runs ``oc.connect(path)`` in a child process, then reads the stream for
    ``stream_seconds``; returns what the child reports. Fails if the child outlives the limit."""
    allowed = CONNECT_LIMIT + stream_seconds + CHILD_START
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, path, str(stream_seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        out, _ = child.communicate(timeout=allowed)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        pytest.fail(f"connect to {path} was still running after {allowed:.0f} s")
    assert child.returncode == 0, out
    return json.loads(out.strip().splitlines()[-1])


def test_connect_after_30_s_of_unread_streaming_is_prompt_and_gives_recent_samples():
    device = PtyDevice(onhead_stream(), config_reply())
    try:
        time.sleep(30)  # the headset streams; nobody reads the port
        result = connect_in_child(device.path, stream_seconds=2.0)
        written = device.written()
        lag = device.lag_at_config
        dropped = device.dropped_packets
    finally:
        device.hang_up()
    facts = {"written": written, "lag": lag, "dropped": dropped, **result}
    facts["chunks"] = result.get("chunks", [])[:3]
    assert "error" not in result, facts
    # The first request was answered, so connect took about the 0.3 s discard and no resend.
    assert written == bytes([0x14]), facts
    assert result["elapsed"] < CONNECT_LIMIT
    assert result["elapsed"] < 0.3 + 1.5, facts
    # Recent: what the caller gets is what followed the configuration reply, and when the
    # reply was played the device was no more than 0.1 s behind its own clock: the backlog
    # (at most BACKLOG_SECONDS, the rest of the 30 s was lost unread) had been discarded.
    assert dropped >= (30 - BACKLOG_SECONDS - 1) * 500, facts
    assert lag is not None and lag <= 0.1, facts
    chunks = result["chunks"]
    assert chunks, result
    # Host times: the first sample is from after connect was called, not from the backlog.
    assert chunks[0]["time"] >= result["started"], facts
    # Continuous: sample indices follow on, with no sample lost. They count from the reply;
    # the library's reader starts before ``stream()`` subscribes, so the first chunk may begin
    # a few samples in (well under 0.2 s), never with an old backlog.
    assert chunks[0]["index"] < 100, facts
    for before, after in zip(chunks, chunks[1:]):
        assert after["index"] == before["index"] + before["samples"]
    assert all(c["lost"] == 0 for c in chunks)
    assert sum(c["samples"] for c in chunks) >= 2.0 * 500 * 0.8


def test_a_configuration_reply_that_never_ends_raises_no_configuration_within_the_limit():
    device = PtyDevice(onhead_stream(), truncated_config_reply())
    try:
        result = connect_in_child(device.path)
        written = device.written()
    finally:
        device.hang_up()
    # The reply to the first request never ends: given up 5 s after that request (about
    # 5.3 s), before the overall limit, so the message is the one for unanswered requests.
    assert result.get("error") == "NoConfiguration", result
    assert "did not answer 1 configuration requests" in result["message"], result
    assert result["elapsed"] < CONNECT_LIMIT
    assert written == bytes([0x14])


def test_a_reply_that_starts_late_and_never_ends_is_cut_off_at_the_limit():
    # The first three requests go unanswered; the fourth (about 4.8 s in) gets a reply that
    # starts and never ends. Its own 5 s would run to about 9.8 s: the 8 s limit ends the wait.
    device = PtyDevice(onhead_stream(), truncated_config_reply(), ignore_requests=3)
    try:
        result = connect_in_child(device.path)
        written = device.written()
    finally:
        device.hang_up()
    assert result.get("error") == "NoConfiguration", result
    assert "within the 8 s limit (4 configuration requests sent)" in result["message"], result
    # At the limit, give or take one read (20 ms) and scheduling: 0.5 s.
    assert CONNECT_LIMIT <= result["elapsed"] < CONNECT_LIMIT + 0.5, result
    assert written == bytes([0x14] * 4)


@pytest.mark.parametrize("bytes_per_second", [37_500, 2_000_000])
def test_a_device_streaming_garbage_raises_no_configuration_within_the_limit(bytes_per_second):
    device = PtyDevice.garbage_device(bytes_per_second)
    try:
        result = connect_in_child(device.path)
        written = device.written()
    finally:
        device.hang_up()
    assert result.get("error") == "NoConfiguration", result
    assert result["elapsed"] < CONNECT_LIMIT
    assert written and set(written) == {0x14}


def test_a_device_that_never_takes_the_request_raises_within_the_limit():
    # The device streams but never reads what is written to it, as when the dongle's flow
    # control holds the computer's output: the request stays queued and is never answered.
    device = PtyDevice(onhead_stream(), config_reply(), deaf=True)
    try:
        result = connect_in_child(device.path)
        written = device.written()
    finally:
        device.hang_up()
    assert result.get("error") == "NoConfiguration", result
    assert result["elapsed"] < CONNECT_LIMIT
    # On giving up, connect discards what the device never took, so it may read nothing.
    assert set(written) <= {0x14}

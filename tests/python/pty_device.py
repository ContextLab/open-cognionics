"""Plays recorded headset bytes through a real OS pseudo-terminal.

The Python counterpart of ``tests/common/pty_device.rs``. The library opens
the slave side as a serial port; everything it does to the port (open,
termios, reads, writes, close) happens for real.

The device side plays ``stream`` in a loop, one whole packet at a time, at
``bytes_per_second``. Every byte the library writes is logged. When the
library writes ``0x14`` the configuration reply is inserted between two
packets, as the headset does.

The only byte of the recording this harness alters is the packet counter
(byte 1 of each packet, modulo 128). It is rewritten so counters stay
continuous when the stream loops and across the inserted configuration
reply. With ``lose_every=n`` the counter skips one value after every
``n``-th packet, which the decoder reports as one lost sample.

With ``contact_stream`` the device answers the contact-check bytes as the headset does: after
``0x11`` it plays ``contact_stream`` (a recording made with the check on, whose packets say so),
after ``0x12`` it plays ``stream`` again. ``contact_on`` is the state it starts in (the test
headset comes on with its check on).

Like a serial link, the two directions are independent: the bytes the
library writes are read (and requests noticed) even while the stream waits
for room in the pty. Stream bytes that are due but not yet played stand for
bytes the headset has sent and the computer holds: like the computer's serial
buffer, they are at most ``BACKLOG_SECONDS`` of stream (a headset never waits
for its reader, so when the port is left unread for longer the oldest packets
are lost, their counter values skipped as when real packets are lost), and a
configuration reply comes after them, never ahead of them.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import struct
import termios
import threading
import time
import tty
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
#: Bytes per packet in the recorded stream (20 EEG + 3 ACC channels).
PACKET_LEN = 75
#: The byte rate of the recorded stream: 500 packets of 75 bytes a second.
REAL_RATE = 37_500
#: The byte that asks the headset for its configuration.
SEND_CONFIGURATION = 0x14
#: The bytes that switch the headset's contact check on and off.
CONTACT_ON, CONTACT_OFF = 0x11, 0x12
#: Packets written per pacing step at most, so requests are noticed quickly.
MAX_BATCH = 20
#: A stream packet is written only while the pty's input queue has this much room for it. The
#: queue holds 1022 bytes on macOS (more elsewhere); a write that does not fit is taken in
#: part, which would cut a packet in two, and a reader that starts a recording between the two
#: parts then holds a file that begins in the middle of a packet.
QUEUE_ROOM = 900
#: Seconds of stream held for a reader that is behind; older packets are lost. A real
#: computer's serial buffer is bounded too (its size for the dongle on macOS is not known);
#: five seconds is long enough that a reader stopped for a couple of seconds (a loaded
#: machine, or a test that stops the reading process for 2 s) loses nothing, and short
#: enough that a port left unread for minutes does not hold minutes of old data.
BACKLOG_SECONDS = 5.0
_MARKER = b"\xff" * 4
_END_WORD = 1_234_567_890


def onhead_stream() -> bytes:
    """5000 whole packets recorded from a worn Quick-20, counters contiguous."""
    return (FIXTURES / "onhead_10s.bin").read_bytes()


def config_reply() -> bytes:
    """The configuration reply alone (marker through end word), cut out of
    ``tests/fixtures/config_reply.bin``."""
    data = (FIXTURES / "config_reply.bin").read_bytes()
    start = data.index(_MARKER)
    at = start + len(_MARKER)
    while at + 4 <= len(data):
        word = int.from_bytes(data[at : at + 4], "big")
        at += 4
        if word == _END_WORD:
            return data[start:at]
    raise ValueError("no end word in config_reply.bin")


def truncated_config_reply() -> bytes:
    """The configuration reply without its end word: a reply that starts and never ends."""
    return config_reply()[:-4]


def garbage_stream() -> bytes:
    """Bytes that are not the headset's format, the same on every call: a fixed pseudo-random
    sequence in which every 25th byte starts a run of four ``0xFF`` (a configuration marker
    out of place). The bytes of a configuration's end word never occur in it, so the runs
    never make a configuration."""
    out = bytearray()
    state = 0x2545F491
    while len(out) < 500 * PACKET_LEN:
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((state >> 16) & 0xFF)
        if len(out) % 25 == 0:
            out.extend(_MARKER)
    out = out[: 500 * PACKET_LEN]
    assert _END_WORD.to_bytes(4, "big") not in out
    return bytes(out)


class PtyDevice:
    """A pseudo-terminal that behaves like the headset's serial port."""

    def __init__(
        self,
        stream: bytes,
        config: bytes | None,
        bytes_per_second: int = REAL_RATE,
        ignore_requests: int = 0,
        silent: bool = False,
        lose_every: int | None = None,
        contact_stream: bytes | None = None,
        contact_on: bool = False,
        deaf: bool = False,
    ) -> None:
        assert stream and len(stream) % PACKET_LEN == 0
        assert contact_stream is None or (contact_stream and len(contact_stream) % PACKET_LEN == 0)
        #: Never reads what the library writes, like a dongle whose flow control holds the
        #: computer's output: the bytes stay queued in the pty and no request is answered.
        self._deaf = deaf
        self._stream = stream
        self._contact_stream = contact_stream
        #: Whether the contact stream is being played (only with ``contact_stream``).
        self.contact_on = bool(contact_on and contact_stream)
        self._config = config
        self._rate = bytes_per_second
        self._ignore = ignore_requests
        self._silent = silent
        self._lose_every = lose_every
        self._lock = threading.Lock()
        self._written = bytearray()
        self._requests = 0
        self._played = bytearray()
        self._after_config: int | None = None
        #: Stream packets played before the last configuration reply.
        self.packets_before_config: int | None = None
        #: How late, in seconds, the stream was when the last configuration reply was played:
        #: the age of the oldest packet then due and not yet played (0 when none was). The
        #: packets after the reply are no older than this.
        self.lag_at_config: float | None = None
        #: Stream packets lost because nobody read the port for longer than
        #: ``BACKLOG_SECONDS``.
        self.dropped_packets = 0
        #: Host time (``time.time()``) the pacing started: stream packet ``g``
        #: (0-based, counting every packet played) is due ``g / 500`` s later
        #: at ``REAL_RATE``. Only meaningful if the device is never paused and
        #: dropped no packet.
        self.wall_start: float | None = None
        self._stop = threading.Event()
        # ``hold_next_packet``: a packet written in two parts on purpose.
        self._hold: int | None = None
        self._held = threading.Event()
        self._release = threading.Event()
        #: Index in the stream (0-based, modulo its length) of the packet that was held.
        self.held_packet: int | None = None
        self._paused = threading.Event()
        #: Set by the device thread each time it finds itself silent (see ``go_silent``).
        self._quiet_seen = threading.Event()
        #: ``time.monotonic()`` of the last ``go_silent``.
        self.went_silent_at: float | None = None
        #: Packet counter values to skip when the stream plays again (see ``speak_again``).
        self._skip = 0
        #: Counter values the last ``speak_again`` skipped (before the modulo 128).
        self.last_skip = 0
        self._master, self._slave = os.openpty()
        # Raw mode before anyone opens the slave: a fresh pty echoes input
        # and translates bytes, which would corrupt both directions.
        tty.setraw(self._slave)
        os.set_blocking(self._master, False)
        self.path = os.ttyname(self._slave)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @classmethod
    def silent_device(cls) -> "PtyDevice":
        """A device that never sends a byte, whatever is written to it."""
        return cls(bytes(PACKET_LEN), None, 1, silent=True)

    @classmethod
    def garbage_device(cls, bytes_per_second: int = REAL_RATE) -> "PtyDevice":
        """A device that streams bytes that are not the headset's format, full of four-byte
        runs of ``0xFF`` (the configuration marker), and never answers a request."""
        return cls(garbage_stream(), None, bytes_per_second)

    @classmethod
    def lossy_device(cls, lose_every: int = 10) -> "PtyDevice":
        """A device that plays the recorded stream and skips one packet counter value after
        every ``lose_every``-th packet, so the decoder reports lost samples (about
        ``500 / lose_every`` a second)."""
        return cls(onhead_stream(), config_reply(), lose_every=lose_every)

    def stop_losing(self) -> None:
        """From now on the stream is played whole again (see ``lossy_device``)."""
        self._lose_every = None

    def written(self) -> bytes:
        """Every byte the library has written to the port so far."""
        self._drain()
        with self._lock:
            return bytes(self._written)

    def played(self) -> bytes:
        """Every byte played so far."""
        with self._lock:
            return bytes(self._played)

    def played_after_config(self) -> bytes | None:
        """Bytes played after the end of the last configuration reply."""
        with self._lock:
            if self._after_config is None:
                return None
            return bytes(self._played[self._after_config :])

    def hold_next_packet(self, offset: int) -> None:
        """Writes the next stream packet in two parts: its first ``offset`` bytes now, the rest
        only after ``release_held_packet``. ``wait_held`` says when the first part is written."""
        assert 0 < offset < PACKET_LEN
        self._held.clear()
        self._release.clear()
        self._hold = offset

    def wait_held(self, timeout: float = 5.0) -> bool:
        """Waits until the first part of the held packet has been written and read by the library."""
        if not self._held.wait(timeout):
            return False
        deadline = time.monotonic() + timeout
        while self._queued() > 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        return self._queued() == 0

    def release_held_packet(self) -> None:
        """Lets the second part of the held packet go."""
        self._release.set()

    def pause(self) -> None:
        """Stop playing stream packets; the port stays open."""
        self._paused.set()

    def go_silent(self, timeout: float = 5.0) -> float:
        """From now on send nothing at all, as a headset that was switched off, ran flat or went
        out of range while its dongle stays plugged in: no stream packet and no configuration
        reply (a request written meanwhile is read and never answered). The port stays open and
        what the library writes is still read. Returns ``time.monotonic()`` just after the last
        packet was written (the device thread has seen the change)."""
        self._quiet_seen.clear()
        self._silent = True
        assert self._quiet_seen.wait(timeout), "the device thread did not go silent"
        self.went_silent_at = time.monotonic()
        return self.went_silent_at

    def speak_again(self, missed: bool = True) -> None:
        """Ends ``go_silent``: the stream plays again (and requests are answered). With
        ``missed`` (a headset that kept sampling while out of range) the packet counter skips
        the packets due during the silence, as when real packets are lost, so the decoder
        reports lost samples (their number modulo 128, as the counter is); without it the
        counter goes on from where it stopped."""
        if missed and self.went_silent_at is not None:
            self._skip = int((time.monotonic() - self.went_silent_at) * self._rate) // PACKET_LEN
            self.last_skip = self._skip
        self._silent = False

    def hang_up(self) -> None:
        """Stop playing and close the device side, as when the dongle is unplugged."""
        self._stop.set()
        self._thread.join()
        self._drain()
        with self._lock:
            for name in ("_master", "_slave"):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)

    def _drain(self) -> None:
        with self._lock:
            if self._master is None:
                return
            while True:
                try:
                    data = os.read(self._master, 256)
                except (BlockingIOError, OSError):
                    # EAGAIN: nothing more queued. EIO: the slave side is not open.
                    return
                if not data:
                    return
                self._requests += data.count(SEND_CONFIGURATION)
                self._written.extend(data)
                if self._contact_stream is not None:
                    for byte in data:
                        if byte in (CONTACT_ON, CONTACT_OFF):
                            self.contact_on = byte == CONTACT_ON

    def host_bytes_waiting(self) -> bool:
        """Whether the library has written bytes that the device side has not read yet (it
        reads none of them when ``deaf``); looks without taking them."""
        with self._lock:
            if self._master is None:
                return False
            readable, _, _ = select.select([self._master], [], [], 0)
            return bool(readable)

    def _queued(self) -> int:
        """Bytes written to the pty and not yet read by the library."""
        return struct.unpack(
            "i", fcntl.ioctl(self._slave, termios.FIONREAD, b"\0\0\0\0")
        )[0]

    def _play(self, data: bytes, is_config: bool) -> bool:
        off = 0
        while off < len(data):
            if self._stop.is_set():
                return False
            with self._lock:
                if self._master is None:
                    return False
                room = is_config or self._queued() + len(data) <= QUEUE_ROOM
                try:
                    # Without room for the whole packet nothing is written: the reader is behind
                    # (a loaded machine), and the pacing catches up afterwards.
                    n = os.write(self._master, data[off:]) if room else None
                except BlockingIOError:
                    n = None
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        raise
                    n = None
                if n:
                    # Recorded as written (a configuration reply may go in parts).
                    self._played.extend(data[off : off + n])
                    off += n
            if not n:
                # The other direction keeps flowing while this one waits, as on a serial
                # link: what the library writes is taken now and answered after this write.
                if not self._deaf:
                    self._drain()
                time.sleep(0.001)
        if is_config:
            with self._lock:
                self._after_config = len(self._played)
        return True

    def _take_request(self) -> bool:
        if self._deaf:
            return False
        self._drain()
        with self._lock:
            if self._requests == 0:
                return False
            self._requests -= 1
            return True

    def _run(self) -> None:
        nxt = 0
        counter = 0
        played_packets = 0
        start = time.monotonic()
        self.wall_start = time.time()
        paced = 0
        # A request waiting for its answer: the stream bytes due when it arrived. They left
        # the headset before the request reached it (the computer holds them), so the reply
        # comes after them, as on the real link.
        answer_after: int | None = None
        while not self._stop.is_set():
            streaming = not (self._silent or self._paused.is_set())
            if answer_after is None and self._take_request():
                answer_after = int((time.monotonic() - start) * self._rate) if streaming else 0
            if answer_after is not None and (not streaming or paced > answer_after):
                answer_after = None
                if self._ignore > 0:
                    self._ignore -= 1
                elif not self._silent and self._config is not None:
                    owed = int((time.monotonic() - start) * self._rate) - paced
                    lag = max(0, owed) / self._rate
                    if not self._play(self._config, True):
                        return
                    self.packets_before_config = played_packets
                    self.lag_at_config = lag
            if self._silent or self._paused.is_set():
                if self._silent:
                    self._quiet_seen.set()
                time.sleep(0.001)
                start = time.monotonic()
                paced = 0
                continue
            if self._skip:
                counter = (counter + self._skip) % 128
                self._skip = 0
            due = int((time.monotonic() - start) * self._rate)
            # Paced against the start time: after a stall (a starved thread
            # or a full pty) the device catches up, MAX_BATCH packets per
            # step, so the bytes played match the elapsed time. Only the last
            # BACKLOG_SECONDS are caught up; older packets are lost.
            owed = due - paced
            limit = int(BACKLOG_SECONDS * self._rate)
            if owed > limit:
                lost = (owed - limit) // PACKET_LEN
                self.dropped_packets += lost
                counter = (counter + lost) % 128
                # The packets of the stream being played now (with or without the contact check).
                packets = len(self._contact_stream if self.contact_on else self._stream) // PACKET_LEN
                nxt = (nxt + lost) % packets
                paced += lost * PACKET_LEN
            batch = 0
            while paced <= due and batch < MAX_BATCH:
                if self._contact_stream is not None:
                    self._drain()  # a contact byte written now changes the next packet
                stream = self._contact_stream if self.contact_on else self._stream
                packets = len(stream) // PACKET_LEN
                nxt %= packets
                at = nxt * PACKET_LEN
                packet = bytearray(stream[at : at + PACKET_LEN])
                packet[1] = counter
                if self._hold is not None:
                    cut, self._hold = self._hold, None
                    if not self._play(bytes(packet[:cut]), False):
                        return
                    self.held_packet = nxt
                    self._held.set()
                    while not self._release.wait(0.001):
                        if self._stop.is_set():
                            return
                    rest = bytes(packet[cut:])
                else:
                    rest = bytes(packet)
                if not self._play(rest, False):
                    return
                played_packets += 1
                step = 1
                if self._lose_every and played_packets % self._lose_every == 0:
                    step = 2
                counter = (counter + step) % 128
                nxt = nxt + 1
                paced += PACKET_LEN
                batch += 1
            time.sleep(0.001)

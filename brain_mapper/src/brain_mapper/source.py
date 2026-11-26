"""Chunk sources: the live headset and a replayed recording behind one interface.

Both sources read on one thread of their own and hand every chunk to each
subscriber through a bounded queue. A subscriber whose queue is full loses
that chunk (as in the Rust server); the source never waits for a reader. The
last item a subscriber gets is a ``SourceEnded``.

The reader thread starts with the first ``subscribe``.
"""

from __future__ import annotations

import json
import queue
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import open_cognionics as oc

#: Size of the queue between the library's reader and our reader thread, in
#: chunks. Our thread only passes chunks on, so it is rarely more than a few full.
READER_QUEUE = 4096
#: Samples joined into one live chunk at least (50 ms at 500 Hz).
LIVE_MIN_SAMPLES = 25
#: Seconds between two attempts to open the headset, counted from the end of an attempt.
RETRY_SECONDS = 2.0
#: Seconds with no sample from an open headset link after which the link counts as lost (the
#: headset streams 500 samples a second, so this long with none is not a gap but a silent link:
#: a headset switched off, run flat or out of range while its dongle stays plugged in). The same
#: limit is how long an attempt to open the headset waits for its first sample after the
#: configuration reply: a link counts as up only once samples flow.
SILENT_SECONDS = 3.0
#: A gap of at least this many seconds between two chunks of an open link that is shorter than
#: ``SILENT_SECONDS`` is noted in ``events.jsonl`` (``link_stall``): the link is kept, and the gap
#: shows in the data as lost samples, counted by the packet counter modulo 128.
STALL_SECONDS = 1.0
#: ``SourceEnded.reason`` (and ``link_lost``'s ``reason``) of a link ended because it was silent.
SILENT = "silent"
#: Files of a session folder that a replayed recording carries over.
SESSION_FILES = ("raw.bin", "meta.json", "clock.jsonl", "recording.bdf")


class LinkLost(RuntimeError):
    """``start_recording`` on a link that has ended (or on a source that was stopped).

    The caller treats it as a drop of the link, to be tried again, and not as a failure to record.
    """


@dataclass(frozen=True)
class SourceEnded:
    """The last item of every subscription: why the source stopped."""

    reason: str


@dataclass(frozen=True)
class LinkProblem:
    """Why the headset could not be opened, or why its link ended, as the page is told.

    ``code`` is one of ``no_headset`` (``oc.NoHeadset``), ``several_headsets``
    (``oc.SeveralHeadsets``; ``ports`` lists them), ``open_error`` (``oc.OpenError``),
    ``no_data`` (``oc.NoData``), ``no_configuration`` (``oc.NoConfiguration``),
    ``disconnected`` (``oc.Disconnected``, or a source that ended) and ``other``.
    ``kind`` is the name of the exception class and ``msg`` its text.
    """

    code: str
    kind: str
    msg: str
    ports: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"code": self.code, "kind": self.kind, "msg": self.msg, "ports": list(self.ports)}


_CODES = (
    (oc.NoHeadset, "no_headset"),
    (oc.SeveralHeadsets, "several_headsets"),
    (oc.OpenError, "open_error"),
    (oc.NoData, "no_data"),
    (oc.NoConfiguration, "no_configuration"),
    (oc.Disconnected, "disconnected"),
)


def describe(error: BaseException) -> LinkProblem:
    """The ``LinkProblem`` of an exception raised while opening or reading the headset."""
    text = str(error)
    for kind, code in _CODES:
        if isinstance(error, kind):
            ports: tuple[str, ...] = ()
            if code == "several_headsets" and ": " in text:
                ports = tuple(p.strip() for p in text.split(": ", 1)[1].split(",") if p.strip())
            return LinkProblem(code, type(error).__name__, text, ports)
    return LinkProblem("other", type(error).__name__, text)


def reason_problem(reason: str) -> LinkProblem:
    """The problem a ``SourceEnded.reason`` of a live source stands for (a lost link)."""
    if reason == SILENT:
        return LinkProblem("disconnected", "Disconnected", f"no samples from the headset for {SILENT_SECONDS:g} s")
    return LinkProblem("disconnected", "Disconnected", reason)


class _Source:
    kind: Literal["live", "replay"]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._ended: SourceEnded | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._position = 0
        self._config: oc.Config | None = None

    @property
    def config(self) -> oc.Config:
        """The ``oc.Config`` of the source; available after ``open``."""
        if self._config is None:
            raise RuntimeError("the source is not open")
        return self._config

    @property
    def position(self) -> int:
        """Samples read so far (lost samples count)."""
        return self._position

    @property
    def ended(self) -> SourceEnded | None:
        """Why the source stopped, or ``None`` while it has not."""
        return self._ended

    def subscribe(self, maxsize: int) -> queue.Queue:
        """A new queue of up to ``maxsize`` chunks, ending with a ``SourceEnded``."""
        sub: queue.Queue = queue.Queue(maxsize)
        with self._lock:
            if self._ended is not None:
                sub.put_nowait(self._ended)
                return sub
            self._subscribers.append(sub)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="source")
                self._thread.start()
        return sub

    def _publish(self, chunk: oc.Chunk) -> None:
        self._position = chunk.index + chunk.samples
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(chunk)
            except queue.Full:
                pass

    def _end(self, reason: str) -> None:
        """Tells every subscriber, even one whose queue is full."""
        with self._lock:
            if self._ended is not None:
                return
            self._ended = SourceEnded(reason)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            while True:
                try:
                    sub.put_nowait(self._ended)
                    break
                except queue.Full:
                    try:
                        sub.get_nowait()
                    except queue.Empty:
                        pass

    def _run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Stops reading, closes a recording in progress and releases the source."""
        raise NotImplementedError


class LiveSource(_Source):
    """The headset on a serial port (``None``: the only headset plugged in)."""

    kind = "live"

    def __init__(self, port: str | None) -> None:
        super().__init__()
        self._port = port
        self._headset: oc.Headset | None = None
        self._recording: oc.Recording | None = None
        self._rec_dir: Path | None = None
        self._rec_lock = threading.Lock()
        self._stopped = False
        #: Set, under ``_rec_lock``, when the reader has finished: no recording may start after.
        self._over = False
        self._rec_from = 0
        #: Samples the library decoded while the last recording was open, *including* the ones
        #: the device reported as lost (``Headset.sample_index`` counts both), or ``None`` when
        #: not known, while it is open, or after ``reset``. The start index is read just after
        #: the recording is registered, so the figure can be a few packets short: it is
        #: informational (it goes into ``session.json``); nothing is decided or deleted by it.
        self.last_recording_decoded: int | None = None
        #: ``(start, end)`` on ``time.monotonic()`` of each attempt ``connect_with_retry`` made
        #: (the last 100): what a test, or an operator, can check the 2 s spacing against.
        self.attempts: list[tuple[float, float]] = []
        #: Why ``drop_link`` ended the link (the reader's ``SourceEnded.reason``), or ``None``.
        self._cut: str | None = None

    def open(self) -> None:
        """Connects and reads the configuration; raises the ``oc`` errors unchanged.

        If ``stop`` was called while this was waiting for the headset, the
        connection is closed again and ``RuntimeError`` is raised.
        """
        headset = oc.connect(self._port)
        with self._lock:
            if self._stopped:
                headset.close()
                raise RuntimeError("stopped while opening")
            self._headset = headset
            self._config = headset.config

    def connect_with_retry(self, report: Callable[[LinkProblem, int], None]) -> bool:
        """Calls ``open`` until it works; returns ``True`` once it has, ``False`` if ``stop`` came first.

        One attempt at a time, on the calling thread, so two opens never run together. A failed
        attempt is told to ``report(problem, attempt)`` (``attempt`` counts from 1) and the next
        one starts ``RETRY_SECONDS`` after the failed one ended: a slow attempt (a headset that
        is silent takes about 2 s to give up) is never followed at once by another, and the
        wait ends as soon as ``stop`` is called. A failed attempt leaves no port open.

        An attempt works only once samples flow: after the configuration reply it waits up to
        ``SILENT_SECONDS`` for the first chunk. A headset that answered and then sends nothing
        is one more failed attempt (``no_data``), its port closed and its reader ended.
        """
        attempt = 0
        while not self._stopping.is_set():
            attempt += 1
            began = time.monotonic()
            try:
                try:
                    self.open_streaming()
                finally:
                    self.attempts.append((began, time.monotonic()))
                    del self.attempts[:-100]
            except RuntimeError as error:
                if self._stopped:
                    return False
                problem = describe(error)
            except Exception as error:  # told to the page; the next attempt may do better
                problem = describe(error)
            else:
                return True
            report(problem, attempt)
            if self._stopping.wait(RETRY_SECONDS):
                return False
        return False

    def open_streaming(self) -> None:
        """``open``, then waits up to ``SILENT_SECONDS`` for the library to decode the first
        sample after the configuration reply (``Headset.sample_index``): a link counts as up only
        once samples flow. Normally the bytes that came with the reply hold samples and there is
        no wait. If none comes the port is closed (nothing is written), the source is left as
        before ``open`` and ``oc.NoData`` is raised; ``RuntimeError`` if ``stop`` came first.
        The errors of ``open`` are raised unchanged."""
        self.open()
        headset = self._headset
        assert headset is not None
        deadline = time.monotonic() + SILENT_SECONDS
        while not self._stopping.is_set():
            try:
                if headset.sample_index > 0:
                    return
            except Exception:
                break
            if time.monotonic() >= deadline:
                break
            self._stopping.wait(0.005)
        with self._lock:
            if self._headset is headset:
                self._headset = None
                self._config = None
        headset.close()
        if self._stopping.is_set():
            raise RuntimeError("stopped while opening")
        raise oc.NoData(
            f"the headset answered but sent no samples for {SILENT_SECONDS:g} s; check that it is "
            "switched on and in range of the dongle"
        )

    def drop_link(self, reason: str) -> None:
        """Ends the open link as a lost one: the port is closed, so the reader stops, closes the
        recording (whole and readable) and ends with ``SourceEnded(reason)``, as when the port
        fails. Writes nothing to the headset. Does nothing if the link has ended already or the
        source was stopped."""
        with self._lock:
            headset = self._headset
            if headset is None or self._ended is not None or self._stopped:
                return
            if self._cut is None:
                self._cut = reason
        headset.close()

    def reset(self) -> None:
        """Makes an ended connection ready for ``connect_with_retry`` again.

        Only valid once the source has ended (its reader thread has finished and the port is
        closed); raises ``RuntimeError`` otherwise, or after ``stop``.
        """
        with self._lock:
            if self._ended is None or self._stopped:
                raise RuntimeError("only a source that ended, and was not stopped, can be reset")
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        # A recording left open (a start that came after the reader finished) is closed here, so
        # nothing stale is carried into the next link.
        self.stop_recording()
        with self._rec_lock:
            self._over = False
            self.last_recording_decoded = None
        with self._lock:
            self._ended = None
            self._thread = None
            self._headset = None
            self._config = None
            self._subscribers = []
            self._position = 0
            self._cut = None

    def _run(self) -> None:
        assert self._headset is not None
        try:
            for chunk in self._headset.stream(queue=READER_QUEUE, min_samples=LIVE_MIN_SAMPLES):
                self._publish(chunk)
            reason = "stopped"
        except oc.Disconnected as error:
            text = str(error)
            reason = text if text.startswith("disconnected") else f"disconnected: {text}"
        except Exception as error:  # reported to the subscribers, not lost with the thread
            reason = f"{type(error).__name__}: {error}"
        if self._cut is not None:
            reason = self._cut  # ``drop_link`` closed the port: the link was lost, not stopped
        # The link is over: close the recording while the headset can still say
        # why (it is noted in the BDF file), then release the port. From here no recording
        # can start (``start_recording`` raises ``LinkLost``).
        with self._rec_lock:
            self._over = True
        try:
            self.stop_recording()
        finally:
            self._headset.close()
            self._end(reason)

    @property
    def recording(self) -> bool:
        """Whether a recording is in progress."""
        return self._recording is not None

    @property
    def decoded(self) -> int:
        """Samples the library has decoded so far (whole packets and lost ones since the
        configuration reply); the figure the reader itself keeps, not the chunks handed on."""
        if self._headset is None:
            raise RuntimeError("the source is not open")
        return self._headset.sample_index

    @property
    def released(self) -> bool:
        """Whether no port is held: not opened yet, or the headset is closed."""
        return self._headset is None or "closed" in repr(self._headset)

    def link_over(self) -> bool:
        """Whether the link has ended or the source was stopped: a recording cannot start."""
        return self._over or self._ended is not None or self._stopped

    def start_recording(self, directory: Path, participant: str, participant_uuid: str) -> None:
        """Records the raw bytes into ``directory`` until ``stop_recording``, ``stop``
        or the end of the link. Raises ``LinkLost`` if the link has ended (or ``stop`` was
        called), whether the reader noticed before this call or while it was starting."""
        if self._headset is None:
            raise RuntimeError("the source is not open")
        with self._rec_lock:
            if self.link_over():
                raise LinkLost("the headset link ended before the recording could start")
            if self._recording is not None:
                raise RuntimeError("already recording")
            self.last_recording_decoded = None  # a failed start leaves no figure of an earlier link
            try:
                self._recording = self._headset.record(directory, participant, participant_uuid)
            except Exception as error:
                if self.link_over() or isinstance(error, oc.Disconnected):
                    raise LinkLost(f"the headset link ended: {error}") from error
                raise
            self._rec_dir = Path(directory)
            try:
                self._rec_from = self._headset.sample_index
            except Exception:
                self._rec_from = 0

    def mark(self, label: str) -> int:
        """Puts a marker into the recording and returns the sample number it was given."""
        with self._rec_lock:
            if self._recording is None or self._rec_dir is None:
                raise RuntimeError("not recording")
            # The recorder returns the sample it wrote, so nothing is read back: constant time
            # however many markers the session has.
            return int(self._recording.mark(label))

    def stop_recording(self) -> None:
        """Closes the recording, if there is one."""
        with self._rec_lock:
            recording, self._recording = self._recording, None
            if recording is not None:
                try:
                    end = self._headset.sample_index if self._headset is not None else None
                except Exception:
                    end = None
                try:
                    recording.close()
                finally:
                    self.last_recording_decoded = (
                        None if end is None else max(0, end - self._rec_from)
                    )

    def set_contact_check(self, on: bool) -> None:
        """Switches the headset's impedance check on or off (bytes 0x11 / 0x12)."""
        if self._headset is None:
            raise RuntimeError("the source is not open")
        self._headset.impedance_check(on)

    def stop(self) -> None:
        """Stops reading, closes the recording and the port.

        The headset is closed first: that stops the library's reader, so every
        byte it received is in the recording, and then the recording is
        completed. Safe while ``open`` is still waiting: ``open`` then closes
        its connection and raises.
        """
        with self._lock:
            self._stopped = True
            headset = self._headset
        self._stopping.set()
        try:
            if headset is not None:
                headset.close()
        finally:
            try:
                self.stop_recording()
            finally:
                if self._thread is not None and self._thread is not threading.current_thread():
                    self._thread.join(timeout=5)
                self._end("stopped")


class ReplaySource(_Source):
    """A saved recording played back at the rate it was recorded.

    ``path`` is a session zip (its ``eeg/`` folder is used), a session folder
    (or one holding ``eeg/``), or a ``raw.bin`` with ``meta.json`` beside it.
    """

    kind = "replay"

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._folder: Path | None = None
        self._temp: Path | None = None
        self._replay: oc.Replay | None = None
        self._marks = None
        self._rec_lock = threading.Lock()

    def open(self) -> None:
        """Finds the recording and reads its configuration."""
        try:
            self._folder = self._resolve()
            self._replay = oc.replay(self._folder, realtime=True)
        except BaseException:
            self._remove_temp()
            raise
        self._config = self._replay.config

    def _remove_temp(self) -> None:
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None

    def _resolve(self) -> Path:
        path = self._path
        if not path.exists():
            raise FileNotFoundError(f"no such recording: {path}")
        if path.is_file() and zipfile.is_zipfile(path):
            self._temp = Path(tempfile.mkdtemp(prefix="brain-mapper-replay-"))
            with zipfile.ZipFile(path) as z:
                members = [m for m in z.namelist() if m.startswith("eeg/") and m != "eeg/"]
                if not members:
                    raise ValueError(f"{path} holds no eeg/ folder")
                z.extractall(self._temp, members)
            return self._temp / "eeg"
        if path.is_file():
            if path.name != "raw.bin":
                raise ValueError(f"{path} is neither a session zip, a folder nor a raw.bin")
            if not (path.parent / "meta.json").is_file():
                raise ValueError(
                    f"{path} has no meta.json beside it, so the headset configuration "
                    "that decodes its bytes is unknown; replay needs the session folder"
                )
            return path.parent
        for candidate in (path, path / "eeg"):
            if (candidate / "raw.bin").is_file():
                return candidate
        raise ValueError(f"no raw.bin in {path} (a session folder or its eeg/ folder is needed)")

    def _run(self) -> None:
        assert self._replay is not None
        try:
            for chunk in self._replay:
                if self._stopping.is_set():
                    return
                self._publish(chunk)
            reason = "end of recording"
        except Exception as error:
            reason = f"{type(error).__name__}: {error}"
        try:
            self.stop_recording()
        finally:
            self._end(reason)

    @property
    def recording(self) -> bool:
        """Whether a recording is in progress."""
        return self._marks is not None

    def start_recording(self, directory: Path, participant: str, participant_uuid: str) -> None:
        """Copies the source's session files unchanged into ``directory`` and
        starts a new ``markers.jsonl`` for the marks of this run.

        ``participant`` and ``participant_uuid`` are not written: ``meta.json``
        stays the source's own.
        """
        if self._folder is None:
            raise RuntimeError("the source is not open")
        with self._rec_lock:
            if self._marks is not None:
                raise RuntimeError("already recording")
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            taken = [n for n in (*SESSION_FILES, "markers.jsonl") if (directory / n).exists()]
            if taken:
                raise FileExistsError(f"{directory} already holds {', '.join(taken)}")
            for name in SESSION_FILES:
                if (self._folder / name).is_file():
                    shutil.copyfile(self._folder / name, directory / name)
            self._marks = open(directory / "markers.jsonl", "w", encoding="utf-8")

    def mark(self, label: str) -> int:
        """Writes a marker at the current ``position`` into the new ``markers.jsonl``
        and returns that sample number."""
        with self._rec_lock:
            if self._marks is None:
                raise RuntimeError("not recording")
            line = {"label": label, "sample": self._position, "unix_time": time.time()}
            self._marks.write(json.dumps(line) + "\n")
            self._marks.flush()
            return line["sample"]

    def stop_recording(self) -> None:
        """Closes the new ``markers.jsonl``, if there is one."""
        with self._rec_lock:
            marks, self._marks = self._marks, None
            if marks is not None:
                marks.close()

    def set_contact_check(self, on: bool) -> None:
        """Does nothing: a recording has no headset to ask."""

    def stop(self) -> None:
        self._stopping.set()
        try:
            self.stop_recording()
        finally:
            if self._thread is not None:
                self._thread.join(timeout=2)
            self._end("stopped")
            self._remove_temp()

"""The session engine: which step and block the session is in, the block timers,
pause, and the event log.

The engine is the only owner of time. A page never runs a block timer of its
own; it draws ``remaining`` from ``state()``.

Every state change and every event puts a marker ``"<step>/<name>#<seq>"``
into the recording through ``source.mark`` and appends one line to
``events.jsonl`` in the session folder, with the same ``seq`` (counting from 1).
The sample number of an event is the sample of the marker with that ``seq``.

A command either happens completely or not at all: the line is written, the
marker is put, and only then does the state stay changed. If either fails, the
line is taken back out and the state is as it was before the command.

``state()`` carries ``rev``, a number that grows by one with every change, so
that a receiver can discard a state older than one it already has.

Points: when a block ends the engine awards the block's ``points`` through the
ledger (reason ``block complete``), in the same all-or-nothing command that
ends the block. ``state()["points_total"]`` and ``blocks_done`` therefore change
only on the state whose phase is ``between``, and ``on_points`` is called, after
``on_change`` has been given that state, with the ``points`` message of the
websocket. While the phase is ``between``, ``state()["award"]`` names the block that
just ended and the points it earned, so a page that missed the ``points`` message can
show the gain. Nothing else awards points and nothing reads an EEG measure.

Scores: ``state()["scores"]`` is earned of possible per task (``brain_mapper.scores``), counted from
the trial events as their lines are saved. The page shows these; the ledger's points are not shown.

Skipping and stopping: ``skip`` ends the current round at once (the block that was running
is not finished, so it earns nothing: no ledger line, no ``points`` message), saves one
``skipped`` event and goes to the next step, waiting; ``stop`` ends the session in the
ordinary way (event ``stopped``, the recording closed, ``stopped`` true in ``state()``,
``failure`` unset). Neither can be done on the participant and device setup steps, ``skip``
not on the summary, and both are refused once the session is over. ``progress.json`` in the
session folder is saved at every change; see ``brain_mapper.progress``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from brain_mapper import contact, package, progress, quality, scores
from brain_mapper.flow import NO_POINTS, Step

LOG = logging.getLogger("brain_mapper")
#: Event names a page may send: they become part of a marker label.
EVENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: The form of a payload key and of a stimulus ``part`` that is used as it is in a behaviour file's
#: column names; any other is stored as sent and given a neutral column name there (see behavior.py).
PAYLOAD_KEY = re.compile(r"[a-z][a-z0-9_]{0,47}")

#: This server process, on every event line it writes: ``server_mono_ns`` of two lines can be
#: compared only when they have the same ``server_run``.
SERVER_RUN = secrets.token_hex(6)


def clock_fields(browser_time: float | None, time_origin: float | None) -> dict:
    """The times of an event line, read once when the line is written.

    * ``unix_time`` -- the server's wall clock at arrival, in seconds (``time.time``). It follows
      every adjustment of the computer's clock, so it can step back or forward; it says when, not
      how long between two events.
    * ``server_wall_ns`` -- the same reading in nanoseconds.
    * ``server_mono_ns`` -- the server's monotonic clock at the same moment (``time.monotonic_ns``):
      it never goes back within one ``server_run``, and measures intervals there.
    * ``server_run`` -- the process that wrote the line.
    * ``page_unix_ms`` -- the page's own time of the event, ``time_origin + browser_time`` (ms;
      ``null`` for the server's own events), on the page's clock, which is not the server's.
    """
    wall_ns = time.time_ns()
    mono_ns = time.monotonic_ns()
    page = None if browser_time is None or time_origin is None else float(time_origin) + float(browser_time)
    return {
        "unix_time": wall_ns / 1e9,
        "server_wall_ns": wall_ns,
        "server_mono_ns": mono_ns,
        "server_run": SERVER_RUN,
        "page_unix_ms": page,
    }

#: The reason recorded for the points a finished block earns.
BLOCK_COMPLETE = "block complete"

WAITING, RUNNING, PAUSED, BETWEEN, DONE = "waiting", "running", "paused", "between", "done"
#: Setup steps that can be neither skipped nor, for the first, stopped at.
PARTICIPANT_STEP = "participant"
DEVICE_STEP = "device"
CANNOT_SKIP = ("participant", "device")
#: The sub-steps of a calibration step, in order (spec 6.3). ``contact`` lasts until the person
#: continues; ``settling`` until the data show the check off (see ``SETTLE_S``); the next three
#: are timed by the engine, like blocks; ``done`` waits for Continue. ``not_off`` replaces
#: ``settling`` when the headset does not confirm the check is off: Check again or Stop.
CONTACT, SETTLING, NOT_OFF, EYES_OPEN, EYES_CLOSED, BLINKS, CALIBRATED = (
    "contact",
    "settling",
    "not_off",
    "eyes_open",
    "eyes_closed",
    "blinks",
    "done",
)
TIMED = (EYES_OPEN, EYES_CLOSED, BLINKS)
#: The signal check (``quality.py``): after settling, ``signal`` runs (phase running, like a
#: timed part) until ``quality.SIGNAL_S`` of data with the tone off have come; ``signal_done``
#: then shows its result with the contact states and waits for Continue (eyes open) or Check
#: again (the contact check, then both again). Only with a ``signal_window`` (the hub's).
SIGNAL, SIGNALED = "signal", "signal_done"
#: The parts a lost link pauses and Resume starts again.
PARTS = (*TIMED, SIGNAL)
#: The pause reason (and ``part_restarted`` reason) of a headset link lost during the session.
LINK_LOST = "link_lost"
#: The settling time (``contact.SETTLE_S``, where it is explained): the gate counts it in samples.
SETTLE_S = contact.SETTLE_S
#: Seconds the data may go on showing the check on after it was switched off: then ``not_off``.
OFF_LIMIT_S = 5.0
#: The file of the session folder that holds the result of the contact check.
CALIBRATION_FILE = "calibration.json"
#: Step kinds whose running block a pause abandons: it ends at once (``block_end`` with ``ended_by``
#: ``interrupted``) and Resume goes to the between-blocks screen, from which the next block starts.
ABANDONED_BY_PAUSE = ("task", "recall")
#: Step kinds whose running block a pause restarts: Resume goes back to the block's start screen
#: (the rest period is played again from its beginning). A video block is held and resumed.
RESTARTED_BY_PAUSE = ("rest",)
#: The ``ended_by`` of a block that a pause ended.
INTERRUPTED = "interrupted"
#: Seconds a task block (a step of kind ``task`` drawn by a page module) may run past its time
#: before the engine ends it: the page finishes the trial in progress and says so with
#: ``block_finished`` (reason ``trials_done``) when no further trial fits.
TASK_GRACE_S = 6.0
#: The reasons of ``block_finished``: ``participant_done`` (the participant says they are done; only
#: for a step whose flow entry has ``may_finish_early``) and ``trials_done`` (the page ran its last
#: trial; only for a step of kind ``task``).
PARTICIPANT_DONE, TRIALS_DONE = "participant_done", "trials_done"
#: The phases of a headset check opened from the paused screen: the contact check is on
#: (``contact``), then off and going off (``settling``, counted in samples as after calibration).
HC_CONTACT, HC_SETTLING = "contact", "settling"
#: Then, with the hub's signal window, the signal check runs as after the calibration's contact
#: check (``signal``, ``quality.SIGNAL_S`` of data with the tone off); its result is kept in
#: ``_hc_result`` (shown on the paused screen, added to the check's entry in calibration.json).
HC_SIGNAL = "signal"


def _serialized(method):
    """Runs a public command and the hand-out of what it changed under ``notify_lock``.

    Two commands, or a command and a tick, therefore never interleave between the
    moment a change is made and the moment it is handed to ``on_change``.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.notify_lock:
            return method(self, *args, **kwargs)

    return wrapper


class CommandRefused(ValueError):
    """The command is not allowed in the state the session is in."""


class BadEvent(CommandRefused):
    """A page event that can never be saved as sent (a bad name, payload key, part or time)."""


class EventLogFailed(ValueError):
    """``events.jsonl`` cannot be written, or a line cannot be taken back out of it.

    Once a line could not be taken back the log is unusable: every later command
    raises this at once, before anything is marked in the recording.
    """


class LedgerFailed(Exception):
    """The points ledger could not record the award of a block that ended."""


def _describe(error: Exception) -> str:
    """The reason a failure ends the session with; names both if both failed."""
    if isinstance(error, LedgerFailed):
        return f"points ledger failed: {error}"
    text = f"event log failed: {error}"
    cause = error.__context__
    while cause is not None:
        if isinstance(cause, LedgerFailed):
            return f"{text}; points ledger failed: {cause}"
        cause = cause.__context__
    return text


class Session:
    """One run through a flow.

    ``source`` must be recording: the session marks the start of the first step
    when it is created. ``folder`` is where ``events.jsonl`` is written.
    ``points`` is a ``points.Ledger`` (or ``None`` for a session without points);
    the engine attaches its ``phase`` and the source's sample count to it.
    ``clock`` gives seconds (``time.monotonic`` unless a test passes another).
    ``on_change``, when set, is called with ``state()`` after every command and
    every change a ``tick`` makes, outside the engine's lock; ``on_points``,
    when set, is then called with each ``points`` message that change brought.
    Both run under ``notify_lock``, so a receiver sees them in the order they
    happened; whoever else sends ``state()`` to the same receivers takes
    ``notify_lock`` too.
    """

    def __init__(
        self,
        flow: list[Step],
        source,
        points,
        clock: Callable[[], float] = time.monotonic,
        *,
        folder: Path,
        meta: dict | None = None,
    ) -> None:
        if not flow:
            raise ValueError("a session needs at least one step")
        self.flow = flow
        #: Saved in ``progress.json``: ``session`` (the folder name), ``uuid``, ``seed``, ``continues``,
        #: ``only`` (a round id, a list of round ids, or ``None``) and ``session_id`` (a random id of this session, without the label: the page keys
        #: its stored progress on it, so a later session in the same tab starts clean).
        self.meta = {
            "session": Path(folder).name,
            "uuid": None,
            "seed": None,
            "continues": None,
            "only": None,
            "session_id": secrets.token_hex(8),
        }
        self.meta.update(meta or {})
        self.stopped = False
        #: Whether a block has ever started: that needs a recording (``_require_recording``).
        self._rounds_begun = False
        self._finished: set[str] = set()
        self._skipped: set[str] = set()
        self._partial: dict[str, int] = {}  # step id -> blocks finished before it was left
        self._progress_failed: str | None = None
        self._progress_dirty = False
        self._progress_saved: dict | None = None  # what progress.json holds (without its time)
        self.source = source
        self.points = points
        self.clock = clock
        self.on_change: Callable[[dict], None] | None = None
        self.on_points: Callable[[dict], None] | None = None
        self.notify_lock = threading.RLock()
        self._pending: list[dict] = []
        self.ended_early = False
        self._lock = threading.RLock()
        self._folder = Path(folder)
        self._folder.mkdir(parents=True, exist_ok=True)
        #: Earned of possible per task, from the trial events saved (``brain_mapper.scores``); fed
        #: each event once its line is in ``events.jsonl`` (and the lines already there, if any).
        self._scores = scores.Scorer()
        for saved in scores.read_events(self._folder / "events.jsonl"):
            self._scores.add(saved)
        self._events = open(self._folder / "events.jsonl", "a", encoding="utf-8")
        self._seq = 0
        self._step = 0
        self._block = 0
        self._blocks_done = 0
        self._phase = WAITING
        self._elapsed = 0.0  # seconds of the running block already used
        self._since = 0.0  # clock reading when the block last started or resumed
        self._reason: str | None = None  # why the block is paused
        self._rev = 0
        self._changed = False
        self._last_sample: int | None = None
        self._failure: str | None = None  # why the session was ended by a failure
        self._events_unusable: str | None = None
        # Calibration (a step with ``calibration`` set): the sub-step, the ``seq`` of the
        # ``contact_start`` whose contact check should be on (``None``: off), and the result
        # saved when the contact sub-step was left.
        self._sub: str | None = None
        self._contact_on: int | None = None
        self._calibration: dict | None = None
        self._contact_used = False  # a contact check was on in this session
        self._settle_since = 0.0  # clock reading when ``settling`` began
        self._cues_logged: list[int] = []  # the blink cues the page has logged
        #: The headset link was lost after the device step and no recording is open until it is
        #: back (``link_lost`` / ``link_back``): events are saved without a marker meanwhile.
        self._link_down = False
        #: The calibration part (eyes open, eyes closed, blinks) that a lost link cut: it starts
        #: again from its beginning on resume, since a baseline with a gap is not a baseline.
        self._lost_part: str | None = None
        #: Why the calibration part in ``_lost_part`` starts again (the pause reason).
        self._lost_reason: str | None = None
        #: What the pause did to the block it interrupted: ``"between"`` (a task or recall block was
        #: ended; Resume goes to the between screen), ``"restart"`` (a rest block was ended; Resume
        #: goes back to its start screen) or ``None`` (the block is held, as the video is).
        self._abandoned: str | None = None
        #: Step id -> the ids of its blocks that a pause ended (``ended_by`` ``interrupted``).
        self._interrupted: dict[str, list[str]] = {}
        #: The headset check opened from the paused screen: ``HC_CONTACT``, ``HC_SETTLING`` or
        #: ``None``; ``_hc_on`` is the ``seq`` of its ``headset_check_start`` while the check is on.
        self._hc: str | None = None
        self._hc_on: int | None = None
        #: The last headset check's contact states and signal result (``{"states", "signal"}``),
        #: shown on the paused screen until Resume or a new check; ``_hc_states`` while it runs.
        self._hc_result: dict | None = None
        self._hc_states: dict = {}
        #: Gives ``{label: state}`` measured for the contact check of the ``contact_start`` whose
        #: ``seq`` it is given, or ``None`` before a measure (set by the hub).
        self.contact_states: Callable[[int], dict | None] | None = None
        #: Gives ``(samples, sample_rate)``: the samples of data received since the last one that
        #: was, or may have been, recorded with the contact check on; or ``None`` when the source
        #: does not say (set by the hub; see ``contact.OffCounter``).
        self.contact_quiet: Callable[[], tuple[int, float] | None] | None = None
        #: The hub's ``quality.SignalWindow``: with one, the signal check runs after settling.
        self.signal_window = None
        self._signal_left: float | None = None
        if points is not None:
            points.phase = lambda: self._phase
            # The sample of the marker that was just put; after the source has ended no
            # marker can be put and the sample is the number of samples read.
            points.sample = lambda: (
                self._last_sample if self._last_sample is not None else source.position
            )
        with self._lock:
            self._record("step_start", {})
            if self.current.calibration is not None:
                self._begin_contact()
            self._save_progress(strict=True)

    # -- reading --------------------------------------------------------------

    def device_pending(self) -> bool:
        """Whether the device step is still ahead or being shown: the session is at or before it.

        Until it has been passed the recording may not exist yet (or may have been closed by a
        lost link and not started again), so events are saved without a marker (``marked``
        false in their payload) and a live link that ended is not the end of the session.
        False for a flow without a device step, and once the session is over.
        """
        with self._lock:
            if self._phase == DONE:
                return False
            for index, step in enumerate(self.flow):
                if step.id == DEVICE_STEP:
                    return self._step <= index
            return False

    def _outcome(self, step: Step) -> str:
        if step.id in self._skipped:
            return "skipped"
        if step.id in self._partial:
            return "partial"
        if step.id in self._finished:
            return "finished"
        if step is self.current:
            if self._phase != DONE:
                return "running"
            # The session ended in this round (stopped, failed, source ended): what its
            # blocks earned stays earned, so a round with a finished block is never replayed.
            if step.blocks and self._blocks_done >= len(step.blocks):
                return "finished"
            if self._blocks_done > 0:
                return "partial"
        return "not_reached"

    def _blocks_finished(self, step: Step) -> int:
        """How many blocks of ``step`` finished (the page counts them in its progress bar)."""
        if step.id in self._partial:
            return self._partial[step.id]
        if step.id in self._skipped:
            return 0
        if step is self.current:
            return self._blocks_done
        return len(step.blocks) if step.id in self._finished else 0

    def _status(self) -> str:
        if self._failure:
            return "failed"
        if self.stopped:
            return "stopped"
        if self.ended_early:
            return "ended_early"
        return "finished" if self._phase == DONE else "running"

    def _progress_record(self) -> dict:
        return {
            "session": self.meta["session"],
            "uuid": self.meta["uuid"],
            "continues": self.meta["continues"],
            "only": self.meta["only"],
            "seed": self.meta["seed"],
            "session_id": self.meta["session_id"],
            "status": self._status(),
            "plan": [s.id for s in self.flow],
            "steps": [
                {"id": s.id, "kind": s.kind, "title": s.title, "outcome": self._outcome(s)}
                for s in self.flow
            ],
            "points_total": self.points.total if self.points is not None else 0,
        }

    def _save_progress(self, strict: bool = False) -> None:
        """Writes ``progress.json``. A failure is raised at the start (``strict``); later it is
        logged once per kind of failure and tried again at the next tick.

        A page event does not change the record, so only a change of the record is written: at a
        step or block boundary, a skip, a stop, the end or a failure. That keeps the fsync and
        the rename off the hundreds of events a block can have."""
        record = self._progress_record()
        if record == self._progress_saved:
            self._progress_failed = None
            self._progress_dirty = False
            return
        try:
            progress.write(self._folder, record)
        except OSError as error:
            if strict:
                raise
            text = error.strerror or type(error).__name__
            if text != self._progress_failed:
                LOG.error("progress.json could not be written (%s); trying again", text)
            self._progress_failed = text
            self._progress_dirty = True
        else:
            self._progress_saved = record
            self._progress_failed = None
            self._progress_dirty = False

    @property
    def current(self) -> Step:
        return self.flow[self._step]

    def _used(self) -> float:
        if self._phase == RUNNING:
            return self._elapsed + (self.clock() - self._since)
        return self._elapsed

    def state(self) -> dict:
        """What a page draws: the step, block, phase and the time left."""
        with self._lock:
            step = self.current
            block = step.blocks[self._block] if self._block < len(step.blocks) else None
            if self._phase == PAUSED and self._abandoned is not None:
                remaining: float | None = None  # the block was ended by the pause
            elif self._phase in (RUNNING, PAUSED) and block is not None:
                remaining = max(0.0, block.seconds - self._used())
            elif self._phase == BETWEEN:
                remaining = 0.0
            elif self._phase == WAITING and block is not None:
                remaining = block.seconds
            else:
                remaining = None
            sub = self._sub if step.calibration is not None else None
            if sub in TIMED and self._phase in (RUNNING, PAUSED):
                remaining = max(0.0, self._sub_seconds() - self._used())
            elif sub == SIGNAL and self._phase in (RUNNING, PAUSED):
                remaining = self._signal_left if self._signal_left is not None else quality.SIGNAL_S
            # The block that just ended earned exactly its ``points`` (the award is made in the
            # same command that ends the block), so the award is read from the flow.
            cut = block is not None and block.id in self._interrupted.get(step.id, ())
            award = (
                {
                    "step": step.id,
                    "block": block.id,
                    "points": block.points if self.points is not None and not cut else 0,
                    "interrupted": cut,
                }
                if self._phase == BETWEEN and block is not None
                else None
            )
            return {
                "award": award,
                "step": step.id,
                "kind": step.kind,
                "block": block.id if block is not None else None,
                "phase": self._phase,
                "remaining": remaining,
                "blocks_done": self._blocks_done,
                "blocks_total": len(step.blocks),
                "points_total": self.points.total if self.points is not None else 0,
                "scores": self._scores.result(),
                "ended_early": self.ended_early,
                "stopped": self.stopped,
                "failure": self._failure,
                "reason": self._reason if self._phase == PAUSED else None,
                "plan": [s.id for s in self.flow],
                "outcomes": {s.id: self._outcome(s) for s in self.flow},
                "blocks_by_step": {s.id: self._blocks_finished(s) for s in self.flow},
                "substep": sub,
                "contact_check": self._contact_check(),
                "calibration": self._calibration,
                "cues_logged": list(self._cues_logged) if sub == BLINKS else [],
                # Between ``link_lost`` and ``link_back``: no recording is open.
                "link_down": self._link_down,
                # While paused: the block the pause ended (Resume goes on to the next one), or None.
                "interrupted": (
                    block.id if self._phase == PAUSED and self._abandoned == "between" and block else None
                ),
                "interrupted_blocks": {k: list(v) for k, v in self._interrupted.items()},
                "headset_check": (
                    {
                        "phase": self._hc,
                        "check": self._hc_on,
                        "signal_left": self._hc_signal_left() if self._hc == HC_SIGNAL else None,
                    }
                    if self._hc is not None
                    else None
                ),
                "headset_result": self._hc_result,
                "seed": self.meta["seed"],
                "session_id": self.meta["session_id"],
                "only": self.meta["only"],
                "rev": self._rev,
            }

    @contextlib.contextmanager
    def _command(self) -> Iterator[None]:
        """Holds the lock; if the command fails, the state is put back as it was."""
        with self._lock:
            saved = self._snapshot()
            try:
                yield
            except BaseException:
                self._restore(saved)
                raise

    def _snapshot(self) -> tuple:
        return (
            self._step,
            self._block,
            self._blocks_done,
            self._phase,
            self._elapsed,
            self._since,
            self._reason,
            self.ended_early,
            self.stopped,
            set(self._finished),
            set(self._skipped),
            dict(self._partial),
            (self._sub, self._contact_on, self._calibration, self._contact_used, self._settle_since, self._cues_logged),
            (self._abandoned, {k: list(v) for k, v in self._interrupted.items()}, self._hc, self._hc_on,
             self._lost_part, self._lost_reason, self._hc_result, self._hc_states),
        )

    def _restore(self, saved: tuple) -> None:
        (
            self._step,
            self._block,
            self._blocks_done,
            self._phase,
            self._elapsed,
            self._since,
            self._reason,
            self.ended_early,
            self.stopped,
            self._finished,
            self._skipped,
            self._partial,
            (self._sub, self._contact_on, self._calibration, self._contact_used, self._settle_since, self._cues_logged),
            (self._abandoned, self._interrupted, self._hc, self._hc_on, self._lost_part, self._lost_reason,
             self._hc_result, self._hc_states),
        ) = saved

    # -- recording ------------------------------------------------------------

    def _record(
        self,
        name: str,
        payload: dict,
        browser_time: float | None = None,
        time_origin: float | None = None,
        mark: bool = True,
        after: Callable[[], None] | None = None,
    ) -> int:
        """Appends the line of ``events.jsonl`` and marks the recording.

        The line is written first and taken back out if the marker cannot be
        put, so a failure leaves neither a line nor a gap in ``seq``. ``after``
        runs once the marker is in (``_last_sample`` is then its sample); if it
        fails the line is taken back out too, but a marker already put cannot be
        removed from the recording, so its ``seq`` is used up and the next event
        gets the next number.
        """
        seq = self._seq + 1
        step = self.current.id
        block = (
            self.current.blocks[self._block].id if self._block < len(self.current.blocks) else None
        )

        def line(extra: dict) -> str:
            return (
                json.dumps(
                    {
                        "seq": seq,
                        "step": step,
                        "block": block,
                        "name": name,
                        "payload": {**payload, **extra},
                        "browser_time": browser_time,
                        "time_origin": time_origin,
                        **clock_fields(browser_time, time_origin),
                    },
                    allow_nan=False,
                )
                + "\n"
            )

        if self._events_unusable is not None:
            raise EventLogFailed(f"the event log is unusable: {self._events_unusable}")
        try:
            start = self._events.tell()
        except (OSError, ValueError) as error:
            self._events_unusable = repr(error)
            raise EventLogFailed(f"the event log is unusable: {error!r}") from error

        def take_back() -> None:
            try:
                self._events.seek(start)
                self._events.truncate(start)
            except (OSError, ValueError) as error:
                # The seek writes out what the failed write left in the buffer, and that fails
                # again for the same reason (a full disk, a size limit). Shrinking a file needs no
                # room: close the handle (its buffer is of no use), cut the file back to where the
                # line began, and open it again, so the log never ends in half a line.
                try:
                    self._reopen_events_at(start)
                except (OSError, ValueError) as again:
                    self._events_unusable = (
                        f"a line could not be taken back: {error!r}, then {again!r}"
                    )
                    raise EventLogFailed(
                        f"the event log is unusable: {self._events_unusable}"
                    ) from error

        extra: dict = {}
        if (
            mark
            and not getattr(self.source, "recording", True)
            and (
                self.device_pending()
                or self._failure is not None
                or self._phase == DONE
                or not self._rounds_begun
                or self._link_down
            )
        ):
            # No recording yet (the link has not come up), or the one there was ended with a
            # lost link, or it could not be started, or the session is ending (``stopped`` at
            # the device step before the link came up): the event is saved without a marker,
            # and so has no sample. Before the first block this holds for every flow, whether or
            # not it has a device step.
            mark = False
            extra = {"marked": False}
            if self._link_down:
                extra["no_recording"] = True  # the link was lost: no recording is open
        try:
            self._events.write(line(extra))
            self._events.flush()
        except BaseException:
            take_back()
            raise
        self._last_sample = None
        if mark:
            try:
                self._last_sample = self.source.mark(f"{step}/{name}#{seq}")
            except RuntimeError:
                take_back()
                # The link ended between the last tick and now, and the source
                # closed the recording: the line is saved without a marker, and
                # the next tick ends the session. Any other failure is raised.
                if getattr(self.source, "ended", None) is None and not self._link_down:
                    raise
                self._events.write(line({"marked": False, "no_recording": True}))
                self._events.flush()
            except BaseException:
                take_back()
                raise
        if after is not None:
            try:
                after()
            except BaseException:
                take_back()
                if self._last_sample is not None:
                    self._seq = seq
                raise
        self._seq = seq
        self._scores.add({"name": name, "payload": payload})
        self._rev += 1
        self._changed = True
        return seq

    def _reopen_events_at(self, size: int) -> None:
        path = self._folder / "events.jsonl"
        try:
            self._events.close()
        except (OSError, ValueError):
            pass  # the buffer could not be written out; it is dropped, the file is cut next
        os.truncate(path, size)
        self._events = open(path, "a", encoding="utf-8")

    def _notify(self) -> None:
        """Calls ``on_change`` if anything changed since the last call."""
        with self.notify_lock:
            with self._lock:
                if self._changed or self._progress_dirty:
                    # Before anyone is told, so that progress.json is never behind what a page saw.
                    self._save_progress()
                if not self._changed:
                    return
                self._changed = False
                callback, on_points = self.on_change, self.on_points
                state = self.state()
                pending, self._pending = self._pending, []
            if callback is not None:
                callback(state)
            if on_points is not None:
                for message in pending:
                    on_points(message)

    # -- commands -------------------------------------------------------------

    def _require_recording(self, what: str) -> None:
        """Refuses ``what`` unless a recording is open, on a live link when the source is live.

        Called before a block starts and before a step that has blocks is entered, whatever
        the flow looks like, so no round marker can come before the recording.
        """
        source = self.source
        if not getattr(source, "recording", True) or (
            source.kind == "live" and getattr(source, "ended", None) is not None
        ):
            raise CommandRefused(
                f"the headset link is not live yet, so nothing is being recorded: wait for the "
                f"link before {what}"
            )

    def _start_block(self) -> None:
        self._require_recording("starting a round")
        self._rounds_begun = True
        self._phase = RUNNING
        self._elapsed = 0.0
        self._since = self.clock()
        block = self.current.blocks[self._block]
        self._record("block_start", {"block": block.id, "seconds": block.seconds})

    def _end_block(self, early: dict | None = None) -> None:
        """Ends the running block. ``early`` (``ended_by``, ``seconds``) is added to the
        ``block_end`` payload of a block that ended before its time was up."""
        block = self.current.blocks[self._block]
        self._elapsed = block.seconds if early is None else early["seconds"]
        self._phase = BETWEEN
        self._blocks_done += 1
        award = None
        if self.points is not None:
            step = self.current

            def award() -> None:
                try:
                    self.points.award(step, block, BLOCK_COMPLETE, block.points)
                except Exception as error:
                    raise LedgerFailed(f"{type(error).__name__}: {error}") from error

        self._record("block_end", {"block": block.id, "points": block.points, **(early or {})}, after=award)
        if award is not None:
            self._pending.append(
                {
                    "t": "points",
                    "total": self.points.total,
                    "gained": block.points,
                    "block": block.id,
                    "reason": BLOCK_COMPLETE,
                }
            )

    def _leave_current(self) -> None:
        """The step being left counts as finished unless it was skipped."""
        if self.current.id not in self._skipped and self.current.id not in self._partial:
            self._finished.add(self.current.id)

    def _enter_step(self, index: int) -> None:
        if self.flow[index].kind not in NO_POINTS:
            self._require_recording("starting a round")
        self._leave_current()
        self._step, self._block, self._blocks_done = index, 0, 0
        self._phase, self._elapsed = WAITING, 0.0
        self._sub, self._contact_on = None, None
        self._abandoned, self._hc, self._hc_on = None, None, None
        self._hc_result = None
        self._lost_part = self._lost_reason = None
        self._record("step_start", {})
        if self.current.calibration is not None:
            self._begin_contact()

    def _finish(self) -> None:
        self._leave_current()
        self._phase = DONE
        self._record("session_end", {"ended_early": False})

    @_serialized
    def advance(self) -> None:
        """Leaves a setup step, starts the next block, or leaves the between-blocks screen."""
        with self._command():
            step = self.current
            if self._phase == WAITING and step.blocks:
                # The recording gate first: while the link is down the page is told so (and
                # enables Start again) instead of "in a moment", which it would retry by itself.
                self._require_recording("starting a round")
                self._refuse_while_check_goes_off()
                self._start_block()
            elif self._phase == WAITING and step.calibration is not None and self._sub == CONTACT:
                self._leave_contact()
            elif self._phase == WAITING and step.calibration is not None and self._sub == SIGNALED:
                self._start_timed(EYES_OPEN)
            elif self._phase == WAITING and step.calibration is not None and self._sub == SETTLING:
                raise CommandRefused("the contact check is going off: the next part starts by itself")
            elif self._phase == WAITING and step.calibration is not None and self._sub == NOT_OFF:
                raise CommandRefused("the contact check did not go off: check again or stop")
            elif self._phase == WAITING:
                if step.id == DEVICE_STEP:
                    self._require_recording("continuing")
                if self._step + 1 < len(self.flow):
                    self._enter_step(self._step + 1)
                else:
                    self._finish()
            elif self._phase == BETWEEN:
                if self._block + 1 < len(step.blocks):
                    self._block += 1
                    self._start_block()
                elif self._step + 1 < len(self.flow):
                    self._enter_step(self._step + 1)
                else:
                    self._finish()
            else:
                raise CommandRefused(f"advance is not possible while {self._phase}")
        self._notify()

    def _unfinished(self) -> tuple[str | None, float]:
        """The block in progress (``None`` between blocks or before the first) and the seconds it ran."""
        if self._phase in (RUNNING, PAUSED) and self._abandoned is None and self._block < len(self.current.blocks):
            return self.current.blocks[self._block].id, round(self._used(), 3)
        return None, 0.0

    @_serialized
    def skip(self, step: str | None = None) -> None:
        """Ends the current round now and goes to the next step (waiting).

        The block in progress is not finished: it earns no points (no ledger line and no
        ``points`` message) and the total does not change. Blocks of the round that ended
        earlier keep their points. ``step``, when given, is the id of the round the person
        saw when they pressed the button; if the session has moved on since, nothing is
        skipped, so two presses never skip two rounds. Refused on the participant and device
        steps, on the summary and once the session is over. Allowed while paused: the next
        round starts unpaused.
        """
        with self._command():
            if self._phase == DONE:
                raise CommandRefused("the session is over")
            current = self.current
            if step is not None and step != current.id:
                raise CommandRefused(f"{step!r} is no longer the current step; nothing was skipped")
            if current.id in CANNOT_SKIP:
                raise CommandRefused(f"the {current.title.lower()} step cannot be skipped")
            if current.kind == "summary":
                raise CommandRefused("the summary cannot be skipped")
            following = self.flow[self._step + 1] if self._step + 1 < len(self.flow) else None
            if following is not None and following.kind not in NO_POINTS:
                # Refused before anything is written, as ``advance`` is: entering a step with
                # blocks needs a recording, and a skip that could not enter it would end the
                # session with a failure.
                self._require_recording("starting a round")
            block, seconds = self._unfinished()
            self._record(
                "skipped",
                {"block": block, "seconds": seconds, "blocks_done": self._blocks_done},
            )
            # The marker is in the recording now and cannot be taken out: from here on the
            # round counts as left, whatever happens next. A round whose blocks all finished
            # stays finished, one with some finished blocks is partial, one with none is skipped.
            if current.blocks and self._blocks_done >= len(current.blocks):
                self._finished.add(current.id)
            elif self._blocks_done > 0:
                self._partial[current.id] = self._blocks_done
            else:
                self._skipped.add(current.id)
            self._reason = None
            self._abandoned, self._hc, self._hc_on = None, None, None
            self._hc_result = None
            saved = self._snapshot()
            try:
                if self._step + 1 < len(self.flow):
                    self._enter_step(self._step + 1)
                else:
                    self._finish()
            except BaseException as error:
                self._restore(saved)
                if not isinstance(error, Exception):
                    raise
                self._fail(f"skipping could not be completed: {_describe(error)}")
        self._notify()

    @_serialized
    def stop(self) -> None:
        """Ends the session at the person's request, in the ordinary way.

        Event ``stopped``, the recording closed, ``stopped`` true, ``failure`` unset,
        ``ended_early`` false. Nothing is awarded for the block in progress. Refused
        before the participant step is done and once the session is over.
        """
        with self._command():
            if self._phase == DONE:
                raise CommandRefused("the session is over")
            if self.current.id == PARTICIPANT_STEP:
                raise CommandRefused("the session cannot be stopped before the participant ID is done")
            block, seconds = self._unfinished()
            self._phase = DONE
            self._reason = None
            self._abandoned, self._hc, self._hc_on = None, None, None
            self._hc_result = None
            self.stopped = True
            self._record(
                "stopped",
                {"block": block, "seconds": seconds, "blocks_done": self._blocks_done, "ended_early": False},
            )
        try:
            self.source.stop_recording()
        except Exception as error:
            self._log_failure("the recording could not be closed after the session was stopped", error)
        self._notify()

    def _pause_running(self, reason: str) -> None:
        """Pauses the running block or calibration part with ``reason``.

        A task or recall block is ended at once (``block_end`` with ``ended_by`` ``interrupted``, the
        ``seconds`` it ran, no points): it counts as played and Resume goes to the between-blocks
        screen. A rest block is ended the same way but does not count: Resume goes back to its start
        screen and the rest is played again. A calibration part starts again from its beginning on
        Resume. A video block is held and resumes where it was."""
        self._elapsed = self._used()
        self._phase = PAUSED
        self._reason = reason
        self._record("paused", {"reason": reason})
        step = self.current
        if step.calibration is not None:
            if self._sub in PARTS:  # the signal check too: its part_restarted keeps the reason
                self._lost_part, self._lost_reason = self._sub, reason
            return
        if step.kind not in ABANDONED_BY_PAUSE + RESTARTED_BY_PAUSE:
            return
        block = step.blocks[self._block]
        self._record(
            "block_end",
            {"block": block.id, "points": 0, "ended_by": INTERRUPTED, "seconds": round(self._elapsed, 3), "reason": reason},
        )
        if step.kind in RESTARTED_BY_PAUSE:
            self._abandoned = "restart"
        else:
            self._abandoned = "between"
            self._blocks_done += 1
            self._interrupted.setdefault(step.id, []).append(block.id)

    @_serialized
    def pause(self, reason: str) -> None:
        """Pauses a running block (see ``_pause_running``); only a running block can be paused."""
        with self._command():
            if self._phase != RUNNING:
                raise CommandRefused(
                    f"pause is only possible in a running block, not {self._phase}"
                )
            self._pause_running(str(reason))
        self._notify()

    @_serialized
    def resume(self) -> None:
        """Ends the pause. Refused while no recording is open on a live link (after a lost link,
        until it is back) and while a headset check is open or its contact check is going off.

        After a pause that ended a task or recall block the session is between blocks (the next
        block starts from there as usual); after one that ended a rest block it waits at that block's
        start. A calibration part starts again from its beginning (``part_restarted``, then its
        ``<part>_start``). Any other block (the video) goes on with the time it had left."""
        with self._command():
            if self._phase != PAUSED:
                raise CommandRefused(
                    f"resume is only possible in a paused block, not {self._phase}"
                )
            if self._link_down:
                raise CommandRefused(
                    "the headset link is not live yet, so nothing is being recorded: wait for the "
                    "link before resuming"
                )
            if self._hc == HC_CONTACT:
                raise CommandRefused("the headset check is open: press Done first")
            if self._hc == HC_SETTLING:
                raise CommandRefused("the headset's contact check is still going off: resume in a moment")
            if self._hc == HC_SIGNAL:
                raise CommandRefused("the headset's signal check is running: resume in a moment")
            self._require_recording("resuming")
            abandoned, self._abandoned = self._abandoned, None
            self._reason = None
            self._hc_result = None
            if abandoned == "between":
                self._phase = BETWEEN
                self._record("resumed", {"to": "between"})
                return_now = True
            elif abandoned == "restart":
                self._phase, self._elapsed = WAITING, 0.0
                self._record("resumed", {"to": "block_start"})
                return_now = True
            else:
                return_now = False
            if not return_now:
                self._since = self.clock()
                self._phase = RUNNING
                self._record("resumed", {})
                part, self._lost_part = self._lost_part, None
                why, self._lost_reason = self._lost_reason, None
                if part is not None and self.current.calibration is not None and self._sub == part:
                    self._record("part_restarted", {"part": part, "reason": why or LINK_LOST})
                    self._begin_signal() if part == SIGNAL else self._start_timed(part)
                elif self.current.calibration is not None and self._sub == SIGNAL:
                    # Any pause cuts the 6 s of the signal check: it starts again.
                    self._record("part_restarted", {"part": SIGNAL, "reason": "paused"})
                    self._begin_signal()
        self._notify()

    def is_over(self) -> bool:
        """Whether the session is over (phase ``done``)."""
        with self._lock:
            return self._phase == DONE

    @_serialized
    def link_lost(self, payload: dict) -> None:
        """The hub lost the headset link (``link_lost``, no marker). After the device step the
        session is not ended: a running block or calibration part is paused with the reason
        ``link_lost`` (the trial in progress is abandoned by the page, as for any pause), and a
        contact check starts again from its beginning. A block that is already paused, between
        blocks, waiting or on the summary changes nothing but the link (and the gate of
        ``resume`` and ``advance``). Nothing is saved once the session is over."""
        if self._phase == DONE:
            return
        try:
            with self._command():
                self._link_down = True
                self._record("link_lost", payload, mark=False)
                timed = self.current.calibration is not None and self._sub in PARTS
                if self._phase == RUNNING:
                    self._pause_running(LINK_LOST)
                elif timed and self._phase == PAUSED:
                    self._lost_part, self._lost_reason = self._sub, LINK_LOST
                if self._hc == HC_CONTACT:
                    # The check cannot be measured without data: it ends, and goes off as usual.
                    self._record("headset_check_end", {"ended_by": LINK_LOST, "measured": False, "states": {}})
                    self._hc, self._hc_on = HC_SETTLING, None
                    self._settle_since = self.clock()
                if self._phase == WAITING and self.current.calibration is not None and self._sub == CONTACT:
                    self._begin_contact({"restarted": LINK_LOST})
        except Exception:
            LOG.exception("the lost link could not be saved")
        self._notify()

    @_serialized
    def link_back(self, payload: dict) -> None:
        """A new recording is open after a lost link (``link_back``, no marker): ``resume`` and
        ``advance`` are allowed again. A paused block stays paused until Resume."""
        if self._phase == DONE:
            return
        try:
            with self._command():
                self._record("link_back", payload, mark=False)
                self._link_down = False
        except Exception:
            LOG.exception("the link back could not be saved")
        self._notify()

    @_serialized
    def video_ended(self, step: str | None = None) -> None:
        """Ends the running block of a video round now: the page says the video has ended.

        The block counts as finished and earns its points, as at the end of its time; its
        ``block_end`` payload also has ``ended_by`` (``video_ended``) and the ``seconds`` it ran.
        ``step``, when given, is the round the page shows (a late message ends nothing). Refused
        unless a block of a step of kind ``video`` is running (not while it is paused).
        """
        with self._command():
            current = self.current
            if step is not None and step != current.id:
                raise CommandRefused(f"{step!r} is no longer the current step; nothing was ended")
            if self._phase != RUNNING:
                raise CommandRefused(f"video_ended is only possible in a running block, not {self._phase}")
            if current.kind != "video":
                raise CommandRefused("video_ended is only possible in a video round")
            self._end_block({"ended_by": "video_ended", "seconds": round(self._used(), 3)})
        self._notify()

    @_serialized
    def block_finished(self, reason: str, step: str | None = None) -> None:
        """Ends the running block before the engine would: the page says it is finished.

        ``reason`` ``participant_done`` (the participant pressed that they are done) is allowed only
        for a step whose flow entry has ``may_finish_early``; ``trials_done`` (the page ran the last
        trial that fits) only for a step of kind ``task``. The block counts as finished and earns its
        points; its ``block_end`` has ``ended_by`` (the reason) and the ``seconds`` it ran. ``step``,
        when given, is the round the page shows (a late message ends nothing). Refused unless a block
        is running (not while it is paused)."""
        with self._command():
            current = self.current
            if reason not in (PARTICIPANT_DONE, TRIALS_DONE):
                raise CommandRefused(
                    f"block_finished needs a reason of {PARTICIPANT_DONE!r} or {TRIALS_DONE!r}, not {reason!r}"
                )
            if step is not None and step != current.id:
                raise CommandRefused(f"{step!r} is no longer the current step; nothing was ended")
            if self._phase != RUNNING or current.calibration is not None:
                raise CommandRefused(f"block_finished is only possible in a running block, not {self._phase}")
            if reason == PARTICIPANT_DONE and not current.may_finish_early:
                raise CommandRefused(f"block_finished ({reason}) is not allowed in the {current.title} round")
            if reason == TRIALS_DONE and current.kind != "task":
                raise CommandRefused(f"block_finished ({reason}) is only possible in a task round")
            self._end_block({"ended_by": reason, "seconds": round(self._used(), 3)})
        self._notify()

    def _grace(self, step: Step) -> float:
        """Seconds a block of ``step`` may run past its time: ``TASK_GRACE_S`` for a task drawn by
        a page module (it ends the block itself with ``trials_done``), else 0."""
        return TASK_GRACE_S if step.kind == "task" and step.module is not None else 0.0

    # -- the headset check of the paused screen ---------------------------------

    @_serialized
    def headset_check(self) -> None:
        """Opens the contact check from the paused screen (any reason): ``headset_check_start``,
        and the check is switched on (``state.contact_check``) until ``headset_check_done``.

        Refused unless paused, while the headset link is down (no data to measure) and without an
        open recording on a live link. Asked again while open, it switches the check on again (a
        reloaded page switched it off)."""
        with self._command():
            if self._phase != PAUSED:
                raise CommandRefused("the headset can be checked only while paused")
            if self._link_down:
                raise CommandRefused("waiting for the headset: the check needs the headset link")
            self._require_recording("checking the headset")
            if self._hc == HC_CONTACT and self._hc_on is not None:
                raise CommandRefused("the headset check is open already")
            if self._hc == HC_SIGNAL and self.signal_window is not None:
                self.signal_window.end()
            seq = self._record("headset_check_start", {"reason": self._reason, "again": self._hc == HC_CONTACT})
            self._hc, self._hc_on = HC_CONTACT, seq
            self._hc_result, self._hc_states = None, {}
            self._contact_used = True
        self._notify()

    @_serialized
    def headset_check_done(self) -> None:
        """Closes the headset check: ``headset_check_end`` with the contact states of the last second
        (or none, ``measured`` false, if none was measured), the result added to ``calibration.json``
        as a further dated entry, the check switched off, and the settling wait begun: Resume is
        refused until the data have shown the check off for ``SETTLE_S``."""
        with self._command():
            if self._hc != HC_CONTACT:
                raise CommandRefused("no headset check is open")
            measured = None
            if self._hc_on is not None and self.contact_states is not None:
                measured = self.contact_states(self._hc_on)
            states = dict(measured or {})
            now = time.time()
            entry = {
                "kind": "headset_check",
                "states": states,
                "excluded": [label for label, state in states.items() if state == "bad"],
                "measured": bool(states),
                "thresholds_version": contact.THRESHOLDS.version,
                "unix_time": now,
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "step": self.current.id,
                "reason": self._reason,
                "check": self._hc_on,
            }
            self._record(
                "headset_check_end",
                {
                    "states": states,
                    "good": sum(1 for v in states.values() if v == "good"),
                    "measured": bool(states),
                },
                after=lambda: self._add_check(entry),
            )
            self._hc, self._hc_on = HC_SETTLING, None
            self._hc_states = states
            self._settle_since = self.clock()
        self._notify()

    def _add_check(self, entry: dict) -> None:
        """Adds ``entry`` to the list ``checks`` of ``calibration.json`` (made if absent); the first
        result (of the calibration step) stays as it is."""
        path = self._folder / CALIBRATION_FILE
        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("calibration.json is not a JSON object")
        checks = data.get("checks")
        data["checks"] = [*(checks if isinstance(checks, list) else []), entry]
        progress.write_json(path, data)

    def _hc_tick(self) -> None:
        """Ends the settling wait of a headset check once the data have shown the check off for
        ``SETTLE_S`` (counted in samples). While the link is down it waits."""
        if self._link_down:
            self._settle_since = self.clock()
            return
        if self._settled(self._quiet()):
            self._hc = None
            self._record("headset_check_settled", {"waited": round(self.clock() - self._settle_since, 3)})
            if self.signal_window is not None:
                # The same signal check as after the calibration's contact check.
                self._hc = HC_SIGNAL
                self.signal_window.begin()
                self._record(
                    "signal_start",
                    {"seconds": quality.SIGNAL_S, "version": quality.VERSION, "for": "headset_check"},
                )

    def _hc_signal_left(self) -> float:
        got, needed = self.signal_window.progress()
        return round((needed - got) / self.signal_window.sample_rate, 1)

    def _hc_signal_tick(self) -> None:
        """Ends the signal check of a headset check once the window holds its data: the result is
        shown on the paused screen, logged as ``signal_end`` and added to the check's entry in
        ``calibration.json``. While the link is down the window starts again."""
        window = self.signal_window
        if self._link_down:
            window.begin()
            return
        result = window.result()
        if result is None:
            return
        window.end()
        self._hc = None
        states = result.get("states", {})
        signal = {**result, "unix_time": time.time()}
        self._hc_result = {"states": dict(self._hc_states), "signal": signal}
        self._record(
            "signal_end",
            {
                "not_fine": list(result.get("not_fine", [])),
                "fine": sum(1 for v in states.values() if v == "fine"),
                "states": dict(states),
                "error": result.get("error"),
                "version": result.get("version"),
                "for": "headset_check",
            },
            after=lambda: self._add_check_signal(signal),
        )

    def _add_check_signal(self, signal: dict) -> None:
        """Adds the signal result to the last entry of ``checks`` in ``calibration.json``."""
        path = self._folder / CALIBRATION_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        checks = data.get("checks") if isinstance(data, dict) else None
        if not isinstance(checks, list) or not checks:
            raise ValueError("calibration.json has no headset check to add the signal result to")
        checks[-1] = {**checks[-1], "signal": signal}
        progress.write_json(path, data)

    @_serialized
    def event(self, name: str, payload: dict, browser_time: float, time_origin: float) -> int:
        """Saves an event sent by the page; returns its ``seq``.

        ``browser_time`` is ``performance.now()`` and ``time_origin`` is
        ``performance.timeOrigin`` in the page, both as sent.
        """
        if not isinstance(name, str) or not EVENT_NAME.fullmatch(name):
            raise BadEvent(f"bad event name {name!r}: use 1 to 64 of A-Z a-z 0-9 _ . -")
        if not isinstance(payload, dict):
            raise BadEvent("an event payload is a JSON object")
        for key, value in (("browser_time", browser_time), ("time_origin", time_origin)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BadEvent(f"{key} must be a number")
            if not math.isfinite(value):
                raise BadEvent(f"{key} must be a finite number")
        try:
            json.dumps(payload, allow_nan=False)
        except ValueError as error:
            raise BadEvent(f"the payload must hold only finite numbers: {error}") from error
        except TypeError as error:
            raise BadEvent(f"the payload must be JSON: {error}") from error
        with self._command():
            if self._phase == DONE:
                raise CommandRefused("the session is over")
            seq = self._record(name, payload, float(browser_time), float(time_origin))
            if (
                name == "blink_cue"
                and self._sub == BLINKS
                and self.current.calibration is not None
                and isinstance(payload.get("n"), int)
                and payload["n"] not in self._cues_logged
            ):
                self._cues_logged = [*self._cues_logged, payload["n"]]
        self._notify()
        return seq

    @_serialized
    def tick(self) -> None:
        """Ends the block whose time is up and notices that the source ended."""
        # Two commands, so that each is all or nothing and writes one line.
        try:
            if self._events_unusable is not None and self._phase != DONE:
                # A command found the event log unusable: nothing can be recorded any more.
                raise EventLogFailed(f"the event log is unusable: {self._events_unusable}")
            with self._command():
                if self._phase == RUNNING and self.current.calibration is not None:
                    self._calibration_tick()
                elif self._phase == WAITING and self.current.calibration is not None and self._sub == SETTLING:
                    self._settling_tick()
                elif self._phase == RUNNING:
                    block = self.current.blocks[self._block]
                    if self._used() >= block.seconds + self._grace(self.current):
                        self._end_block()
                elif self._phase == PAUSED and self._hc == HC_SETTLING:
                    self._hc_tick()
                elif self._phase == PAUSED and self._hc == HC_SIGNAL:
                    self._hc_signal_tick()
            # The block's end is handed out before anything else can happen to it.
            self._notify()
            with self._command():
                if self._phase != DONE:
                    ended = getattr(self.source, "ended", None)
                    # A live link that ended is tried again (the hub does that) while the
                    # session lasts: the session goes on (paused, see ``link_lost``). Only a
                    # source stopped on request (the process is stopping) ends it.
                    retrying = (
                        self.source.kind == "live" and ended is not None and ended.reason != "stopped"
                    )
                    if ended is not None and not retrying:
                        self._source_ended(ended.reason)
        except (LedgerFailed, EventLogFailed) as error:
            # Trying again would put another block_end marker into the recording every
            # tick (or fail at every tick), so the session ends once, with the reason.
            if self._phase != DONE:
                self._fail(_describe(error))
        self._notify()

    @_serialized
    def fail(self, reason: str) -> None:
        """Ends the session once, early, with ``reason`` as its ``failure`` (see ``_fail``)."""
        if self._phase != DONE:
            self._fail(reason)
        self._notify()

    @_serialized
    def note(self, name: str, payload: dict) -> None:
        """Saves one line in ``events.jsonl`` that is not a marker (the hub's ``link_lost`` and
        ``link_back``). It has no sample: nothing is put into the recording. Ignored once the
        session is over, and a failure to write it is logged, not raised."""
        if self._phase == DONE:
            return
        try:
            with self._command():
                self._record(name, payload, mark=False)
        except Exception:
            LOG.exception("the event %r could not be saved", name)
        self._notify()

    def _fail(self, reason: str) -> None:
        """Ends the session early because of ``reason``: phase ``done``, ``failure`` set.

        An ``ended`` event is saved and the recording is closed normally. If the
        event cannot be saved either, the state still says what happened.
        """
        with self._command():
            self._elapsed = self._used()
            self._phase = DONE
            self._reason = None
            self._failure = reason
            self.ended_early = True
            try:
                self._record("ended", {"reason": reason, "ended_early": True})
            except Exception as error:
                if isinstance(error, EventLogFailed):
                    LOG.warning("session ended by a failure; the event log cannot record it")
                else:
                    self._log_failure("the session was ended by a failure and could not record it", error)
                self._rev += 1
                self._changed = True
        try:
            self.source.stop_recording()
        except Exception as error:
            self._log_failure("the recording could not be closed after a failure", error)

    def _log_failure(self, what: str, error: BaseException) -> None:
        """Logs ``error`` as its type and message with the session folder replaced (no traceback)."""
        LOG.error("%s: %s", what, package.scrub(f"{type(error).__name__}: {error}", self._folder))

    def _source_ended(self, reason: str) -> None:
        """The source ended: the session is over, early unless it was at its last step."""
        last = self._step == len(self.flow) - 1
        blocks = self.current.blocks
        finished = last and (
            (not blocks and self._phase == WAITING)
            or (blocks and self._phase == BETWEEN and self._blocks_done == len(blocks))
        )
        self.ended_early = not finished
        if finished:
            self._leave_current()
        self._elapsed = self._used()
        self._phase = DONE
        self._reason = None
        # The recording is already closed by the source, so nothing can be marked.
        self._record(
            "source_ended",
            {"reason": reason, "marked": False, "ended_early": self.ended_early},
            mark=False,
        )

    # -- calibration ----------------------------------------------------------

    def _contact_check(self) -> int | None:
        """The ``seq`` of the ``contact_start`` whose contact check should be on now, else ``None``."""
        if self._phase == PAUSED and self._hc == HC_CONTACT:
            return self._hc_on
        if self._phase == DONE or self.current.calibration is None or self._sub != CONTACT:
            return None
        return self._contact_on

    def _sub_seconds(self) -> float:
        timing = self.current.calibration
        return {EYES_OPEN: timing.eyes_open, EYES_CLOSED: timing.eyes_closed, BLINKS: timing.blinks}[
            self._sub
        ]

    def _begin_contact(self, payload: dict | None = None) -> None:
        """The contact sub-step begins, or begins again: the check is to be switched on."""
        seq = self._record("contact_start", payload or {})
        self._sub, self._contact_on = CONTACT, seq
        self._contact_used = True

    def _quiet(self) -> tuple[int, float] | None:
        return self.contact_quiet() if self.contact_quiet is not None else None

    @staticmethod
    def _settled(quiet: tuple[int, float] | None) -> bool:
        """Whether the data have shown the check off for ``SETTLE_S``, counted in samples of
        the data stream (``contact.settle_samples``); a source that does not say (``None``) is."""
        return quiet is None or quiet[0] >= contact.settle_samples(quiet[1])

    def _refuse_while_check_goes_off(self) -> None:
        """After a contact check, a round's first block waits until the data have shown the check
        off for ``SETTLE_S`` (skipping from the contact check leads straight to a round)."""
        if not self._contact_used:
            return
        if not self._settled(self._quiet()):
            raise CommandRefused(
                "the headset's contact check is still going off: the round can start in a moment"
            )

    def _settling_tick(self) -> None:
        """Starts eyes open once the data have shown the check off for ``SETTLE_S``; gives up
        (``not_off``) when they still show it on ``OFF_LIMIT_S`` after it was switched off.

        While the headset link is down there are no data to show anything: settling waits, and
        the limit is counted again from the link being back (no ``not_off`` because of a drop)."""
        if self._link_down:
            self._settle_since = self.clock()
            return
        held = self._quiet()
        waited = self.clock() - self._settle_since
        if self._settled(held):
            self._after_settled()
            return
        quiet = held[0] / held[1]
        if waited >= OFF_LIMIT_S and quiet < waited - OFF_LIMIT_S:
            # The last sample with the check on came after the limit: it did not go off.
            self._record("contact_not_off", {"waited": round(waited, 3), "quiet": round(quiet, 3)})
            self._sub = NOT_OFF
            LOG.warning(
                "the headset still sent with its contact check on %.0f s after it was switched off;"
                " the eyes-open part was not started",
                waited,
            )

    def _after_settled(self) -> None:
        """The check is off and settled: the signal check (with the hub's window), else eyes open."""
        if self.signal_window is not None:
            self._begin_signal()
        else:
            self._start_timed(EYES_OPEN)

    def _begin_signal(self) -> None:
        """The signal check starts (again): the window collects the data from now on."""
        self._sub = SIGNAL
        self._phase = RUNNING
        self._elapsed = 0.0
        self._since = self.clock()
        self._signal_left = quality.SIGNAL_S
        self.signal_window.begin()
        self._record("signal_start", {"seconds": quality.SIGNAL_S, "version": quality.VERSION})

    def _signal_tick(self) -> None:
        """Ends the signal check once the window holds its data: the result joins the contact
        result (``calibration.json``, field ``signal``) and ``signal_end`` is logged."""
        window = self.signal_window
        got, needed = window.progress()
        self._signal_left = round((needed - got) / window.sample_rate, 1)
        result = window.result()
        if result is None:
            return
        window.end()
        self._phase = WAITING
        self._sub = SIGNALED
        self._signal_left = None
        states = result.get("states", {})
        signal = {**result, "unix_time": time.time()}
        merged = {**(self._calibration or {}), "signal": signal}
        self._record(
            "signal_end",
            {
                "not_fine": list(result.get("not_fine", [])),
                "fine": sum(1 for v in states.values() if v == "fine"),
                "states": dict(states),
                "error": result.get("error"),
                "version": result.get("version"),
            },
            after=lambda: progress.write_json(self._folder / CALIBRATION_FILE, merged),
        )
        self._calibration = merged

    def _start_timed(self, sub: str) -> None:
        self._sub = sub
        self._phase = RUNNING
        self._elapsed = 0.0
        self._since = self.clock()
        timing = self.current.calibration
        payload = {"seconds": self._sub_seconds()}
        if sub == BLINKS:
            payload.update(cues=timing.blink_cues, gap=timing.blink_gap, lead=timing.blink_lead)
            self._cues_logged = []
        self._record(f"{sub}_start", payload)

    def _then(self, action: Callable[[], None]) -> None:
        """Runs the second half of a change whose first marker is in the recording already and
        cannot be taken out: if it fails, the session ends with the reason (as ``skip`` does)."""
        saved = self._snapshot()
        try:
            action()
        except BaseException as error:
            self._restore(saved)
            if not isinstance(error, Exception):
                raise
            self._fail(f"calibration could not go on: {_describe(error)}")

    def _leave_contact(self) -> None:
        """Continue from the contact sub-step: the states of the last second are the result,
        the bad electrodes are left out, the check goes off and ``settling`` begins (eyes open
        starts once the data show the check off; see ``_settling_tick``).

        Refused until the check that is on now has been measured (a reloaded page switched it
        off: Check again first)."""
        measured = None
        if self._contact_on is not None and self.contact_states is not None:
            measured = self.contact_states(self._contact_on)
        if not measured:
            raise CommandRefused(
                "the contacts have not been measured since the check was switched on: wait for the"
                " measure, or press Check again"
            )
        states = dict(measured)
        result = {
            "states": states,
            "excluded": [label for label, state in states.items() if state == "bad"],
            "thresholds_version": contact.THRESHOLDS.version,
            "unix_time": time.time(),
        }
        self._record(
            "contact_end",
            {
                "excluded": result["excluded"],
                "good": sum(1 for s in states.values() if s == "good"),
                "measured": bool(states),
            },
            after=lambda: progress.write_json(self._folder / CALIBRATION_FILE, result),
        )
        self._calibration = result
        self._contact_on = None
        self._then(self._begin_settling)

    def _begin_settling(self) -> None:
        self._sub = SETTLING
        self._settle_since = self.clock()
        self._record("settling_start", {"settle": SETTLE_S, "limit": OFF_LIMIT_S})

    def _calibration_tick(self) -> None:
        """Ends a timed sub-step whose time is up and starts the next one."""
        if self._sub == SIGNAL:
            self._signal_tick()
            return
        if self._sub not in TIMED or self._used() < self._sub_seconds():
            return
        ended = self._sub
        self._elapsed = self._sub_seconds()
        if ended == BLINKS:
            self._phase = WAITING
            self._record("blinks_end", {})
            self._sub = CALIBRATED
            return
        self._record(f"{ended}_end", {})
        self._then(lambda: self._start_timed(EYES_CLOSED if ended == EYES_OPEN else BLINKS))

    @_serialized
    def check_again(self) -> None:
        """Back to the start of the contact sub-step: a new ``contact_start``, the check on again."""
        with self._command():
            if self._phase != WAITING or self.current.calibration is None or self._sub not in (CONTACT, NOT_OFF, SIGNALED):
                raise CommandRefused("the contact check can be done again only while it is shown")
            self._begin_contact({"again": True})
        self._notify()

    @_serialized
    def contact_hold(self, reason: str) -> None:
        """Switches the contact check off while the contact sub-step stays shown (the page that
        showed it was reloaded or closed); ``check_again`` switches it on again."""
        with self._command():
            if self._contact_check() is None:
                return
            self._record("contact_check_off", {"reason": str(reason)})
            if self._hc == HC_CONTACT:
                self._hc_on = None
            else:
                self._contact_on = None
        self._notify()

    def close(self) -> None:
        """Closes ``events.jsonl``."""
        with self._lock:
            self._events.close()

"""The Starlette application: bundled page files, a websocket, origin and host checks.

Websocket messages (JSON text), server to page:

* ``{"t":"hello","fs","labels","kind","name"}``: sample rate, the EEG labels and
  ``"live"`` or ``"replay"``; ``name`` is the file name (not the path) of a replayed recording.
* ``{"t":"link","state":"connecting"|"live"|"ended","reason","recording","problem"}``:
  ``problem`` is ``null`` or ``{"code","kind","msg","ports","dropped","attempt","retry_s"}``, the
  error the last attempt to open the headset (or the read of its link) came back with; while it
  is set the state is ``connecting`` and another attempt follows in ``retry_s`` seconds
  (see ``source.LinkProblem``). ``dropped`` is true when a link that was live was lost and has
  not come back. ``recording`` says whether the recording has started. ``ended`` is for a
  replay that ran out and for a live link lost after the device step.
* ``{"t":"status","rate","lost","lost10","bat","imp"}``: once a second while the link is live:
  rows received a second (``null`` in the first second), samples lost since the link opened and in
  the last 10 s, the battery reading in volts and whether the contact check is on.
* ``{"t":"checks","items":[{"id","ok","text","fix"}]}``: the start-up checks, on connect.
* ``{"t":"error","kind","msg"}``
* ``{"t":"state", ...}``: the session state of ``engine.Session.state``, on
  connect, on every change and once a second. Until a participant label has been
  accepted (``POST /api/participant``) there is no session: the state is that of
  the first step, waiting, with ``rev`` 0, and every command is refused. ``points_total`` and
  ``blocks_done`` change only on a state whose phase is ``between``; ``kind`` is
  ``rest`` during the two rest steps, where a page hides both.
* ``{"t":"points","total","gained","block","reason"}``: sent once, right after
  the ``state`` whose phase is ``between``, when a block has ended and earned
  points. Never sent while a block runs or is paused.
* ``{"t":"d","x":[[..]],"lost","bat","imp"}``: EEG rows in microvolts, only
  to clients that sent ``{"cmd":"traces","on":true}``.
* ``{"t":"contact","states":{label: "good"|"marginal"|"bad"},"good":n,"check","settling","sample","error","tone_seen"}``:
  once a second while the contact check is on (the calibration step's ``contact`` sub-step), the
  state of every electrode over the last second (``contact.ContactMonitor``); ``check`` is the
  ``contact_check`` of the state the measure belongs to and ``sample`` the number of the sample
  after the last one measured. Until the first measure ``states`` and ``good`` are null and
  ``settling`` is true (the first second after ``0x11`` is left out); ``error`` says, plainly,
  why the contact cannot be measured at the headset's sample rate (else null). ``tone_seen``
  (null until the first measure) is false when the tone is not there at all
  (``contact.tone_seen``): then the states say nothing.
* ``{"t":"trace","hz":50,"x":[[..]]}``: EEG rows thinned to 50 a second (``display.Decimator``), in
  microvolts, five rows to a message, only to clients that sent ``{"cmd":"display","on":true}``
  (the device screen does, while it is shown). For drawing only; it changes nothing that is recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import numpy as np
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from brain_mapper import behavior, contact, display, flow, importer, package, progress, quality
from brain_mapper.analysis import web as results_web
from brain_mapper.engine import EVENT_NAME, BadEvent, CommandRefused, EventLogFailed, Session, clock_fields
from brain_mapper.participants import RULE, IdentityTaken, Store, StoreDamaged, valid
from brain_mapper.points import Ledger
from brain_mapper.source import (
    RETRY_SECONDS,
    SILENT,
    SILENT_SECONDS,
    STALL_SECONDS,
    LinkLost,
    LinkProblem,
    LiveSource,
    ReplaySource,
    SourceEnded,
    reason_problem,
)

LOG = logging.getLogger("brain_mapper")
#: What a participant page may send as its page id.
PAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: The client id of a page event: written once, acknowledged every time it arrives.
CID = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Seconds between two ticks of the session engine.
TICK = 0.05
WEB = Path(__file__).resolve().parent / "web"
#: The largest body ``POST /api/participant`` reads as a label (bytes).
MAX_BODY = 1024
#: Chunks the forwarder may hold before the source drops them for it.
FORWARDER_QUEUE = 4096
#: Seconds ``Hub.stop`` waits for the hub thread: an attempt to open the headset cannot be
#: interrupted; it takes up to about 6.3 s (a headset that sends data but no configuration) and
#: the library ends it within 8 s whatever the device does (docs/device.md).
STOP_WAIT = 9.0
#: Messages a websocket client may have waiting before the oldest are dropped.
CLIENT_QUEUE = 1000
#: The same for trace messages (0.1 s each): 100 are ten seconds. A page that stops reading
#: (a tab in the background) loses the older drawing data, never memory in the server.
TRACE_QUEUE = 100
#: How many droppable messages of each kind a client may have waiting.
DROP_CAPS = {"d": CLIENT_QUEUE, "trace": TRACE_QUEUE}
#: Seconds between two ``0x12`` sent because the data say the contact check is on when it
#: should be off (a headset that missed the byte, or one that came on with the check on),
#: counted from the last contact byte written.
CONTACT_GUARD_SECONDS = 2.0
#: Saved session files kept loaded (in memory) at once; the oldest is forgotten first.
LOADED_FILES = 16
#: The answer to a file whose person is not the one this computer knows by that label, or whose
#: person this computer knows by another label. Neither label is named.
CONFLICT = (
    "The participant in this file does not match this computer's list: either this ID belongs "
    "here to a different person, or this person is known here under another ID. The file was not "
    "used. Nothing was changed."
)


@dataclass
class Settings:
    data_dir: Path
    port: str | None
    replay: Path | None
    flow: Path | None
    #: The port the server listens on; the command line sets the bound port here.
    http_port: int
    #: Seconds a participant label waits for the first attempt to open the headset (a replay:
    #: for the recording to open, after which the label is refused with ``source_not_ready``).
    ready_wait: float = 15.0


class LoopbackOnly:
    """Refuses requests that do not come from a page served by this app.

    Every request needs ``Host`` equal to ``127.0.0.1:<port>`` or
    ``localhost:<port>``. Websocket upgrades and every non-GET request also
    need ``Origin`` equal to ``http://127.0.0.1:<port>`` or
    ``http://localhost:<port>``. The answer to a refusal is 403.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    def allowed(self, scope: Scope) -> bool:
        port = self.settings.http_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        headers = dict(scope["headers"])
        if headers.get(b"host", b"").decode("latin-1") not in hosts:
            return False
        if scope["type"] == "websocket" or scope["method"] != "GET":
            origin = headers.get(b"origin", b"").decode("latin-1")
            return origin in {f"http://{h}" for h in hosts}
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or self.allowed(scope):
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})  # answered with HTTP 403
        else:
            response = JSONResponse({"error": "forbidden"}, status_code=403)
            await response(scope, receive, send)


class ParticipantRefused(Exception):
    """A participant label that cannot be accepted.

    ``status`` is the HTTP answer, ``code`` a short name the page switches on
    (``invalid_label``, ``already_set``, ``source_not_ready``, ``link_ended``,
    ``recording_failed``, ``write_failed``, ``bad_body``, ``bad_selection``, ``unknown_step``,
    ``not_continuable``, ``recall_needs_words``, ``unknown_import``, ``import_mismatch``,
    ``participant_conflict``), the text is for the person.
    """

    def __init__(self, status: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def make_session_folder(sessions: Path, label: str, now: float | None = None) -> Path:
    """Creates and returns ``sessions/<label>_<YYYYMMDD>_<HHMMSS>``, adding ``-2``, ``-3`` ... if taken.

    ``mkdir`` is the exclusive step, so a name is never given out twice. ``now`` (seconds since
    the epoch, default: the clock) fixes the time part. ``label`` must have passed ``valid``.
    """
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    number = 1
    while True:
        folder = sessions / (f"{label}_{stamp}" + ("" if number == 1 else f"-{number}"))
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            number += 1


_write_json = progress.write_json


def _message(**fields) -> str:
    return json.dumps(fields, separators=(",", ":"))


def _hello_for(client: "Client", text: str) -> str:
    """``hello`` as ``client`` is told it: an onlooker gets no recording name (a replayed session
    folder is named after its participant label)."""
    if client.role == "participant":
        return text
    fields = json.loads(text)
    fields.pop("name", None)
    return json.dumps(fields, separators=(",", ":"))


class Lanes:
    """The messages waiting for one client, in two lanes, each in the order they came.

    The messages that are always kept (state, link, hello, error, contact, status, ...) are in
    the first lane, the droppable drawing data (``d``, ``trace``; items ``(text, key)`` with a
    key) in the second. The first lane is always emptied first, so a state never waits behind
    drawing data however slowly the page reads. The close (``(None, None)``) is given out only
    once both lanes are empty. The interface is that of the ``asyncio.Queue`` it replaces.
    """

    def __init__(self) -> None:
        self._kept: deque[tuple[str, None]] = deque()
        self._bulk: deque[tuple[str, str]] = deque()
        self._closing = False
        self._ready = asyncio.Event()

    def put_nowait(self, item: tuple[str | None, str | None]) -> None:
        text, key = item
        if text is None:
            self._closing = True
        elif key is None:
            self._kept.append((text, None))
        else:
            self._bulk.append((text, key))
        self._ready.set()

    def qsize(self) -> int:
        return len(self._kept) + len(self._bulk) + int(self._closing)

    def empty(self) -> bool:
        return self.qsize() == 0

    def get_nowait(self) -> tuple[str | None, str | None]:
        if self._kept:
            return self._kept.popleft()
        if self._bulk:
            return self._bulk.popleft()
        if self._closing:
            self._closing = False
            return None, None
        raise asyncio.QueueEmpty

    async def get(self) -> tuple[str | None, str | None]:
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                self._ready.clear()
            await self._ready.wait()


class Client:
    """One websocket client: the messages waiting for it (``Lanes``) and what it asked for.

    At most ``DROP_CAPS[kind]`` droppable messages (drawing data) of each kind wait; further
    ones are dropped. The others (state, hello, link, error, ...) are always kept, and are sent
    before any drawing data that is waiting.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, role: str = "onlooker", page: str | None = None
    ) -> None:
        self.loop = loop
        self.role = role
        #: The random id a participant page sends, the same one after a reload.
        self.page = page
        self.messages = Lanes()
        self.waiting = {key: 0 for key in DROP_CAPS}
        self.traces = False
        #: Set while the page shows something drawn from ``trace`` messages.
        self.display = False
        #: Set when its event loop was found closed; the hub then drops it.
        self.dead = False
        #: When a state was last queued for this client (the heartbeat goes by it).
        self.state_at = time.monotonic()

    def offer(self, text: str, droppable: bool | str = False) -> None:
        """Queues ``text``; must run on the event loop.

        ``droppable`` is ``True`` for trace data (``d``), the name of a kind in ``DROP_CAPS``, or
        false for a message that is always kept.
        """
        key = "d" if droppable is True else (droppable or None)
        if key is not None:
            if self.waiting[key] >= DROP_CAPS[key]:
                return
            self.waiting[key] += 1
        self.messages.put_nowait((text, key))

    def close(self) -> None:
        """Ends the connection once the messages already queued are sent; runs on the loop."""
        self.messages.put_nowait((None, None))

    async def next(self) -> str | None:
        """The next message to send; ``None`` means close the connection."""
        text, key = await self.messages.get()
        if key is not None:
            self.waiting[key] -= 1
        return text


class Hub:
    """Owns the source and forwards what it produces to the websocket clients.

    ``start`` opens the source on a thread (so a slow or failing headset does
    not hold up the page) and then forwards chunks on a second thread, which
    reaches each client's event loop with ``call_soon_threadsafe``.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.replay is not None:
            self.source: LiveSource | ReplaySource = ReplaySource(settings.replay)
        else:
            self.source = LiveSource(settings.port)
        self._lock = threading.RLock()
        self.steps = flow.load(settings.flow) if settings.flow else flow.default_flow()
        self.participants = Store(settings.data_dir / "participants.json")
        #: Set once the source is open and the clients have been told ``hello``.
        self.ready = threading.Event()
        #: Set when the source thread failed (a replay could not be opened).
        self._failed = threading.Event()
        #: Set once the first attempt to open the source has ended, whichever way it went.
        self._first_attempt = threading.Event()
        #: Whether the link is up (the source is open and reading), and a recording that was
        #: promised and waits for it: ``(session folder, label, identifier)``. Both change under
        #: ``_rec_lock``, which is never taken while ``_lock`` is held.
        self._link_up = False
        self._pending: tuple[Path, str, str] | None = None
        self._rec_lock = threading.RLock()
        #: The start-up checks (``checks.Check`` as dicts), told to each page on connect.
        self.checks: list[dict] = []
        #: When the link last opened (monotonic), and whether a live link has been lost since
        #: the last time it was up.
        self._opened_at = 0.0
        self._dropped = False
        #: What ``session.json`` holds, and the recordings set aside after lost links.
        self._session_meta: dict = {}
        self._recordings: list[dict] = []
        #: The text of a fault of the hub thread, once there was one (see ``_run``).
        self._fault: str | None = None
        #: The label and identifier the session belongs to, once one was accepted.
        self.participant: tuple[str, str] | None = None
        #: The ``(only, start_at, continues, imported)`` the accepted label came with (``only`` as a
        #: tuple of round ids in route order, so that one choice sent in another form is the same
        #: choice).
        self._choice: tuple = (None, None, None, None)
        #: Saved session files loaded on the participant screen, by the random token the page got:
        #: ``(importer.Saved, sha256, path of the kept copy in the data folder)``. In memory only.
        self._imports: dict[str, tuple[importer.Saved, str, str]] = {}
        self._import_lock = threading.Lock()
        self._accept_lock = threading.Lock()
        self._rev_sent = -1
        self._clients: list[Client] = []
        self.session: Session | None = None
        #: The session folder, once a participant label has been accepted.
        self.folder: Path | None = None
        self.ledger: Ledger | None = None
        self._participant: Client | None = None
        # What a client that connects now is told, in order: hello, error, link.
        self._state: dict[str, str] = {"link": _message(t="link", state="connecting", reason="")}
        self._thread: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._stopped = threading.Event()
        #: The zip of the session, made once the session is over.
        self.record = SessionRecord(self)
        #: The contact check as last switched: the ``seq`` of the ``contact_start`` it was
        #: switched on for, or ``None`` for off; and the states of the last second it gave.
        self._contact_lock = threading.Lock()
        self._contact_written: int | None = None
        self._contact_sent_at = 0.0  # time.monotonic() of the last contact byte written
        self._contact_last: tuple[int, dict] | None = None
        #: Of the live link: what counts the samples received since the last one that was, or may
        #: have been, recorded with the check on, and the sample rate (``contact_quiet``);
        #: ``None`` while no link is up. A new counter is put in place at each link-up.
        self._quiet_counter: tuple[contact.OffCounter, float] | None = None
        #: The signal check's window (``quality.SignalHolder``), a new one at each link-up.
        self._signal = quality.SignalHolder()
        #: The answer given to each page event's ``cid`` (``ack`` with ``seq`` or ``refused``).
        self._cids: dict[str, dict] = {}
        self._carried_seed: int | None = None
        #: Folders of earlier sessions that took late events in this process.
        self._late_folders: set[Path] = set()
        self._cid_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hub")
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._ticker is not None:
            self._ticker.join(timeout=3)
        # The headset is never left with its contact check on, whatever the session was doing.
        self._sync_contact(None, force=True)
        self.source.stop()
        if self._thread is not None:
            # An attempt to open the headset cannot be interrupted: wait it out (it closes its
            # own connection when it sees ``stop``), so nothing is left running behind.
            self._thread.join(timeout=STOP_WAIT)
        # The behaviour files are written before the zip (``package.BEFORE_ZIP``), so a process
        # stopped during a session zips the trials it has.
        self.record.end_for_exit()
        # Earlier sessions that took events after their zip get an additional zip now.
        for folder in sorted(self._late_folders):
            try:
                package.late_zip(folder)
            except Exception as error:
                self.log_failure("an additional zip for late events could not be made", error)
        if self.session is not None:
            self.session.close()
        if self.ledger is not None:
            self.ledger.close()

    def _idle_state(self) -> dict:
        """The state before a participant label has been accepted: no session exists.

        The first step, waiting, ``rev`` 0; ``phase`` is ``done`` and ``ended_early``
        true if the source ended first (nothing can be recorded any more).
        """
        step = self.steps[0]
        block = step.blocks[0] if step.blocks else None
        # A live link that ended is tried again until a session has passed the device step, so
        # without a session it is never the end of anything; a replay that ended is.
        ended = self.source.kind == "replay" and self.source.ended is not None
        return {
            "award": None,
            "step": step.id,
            "kind": step.kind,
            "block": block.id if block else None,
            "phase": "done" if ended else "waiting",
            "remaining": None if ended or block is None else block.seconds,
            "blocks_done": 0,
            "blocks_total": len(step.blocks),
            "points_total": 0,
            "ended_early": ended,
            "stopped": False,
            "failure": None,
            "reason": None,
            "plan": [s.id for s in self.steps],
            "outcomes": {
                s.id: "running" if s is step and not ended else "not_reached" for s in self.steps
            },
            "blocks_by_step": {s.id: 0 for s in self.steps},
            "substep": None,
            "contact_check": None,
            "calibration": None,
            "seed": None,
            "session_id": None,
            "only": None,
            "rev": 0,
        }

    def _check_choice(self, only, start_at, continues, imported=None) -> list[str] | None:
        """Refuses a choice of rounds that cannot be played, before anything is looked at or made.

        Returns the rounds ``only`` names, in route order (``None`` without ``only``).
        """
        for name, value in (("start_at", start_at), ("continues", continues), ("import", imported)):
            if value is not None and not isinstance(value, str):
                raise ParticipantRefused(400, f'"{name}" is text, or left out.', "bad_body")
        if only is not None and not isinstance(only, str) and not (
            isinstance(only, list) and all(isinstance(x, str) for x in only)
        ):
            raise ParticipantRefused(
                400, '"only" is a round id, a list of round ids, or left out.', "bad_body"
            )
        if continues is not None and imported is not None:
            raise ParticipantRefused(
                400,
                "Continuing an earlier session and starting a new one from a saved session file "
                "cannot be done at once.",
                "bad_selection",
            )
        if only is not None and start_at is not None:
            raise ParticipantRefused(
                400, "Choose one round only or a round to start at, not both.", "bad_selection"
            )
        if continues is not None and (only is not None or start_at is not None):
            raise ParticipantRefused(
                400,
                "Continuing an earlier session starts at its next round; "
                "it cannot be combined with a choice of rounds.",
                "bad_selection",
            )
        try:
            flow.select(self.steps, only, start_at)
        except flow.EmptyChoice as error:
            raise ParticipantRefused(
                400,
                "Choose at least one round, or play the whole route.",
                "bad_selection",
            ) from error
        except flow.RepeatedChoice as error:
            raise ParticipantRefused(
                400, "A round was chosen more than once. Choose each round once.", "bad_selection"
            ) from error
        except ValueError as error:
            raise ParticipantRefused(400, str(error), "unknown_step") from error
        return flow.chosen(self.steps, only)

    def _title(self, step_id: str) -> str:
        """The title of a step of this route (its id if the route has no such step)."""
        return next((s.title for s in self.steps if s.id == step_id), step_id)

    def _plan(self, ident: str, only, start_at, continues):
        """The steps to play, the session continued (or ``None``) and the rounds finished before.

        Raises ``ParticipantRefused`` for a ``continues`` that is not the unfinished newest session
        of this participant, and for a round that needs another round's results (``recall``
        needs ``words``) chosen as the only round, or chosen with others but not with that one,
        when the participant has not played that round in an earlier session.
        """
        found = progress.scan(self.settings.data_dir, ident)
        if continues is not None:
            earlier = found.latest
            if earlier is None or earlier.session != continues:
                raise ParticipantRefused(
                    409,
                    "That session cannot be continued: it is not an unfinished earlier session "
                    "of this participant.",
                    "not_continuable",
                )
            keep = set(earlier.rounds_from_next())
            steps = [s for s in self.steps if s.kind in progress.NOT_ROUNDS or s.id in keep]
            if not flow.rounds(steps):
                raise ParticipantRefused(
                    409,
                    "None of the rounds left in that session are in this route any more.",
                    "not_continuable",
                )
            return steps, earlier
        steps = flow.select(self.steps, only, start_at)
        names = flow.rounds(steps)
        self._carried_seed = None
        for step in steps:
            needs = progress.NEEDS.get(step.id)
            if not needs or step.id not in names:
                continue
            before = names[: names.index(step.id)]
            alone = names == [step.id]
            if (alone or (only is not None and needs not in before)) and needs not in found.finished_rounds:
                how = "on its own" if alone else f"without the {self._title(needs)} round"
                raise ParticipantRefused(
                    409,
                    f"{step.title} can be played {how} only after this participant has "
                    f"studied the {needs} lists in an earlier session, and none was found. "
                    "Play the whole route instead, or choose the words round.",
                    "recall_needs_words",
                )
            if needs not in before:
                # The round is played without the round it needs before it in this session (only,
                # start_at): it uses the material studied, the seed of the newest session in which
                # that round was finished or partly played.
                self._carried_seed = found.round_seeds.get(needs, self._carried_seed)
        return steps, None

    def accept(
        self,
        label: str,
        only: str | None = None,
        start_at: str | None = None,
        continues: str | None = None,
        imported: str | None = None,
    ) -> dict:
        """Accepts the participant label; the first one starts the session (and the recording).

        Returns ``{"label", "uuid"}``. The session folder
        ``<data-dir>/sessions/<label>_<YYYYMMDD>_<HHMMSS>/`` is created, the recording starts
        in its ``eeg/`` folder with the label and the identifier, and ``session.json`` is
        written there, only now: nothing is saved before. A live headset need not be linked:
        the label waits for the first attempt to open it (at most ``ready_wait`` seconds) and
        is accepted whatever came of it; if the link is not up the recording starts when it
        first is (``_begin_recording``), and until it has the device step cannot be passed.
        It is all or nothing: if the
        recording cannot start, or the label cannot be written to the participant list, the
        folder is kept and marked failed in its ``session.json`` (nothing is deleted), the list is
        unchanged and the label can be sent again (it gets a folder of its own).
        The label goes into the list last, so that a label that never got a session is not
        offered as "used before". Raises ``ParticipantRefused`` (with the HTTP status, a
        code and the words to show) and ``StoreDamaged`` for a damaged list.

        ``only`` (a round id, or a list of them) plays those rounds alone, in route order;
        ``start_at`` (a round id; not with ``only``) plays that round and the ones after it
        (``flow.select``). ``continues`` (the folder name of an
        earlier session) continues the newest unfinished session of this participant: the
        rounds from its next one on, the same word-list seed, and a points total that starts
        at its total. All of it is checked before anything is created.

        ``imported`` is the token ``load_saved`` gave for a saved session file: the session is a new
        one of the person in the file (its identifier, a ``sample_point`` one higher than any known
        and the earlier sessions in ``previous_sessions``; ``imported_from`` names the kept copy of
        the file and its sha256). ``label`` must be the file's.

        Every new session's ``session.json`` has ``sample_point`` and ``previous_sessions``
        (``importer.lineage``); a continued session has those of the session it continues.
        """
        if not valid(label):
            raise ParticipantRefused(400, RULE, "invalid_label")
        rounds = self._check_choice(only, start_at, continues, imported)
        wanted = (tuple(rounds) if rounds is not None else None, start_at, continues, imported)
        # Recorded as sent before: one round as its id, several as a list in route order.
        only = rounds if rounds is None or len(rounds) > 1 else rounds[0]
        loaded = None
        if imported is not None:
            with self._import_lock:
                loaded = self._imports.get(imported)
            if loaded is None:
                raise ParticipantRefused(
                    409, "The saved session file is no longer loaded. Load it again.", "unknown_import"
                )
            if label != loaded[0].label:
                raise ParticipantRefused(
                    409,
                    "A new session from a saved session file uses the participant ID in that file.",
                    "import_mismatch",
                )
        with self._accept_lock:
            if self.participant is not None:
                known, ident = self.participant
                if label != known:
                    raise ParticipantRefused(
                        409,
                        f"This session already belongs to {known}. "
                        "Close it and start brain-mapper again to use another label.",
                        "already_set",
                    )
                if wanted != self._choice:
                    raise ParticipantRefused(
                        409,
                        "This session was started with another choice of rounds. "
                        "Close it and start brain-mapper again to choose differently.",
                        "already_set",
                    )
                return {"label": known, "uuid": ident}
            if self.source.kind == "replay":
                deadline = time.monotonic() + self.settings.ready_wait
                while not self.ready.wait(0.05):
                    if self._failed.is_set():
                        raise ParticipantRefused(
                            409,
                            "The recording could not be opened, so nothing can be recorded. "
                            "Start brain-mapper again.",
                            "source_not_ready",
                        )
                    if time.monotonic() > deadline:
                        raise ParticipantRefused(
                            409,
                            "The recording is not open yet. Try again in a moment.",
                            "source_not_ready",
                        )
                if self.source.ended is not None:
                    raise ParticipantRefused(
                        409,
                        "The replay has ended, so nothing can be recorded. "
                        "Start brain-mapper again.",
                        "link_ended",
                    )
            else:
                # The label is accepted whether or not the headset is linked: the screen that
                # helps with the link comes after this one. The first attempt to open it is
                # given up to ``ready_wait`` seconds to finish, so that a headset that opens
                # normally is already recording when the session starts.
                if self._fault is None:
                    self._first_attempt.wait(self.settings.ready_wait)
                if self._fault is not None:  # also one that happened while this waited
                    raise ParticipantRefused(
                        409, f"{self._fault}. Start brain-mapper again.", "source_not_ready"
                    )
            ident = self.participants.peek(label)
            if loaded is not None:
                saved, digest, kept = loaded
                if self._identity_conflict(label, saved.uuid):  # before any look-up by identifier
                    raise ParticipantRefused(409, CONFLICT, "participant_conflict")
                ident = saved.uuid
            steps, earlier = self._plan(ident, only, start_at, continues)
            if earlier is not None:
                point, previous = importer.continued(earlier.folder)
            else:
                local = importer.history(self.settings.data_dir, ident)
                point, previous = importer.lineage(local, *((saved, digest) if loaded else ()))
            extra: dict = {"sample_point": point, "previous_sessions": previous}
            if loaded is not None:
                extra["imported_from"] = {
                    "file": kept,
                    "sha256": digest,
                    "session": saved.session,
                    "session_id": saved.session_id,
                    "sample_point": saved.sample_point,
                }
            with self._rec_lock:
                started = self._start_session(label, ident, steps, earlier, only, extra)
                try:
                    self.participants.identify(label, fresh=ident)
                except OSError as error:
                    self._discard(*started, f"the label could not be saved in the participant list ({error})")
                    raise ParticipantRefused(
                        500,
                        f"The label could not be saved in the participant list ({error.strerror or error}). "
                        "Nothing was recorded. You can try again.",
                        "write_failed",
                    ) from error
                except IdentityTaken as error:
                    self._discard(*started, "the identifier belongs to another label in the participant list")
                    raise ParticipantRefused(409, CONFLICT, "participant_conflict") from error
                except BaseException as error:
                    self._discard(*started, f"the label was not accepted ({type(error).__name__})")
                    raise
                self._publish(*started)
                self.participant = (label, ident)
                self._choice = wanted
            return {"label": label, "uuid": ident}

    def load_saved(self, handle, digest: str) -> dict:
        """Checks a saved session file (``importer.check``), keeps a copy (``importer.keep``) and
        returns what the participant screen shows of it, with a token for ``accept``.

        ``{"token", "label", "started_unix", "rounds_done", "rounds_total", "complete", "ending",
        "points_total", "sample_point", "next_sample_point", "continue"}``; ``continue`` is the
        existing offer (``next_step``, ``next_title``, ``points_total``, ``session``) only when that
        same session's folder is here and is the person's unfinished newest session, else ``None``.
        Raises ``importer.ImportRefused`` and ``StoreDamaged``. Nothing of the file is unpacked;
        no session folder is touched.
        """
        if self.participant is not None:
            raise importer.ImportRefused(
                "A session is already under way. Close brain-mapper and start it again to load a saved session file.",
                "already_set",
                409,
            )
        saved = importer.check(handle)
        token = secrets.token_hex(16)
        # The check of the identity and the registration of the file are one step under one lock,
        # and both come before anything is looked up by the identifier: two files of one person
        # under two labels never both load, and nothing of another label's sessions is read.
        with self._import_lock:
            if self._identity_conflict(saved.label, saved.uuid) or any(
                other.uuid == saved.uuid and other.label != saved.label for other, _, _ in self._imports.values()
            ):
                raise importer.ImportRefused(CONFLICT, "participant_conflict", 409)
            kept = importer.keep(self.settings.data_dir, handle, digest)
            self._imports[token] = (saved, digest, kept)
            while len(self._imports) > LOADED_FILES:
                self._imports.pop(next(iter(self._imports)))
        point, _ = importer.lineage(importer.history(self.settings.data_dir, saved.uuid), saved, digest)
        latest = progress.scan(self.settings.data_dir, saved.uuid).latest
        offer = None
        if latest is not None and latest.session == saved.session:
            offer = {
                "next_step": latest.next_step,
                "next_title": latest.next_title,
                "points_total": latest.points_total,
                "session": latest.session,
            }
        return {
            "token": token,
            "label": saved.label,
            "started_unix": saved.started_unix,
            "rounds_done": saved.rounds_done,
            "rounds_total": saved.rounds_total,
            "complete": saved.complete,
            "ending": saved.ending,
            "points_total": saved.points_total,
            "sample_point": saved.sample_point,
            "next_sample_point": point,
            "continue": offer,
        }

    def _identity_conflict(self, label: str, ident: str) -> bool:
        """Whether the list on this computer has ``label`` with another identifier, or ``ident``
        under another label (one identifier is one person under one label). ``StoreDamaged`` if
        the list cannot be used."""
        if label in self.participants.labels() and self.participants.peek(label) != ident:
            return True
        holder = self.participants.label_of(ident)
        return holder is not None and holder != label

    def _start_session(
        self,
        label: str,
        ident: str,
        steps: list[flow.Step],
        earlier=None,
        only: str | list[str] | None = None,
        extra: dict | None = None,
    ):
        """Makes the session folder, starts the recording and builds the session engine.

        Returns ``(folder, session, ledger)``; nothing is published to the clients yet.
        ``label`` has passed ``valid``, so it is safe in a file name. On any failure the
        recording is stopped and the folder removed, and ``ParticipantRefused`` is raised.
        ``earlier`` is the ``progress.Progress`` of the session this one continues; ``extra`` holds
        further fields of ``session.json`` (``sample_point``, ``previous_sessions``, ``imported_from``).
        """
        sessions = self.settings.data_dir / "sessions"
        folder = None
        recording = False
        ledger = None
        session = None
        try:
            folder = make_session_folder(sessions, label)
            meta = {
                "session": folder.name,
                "label": label,
                "uuid": ident,
                "started_unix": time.time(),
                "source": self.source.kind,
                "session_id": secrets.token_hex(8),
            }
            if earlier is not None:
                meta["continues"] = earlier.session
            if only is not None:
                meta["only"] = only  # a session of chosen rounds stands apart (see progress.scan)
            meta.update(extra or {})
            # Locked before session.json exists, so another process never finds it unlocked.
            self.record.claim(folder)
            if self._link_up:
                try:
                    self.source.start_recording(folder / "eeg", label, ident)
                    recording = True
                except LinkLost:
                    # The link dropped just now: the same as a link that is not up. The hub
                    # thread sets the drop aside and the recording starts when it is back.
                    self._pending = (folder, label, ident)
            else:
                # The headset is not linked yet: the recording starts when the link comes up
                # (``_begin_recording``); the device step cannot be passed before it has.
                self._pending = (folder, label, ident)
            _write_json(folder / "session.json", meta)
            self._session_meta = dict(meta)
            self._recordings = []
            carried = (earlier.points_total, earlier.session) if earlier is not None else None
            ledger = Ledger(folder / "points.jsonl", carried=carried)
            seed = earlier.seed if earlier is not None and earlier.seed is not None else None
            if earlier is None and self._carried_seed is not None:
                seed = self._carried_seed
            session = Session(
                steps,
                self.source,
                ledger,
                folder=folder,
                meta={
                    "session": folder.name,
                    "uuid": ident,
                    "continues": earlier.session if earlier is not None else None,
                    "only": only,
                    "session_id": meta["session_id"],
                    "seed": seed if seed is not None else secrets.randbelow(2**31),
                },
            )
        except BaseException as error:
            # Everything that was opened is closed again. The folder is kept, whatever it holds
            # (nothing under a session folder is ever deleted), and marked as failed in its
            # session.json; a later attempt with the same label gets a folder of its own.
            if session is not None:
                session.close()
            if ledger is not None:
                ledger.close()
            self._pending = None
            if recording:
                try:
                    self.source.stop_recording()
                except Exception:
                    LOG.error("the recording could not be closed after a failed start")
            self.record.unclaim()
            if folder is not None:
                self._mark_failed(folder, meta, f"the session could not be started ({error})")
            if isinstance(error, (RuntimeError, OSError)):
                raise ParticipantRefused(
                    500,
                    f"The recording could not be started ({error}). The label was not accepted; "
                    "the folder that was made is kept and marked as failed. You can try again.",
                    "recording_failed",
                ) from error
            raise
        return folder, session, ledger

    def _mark_failed(self, folder: Path, meta: dict, reason: str) -> None:
        """Writes ``failure`` into the folder's ``session.json`` if it can; nothing is removed."""
        try:
            _write_json(folder / "session.json", {**meta, "failure": reason})
        except OSError:
            LOG.error("session.json of a failed start could not be written")

    def _discard(self, folder: Path, session: Session, ledger: Ledger, reason: str) -> None:
        """Takes back a session that was started but not published: closes it, keeps its folder
        and marks it as failed."""
        session.close()
        ledger.close()
        self._pending = None
        try:
            self.source.stop_recording()
        except Exception:
            LOG.error("the recording could not be closed after a failed start")
        self.record.unclaim()
        self._mark_failed(folder, self._session_meta, reason)

    def _publish(self, folder: Path, session: Session, ledger: Ledger) -> None:
        """Makes the session the hub's and tells the clients its first state."""
        session.on_change = self._state_changed
        session.on_points = self._points
        session.contact_states = self._contact_states
        session.contact_quiet = self._contact_quiet
        session.signal_window = self._signal
        with self._lock:
            self.folder = folder
            self.session = session
            self.ledger = ledger
        self._state_changed(session.state())

    def _state_changed(self, state: dict) -> None:
        """Sends ``state`` unless a newer one (a higher ``rev``) has gone out already.

        Two threads can reach here with an old and a new state in either order;
        the check and the sending are under one lock, so clients never see ``rev``
        go backwards. A state whose phase is ``done`` starts the session zip.
        """
        self.record.on_state(state)
        with self._lock:
            if state["rev"] < self._rev_sent:
                return
            self._rev_sent = state["rev"]
            self._emit("state", _message(t="state", **state))
        self._sync_contact(state.get("contact_check"))

    def _sync_contact(self, wanted: int | None, force: bool = False) -> None:
        """Switches the headset's contact check to what the session wants: on (``0x11``) for the
        ``contact_start`` whose ``seq`` is ``wanted``, off (``0x12``) for ``None``.

        A new ``wanted`` (Check again) is sent even if the check is on already. ``force`` sends
        it whatever was sent before (when the link comes up, and at the end). A write that fails
        is logged and tried again at the next change.
        """
        with self._contact_lock:
            if not force and wanted == self._contact_written:
                return
            try:
                self.source.set_contact_check(wanted is not None)
            except Exception as error:
                if force and wanted is None and not self._link_up:
                    return  # no headset is open: there is nothing to switch off
                LOG.warning(
                    "the contact check could not be switched %s (%s)",
                    "off" if wanted is None else "on",
                    error,
                )
                return
            self._contact_written = wanted
            self._contact_sent_at = time.monotonic()
            if wanted is None:
                self._contact_last = None

    def _guard_contact_off(self) -> bool:
        """Sends ``0x12`` again if the check should still be off (read under the lock, so a Check
        again that has just switched it on is never undone) and the last contact byte went out
        ``CONTACT_GUARD_SECONDS`` ago or more. Whether it was sent."""
        with self._contact_lock:
            if self._contact_written is not None:
                return False
            if time.monotonic() - self._contact_sent_at < CONTACT_GUARD_SECONDS:
                return False
            try:
                self.source.set_contact_check(False)
            except Exception as error:
                LOG.warning("the contact check could not be switched off (%s)", error)
                return False
            self._contact_sent_at = time.monotonic()
            return True

    def _contact_states(self, check: int) -> dict | None:
        """The states of the last second measured for the contact check of the ``contact_start``
        whose ``seq`` is ``check``, while it is the one switched on; else ``None``."""
        with self._contact_lock:
            last = self._contact_last
            if last is None or last[0] != check or self._contact_written != check:
                return None
            return dict(last[1])

    def _contact_quiet(self) -> tuple[int, float] | None:
        """``(samples, sample_rate)``: the samples received since the last one that was, or may
        have been, received with the contact check on (``contact.OffCounter``; since the link
        came up when there was none); ``0`` samples before any data or while no link is up;
        ``None`` for a source the hub does not switch (a replay)."""
        if self.source is None or self.source.kind != "live":
            return None
        held = self._quiet_counter
        if held is None:
            return 0, 1.0
        counter, rate = held
        return counter.quiet_samples(), rate

    def _wanted_contact(self) -> int | None:
        session = self.session
        return session.state()["contact_check"] if session is not None else None

    def _hold_contact(self, reason: str) -> None:
        """The participant page went away (closed or reloaded): the contact check goes off."""
        session = self.session
        if session is None:
            return
        try:
            session.contact_hold(reason)
        except Exception:
            LOG.exception("the contact check could not be held after the page went away")

    def _points(self, message: dict) -> None:
        """Sends a ``points`` message; the engine calls this right after the ``between`` state."""
        self._emit("points", _message(**message))

    def _tick(self) -> None:
        """Ticks the engine; each client gets a state at least once a second (see ``_heartbeat``)."""
        failure = None
        beat_failure = None
        while not self._stopped.wait(TICK):
            try:
                session = self.session
                if session is not None:
                    session.tick()
                failure = None
            except Exception as error:
                # The engine undid the failed step, so the next tick tries again.
                # Each distinct failure is logged and sent to the pages once.
                text = f"{type(error).__name__}: {error}"
                if text != failure:
                    failure = text
                    self.log_failure("the session engine failed; it will try again", error)
                    self._emit_error(error)
            try:
                self._heartbeat()
                beat_failure = None
            except Exception as error:
                # The same care for the heartbeat: one that fails must not end the ticker.
                text = f"{type(error).__name__}: {error}"
                if text != beat_failure:
                    beat_failure = text
                    self.log_failure("the state heartbeat failed; it will try again", error)
                    self._emit_error(error)

    def log_failure(self, what: str, error: BaseException, label: str | None = None) -> None:
        """Logs ``error`` as its type and message, with every path under the data folder replaced
        by ``<data>/...`` and the labels known here (``label``, given by a caller that handles one,
        and the session's) by ``<label>``; no traceback (it would show them)."""
        who = self.participant
        text = package.scrub_paths(
            f"{type(error).__name__}: {error}", self.settings.data_dir, (label, who[0] if who else None)
        )
        LOG.error("%s: %s", what, text)

    def _emit_error(self, error: Exception) -> None:
        """Tells the pages about a failure; a failure of this must not end the ticker either."""
        try:
            self._emit("error", _message(t="error", kind=type(error).__name__, msg=str(error)))
        except Exception as fault:
            self.log_failure("the page could not be told about a failure", fault)

    def _heartbeat(self) -> None:
        """Sends the current state to each client that has had none for a second.

        The time is kept per client: a state sent to one client (a connect, a change)
        neither delays nor repeats the heartbeat of another.
        """
        session = self.session
        if session is None and not (self.ready.is_set() or self.source.kind == "live"):
            return  # a replay that is still opening has no state to tell yet
        with session.notify_lock if session is not None else self._lock, self._lock:
            now = time.monotonic()
            due = [c for c in self._clients if now - c.state_at >= 1.0]
            if not due:
                return
            state = session.state() if session is not None else self._idle_state()
            if state["rev"] < self._rev_sent:
                return
            self._rev_sent = state["rev"]
            text = _message(t="state", **state)
            for client in due:
                client.state_at = now
                self._send(client, text, False)
            self._drop_dead()

    def _emit(self, kind: str, text: str, state: str | None = None) -> None:
        """Sends ``text`` to every client; ``state`` also keeps it for later clients."""
        with self._lock:
            if state is not None:
                self._state[state] = text
            now = time.monotonic()
            for client in self._clients:
                if kind == "d" and not client.traces:
                    continue
                if kind == "trace" and not client.display:
                    continue
                if kind == "state":
                    client.state_at = now
                out = _hello_for(client, text) if kind == "hello" else text
                self._send(client, out, kind if kind in DROP_CAPS else False)
            self._drop_dead()

    def _send(self, client: Client, text: str, droppable: bool | str) -> None:
        """Queues ``text`` for ``client`` on its loop. A loop that has been closed cannot take it,
        and that client will never be reached again: it is marked, and dropped by ``_drop_dead``."""
        try:
            client.loop.call_soon_threadsafe(client.offer, text, droppable)
        except RuntimeError:
            client.dead = True

    def _drop_dead(self) -> None:
        """Forgets the clients whose event loop was closed (the caller holds the lock)."""
        for client in [c for c in self._clients if c.dead]:
            self._clients.remove(client)
            if self._participant is client:
                self._participant = None
            LOG.warning("a page whose connection had closed was dropped")

    def _wanted(self) -> tuple[bool, bool]:
        """Whether any client wants ``d`` messages, and whether any wants ``trace`` messages."""
        with self._lock:
            return (
                any(client.traces for client in self._clients),
                any(client.display for client in self._clients),
            )

    def _link_message(
        self, state: str, reason: str = "", problem: LinkProblem | None = None, attempt: int = 0
    ) -> str:
        """A ``link`` message (see the module's message list)."""
        detail = None
        if problem is not None:
            detail = {
                **problem.as_dict(),
                "dropped": self._dropped,
                "attempt": attempt,
                "retry_s": RETRY_SECONDS,
            }
        return _message(
            t="link", state=state, reason=reason, recording=self.source.recording, problem=detail
        )

    def _run(self) -> None:
        self._ticker = threading.Thread(target=self._tick, daemon=True, name="ticker")
        self._ticker.start()
        try:
            if self.source.kind == "live":
                self._run_live()
            else:
                self._run_replay()
        except Exception as error:  # a fault while forwarding, or a replay that could not be opened
            if self._stopped.is_set():
                who = self.participant
                LOG.info(
                    "the source thread ended with an error while stopping: %s",
                    package.scrub_paths(
                        f"{type(error).__name__}: {error}", self.settings.data_dir, (who[0] if who else None,)
                    ),
                )
                return
            self.log_failure("the source thread failed", error)
            self._failed.set()
            self._first_attempt.set()  # a label waiting for the first attempt is not held up
            # A fault inside brain-mapper (the live thread, or a replay that was already
            # playing) is told as such: it is not a dropped link, and nothing will be tried
            # again, so a session waiting at the device step ends here, with a failure.
            fault = self.source.kind == "live" or self.ready.is_set()
            if fault:
                self._fault = (
                    "brain-mapper stopped reading the headset because of an error inside "
                    f"brain-mapper ({type(error).__name__}: {error})"
                )
            self._emit(
                "error",
                _message(t="error", kind=type(error).__name__, msg=str(error)),
                "error",
            )
            self._emit(
                "link", _message(t="link", state="ended", reason=str(error), fault=fault), "link"
            )
            session = self.session
            if fault and session is not None:
                session.fail(self._fault)

    def _run_replay(self) -> None:
        self.source.open()
        ended = self._serve()
        if ended is not None:
            self._end_of_link(ended)

    def _run_live(self) -> None:
        """Opens the headset (trying again every ``RETRY_SECONDS`` while it will not open) and
        forwards what it sends; a lost link is tried again until the device step has been passed."""
        while not self._stopped.is_set():
            if not self.source.connect_with_retry(self._open_failed):
                return
            ended = self._serve()
            if ended is None or self._stopped.is_set():
                return
            if not self._may_retry():
                self._end_of_link(ended)
                return
            if not self._link_lost(ended):
                self._end_of_link(ended)  # the session ended meanwhile: nothing is set aside
                return
            # A link that keeps dropping is not tried at once again: each try asks the headset
            # for its configuration and each lost link sets a recording aside.
            if self._stopped.wait(RETRY_SECONDS):
                return

    def _may_retry(self) -> bool:
        """Whether a live link that ended is tried again: while there is no session or the
        session is not over (after the device step the session is paused, see ``_link_lost``)."""
        session = self.session
        return session is None or not session.is_over()

    def _open_failed(self, problem: LinkProblem, attempt: int) -> None:
        """One attempt to open the headset came back with ``problem``: tell the pages."""
        self._first_attempt.set()
        self._emit("link", self._link_message("connecting", problem.msg, problem, attempt), "link")

    def _link_lost(self, ended: SourceEnded) -> bool:
        """A live link ended before the device step was passed: the recording it made is set
        aside (``eeg-lost-1``, ``-2`` ... in the session folder, whole and closed), a new one
        starts when the link is back, and the pages are told.

        Nothing is deleted: a set-aside folder is kept even if it holds no samples. The drop
        and the folder are noted in ``events.jsonl`` (``link_lost``) and in ``session.json``
        (``recordings``, each with ``decoded_incl_lost``: the samples decoded while it was open,
        lost ones included, ``null`` if not known; informational).

        The state of the session is looked at again under ``_rec_lock``: once the session is over
        (stopped, failed, or the device step passed) a drop sets nothing aside and starts nothing
        (the recording may already be closed, even zipped); ``False`` is returned and the caller
        ends the link. Returns ``True`` when the drop was handled and the link will be tried again.
        """
        with self._rec_lock:
            self._link_up = False
            session = self.session
            if session is not None and session.is_over():
                return False
            self._dropped = True
            folder, who = self.folder, self.participant
            if folder is not None and who is not None:
                try:
                    self.source.stop_recording()  # closes one a late start left open
                except Exception:
                    LOG.error("the recording of a lost link could not be closed")
                eeg = folder / "eeg"
                decoded = self.source.last_recording_decoded if eeg.exists() else None
                kept = None
                if eeg.exists():
                    n = 1
                    while (folder / f"eeg-lost-{n}").exists():
                        n += 1
                    try:
                        eeg.rename(folder / f"eeg-lost-{n}")
                        kept = f"eeg-lost-{n}"
                    except OSError:
                        LOG.error("the recording of a lost link could not be set aside")
                if kept is not None:
                    self._recordings.append({"folder": kept, "decoded_incl_lost": decoded})
                    self._write_recordings(folder)
                self._pending = (folder, who[0], who[1])
                session = self.session
                if session is not None:
                    session.link_lost(
                        {
                            "reason": ended.reason,
                            "earlier_recording": kept,
                            "decoded_incl_lost": decoded,
                            # The last sample of the closed recording, from the samples decoded
                            # while it was open (lost ones included); ``null`` if not known.
                            "last_sample": decoded - 1 if isinstance(decoded, int) and decoded > 0 else None,
                            # A silent link (no sample for ``SILENT_SECONDS``) says how long it waited.
                            **({"silent_s": SILENT_SECONDS} if ended.reason == SILENT else {}),
                        }
                    )
            self.source.reset()
        problem = reason_problem(ended.reason)
        self._emit("link", self._link_message("connecting", ended.reason, problem, 0), "link")
        return True

    def _write_recordings(self, folder: Path) -> None:
        """Puts the set-aside recordings into ``session.json`` (``recordings``, in order, each
        with the folder and ``decoded_incl_lost``; ``null`` if not known)."""
        meta = dict(self._session_meta, recordings=list(self._recordings))
        try:
            _write_json(folder / "session.json", meta)
        except OSError:
            LOG.error("session.json could not be updated with the set-aside recordings")

    def _begin_recording(self, back: bool = False) -> None:
        """Starts the recording that was promised when the label was accepted (caller holds
        ``_rec_lock``). If the link ended meanwhile, that is a drop like any other: the
        recording stays promised and the next link-up starts it. If it cannot start for another
        reason, the session ends once, with that as its failure. ``back`` says this link
        came back after a drop (``link_back`` is noted in ``events.jsonl``)."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        if self._stopped.is_set():
            self._pending = pending
            return
        session = self.session
        if session is not None and session.is_over():
            return  # the session is over: nothing is started for it, so nothing stays open
        folder, label, ident = pending
        try:
            self.source.start_recording(folder / "eeg", label, ident)
        except LinkLost:
            self._pending = pending
            return
        except Exception as error:
            self.log_failure("the recording could not be started when the link came up", error)
            session = self.session
            if session is not None:
                session.fail(f"the recording could not be started ({error})")
            return
        session = self.session
        if back and session is not None:
            session.link_back(
                {
                    "recording": "eeg",
                    "first_sample": 0,  # a new recording counts its samples from 0
                    "earlier_recordings": [r["folder"] for r in self._recordings],
                }
            )

    def _end_of_link(self, ended: SourceEnded) -> None:
        with self._lock:
            if self.session is None:  # no session will tell the pages: the idle state does
                self._emit("state", _message(t="state", **self._idle_state()))
        self._emit("link", self._link_message("ended", ended.reason), "link")

    def _serve(self) -> SourceEnded | None:
        """Announces the open source and forwards its chunks until it ends; returns what it
        ended with, or ``None`` if the hub was stopped first."""
        source = self.source
        config = source.config
        inbox = source.subscribe(FORWARDER_QUEUE)
        extra = {"name": Path(self.settings.replay).name} if self.settings.replay else {}
        self._emit(
            "hello",
            _message(
                t="hello", fs=config.sample_rate, labels=config.labels, kind=source.kind, **extra
            ),
            "hello",
        )
        with self._rec_lock:
            back = self._dropped
            self._link_up = True
            quiet = contact.OffCounter()
            self._quiet_counter = (quiet, float(config.sample_rate))
            self._dropped = False
            self._opened_at = time.monotonic()
            self.ready.set()
            self._begin_recording(back)
        # Whatever the headset was doing before (it may come on with its contact check on, or a
        # link was lost with the check on), it is switched to what the session wants now.
        self._sync_contact(self._wanted_contact(), force=True)
        with self._lock:
            if self.session is None:
                self._emit("state", _message(t="state", **self._idle_state()))
        self._emit("link", self._link_message("live"), "link")
        self._first_attempt.set()
        stats = display.LinkStats()
        thinner = display.Decimator(config.sample_rate, len(config.labels))
        monitor = contact.ContactMonitor(config)
        self._signal.attach(config)
        measuring: int | None = None  # the contact check the monitor's window belongs to
        reported = time.monotonic()
        warned = False
        # The silent-link watchdog (live only): when no chunk has come for ``SILENT_SECONDS`` on
        # this thread's own clock, the link is ended as lost (``drop_link`` closes the port, the
        # reader ends with ``SILENT``) and the same path as a port that failed follows.
        watch = source.kind == "live"
        last_chunk = time.monotonic()
        cut = False
        while not self._stopped.is_set():
            now = time.monotonic()
            if now - reported >= 1.0:
                reported = now
                self._emit("status", _message(**stats.message(now, self._opened_at)))
                self._report_contact(monitor, measuring)
            if watch and not cut and now - last_chunk >= SILENT_SECONDS:
                cut = True
                LOG.warning(
                    "no sample from the headset for %.1f s: the link counts as lost", now - last_chunk
                )
                source.drop_link(SILENT)
            try:
                item = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(item, SourceEnded):
                self._quiet_counter = None
                return item
            arrived = time.monotonic()
            if watch and not cut and arrived - last_chunk >= STALL_SECONDS:
                # A stall under the limit: the link is kept, and the gap is noted, not hidden.
                session = self.session
                if session is not None:
                    session.note("link_stall", {"seconds": round(arrived - last_chunk, 3)})
            last_chunk = arrived
            stats.add(time.monotonic(), item.samples, item.lost, item.battery_volts, item.impedance_on)
            quiet.push(item.index, item.samples, item.impedance_on)
            self._signal.push(item)
            with self._contact_lock:
                wanted = self._contact_written
            if wanted != measuring:
                monitor.reset()
                measuring = wanted
            if wanted is not None:
                monitor.push(item)
            elif item.impedance_on and self._guard_contact_off():
                # The data say the check is on although it should be off: it was switched off again.
                if not warned:
                    warned = True
                    LOG.warning("the headset sends with its contact check on; switching it off again")
            wants_d, wants_trace = self._wanted()
            if wants_d:
                rows = np.round(item.data * 1e6, 2).tolist()
                self._emit(
                    "d",
                    _message(
                        t="d",
                        x=rows,
                        lost=item.lost,
                        bat=item.battery_volts,
                        imp=item.impedance_on,
                    ),
                )
            if wants_trace:
                thin = thinner.push(item.data)
                if thin is not None:
                    self._emit("trace", _message(t="trace", hz=display.DISPLAY_HZ, x=thin))
            else:
                thinner.reset()
        return None

    def _report_contact(self, monitor: contact.ContactMonitor, measuring: int | None) -> None:
        """Sends the ``contact`` message of the last second, while the contact check is on:
        ``settling`` (no states) until the first measure of the check."""
        states = monitor.states()
        amplitudes = monitor.amplitudes() if states is not None else None
        with self._contact_lock:
            if measuring is None or measuring != self._contact_written:
                return
            if states is not None:
                self._contact_last = (measuring, states)
        good = sum(1 for state in states.values() if state == "good") if states is not None else None
        self._emit(
            "contact",
            _message(
                t="contact",
                states=states,
                good=good,
                check=measuring,
                settling=states is None and monitor.unmeasurable is None,
                sample=monitor.sample,
                error=monitor.unmeasurable,
                tone_seen=contact.tone_seen(amplitudes) if amplitudes is not None else None,
            ),
        )

    def connect(self, client: Client) -> bool:
        """Adds ``client`` and queues the current state for it, with no gap.

        Returns ``False``, and adds nothing, for a second participant.
        """
        reloaded = False
        with self._lock:
            if client.role == "participant":
                old = self._participant
                if old is not None:
                    if client.page is None or client.page != old.page:
                        return False
                    reloaded = True
                    # The same page again (a reload): it takes over, the old socket is closed.
                    if old in self._clients:
                        self._clients.remove(old)
                    try:
                        old.loop.call_soon_threadsafe(old.close)
                    except RuntimeError:
                        pass  # its loop is closed: there is no socket left to close
                self._participant = client
            for key in ("hello", "error", "link"):
                if key in self._state:
                    text = self._state[key]
                    client.offer(_hello_for(client, text) if key == "hello" else text)
            if self.checks:
                client.offer(_message(t="checks", items=self.checks))
            if self.record.message is not None and self.record.for_client(client):
                client.offer(self.record.text_for(client))
            if self.session is not None:
                state = self.session.state()
                self._rev_sent = max(self._rev_sent, state["rev"])
                client.state_at = time.monotonic()
                client.offer(_message(t="state", **state))
            elif self.ready.is_set() or self.source.kind == "live":
                client.state_at = time.monotonic()
                client.offer(_message(t="state", **self._idle_state()))
            self._clients.append(client)
        if reloaded:  # outside the hub's lock: the engine's lock comes first
            self._hold_contact("page_reloaded")
        return True

    def set_traces(self, client: Client, on: bool) -> None:
        with self._lock:
            client.traces = on

    def set_display(self, client: Client, on: bool) -> None:
        with self._lock:
            client.display = on

    def disconnect(self, client: Client) -> None:
        left = False
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
            if self._participant is client:
                self._participant = None
                left = True
        if left:
            self._hold_contact("page_left")

    def _late_event(self, command: dict, owner) -> dict:
        """Saves an event that a page kept for another session (sent after a restart or after the
        session ended): appended to that session's ``events.jsonl``, with ``late: true`` and no
        marker, if its folder is on this server; otherwise answered as ``dropped`` (the page then
        sends one ``events_dropped`` report in the current session). A folder that was zipped
        already takes it too (nothing is lost): ``session.json`` counts it in
        ``late_events_after_zip`` and the next packaging (at stop, or at the next start) makes an
        additional zip of the folder (``package.late_zip``). While another holder has the folder's
        lock (it is being zipped) the event is not answered, so the page keeps it and sends it
        again after its next reconnect. Returns the answer, or ``None`` for no answer. Nothing is
        said to the participant."""
        cid = command.get("cid")
        if not isinstance(cid, str) or not CID.fullmatch(cid) or not isinstance(owner, str):
            raise ValueError('a late event needs its "cid" and "session_id"')
        with self._cid_lock:
            if cid in self._cids:
                return self._cids[cid]
            folder = self._session_folder_of(owner)
            answer = {"t": "ack", "cid": cid, "dropped": True}
            if folder is not None:
                try:
                    # Under the folder's lock (``package.locked``), taken without waiting: a folder
                    # being zipped, or zipped already (``complete`` or ``zip`` in session.json), takes
                    # nothing more, so a zip always holds the folder as it stays.
                    with package.locked(folder, wait=False) as held:
                        if not held:
                            LOG.warning("a late event waits: its session folder is in use")
                            return None
                        zipped = _packaged(folder)
                        if _append_late(folder, command, cid) and zipped:
                            package.note_late(folder, time.time())
                        answer = {"t": "ack", "cid": cid, "late": True}
                        self._late_folders.add(folder)
                        try:
                            behavior.write(folder)  # that session's behavior files take it in
                        except OSError as error:
                            LOG.error("the behavior files could not be written (%s)", error.strerror or error)
                except (OSError, ValueError) as error:
                    LOG.error("a late event could not be saved (%s)", type(error).__name__)
            self._cids[cid] = answer
        return answer

    def _session_folder_of(self, session_id: str) -> Path | None:
        """The folder of the session with ``session_id`` under ``<data-dir>/sessions``, if it is
        there and is not the session running (zipped or not)."""
        sessions = self.settings.data_dir / "sessions"
        current = self.session.meta.get("session_id") if self.session is not None else None
        if session_id == current:
            return None
        try:
            folders = [p for p in sessions.iterdir() if p.is_dir()]
        except OSError:
            return None
        for folder in folders:
            try:
                meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("session_id") == session_id:
                return folder
        return None

    def command(self, client: Client, command: dict) -> None:
        """Runs a session command of the participant; others are ignored.

        Raises ``CommandRefused`` for a command the engine refuses and
        ``ValueError`` for one that is unknown.
        """
        with self._lock:
            current = client is self._participant
        if client.role != "participant":
            return
        if not current:
            # Not run, and said so: the page that sent it was replaced (a reload of the same page).
            client.offer(
                _message(
                    t="error",
                    kind="replaced",
                    msg="this connection was replaced by a newer one of the same page; "
                    "the command was not run",
                )
            )
            return
        session = self.session
        name = command.get("cmd")
        owner = command.get("session_id") if name == "event" else None
        if owner is not None and (session is None or owner != session.meta.get("session_id")):
            answer = self._late_event(command, owner)
            if answer is not None:
                client.offer(_message(**answer))
            return
        if session is None:
            raise CommandRefused("enter a participant ID first: the session starts when one is accepted")
        if name == "advance":
            session.advance()
        elif name == "pause":
            session.pause(str(command.get("reason", "")))
        elif name == "resume":
            session.resume()
        elif name == "skip":
            step = command.get("step")
            if step is not None and not isinstance(step, str):
                raise ValueError('"step" is the id of the round shown, or left out')
            session.skip(step)
        elif name == "stop":
            session.stop()
        elif name == "check_again":
            session.check_again()
        elif name == "video_ended":
            step = command.get("step")
            if step is not None and not isinstance(step, str):
                raise ValueError('"step" is the id of the round shown, or left out')
            session.video_ended(step)
        elif name == "block_finished":
            step = command.get("step")
            if step is not None and not isinstance(step, str):
                raise ValueError('"step" is the id of the round shown, or left out')
            reason = command.get("reason")
            if not isinstance(reason, str):
                raise ValueError('"reason" is "participant_done" or "trials_done"')
            session.block_finished(reason, step)
        elif name == "headset_check":
            session.headset_check()
        elif name == "headset_check_done":
            session.headset_check_done()
        elif name == "event":
            cid = command.get("cid")
            if cid is None:
                session.event(
                    command.get("name"),
                    command.get("payload", {}),
                    command.get("browser_time"),
                    command.get("time_origin"),
                )
                return
            if not isinstance(cid, str) or not CID.fullmatch(cid):
                raise ValueError('"cid" is 1 to 64 of A-Z a-z 0-9 _ -, or left out')
            # A cid is written once: an event sent again (after a reconnect or a reload) whose
            # first copy arrived is only acknowledged again.
            with self._cid_lock:
                answer = self._cids.get(cid)
                if answer is None:
                    payload = _with_cid(command.get("payload", {}), cid)
                    try:
                        seq = session.event(
                            command.get("name"),
                            payload,
                            command.get("browser_time"),
                            command.get("time_origin"),
                        )
                        answer = {"t": "ack", "cid": cid, "seq": seq}
                        if command.get("name") == "events_dropped" and isinstance(payload, dict):
                            LOG.warning(
                                "a page reported %s events of an earlier session that could not be saved",
                                payload.get("count"),
                            )
                    except BadEvent as error:
                        # Never stored, and not acknowledged as stored: an error naming what is
                        # wrong, with the cid, so the page drops it instead of sending it again.
                        answer = {"t": "error", "kind": "bad_event", "cid": cid, "msg": str(error)}
                    except CommandRefused as error:
                        answer = {"t": "ack", "cid": cid, "refused": str(error)}
                    self._cids[cid] = answer
            client.offer(_message(**answer))
        else:
            raise ValueError(f"unknown command {name!r}")


class SessionRecord:
    """The zip of the hub's session, made on a thread of its own once the session is over.

    While the session runs its folder is locked (``package.locked``), so that another brain-mapper
    process started on the same data folder leaves it alone; the same lock is held through
    ``package.finish``, so there is no moment in which another process could zip it. If the lock
    cannot be taken the page and the log are told. When a state whose phase is ``done`` arrives,
    the recording is closed and ``package.finish`` runs, off the event loop. The pages are told
    ``{"t":"package","state":"making"|"saved"|"failed","folder","file","msg","summary","notes",
    "download"}``: ``folder`` is the sessions folder, ``file`` the zip's name, ``msg`` a failure in
    words, ``summary`` what the summary screen shows (``package.summary``), ``notes`` words about
    what did not go as planned (a hook that failed, a link that was not zipped), and ``download``
    the address of the zip. The message goes only to the participant connection (``for_client``):
    an onlooker is never sent it, during the session or after.
    """

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._key = secrets.token_urlsafe(24)
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        self._locked: Path | None = None
        self._problem: str | None = None
        #: The last ``package`` message (``download`` is added per client), or ``None``.
        self.message: dict | None = None
        #: The verified zip, once it is saved.
        self.zip: Path | None = None
        #: The analysis of the session (``analysis.Job``), started once the zip is saved.
        self.analysis = None

    def allows(self, key: str) -> bool:
        """Whether ``key`` is the one given to the participant connection."""
        return secrets.compare_digest(key.encode("utf-8", "replace"), self._key.encode())

    def for_client(self, client: Client) -> bool:
        """Whether ``client`` is told about the record at all: only the participant connection.

        An onlooker never is: the message names the session folder and the zip and carries the
        summary, all of which hold the participant label, and the download key is the
        participant's alone. (The hub's lock is held by the caller.)
        """
        return client.role == "participant" and client is self.hub._participant

    def text_for(self, client: Client) -> str:
        """The ``package`` message for ``client``; only the participant connection gets ``download``."""
        message = dict(self.message or {})
        own = client.role == "participant" and client is self.hub._participant
        saved = message.get("state") == "saved"
        message["download"] = f"/api/session/zip?key={self._key}" if own and saved else None
        message["results"] = f"/api/results?key={self._key}" if own and saved else None
        return _message(**message)

    def _scrub(self, text: str) -> str:
        who = self.hub.participant
        return package.scrub(text, self.hub.folder, who[0] if who else None)

    def _log(self, what: str, error: BaseException) -> None:
        """Logs a failure as its type and message, the session folder and label replaced (no traceback)."""
        self.hub.log_failure(what, error)

    def claim(self, folder: Path) -> None:
        """Takes the lock of a new session folder (held until its zip is made); a lock that cannot
        be taken is remembered and reported once the session is published."""
        self._problem = None
        if package.fcntl is None:
            return
        try:
            fd = os.open(folder, os.O_RDONLY)
        except OSError as error:
            self._problem = f"the session folder could not be opened to lock it ({error.strerror})"
            return
        try:
            package.fcntl.flock(fd, package.fcntl.LOCK_EX | package.fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            self._problem = f"the session folder could not be locked ({error.strerror})"
            return
        self._lock_fd = fd

    def unclaim(self) -> None:
        """Lets go of the lock of a session that was not started after all."""
        self._release()
        self._problem = None

    def on_state(self, state: dict) -> None:
        """Reports a lock that could not be taken (once); starts the zip once the session is over."""
        folder = self.hub.folder
        if folder is not None and self._locked is None:
            self._locked = folder
            problem = self._problem
            if problem is not None:
                text = (
                    f"{problem[0].upper()}{problem[1:]}. Another brain-mapper process started on the same "
                    "data folder could touch this session; run only one at a time."
                )
                LOG.warning("%s", self._scrub(text))
                self.hub._emit("error", _message(t="error", kind="folder_not_locked", msg=text))
        if state.get("phase") == "done":
            self.start()

    def start(self) -> None:
        """Starts making the zip, once."""
        with self._guard:
            if self._thread is not None or self.hub.session is None or self.hub.folder is None:
                return
            # Not a daemon: a process that is asked to stop still finishes the zip it began.
            self._thread = threading.Thread(target=self._run, name="package")
            self._thread.start()

    def end_for_exit(self) -> None:
        """Ends a session still under way (the process is stopping) and waits for its zip.

        Runs after the source has stopped: a tick then ends the session early (the source ended
        with the reason ``stopped``), and that state starts the zip as every other ending does.
        """
        session = self.hub.session
        if session is None:
            return
        if session.state()["phase"] != "done":
            try:
                session.tick()
            except Exception as error:
                self._log("the session could not be ended before the process stopped", error)
        self.start()
        thread = self._thread
        if thread is not None:
            thread.join()

    def _release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing it releases the lock
            self._lock_fd = None

    def _tell(self, state: str, **fields) -> None:
        folder = self.hub.folder
        self.message = {
            "t": "package",
            "state": state,
            "folder": str(folder.parent) if folder is not None else None,
            "file": None,
            "msg": None,
            "summary": None,
            "notes": [],
            **fields,
        }
        hub = self.hub
        with hub._lock:
            for client in hub._clients:
                if self.for_client(client):
                    hub._send(client, self.text_for(client), False)
            hub._drop_dead()

    def _run(self) -> None:
        try:
            self._make()
        finally:
            self._release()

    def _make(self) -> None:
        hub, folder, session = self.hub, self.hub.folder, self.hub.session
        assert folder is not None and session is not None
        try:
            with hub._rec_lock:
                hub._pending = None  # a link that comes up now records nothing into this folder
                hub.source.stop_recording()  # the recording is complete before anything is zipped
        except Exception as error:
            self._log("the recording could not be closed, so the session zip was not made", error)
            self._tell("failed", msg=self._failure(folder, f"the recording could not be closed: {error}"))
            return
        state = session.state()
        # Complete only if the participant finished the last step; ``package`` checks that too.
        complete = state["phase"] == "done" and not (state["stopped"] or state["ended_early"] or state["failure"])
        ended = hub.source.ended
        reason = state["failure"] or (ended.reason if ended is not None and state["ended_early"] else None)
        try:
            summary = package.summary(folder)
        except Exception as error:
            self._log("the summary of the session could not be read", error)
            summary = None
        self._tell("making", summary=summary)
        replay = hub.settings.replay
        try:
            path = package.finish(
                folder,
                complete,
                steps=session.flow,
                replay_name=replay.name if replay is not None else None,
                reason=reason,
                held=self._lock_fd is not None,
            )
        except Exception as error:
            self._log("the session zip could not be made", error)
            self._tell("failed", summary=summary, msg=self._failure(folder, package.in_words(error)))
            return
        notes = []
        try:
            record = package.session_meta(folder)
        except package.PackageError:
            record = {}
        for item in record.get("hook_errors") or []:
            notes.append(f"A step before the zip ({item['hook']}) failed: {item['error']}. The zip holds everything else.")
        for item in record.get("not_zipped") or []:
            notes.append(f"{item['path']} was not put into the zip: {item['reason']}.")
        self.zip = path
        self._tell("saved", summary=summary, file=path.name, notes=notes)
        try:  # only now, with the zip saved; a failure here leaves the session as it is
            self.analysis = results_web.start_after_zip(self)
        except Exception as error:
            self._log("the results of the session could not be started", error)

    @staticmethod
    def _failure(folder: Path, why: str) -> str:
        return (
            f"The session zip could not be made ({why}). The session folder {folder.name} in "
            f"{folder.parent} is complete and stays as it is; brain-mapper makes the zip from it "
            "the next time it starts."
        )


def _packaged(folder: Path) -> bool:
    """Whether the session of ``folder`` is zipped or being zipped: ``session.json`` has
    ``complete`` (added just before the zip is written) or ``zip``, or cannot be read."""
    try:
        meta = package.session_meta(folder)
    except (package.PackageError, OSError, ValueError):
        return True
    return "complete" in meta or bool(meta.get("zip"))


def _append_late(folder: Path, command: dict, cid: str) -> bool:
    """Appends a late event to ``folder/events.jsonl`` (the next ``seq``, ``step`` and ``block``
    ``null``, ``late: true`` in the payload, no marker: its sample is unknown). Returns False if
    an event with this ``cid`` is already there. Raises ``ValueError`` for an event that is not
    well formed, as ``Session.event`` would."""
    name, payload = command.get("name"), command.get("payload", {})
    browser_time, time_origin = command.get("browser_time"), command.get("time_origin")
    if not isinstance(name, str) or not EVENT_NAME.fullmatch(name):
        raise ValueError("bad event name")
    if not isinstance(payload, dict):
        raise ValueError("an event payload is a JSON object")
    for value in (browser_time, time_origin):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError("a time must be a finite number")
    path = folder / "events.jsonl"
    last = 0
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            if isinstance(event.get("payload"), dict) and event["payload"].get("cid") == cid:
                return False
            if isinstance(event.get("seq"), int):
                last = max(last, event["seq"])
    line = json.dumps(
        {
            "seq": last + 1,
            "step": None,
            "block": None,
            "name": name,
            "payload": {**_with_cid(payload, cid), "late": True},
            "browser_time": float(browser_time),
            "time_origin": float(time_origin),
            **clock_fields(float(browser_time), float(time_origin)),
        },
        allow_nan=False,
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _with_cid(payload, cid: str):
    """The payload as logged: with the event's ``cid``; a ``cid`` key of the page's own is kept
    as ``payload_cid``."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if "cid" in out and out["cid"] != cid:
        out["payload_cid"] = out.pop("cid")
    out["cid"] = cid
    return out


def create_app(settings: Settings) -> Starlette:
    hub = Hub(settings)

    async def participant(request: Request) -> FileResponse:
        return FileResponse(WEB / "index.html")

    async def onlooker(request: Request) -> FileResponse:
        return FileResponse(WEB / "onlooker.html")

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": version("brain-mapper")})

    async def session_flow(request: Request) -> JSONResponse:
        """The steps of the session, for the page:
        ``[{id, kind, title, module, blocks: [{id, seconds, points}], calibration, video, may_finish_early}]``."""
        steps = [
            {
                "id": step.id,
                "kind": step.kind,
                "title": step.title,
                "module": step.module,
                "blocks": [
                    {"id": b.id, "seconds": b.seconds, "points": b.points} for b in step.blocks
                ],
                "calibration": step.calibration.as_dict() if step.calibration else None,
                "video": step.video.as_dict() if step.video else None,
                "may_finish_early": step.may_finish_early,
            }
            for step in hub.steps
        ]
        return JSONResponse(steps)

    def refusal(status: int, code: str, text: str) -> JSONResponse:
        return JSONResponse({"error": text, "code": code}, status_code=status)

    async def participants(request: Request) -> JSONResponse:
        """``{"labels": [...]}``, most recently used first; 500 with ``error`` and ``code`` if the file is damaged."""
        try:
            return JSONResponse({"labels": hub.participants.labels()})
        except StoreDamaged as error:
            return JSONResponse(
                {"labels": [], "error": str(error), "code": "store_damaged"}, status_code=500
            )

    async def read_body(request: Request) -> bytes | None:
        """The body, or ``None`` once it is known to be longer than ``MAX_BODY``.

        A declared length over the limit is refused before anything is read; otherwise the
        stream is read chunk by chunk and abandoned as soon as the limit is passed.
        """
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY:
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def participant_post(request: Request) -> JSONResponse:
        """``POST {"label"}`` -> ``{"label","uuid"}``, or an error with ``code`` and the words to show.

        The first accepted label creates the session folder and starts the recording.
        """
        body = await read_body(request)
        if body is None:
            return refusal(
                413, "body_too_large", f"That request is longer than {MAX_BODY} bytes, too long for a label."
            )
        label = None
        choice: dict = {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                label = parsed.get("label")
                choice = {k: parsed.get(k) for k in ("only", "start_at", "continues")}
                choice["imported"] = parsed.get("import")
        except ValueError:
            pass
        if not isinstance(label, str):
            return refusal(400, "bad_body", f'Send a JSON object with a text "label". {RULE}')
        try:
            # The work (file writes, a recording start) runs off the event loop.
            result = await asyncio.to_thread(lambda: hub.accept(label, **choice))
        except ParticipantRefused as error:
            return refusal(error.status, error.code, str(error))
        except StoreDamaged as error:
            return refusal(500, "store_damaged", str(error))
        except Exception as error:
            # An unexpected failure (not a refusal, not a recording that would not start).
            hub.log_failure("accepting a participant label failed unexpectedly", error, label)
            return refusal(
                500,
                "internal_error",
                f"The label could not be accepted because of an unexpected problem ({type(error).__name__}). "
                "Nothing was saved that you need; you can try again, and the server log has the details.",
            )
        return JSONResponse(result)

    logged_problems: set[int] = set()

    async def progress_answer(label: object) -> JSONResponse:
        """What the earlier sessions of ``label`` leave to continue.

        ``{"next_step", "next_title", "points_total", "session", "words_done", "note"}``. A label
        with no earlier session, a label never used, and one whose record is damaged all get the
        same keys, with ``next_step`` null; ``note`` is words about a damaged record of this
        participant's newest session, else null. Nothing is written and nothing is said about
        any other participant.
        """
        if not isinstance(label, str) or not valid(label):
            return refusal(400, "invalid_label", RULE)
        try:
            ident = hub.participants.peek(label)
        except StoreDamaged as error:
            return refusal(500, "store_damaged", str(error))
        live = {hub.folder.name} if hub.folder is not None else set()
        found = await asyncio.to_thread(progress.scan, settings.data_dir, ident, live)
        if found.problems and len(found.problems) not in logged_problems:
            # Without the paths, which hold labels: the words are in ``note`` and in the data folder.
            logged_problems.add(len(found.problems))
            LOG.warning("%d session folder(s) could not be read and were left alone", len(found.problems))
        earlier = found.latest
        return JSONResponse(
            {
                "next_step": earlier.next_step if earlier else None,
                "next_title": earlier.next_title if earlier else None,
                "points_total": earlier.points_total if earlier else 0,
                "session": earlier.session if earlier else None,
                "words_done": "words" in found.finished_rounds,
                "note": found.own_problem,
            }
        )

    async def progress_post(request: Request) -> JSONResponse:
        """``POST /api/progress`` with ``{"participant": <label>}``: the same answer, with the label
        in the body, which is what the page uses so that no label is ever part of a URL."""
        body = await read_body(request)
        if body is None:
            return refusal(413, "body_too_large", f"That request is longer than {MAX_BODY} bytes.")
        label = None
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                label = parsed.get("participant")
        except ValueError:
            pass
        if not isinstance(label, str):
            return refusal(400, "bad_body", f'Send a JSON object with a text "participant". {RULE}')
        return await progress_answer(label)

    async def import_post(request: Request) -> JSONResponse:
        """``POST /api/import`` with a saved session zip as the body: what the participant screen
        shows of it, or a refusal with ``code`` and words that repeat nothing of the file.

        Only from this computer (the Host and Origin rules of every route, and the peer address);
        the body is read up to ``importer.MAX_UPLOAD`` bytes into an anonymous temporary file
        (never into the data folder), and the label is never part of the address.
        """
        peer = request.client.host if request.client is not None else ""
        if peer not in ("127.0.0.1", "::1"):
            return refusal(403, "forbidden", "Saved session files are taken only from this computer.")
        too_large = (
            f"That file is larger than {importer.MAX_UPLOAD >> 20} MB, too large for a saved session file."
        )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > importer.MAX_UPLOAD:
            return refusal(413, "too_large", too_large)
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile() as spool:  # anonymous: it has no name in any folder
            async for chunk in request.stream():
                size += len(chunk)
                if size > importer.MAX_UPLOAD:
                    return refusal(413, "too_large", too_large)
                digest.update(chunk)
                spool.write(chunk)
            spool.flush()
            try:
                answer = await asyncio.to_thread(hub.load_saved, spool, digest.hexdigest())
            except importer.ImportRefused as error:
                return refusal(error.status, error.code, str(error))
            except StoreDamaged as error:
                return refusal(500, "store_damaged", str(error))
            except Exception as error:
                hub.log_failure("loading a saved session file failed unexpectedly", error)
                return refusal(
                    500,
                    "internal_error",
                    f"The file could not be loaded because of an unexpected problem ({type(error).__name__}). "
                    "Nothing was changed.",
                )
        return JSONResponse(answer)

    async def session_zip(request: Request):
        """``GET /api/session/zip?key=<key>``: the session zip, for the participant page only.

        The key is sent only to the participant connection, with the ``package`` message that says
        the zip is saved; anything else is refused with 403. No label is part of the address.
        """
        record = hub.record
        if not record.allows(request.query_params.get("key", "")):
            return refusal(403, "not_allowed", "Only the participant page can download the session zip.")
        path = record.zip
        if path is None:
            return refusal(404, "not_ready", "The session zip has not been made.")
        return FileResponse(path, media_type="application/zip", filename=path.name)

    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        role = websocket.query_params.get("role", "onlooker")
        page = websocket.query_params.get("page", "")
        client = Client(
            asyncio.get_running_loop(),
            "participant" if role == "participant" else "onlooker",
            page if PAGE_ID.fullmatch(page) else None,
        )
        if not hub.connect(client):
            await websocket.send_text(
                _message(
                    t="error",
                    kind="second_window",
                    msg="the session is open in another window; open /onlooker to watch it here",
                )
            )
            await websocket.close()
            return

        async def send() -> None:
            try:
                while True:
                    text = await client.next()
                    if text is None:
                        await websocket.close()
                        return
                    await websocket.send_text(text)
            except Exception as error:
                hub.log_failure("sending to a websocket client failed; its sender ended", error)

        sender = asyncio.create_task(send())
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    command = json.loads(text)
                    if not isinstance(command, dict):
                        raise ValueError("a command is a JSON object")
                except ValueError as error:
                    client.offer(_message(t="error", kind="bad_message", msg=str(error)))
                    continue
                if command.get("cmd") == "traces":
                    hub.set_traces(client, bool(command.get("on")))
                    continue
                if command.get("cmd") == "display":
                    hub.set_display(client, bool(command.get("on")))
                    continue
                try:
                    hub.command(client, command)
                except CommandRefused as error:
                    client.offer(_message(t="error", kind="refused", msg=str(error)))
                except EventLogFailed as error:
                    client.offer(_message(t="error", kind="event_log_failed", msg=str(error)))
                except ValueError as error:
                    client.offer(_message(t="error", kind="bad_command", msg=str(error)))
                except Exception as error:  # a failure to mark: told to the page, kept in the log
                    hub.log_failure("a session command failed", error)
                    client.offer(_message(t="error", kind=type(error).__name__, msg=str(error)))
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            hub.disconnect(client)

    routes = [
        Route("/", participant),
        Route("/onlooker", onlooker),
        Route("/api/health", health, methods=["GET", "POST"]),
        Route("/api/flow", session_flow, methods=["GET"]),
        Route("/api/participants", participants, methods=["GET"]),
        Route("/api/participant", participant_post, methods=["POST"]),
        Route("/api/progress", progress_post, methods=["POST"]),
        Route("/api/import", import_post, methods=["POST"]),
        Route("/api/session/zip", session_zip, methods=["GET"]),
        *results_web.routes(hub),
        WebSocketRoute("/ws", socket),
        Mount("/static", StaticFiles(directory=WEB), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(LoopbackOnly, settings=settings)])
    app.state.settings = settings
    app.state.hub = hub
    return app

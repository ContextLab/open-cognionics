"""What each session got through: ``progress.json``, and finding a session to continue.

``progress.json`` sits in the session folder next to ``session.json``. The engine saves it
when the session begins, at every change and at the end, by replace-rename (a reader sees
the old file or the whole new one). It holds, in ``steps``, every step of the session's
plan in order with its ``outcome``: ``finished``, ``partial`` (left with some blocks
finished), ``skipped`` (left with none), ``not_reached`` or ``running`` (only while the
session is going on). A round stopped before its first block ended is ``not_reached``, so
continuing starts it again; ``finished``, ``partial`` and ``skipped`` rounds are done, so no
block is paid twice.

Earlier sessions are found through ``session.json`` (its ``uuid`` says whose session it is),
never by reading a folder name. A folder whose ``session.json`` or ``progress.json`` cannot
be read, or does not fit, is described in words (``Scan.problems``), is treated as no earlier
session, and is never changed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from brain_mapper import points

LOG = logging.getLogger("brain_mapper")
VERSION = 1
STATUSES = ("running", "finished", "stopped", "ended_early", "failed")
OUTCOMES = ("finished", "partial", "skipped", "not_reached", "running")
#: Step kinds that are not rounds (they carry no blocks).
NOT_ROUNDS = ("setup", "summary")
#: A round that cannot be played on its own, and the round whose results it needs.
NEEDS = {"recall": "words"}


class ProgressDamaged(ValueError):
    """A ``progress.json`` or ``session.json`` that cannot be read or does not fit."""


def write_json(path: Path, data: dict) -> None:
    """Writes ``data`` as JSON by replace-rename: a reader sees the old file or the whole new one."""
    fd, temp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def write(folder: Path, record: dict) -> None:
    """Saves ``record`` as ``folder/progress.json`` (``version`` and ``updated_unix`` are added)."""
    write_json(Path(folder) / "progress.json", {"version": VERSION, **record, "updated_unix": time.time()})


@dataclass(frozen=True)
class Progress:
    """The content of one ``progress.json``."""

    session: str
    folder: Path
    uuid: str | None
    status: str
    steps: list[dict]
    points_total: int
    seed: int | None
    continues: str | None
    started_unix: float | None = None
    #: The rounds the session was made to play: a round id (one round, as older sessions have it),
    #: a list of round ids in route order (several), or ``None`` (the whole route).
    only: str | list[str] | None = None

    @property
    def plan(self) -> list[str]:
        return [s["id"] for s in self.steps]

    @property
    def outcomes(self) -> dict[str, str]:
        return {s["id"]: s["outcome"] for s in self.steps}

    @property
    def next_step(self) -> str | None:
        """The first round that was not reached, or ``None`` if there is none."""
        for step in self.steps:
            if step["kind"] not in NOT_ROUNDS and step["outcome"] in ("not_reached", "running"):
                return step["id"]
        return None

    @property
    def next_title(self) -> str | None:
        found = next((s["title"] for s in self.steps if s["id"] == self.next_step), None)
        return found

    @property
    def played_anything(self) -> bool:
        """Whether there is something to carry on from: points, a finished round or a skipped one."""
        return self.points_total > 0 or any(
            s["kind"] not in NOT_ROUNDS and s["outcome"] in ("finished", "partial", "skipped") for s in self.steps
        )

    def rounds_from_next(self) -> list[str]:
        """The rounds of this session's plan from ``next_step`` on (skipped ones stay skipped)."""
        ids = [s["id"] for s in self.steps if s["kind"] not in NOT_ROUNDS]
        nxt = self.next_step
        return ids[ids.index(nxt) :] if nxt in ids else []

    def finished_rounds(self) -> set[str]:
        """Rounds with at least one finished block (``finished`` or ``partial``)."""
        return {
            s["id"]
            for s in self.steps
            if s["kind"] not in NOT_ROUNDS and s["outcome"] in ("finished", "partial")
        }


def _json(path: Path, what: str) -> dict:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError as error:
        raise ProgressDamaged(f"{path} is missing ({what})") from error
    except OSError as error:
        raise ProgressDamaged(f"{path} could not be read ({error.strerror or error})") from error
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProgressDamaged(f"{path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise ProgressDamaged(f"{path} is not a JSON object")
    return data


def read(folder: Path) -> Progress:
    """Reads and checks ``folder/progress.json``; raises ``ProgressDamaged`` in words."""
    path = Path(folder) / "progress.json"
    data = _json(path, "progress")
    if data.get("version") != VERSION:
        raise ProgressDamaged(f"{path} is not a version {VERSION} progress file")
    if data.get("status") not in STATUSES:
        raise ProgressDamaged(f"{path} has no valid status")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ProgressDamaged(f"{path} has no list of steps")
    for step in steps:
        if (
            not isinstance(step, dict)
            or not all(isinstance(step.get(k), str) for k in ("id", "kind", "title"))
            or step.get("outcome") not in OUTCOMES
        ):
            raise ProgressDamaged(f"{path} has a step that is not an id, kind, title and outcome")
    total = data.get("points_total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ProgressDamaged(f"{path} has no whole points total")
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ProgressDamaged(f"{path} has a seed that is not a whole number")
    for key in ("session", "uuid", "continues"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ProgressDamaged(f"{path} has a {key} that is not text")
    only = data.get("only")
    if only is not None and not isinstance(only, str) and not (
        isinstance(only, list) and only and all(isinstance(x, str) for x in only)
    ):
        raise ProgressDamaged(f"{path} has an only that is neither a round id nor a list of them")
    if not isinstance(data.get("session"), str):
        raise ProgressDamaged(f"{path} does not name its session")
    return Progress(
        session=data["session"],
        folder=Path(folder),
        uuid=data.get("uuid"),
        status=data["status"],
        steps=[dict(s) for s in steps],
        points_total=total,
        seed=seed,
        continues=data.get("continues"),
        only=list(only) if isinstance(only, list) else only,
    )


@dataclass
class Scan:
    """What was found about one participant's earlier sessions."""

    #: The newest session of the participant if it has rounds not reached and can be read.
    latest: Progress | None = None
    #: In words, every folder that could not be used; paths are named, so keep them off the log.
    problems: list[str] = field(default_factory=list)
    #: The part of ``problems`` that belongs to this participant (their newest session).
    own_problem: str | None = None
    #: Rounds this participant finished in any earlier session that could be read.
    finished_rounds: set[str] = field(default_factory=set)
    #: For each of those rounds, the seed of the most recent session (by start time) in which it was
    #: ``finished`` or ``partial``: a round played alone that needs it (``recall`` needs ``words``)
    #: takes this seed, so it gets the material the participant studied.
    round_seeds: dict[str, int] = field(default_factory=dict)


def scan(data_dir: Path, participant_uuid: str, exclude: frozenset[str] | set[str] = frozenset()) -> Scan:
    """Looks through ``<data_dir>/sessions/`` for the sessions of ``participant_uuid``.

    Which sessions are the participant's comes from each folder's ``session.json``. Sessions made with ``only`` (rounds chosen on the start screen, one or several) stand apart: they are never offered and never
    block the offer of an earlier whole-route session, but their finished rounds count as studied. Only the
    newest whole-route session of them can be continued (earlier ones were continued, or finished, or are older),
    and only if something was played in it (a session that got no further than the setup has
    nothing to carry on from: starting again is the same);
    if its ``progress.json`` or its points ledger is missing or does not fit, there is no
    session to continue and ``own_problem`` says why. Nothing is written. Folders named in
    ``exclude`` (the session this process is running) are not looked at.
    """
    found = Scan()
    sessions = Path(data_dir) / "sessions"
    mine: list[tuple[float, str, Path]] = []
    whole: list[tuple[float, str, Path]] = []  # the ones not made with ``only``
    try:
        folders = sorted(p for p in sessions.iterdir() if p.is_dir())
    except FileNotFoundError:
        return found
    except OSError as error:
        found.problems.append(f"{sessions} could not be read ({error.strerror or error})")
        return found
    for folder in folders:
        if folder.name in exclude:
            continue
        try:
            meta = _json(folder / "session.json", "session")
            if meta.get("session") != folder.name:
                raise ProgressDamaged(f"{folder / 'session.json'} names another session than its folder")
            owner, started = meta.get("uuid"), meta.get("started_unix")
            if not isinstance(owner, str) or isinstance(started, bool) or not isinstance(started, (int, float)):
                raise ProgressDamaged(f"{folder / 'session.json'} has no uuid or start time")
        except ProgressDamaged as error:
            found.problems.append(f"{error}; this folder is left alone and not offered")
            continue
        if owner == participant_uuid:
            mine.append((float(started), folder.name, folder))
            if meta.get("only") is None:
                whole.append((float(started), folder.name, folder))
    for _, _, folder in sorted(mine):  # oldest first, so the newest seed of a round wins
        try:
            earlier = read(folder)
        except ProgressDamaged:
            continue  # reported below if it is the newest; an older one just adds nothing
        found.finished_rounds |= earlier.finished_rounds()
        if earlier.seed is not None:
            for name in earlier.finished_rounds():
                found.round_seeds[name] = earlier.seed
    if not whole:
        return found  # only one-round sessions (or none): nothing can be continued
    _, name, folder = max(whole)
    try:
        newest = read(folder)
        if newest.session != name or (newest.uuid is not None and newest.uuid != participant_uuid):
            raise ProgressDamaged(f"{folder / 'progress.json'} belongs to another session")
        ledger = points.read_awards(folder / "points.jsonl") if (folder / "points.jsonl").exists() else None
        shown = ledger[-1]["total"] if ledger else 0
        if ledger is None and newest.points_total:
            raise ProgressDamaged(f"{folder / 'points.jsonl'} is missing")
        if shown != newest.points_total:
            raise ProgressDamaged(
                f"{folder / 'progress.json'} says {newest.points_total} points but the ledger says {shown}"
            )
    except (ProgressDamaged, points.PointsError) as error:
        text = f"{error}; it is treated as no earlier session and has not been changed"
        found.problems.append(text)
        found.own_problem = text
        return found
    found.finished_rounds |= newest.finished_rounds()
    if newest.next_step is not None and newest.played_anything:
        found.latest = newest
    return found


def latest(data_dir: Path, participant_uuid: str) -> Progress | None:
    """The newest session of the participant if it has rounds not reached, else ``None``."""
    return scan(data_dir, participant_uuid).latest

"""Loading a session zip made by this app, to start a later session of the same person.

The file comes from a person's computer and is treated as untrusted. ``check(handle)`` reads it
from an open file (the upload, held in an anonymous temporary file) and never writes a member to
disk:

- the file starts with a zip local header (``PK\\x03\\x04``) and the standard ``zipfile`` module
  can read its directory;
- at most ``MAX_MEMBERS`` members; every name is 1 to 512 characters of ``A-Z a-z 0-9 _ . -`` in
  parts separated by ``/``, no part starting with a dot (so no ``..``), no absolute path, no drive
  letter, no backslash, no folder entries, no name twice; no member is a symbolic link or other
  special file (the Unix mode bits), encrypted, or packed other than stored or deflated; no two
  members share bytes of the file;
- each member's declared size is at most ``MAX_MEMBER`` and the sum at most ``MAX_TOTAL``; then
  every member is read to its end in chunks (so ``zipfile`` checks its CRC), counting what comes
  out, and the read stops as soon as a member gives more than it declared or than the limits;
- the members of a session zip are there (``session.json``, ``progress.json``, ``events.jsonl``,
  ``points.jsonl``, and ``<folder>/raw.bin`` for each recording ``session.json`` lists);
- only ``session.json`` is parsed (at most ``MAX_SESSION_JSON`` bytes), as data, and every field
  used is checked for its type, length and range (``_record``).

A file that fails is refused with ``ImportRefused``, whose words never repeat anything from the
file. From a file that passes comes a ``Saved``: the label, the participant identifier
(``uuid``), the session's folder name, ``session_id``, start time, the rounds completed, its
``sample_point`` and ``previous_sessions`` (1 and none for a zip made before those fields
existed).

``keep(data_dir, handle, sha256)`` copies the file, byte for byte, to
``<data_dir>/imported/<sha256>.zip``: written to a new file opened exclusively, synced, and given
its name with an exclusive rename, so nothing is ever overwritten; a file already there under that
name is checked to have the same sha256 and used as it is. Nothing here deletes anything, and
nothing from the zip is unpacked into, merged into or changes any session folder.

``history(data_dir, uuid)`` lists this computer's sessions of a person; ``lineage`` gives a new
session its ``sample_point`` (one more than the highest known for the person) and its
``previous_sessions`` (the earlier sessions known: on this computer, and in the loaded file).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from brain_mapper import flow, package, participants

#: The largest upload read (bytes): about ten times a 30-minute session's zip.
MAX_UPLOAD = 1 << 30
#: The most members a zip may have (a session zip has about a dozen).
MAX_MEMBERS = 1000
#: The largest uncompressed member, and the largest uncompressed total (bytes).
MAX_MEMBER = 1 << 30
MAX_TOTAL = 2 << 30
#: The largest ``session.json`` parsed (bytes; one of a full session is about 40 kB).
MAX_SESSION_JSON = 8 << 20
#: Longest text taken from ``session.json`` for a title, and most entries of each list.
MAX_TITLE = 200
MAX_STEPS = 100
MAX_BLOCKS = 100
MAX_TIMELINE = 100_000
MAX_PREVIOUS = 1000
MAX_SAMPLE_POINT = 100_000
CHUNK = 1 << 20
#: A member name: parts of ``A-Z a-z 0-9 _ . -`` that do not start with a dot, joined by ``/``.
PART = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}"
NAME = re.compile(rf"{PART}(/{PART}){{0,7}}")
REQUIRED = ("session.json", "progress.json", "events.jsonl", "points.jsonl")
#: A session folder name: ``<label>_<YYYYMMDD>_<HHMMSS>`` and maybe ``-<n>``.
FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}_\d{8}_\d{6}(-\d{1,4})?")
SESSION_ID = re.compile(r"[0-9a-f]{16}")
SHA256 = re.compile(r"[0-9a-f]{64}")
STEP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENDINGS = ("finished", "stopped", "ended_early", "failed", "interrupted", "failed_start")
#: Unix times taken as a session's start: 2001-09-09 to 2286-11-20.
EARLIEST, LATEST = 1e9, 1e10
#: The folder in the data folder that keeps the loaded files.
IMPORTED = "imported"

NOT_A_ZIP = "This file is not a zip file. Choose the .zip file that brain-mapper saved at the end of a session."
DAMAGED = "This zip file is damaged or incomplete, so it cannot be used. Try another copy of it."
NOT_OURS = "This zip file is not a session saved by brain-mapper, so it cannot be used."
UNSAFE = (
    "This zip file holds entries that a session zip never has (a name, a link or a size that is not "
    "allowed), so it was not used."
)


class ImportRefused(Exception):
    """A file that cannot be used; the words are for the person and repeat nothing of the file."""

    def __init__(self, message: str, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Saved:
    """What a valid session zip says about its session."""

    label: str
    uuid: str
    session: str
    session_id: str | None
    started_unix: float
    complete: bool
    ending: str
    points_total: int
    rounds_done: int
    rounds_total: int
    sample_point: int
    previous_sessions: tuple = field(default_factory=tuple)

    def entry(self, sha256: str) -> dict:
        """This session as an entry of a later session's ``previous_sessions``."""
        return {**_entry(self.session_id, self.session, self.started_unix, self.sample_point), "zip_sha256": sha256}


def _entry(session_id, session, started_unix, sample_point) -> dict:
    return {
        "session_id": session_id,
        "session": session,
        "started_unix": started_unix,
        "start_utc": datetime.fromtimestamp(started_unix, timezone.utc).isoformat(timespec="seconds"),
        "sample_point": sample_point,
    }


# -- the zip ---------------------------------------------------------------------------------------


def _names(infos: list[zipfile.ZipInfo], end: int) -> None:
    """Refuses a member list that a session zip never has; see the module text."""
    if not infos or len(infos) > MAX_MEMBERS:
        raise ImportRefused(UNSAFE, "unsafe")
    seen: set[str] = set()
    declared = 0
    for info in infos:
        name = info.filename
        if len(name) > 512 or not NAME.fullmatch(name) or name in seen:
            raise ImportRefused(UNSAFE, "unsafe")
        seen.add(name)
        mode = info.external_attr >> 16
        if info.create_system == 3 and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ImportRefused(UNSAFE, "unsafe")  # a symbolic link, a folder or a special file
        if info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ImportRefused(UNSAFE, "unsafe")
        if info.file_size > MAX_MEMBER or info.compress_size > MAX_UPLOAD:
            raise ImportRefused(UNSAFE, "unsafe")
        declared += info.file_size
    if declared > MAX_TOTAL:
        raise ImportRefused(UNSAFE, "unsafe")
    # No two members may share bytes (a zip bomb made of one block referred to many times).
    spans = sorted((info.header_offset, info) for info in infos)
    for at, (offset, info) in enumerate(spans):
        stop = offset + 30 + len(info.filename.encode()) + info.compress_size
        limit = spans[at + 1][0] if at + 1 < len(spans) else end
        if stop > limit:
            raise ImportRefused(UNSAFE, "unsafe")


def _read_all(z: zipfile.ZipFile) -> bytes:
    """Reads every member to its end with a cap; returns ``session.json``."""
    total = 0
    record = b""
    for info in z.infolist():
        got = 0
        keep = info.filename == "session.json"
        parts: list[bytes] = []
        with z.open(info) as member:
            while True:
                chunk = member.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                total += len(chunk)
                if got > info.file_size or got > MAX_MEMBER or total > MAX_TOTAL:
                    raise ImportRefused(UNSAFE, "unsafe")
                if keep:
                    if got > MAX_SESSION_JSON:
                        raise ImportRefused(NOT_OURS, "not_ours")
                    parts.append(chunk)
        if got != info.file_size:
            raise ImportRefused(DAMAGED, "damaged")
        if keep:
            record = b"".join(parts)
    return record


def check(handle) -> Saved:
    """Checks the zip in the open binary file ``handle`` and returns what its ``session.json`` says.

    Raises ``ImportRefused`` (``not_a_zip``, ``damaged``, ``unsafe`` or ``not_ours``).
    """
    handle.seek(0)
    if handle.read(4) != b"PK\x03\x04":
        raise ImportRefused(NOT_A_ZIP, "not_a_zip")
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    try:
        with zipfile.ZipFile(handle) as z:
            _names(z.infolist(), getattr(z, "start_dir", size))
            names = set(z.namelist())
            if not all(name in names for name in REQUIRED):
                raise ImportRefused(NOT_OURS, "not_ours")
            raw = _read_all(z)
    except ImportRefused:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, OSError, ValueError,
            NotImplementedError, RuntimeError, OverflowError, KeyError) as error:
        raise ImportRefused(DAMAGED, "damaged") from error
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ImportRefused(NOT_OURS, "not_ours") from error
    saved = _record(data)
    for folder in data["recordings"]:
        if f"{folder}/raw.bin" not in names:
            raise ImportRefused(NOT_OURS, "not_ours")
    return saved


# -- session.json -----------------------------------------------------------------------------------


def _text(value, pattern: re.Pattern | None = None, longest: int = MAX_TITLE) -> bool:
    if not isinstance(value, str) or not value or len(value) > longest:
        return False
    return pattern is None or pattern.fullmatch(value) is not None


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _moment(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and EARLIEST < value < LATEST
    )


def _is_uuid(value) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _previous(items) -> tuple:
    """The ``previous_sessions`` of a file, each entry checked and rebuilt from the checked fields."""
    if items is None:
        return ()
    if not isinstance(items, list) or len(items) > MAX_PREVIOUS:
        raise ImportRefused(NOT_OURS, "not_ours")
    found = []
    for item in items:
        if not isinstance(item, dict):
            raise ImportRefused(NOT_OURS, "not_ours")
        session_id, session = item.get("session_id"), item.get("session")
        started, point, digest = item.get("started_unix"), item.get("sample_point"), item.get("zip_sha256")
        if (
            not (session_id is None or _text(session_id, SESSION_ID, 16))
            or not _text(session, FOLDER, 64)
            or not _moment(started)
            or not _whole(point, 1, MAX_SAMPLE_POINT)
            or not (digest is None or _text(digest, SHA256, 64))
        ):
            raise ImportRefused(NOT_OURS, "not_ours")
        entry = _entry(session_id, session, float(started), point)
        if digest is not None:
            entry["zip_sha256"] = digest
        found.append(entry)
    return tuple(found)


def _rounds(steps, timeline) -> tuple[int, int]:
    """``(rounds completed, rounds)``: a round is completed when each of its blocks finished."""
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ImportRefused(NOT_OURS, "not_ours")
    if not isinstance(timeline, list) or len(timeline) > MAX_TIMELINE:
        raise ImportRefused(NOT_OURS, "not_ours")
    finished: set[tuple[str, str]] = set()
    for item in timeline:
        if not isinstance(item, dict):
            raise ImportRefused(NOT_OURS, "not_ours")
        step, block, outcome = item.get("step"), item.get("block"), item.get("outcome")
        if not (step is None or _text(step, STEP_ID, 64)) or not (block is None or _text(block, STEP_ID, 64)):
            raise ImportRefused(NOT_OURS, "not_ours")
        if not (outcome is None or _text(outcome, None, 16)):
            raise ImportRefused(NOT_OURS, "not_ours")
        if step is not None and block is not None and outcome == "finished":
            finished.add((step, block))
    done = total = 0
    for step in steps:
        if not isinstance(step, dict) or not _text(step.get("id"), STEP_ID, 64) or step.get("kind") not in flow.KINDS:
            raise ImportRefused(NOT_OURS, "not_ours")
        if not _text(step.get("title"), None, MAX_TITLE):
            raise ImportRefused(NOT_OURS, "not_ours")
        blocks = step.get("blocks")
        if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
            raise ImportRefused(NOT_OURS, "not_ours")
        ids = []
        for block in blocks:
            if not isinstance(block, dict) or not _text(block.get("id"), STEP_ID, 64):
                raise ImportRefused(NOT_OURS, "not_ours")
            ids.append(block["id"])
        if step["kind"] in ("setup", "summary") or not ids:
            continue
        total += 1
        done += all((step["id"], b) in finished for b in ids)
    return done, total


def _record(data) -> Saved:
    """The fields of ``session.json`` this feature uses, each checked; ``ImportRefused`` otherwise."""
    if not isinstance(data, dict):
        raise ImportRefused(NOT_OURS, "not_ours")
    label, ident, session = data.get("label"), data.get("uuid"), data.get("session")
    session_id, started = data.get("session_id"), data.get("started_unix")
    if not participants.valid(label) or not _is_uuid(ident) or not _text(session, FOLDER, 64):
        raise ImportRefused(NOT_OURS, "not_ours")
    if not session.startswith(label + "_"):
        raise ImportRefused(NOT_OURS, "not_ours")
    if not (session_id is None or _text(session_id, SESSION_ID, 16)) or not _moment(started):
        raise ImportRefused(NOT_OURS, "not_ours")
    complete, ending, points_total = data.get("complete"), data.get("ending"), data.get("points_total")
    if not isinstance(complete, bool) or ending not in ENDINGS or not _whole(points_total, 0, 10**9):
        raise ImportRefused(NOT_OURS, "not_ours")
    recordings = data.get("recordings")
    if not isinstance(recordings, list) or len(recordings) > 100:
        raise ImportRefused(NOT_OURS, "not_ours")
    if not all(_text(r, package.RECORDING_DIR, 32) for r in recordings):
        raise ImportRefused(NOT_OURS, "not_ours")
    point = data.get("sample_point", 1)
    if not _whole(point, 1, MAX_SAMPLE_POINT):
        raise ImportRefused(NOT_OURS, "not_ours")
    done, total = _rounds(data.get("flow"), data.get("timeline"))
    return Saved(
        label=label,
        uuid=ident,
        session=session,
        session_id=session_id,
        started_unix=float(started),
        complete=complete,
        ending=ending,
        points_total=points_total,
        rounds_done=done,
        rounds_total=total,
        sample_point=point,
        previous_sessions=_previous(data.get("previous_sessions")),
    )


# -- keeping the file ----------------------------------------------------------------------------


def _hash(handle) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    for chunk in iter(lambda: handle.read(CHUNK), b""):
        digest.update(chunk)
    return digest.hexdigest()


def keep(data_dir: Path, handle, sha256: str) -> str:
    """Copies the file in ``handle`` to ``<data_dir>/imported/<sha256>.zip``; returns that path
    relative to the data folder. Never overwrites and never deletes: see the module text."""
    folder = Path(data_dir) / IMPORTED
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{sha256}.zip"
    relative = f"{IMPORTED}/{final.name}"
    if os.path.lexists(final):
        if not final.is_file() or final.is_symlink():
            raise ImportRefused("The folder for loaded files holds something unexpected; nothing was changed.", "keep_failed", 500)
        with open(final, "rb") as there:
            if _hash(there) != sha256:
                raise ImportRefused("A different file already has this file's name; nothing was changed.", "keep_failed", 500)
        return relative
    temp = folder / f".{sha256}.zip.partial-{secrets.token_hex(4)}"
    with open(temp, "xb") as out:  # exclusive: a new file of this call's own
        handle.seek(0)
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    with open(temp, "rb") as written:
        if _hash(written) != sha256:
            raise ImportRefused("The file could not be copied whole; nothing was changed.", "keep_failed", 500)
    try:
        package.rename_exclusive(temp, final)
    except FileExistsError:
        with open(final, "rb") as there:  # another request kept the same file a moment ago
            if _hash(there) != sha256:
                raise ImportRefused("A different file already has this file's name; nothing was changed.", "keep_failed", 500) from None
    return relative


def sha256_of(handle) -> str:
    return _hash(handle)


# -- the sessions of a person --------------------------------------------------------------------


def _session_json(folder: Path) -> dict | None:
    try:
        with open(folder / "session.json", "rb") as handle:
            raw = handle.read(MAX_SESSION_JSON + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_SESSION_JSON else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) and data.get("session") == folder.name else None


def point_of(meta: dict) -> int:
    """The ``sample_point`` of a ``session.json`` (1 when it has none: made before the field existed)."""
    value = meta.get("sample_point", 1)
    return value if _whole(value, 1, MAX_SAMPLE_POINT) else 1


def history(data_dir: Path, ident: str) -> list[dict]:
    """This computer's sessions of the person ``ident`` (from each folder's ``session.json``), as
    ``previous_sessions`` entries, oldest first. Folders from a start that failed, and folders whose
    ``session.json`` cannot be read, are left out. Nothing is written."""
    found = []
    try:
        folders = sorted(p for p in (Path(data_dir) / "sessions").iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return found
    for folder in folders:
        meta = _session_json(folder)
        if meta is None or meta.get("uuid") != ident or isinstance(meta.get("failure"), str):
            continue
        started, session_id = meta.get("started_unix"), meta.get("session_id")
        if not _moment(started):
            continue
        found.append(_entry(session_id if _text(session_id, SESSION_ID, 16) else None, folder.name, float(started), point_of(meta)))
    return sorted(found, key=lambda e: e["started_unix"])


def lineage(local: list[dict], saved: Saved | None = None, sha256: str | None = None) -> tuple[int, list[dict]]:
    """``(sample_point, previous_sessions)`` of a new session (not a continued one).

    The earlier sessions known are ``local`` (``history``) and, for a loaded file, the file's own
    ``previous_sessions`` and its session (with the file's sha256). One entry per session (by
    ``session_id``, or the folder name for a session made before ids existed), oldest first; the
    sample point is one more than the highest among them, 1 if there are none.
    """
    merged: dict[str, dict] = {}
    chain = list(saved.previous_sessions) + [saved.entry(sha256)] if saved is not None else []
    for entry in [*chain, *local]:
        key = entry["session_id"] or entry["session"]
        merged[key] = {**entry, **merged.get(key, {})}
    previous = sorted(merged.values(), key=lambda e: e["started_unix"])
    return 1 + max((e["sample_point"] for e in previous), default=0), previous


def continued(folder: Path) -> tuple[int, list]:
    """``(sample_point, previous_sessions)`` of a session that continues the one in ``folder``: the
    same as that session's (it is the same sample point)."""
    meta = _session_json(Path(folder)) or {}
    previous = meta.get("previous_sessions")
    return point_of(meta), previous if isinstance(previous, list) else []

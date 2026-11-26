"""The participant labels remembered on this computer, and the identifier of each.

A person types a label (``P001``); the first time the label is used on this
machine it is paired with a random UUID4, and the pair is kept in
``participants.json`` in the data folder. The label is what people see and what
appears in file names; the identifier is what tells two people who chose the same
label on different machines apart.

The file looks like ``{"version": 1, "participants": [{"label", "uuid"}, ...]}``
with the most recently used label first. It is written to a new file in the same
folder and renamed over the old one, so a reader sees the old file or the new one,
never half of one. A file that cannot be read or does not fit this layout is
reported (``StoreDamaged``) and is never replaced: nothing is written until
someone repairs or moves it.

The store assumes one brain-mapper process per data folder: two processes writing the same
file at the same moment can lose one another's new label (each write is still whole).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path

LOG = logging.getLogger("brain_mapper")
#: Labels are used in file names, so this is the whole alphabet: ASCII only.
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
#: The rule, in words, as shown to the person.
RULE = (
    "A participant label is 1 to 32 characters: the letters A to Z (no accents) and digits, "
    "with _ and - allowed after the first character."
)
VERSION = 1


def valid(label: object) -> bool:
    """Whether ``label`` is 1 to 32 of ``A-Z a-z 0-9 _ -`` and nothing else, and does not start with ``_`` or ``-``."""
    return isinstance(label, str) and LABEL.fullmatch(label) is not None


_folder_sync_logged = False


def sync_folder(folder: Path) -> None:
    """Best effort: asks the OS to make the renames in ``folder`` durable.

    A failure (a filesystem that refuses to sync a folder, a folder that is gone) is logged
    once and otherwise ignored: it comes after the rename, so the file is already written and
    reporting it as a failed write would be wrong.
    """
    global _folder_sync_logged
    try:
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        if not _folder_sync_logged:
            _folder_sync_logged = True
            LOG.warning("the folder %s could not be synced after a rename (%s)", folder, error)


class StoreDamaged(ValueError):
    """``participants.json`` cannot be read or is not in the expected layout."""


class IdentityTaken(ValueError):
    """An identifier that another label already has: one identifier is one person, under one label."""


class Store:
    """The labels and identifiers in one file; safe to use from several threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _damaged(self, why: str) -> StoreDamaged:
        return StoreDamaged(
            f"The participant list ({self.path}) cannot be used: {why}. "
            "The file has not been changed. Repair it, or move it away so that a new list "
            "is started, and then reload this page."
        )

    def _read(self) -> list[dict]:
        """The entries, most recent first; an empty list if there is no file."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise self._damaged(f"it could not be read ({error.strerror or error})") from error
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise self._damaged("it is not valid JSON") from error
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise self._damaged(f"it is not a version {VERSION} participant list")
        entries = data.get("participants")
        if not isinstance(entries, list):
            raise self._damaged("it has no list of participants")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise self._damaged("an entry is not a label with an identifier")
            label, ident = entry.get("label"), entry.get("uuid")
            if not valid(label):
                raise self._damaged("it holds a label that is not allowed")
            if label in seen:
                raise self._damaged(f"the label {label} is in it twice")
            seen.add(label)
            try:
                ok = isinstance(ident, str) and str(uuid.UUID(ident)) == ident
            except ValueError:
                ok = False
            if not ok:
                raise self._damaged(f"the identifier of {label} is not a UUID")
        return [{"label": e["label"], "uuid": e["uuid"]} for e in entries]

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": VERSION, "participants": entries}, indent=2) + "\n"
        fd, temp = tempfile.mkstemp(prefix=".participants-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        # The file is in place; syncing the folder only makes the rename durable.
        sync_folder(self.path.parent)

    def labels(self) -> list[str]:
        """The remembered labels, most recently used first."""
        with self._lock:
            return [e["label"] for e in self._read()]

    def label_of(self, ident: str) -> str | None:
        """The label that has the identifier ``ident``, or ``None``; nothing is written.

        Raises ``StoreDamaged`` for a file that cannot be used.
        """
        with self._lock:
            return next((e["label"] for e in self._read() if e["uuid"] == ident), None)

    def peek(self, label: str) -> str:
        """The identifier ``label`` has, or a fresh one it would get; nothing is written.

        Raises ``ValueError`` for a label ``valid`` refuses and ``StoreDamaged`` for a
        file that cannot be used. Pass the fresh one back to ``identify`` as ``fresh`` to keep it.
        """
        if not valid(label):
            raise ValueError(RULE)
        with self._lock:
            found = next((e for e in self._read() if e["label"] == label), None)
            return found["uuid"] if found else str(uuid.uuid4())

    def identify(self, label: str, fresh: str | None = None) -> str:
        """The identifier of ``label``, created (and saved) the first time it is used.

        A new label gets ``fresh`` if given (the one ``peek`` showed), else a random UUID4;
        ``IdentityTaken`` is raised, and nothing written, if another label already has ``fresh``.
        Using a label moves it to the front of the list. Raises ``ValueError`` for a
        label that ``valid`` refuses (before anything is read or written) and
        ``StoreDamaged`` for a file that cannot be used; an ``OSError`` from the write is
        raised as it is.
        """
        if not valid(label):
            raise ValueError(RULE)
        with self._lock:
            entries = self._read()
            found = next((e for e in entries if e["label"] == label), None)
            if found is None and fresh is not None and any(e["uuid"] == fresh for e in entries):
                raise IdentityTaken("that identifier already belongs to another label on this computer")
            entry = found or {"label": label, "uuid": fresh or str(uuid.uuid4())}
            rest = [e for e in entries if e["label"] != label]
            if not entries or entries[0]["label"] != label:
                self._write([entry, *rest])
            return entry["uuid"]

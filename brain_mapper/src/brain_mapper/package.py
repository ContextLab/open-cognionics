"""The session zip: ``session.json`` completed, and one zip per session beside its folder.

A session folder ``<data-dir>/sessions/<label>_<YYYYMMDD>_<HHMMSS>[-N]/`` stays where it is (earlier
sessions are found by their folders). ``finish(folder, complete)`` does these steps, each of which
can be interrupted at any point and done again:

1. The ``BEFORE_ZIP`` hooks run (``behavior.before_zip`` writes ``behavior/<task>.csv`` there,
   ``scores.write_before_zip`` ``behavior/scores.json``). A hook that
   raises is reported (``hook_errors``) and does not stop the zip.
2. ``describe``: adds the fields of the record (``participant``, ``start_utc``, ``timeline`` ...) to
   ``session.json`` by replace-rename, keeping every key already there. If the fields are there
   already (an earlier attempt got this far) the file is left as it is, so the zip of a later
   attempt holds the same record.
3. Every regular file of the folder, at any depth, is listed (``members``); symbolic links are not
   followed and are reported. The zip is written to a temporary file ``.<name>.zip.partial-<8 hex>``
   beside the folder and read back: every member to its end (its CRC is checked there), and each
   member's size and sha256 compared with the file listed. Only then is it given its name
   ``<folder name>.zip`` (``<folder name>_replay.zip`` for a replay) with a hard link, or, where hard
   links are not possible, an exclusive rename; neither can replace a file, and a taken name gets
   ``-2``, ``-3`` ... So the final name only ever holds a verified zip. A failed attempt removes the
   temporary file it made (and only that one).
4. The zip's name is added to ``session.json`` (``zip``). A session is packaged once that key is
   there. A zip that was placed but not yet recorded (a crash between 3 and 4) is found again,
   checked, and recorded, so no second zip is made.

Nothing that was recorded is overwritten or deleted, and files are read and written in chunks (no
whole file in memory). ``recover(data_dir)`` packages, at start-up, every session folder that has no
zip, and reports temporary files left by a crash (removing them once the session's verified zip
exists).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from brain_mapper import behavior, points, progress, scores

try:  # the folder lock is POSIX only; elsewhere one process per data folder is assumed
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

LOG = logging.getLogger("brain_mapper")
#: Called with the session folder just before the files are listed, so that a later stage can put
#: its files into the folder; every file in the folder is zipped. ``behavior.before_zip`` writes
#: ``behavior/<task>.csv`` and ``behavior/columns.json`` there, for every way a session ends, and
#: ``scores.write_before_zip`` then writes ``behavior/scores.json`` (the same scores as ``session.json``).
BEFORE_ZIP: list[Callable[[Path], None]] = [behavior.before_zip, scores.write_before_zip]
#: The temporary files of ``progress.write_json`` (a replace-rename of ``session.json`` or
#: ``progress.json`` that a crash cut short): the only files of the folder left out of the zip.
#: ``tempfile.mkstemp(prefix=".tmp-", suffix=".json")``: 8 characters from its alphabet.
OWN_TEMPORARY = re.compile(r"\.tmp-[a-z0-9_]{8}\.json")
#: The folders of analysis/run.py at the top of a session folder (analysis/, analysis-2/ ...).
ANALYSIS_DIR = re.compile(r"analysis(-[0-9]+)?")
#: Events that end the session; with ``block_end`` and ``skipped`` they also end a block.
ENDINGS = ("session_end", "stopped", "ended", "source_ended")
BLOCK_ENDS = ("block_end", "skipped", *ENDINGS)
#: The parts of a calibration step (``engine``), by the event that starts and the one that ends
#: each; ``settling`` has no end event of its own (eyes open starts when it is over).
PART_STARTS = {
    "contact_start": "contact",
    "settling_start": "settling",
    "signal_start": "signal",
    "eyes_open_start": "eyes_open",
    "eyes_closed_start": "eyes_closed",
    "blinks_start": "blinks",
}
PART_ENDS = {
    "contact_end": "contact",
    "signal_end": "signal",
    "eyes_open_end": "eyes_open",
    "eyes_closed_end": "eyes_closed",
    "blinks_end": "blinks",
}
#: The folders of the session that hold a recording: ``eeg`` and those set aside when a link was lost.
RECORDING_DIR = re.compile(r"eeg(-lost-\d+)?")
CHUNK = 1 << 20


class PackageError(Exception):
    """A session folder that cannot be packaged; the words say why and name no path."""


@dataclass(frozen=True)
class Recovered:
    """What ``recover`` did with one folder.

    ``zip`` is the zip's name when one was made now; ``problem`` says why none was made;
    ``leftovers`` counts the temporary files of an earlier crash found for this session, and
    ``removed`` says whether they were removed (only once a verified zip of the session exists).
    """

    folder: str
    zip: str | None = None
    problem: str | None = None
    leftovers: int = 0
    removed: bool = False
    #: The folder is from a start that failed (``failure`` in its session.json).
    failed_start: bool = False
    #: The name of an additional zip made now for events that came after the first zip.
    late_zip: str | None = None


@dataclass
class Listing:
    """The files that go into the zip (name in the zip -> file), and what was left out and why."""

    files: dict[str, Path] = field(default_factory=dict)
    #: ``{"path", "reason"}`` (path relative to the folder) of each entry left out of the zip.
    not_zipped: list[dict] = field(default_factory=list)


def scrub(text: str, folder: Path | None = None, label: str | None = None) -> str:
    """``text`` with every path in the sessions folder replaced by ``<data>/...`` (``scrub_paths``,
    so any session's path goes, not only this one's), the folder's name by ``<session>`` and the
    label by ``<label>``, so that it can go to the log."""
    if folder is not None:
        text = scrub_paths(text, folder.parent).replace(folder.name, "<session>")
    if label:
        text = text.replace(label, "<label>")
    return text


def scrub_paths(text: str, data_dir: Path, labels=()) -> str:
    """``text`` with every path under ``data_dir`` replaced by ``<data>/...`` and each of ``labels``
    by ``<label>``, so that it can go to the log whichever session it is about.

    The data folder is matched as given, made absolute and with links resolved (``/tmp`` is
    ``/private/tmp`` on macOS); a path runs from there up to the next white space or quote.
    """
    data_dir = Path(data_dir)
    forms = {str(data_dir), str(data_dir.absolute())}
    try:
        forms.add(str(data_dir.resolve()))
    except OSError:
        pass
    for form in sorted(forms, key=len, reverse=True):
        text = re.sub(re.escape(form) + r"[^\s'\"]*", "<data>/...", text)
    for label in labels:
        if label:
            text = text.replace(label, "<label>")
    return text


# -- reading the folder ------------------------------------------------------------------------


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PackageError(f"its {what} is missing") from error
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise PackageError(f"its {what} cannot be read") from error
    if not isinstance(data, dict):
        raise PackageError(f"its {what} is not a JSON object")
    return data


def _lines(path: Path) -> list[dict]:
    """The JSON lines of ``path`` that can be read (a crash can leave the last one half written)."""
    found: list[dict] = []
    try:
        handle = open(path, encoding="utf-8")
    except FileNotFoundError:
        return found
    with handle:
        for line in handle:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                found.append(item)
    return found


def session_meta(folder: Path) -> dict:
    """``session.json`` of ``folder``, checked to name its own folder; raises ``PackageError``."""
    meta = _read_json(folder / "session.json", "session.json")
    if meta.get("session") != folder.name:
        raise PackageError("its session.json names another session than its folder")
    if not isinstance(meta.get("label"), str) or not isinstance(meta.get("uuid"), str):
        raise PackageError("its session.json has no label or uuid")
    started = meta.get("started_unix")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        raise PackageError("its session.json has no start time")
    return meta


def is_replay(meta: dict) -> bool:
    return meta.get("source") == "replay"


def recordings(folder: Path) -> list[str]:
    """The recording folders of the session that hold a ``raw.bin``: ``eeg-lost-N`` in order, then ``eeg``."""
    found = []
    for path in folder.iterdir():
        if RECORDING_DIR.fullmatch(path.name) and not path.is_symlink() and (path / "raw.bin").is_file():
            found.append(path.name)
    return sorted(found, key=lambda n: (n == "eeg", int(n.rsplit("-", 1)[1]) if n != "eeg" else 0))


def _iso(unix: float | None, local: bool = False) -> str | None:
    if unix is None:
        return None
    moment = datetime.fromtimestamp(unix, timezone.utc)
    return (moment.astimezone() if local else moment).isoformat(timespec="milliseconds")


def _marked(folder: Path) -> dict[int, tuple[int, str]]:
    """``seq`` to sample number and recording folder, from the markers of every recording of
    the session (``<step>/<name>#<seq>``; ``eeg-lost-<n>`` first, then ``eeg``)."""
    found = {}
    for name in recordings(folder) if folder.is_dir() else []:
        for marker in _lines(folder / name / "markers.jsonl"):
            label, sample = marker.get("label"), marker.get("sample")
            match = re.search(r"#(\d+)$", label) if isinstance(label, str) else None
            if match and isinstance(sample, int):
                found[int(match.group(1))] = (sample, name)
    return found


def _samples(folder: Path) -> dict[int, int]:
    """``seq`` to sample number, from the markers of the session's recordings."""
    return {seq: sample for seq, (sample, _) in _marked(folder).items()}


def coverage(folder: Path) -> list[dict]:
    """Each recording of the session, in order: ``folder``, ``markers`` (how many), ``start_unix``
    and ``end_unix`` (the first and last event marked in it; ``null`` without one) and ``blocks``
    (``[step, block]`` of every block with a marked event in it, in order)."""
    marked = _marked(folder)
    events = {e.get("seq"): e for e in _lines(folder / "events.jsonl")}
    out = []
    for name in recordings(folder):
        seqs = sorted(seq for seq, (_, where) in marked.items() if where == name)
        times = [events[s]["unix_time"] for s in seqs if s in events and isinstance(events[s].get("unix_time"), (int, float))]
        blocks: list[list] = []
        for s in seqs:
            e = events.get(s)
            if e and e.get("step") is not None and e.get("block") is not None and [e["step"], e["block"]] not in blocks:
                blocks.append([e["step"], e["block"]])
        out.append(
            {
                "folder": name,
                "markers": len(seqs),
                "start_unix": min(times) if times else None,
                "end_unix": max(times) if times else None,
                "blocks": blocks,
            }
        )
    return out


def timeline(folder: Path) -> list[dict]:
    """Each step and each block, in the order they began, with start and end (UTC and sample).

    From ``events.jsonl`` and the markers of ``eeg/``: the sample of an event is that of the marker
    with its ``seq``. A step ends where the next one starts, or at the event that ended the session;
    a block at its ``block_end`` (``outcome`` ``finished``, or ``interrupted`` when a pause ended it)
    or at the event that cut it short (``left``). An end that was never recorded (the process died) is ``null``; so is the sample of
    an event that has no marker in ``eeg/`` (``source_ended``, or anything before a recording began).
    """
    samples = _samples(folder)
    items: list[dict] = []
    step_item: dict | None = None
    block_item: dict | None = None
    part_item: dict | None = None

    def close(item: dict | None, event: dict, outcome: str | None = None) -> None:
        if item is None or item["end_unix"] is not None:
            return
        item["end_unix"] = event.get("unix_time")
        item["end_sample"] = samples.get(event.get("seq"))
        if outcome is not None:
            item["outcome"] = outcome

    def opened(event: dict, block) -> dict:
        return {
            "step": event.get("step"),
            "block": block,
            "start_unix": event.get("unix_time"),
            "start_sample": samples.get(event.get("seq")),
            "end_unix": None,
            "end_sample": None,
            "outcome": None,
        }

    for event in _lines(folder / "events.jsonl"):
        name = event.get("name")
        if name in PART_STARTS:
            # A part of the calibration step: it ends at its own end event, or (settling) at the
            # next part; anything else that comes first (a stop, a skip, Check again) cuts it short.
            close(part_item, event, "finished" if part_item is not None and part_item["part"] == "settling" else "left")
            part_item = {**opened(event, None), "part": PART_STARTS[name]}
            items.append(part_item)
            continue
        if name in PART_ENDS:
            if part_item is not None and part_item["part"] == PART_ENDS[name]:
                close(part_item, event, "finished")
            continue
        if name == "contact_not_off" or name == "step_start" or name in BLOCK_ENDS:
            close(part_item, event, "left")
        if name == "step_start":
            close(block_item, event, "left")
            close(step_item, event)
            step_item, block_item = opened(event, None), None
            items.append(step_item)
        elif name == "block_start":
            block_item = opened(event, (event.get("payload") or {}).get("block", event.get("block")))
            items.append(block_item)
        elif name in BLOCK_ENDS:
            ended_by = (event.get("payload") or {}).get("ended_by") if name == "block_end" else None
            close(
                block_item,
                event,
                "interrupted" if ended_by == "interrupted" else "finished" if name == "block_end" else "left",
            )
            if name in ENDINGS:
                close(step_item, event)
    result = []
    for item in items:
        start, end = item["start_unix"], item["end_unix"]
        entry = {
            "step": item["step"],
            "block": item["block"],
            "start_utc": _iso(start),
            "end_utc": _iso(end),
            "start_unix": start,
            "end_unix": end,
            "start_sample": item["start_sample"],
            "end_sample": item["end_sample"],
            "seconds": round(end - start, 3) if start is not None and end is not None else None,
        }
        if "part" in item:
            entry["part"] = item["part"]
        if item["block"] is not None or "part" in item:
            entry["outcome"] = item["outcome"]
        result.append(entry)
    return result


def reached_end(folder: Path) -> bool:
    """Whether the participant finished the last step (the ``session_end`` event is saved)."""
    return any(e.get("name") == "session_end" for e in _lines(folder / "events.jsonl"))


def _flow_from(folder: Path, steps, saved: progress.Progress) -> list[dict]:
    """The steps and blocks of the session: from the engine's flow, or else from what the folder holds."""
    if steps is not None:
        return [
            {
                "id": s.id,
                "kind": s.kind,
                "title": s.title,
                "blocks": [{"id": b.id, "seconds": b.seconds, "points": b.points} for b in s.blocks],
            }
            for s in steps
        ]
    # Recovered without the engine: the steps of progress.json, and the blocks that started.
    blocks: dict[str, dict[str, dict]] = {}
    for event in _lines(folder / "events.jsonl"):
        if event.get("name") == "block_start":
            payload = event.get("payload") or {}
            blocks.setdefault(event.get("step"), {})[payload.get("block")] = {
                "id": payload.get("block"),
                "seconds": payload.get("seconds"),
                "points": None,
            }
    return [
        {"id": s["id"], "kind": s["kind"], "title": s["title"], "blocks": list(blocks.get(s["id"], {}).values())}
        for s in saved.steps
    ]


def _software() -> dict:
    found = {}
    for key, name in (("brain_mapper", "brain-mapper"), ("open_cognionics", "open-cognionics")):
        try:
            found[key] = version(name)
        except PackageNotFoundError:
            found[key] = None
    return found


def _calibration(folder: Path) -> tuple[list, str | None, str | None]:
    """``(channel_quality_history, contact_thresholds_version, problem)`` from ``calibration.json``.

    Absent: ``[]``, ``None``. Present: its content (without ``checks``) is the first entry of the
    history and its ``thresholds_version`` the version; the headset checks made from the paused
    screen (``checks``, each dated) follow it. A file that cannot be read gives ``[]``, ``None`` and
    words.
    """
    path = folder / "calibration.json"
    if not path.exists():
        return [], None, None
    try:
        data = _read_json(path, "calibration.json")
    except PackageError as error:
        return [], None, str(error)
    checks = data.get("checks")
    later = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    first = {k: v for k, v in data.items() if k != "checks"}
    history = ([first] if "states" in first else []) + later
    found = data.get("thresholds_version")
    if not isinstance(found, str) and later:
        found = later[0].get("thresholds_version")
    return history, found if isinstance(found, str) else None, None


def _set_aside(meta: dict) -> bool:
    """Whether ``meta`` holds the hub's list of set-aside recordings (``[{"folder", ...}]``)."""
    found = meta.get("recordings")
    return isinstance(found, list) and all(isinstance(r, dict) and "folder" in r for r in found) and bool(found)


def awards(folder: Path) -> list[dict]:
    """The lines of ``points.jsonl`` that can be read, in order."""
    return _lines(folder / "points.jsonl")


def _progress(folder: Path) -> progress.Progress:
    try:
        return progress.read(folder)
    except progress.ProgressDamaged as error:
        raise PackageError("its progress.json cannot be read or does not fit") from error


def describe(
    folder: Path,
    complete: bool,
    *,
    steps=None,
    replay_name: str | None = None,
    reason: str | None = None,
    hook_errors: list[dict] | None = None,
    not_zipped: list[dict] | None = None,
) -> dict:
    """Adds the record's fields to ``session.json`` (once) and returns it.

    Keeps every key already there. If the fields were added before (``complete`` is there), the file
    is left exactly as it is. ``complete`` is kept only if the session reached its normal end: the
    participant finished the last step (``session_end`` saved, ``progress.json`` ``finished``).
    ``steps`` is the engine's flow (``None`` when recovering: then the steps of ``progress.json``
    and the blocks that started). ``reason`` is why the session ended, if known. ``hook_errors``
    are the ``BEFORE_ZIP`` hooks that raised, ``not_zipped`` the links and special files of the folder.
    """
    folder = Path(folder)
    meta = session_meta(folder)
    if "complete" in meta:
        return meta
    saved = _progress(folder)
    recorder: dict = {}
    try:
        recorder = _read_json(folder / "eeg" / "meta.json", "eeg/meta.json")
    except PackageError:
        pass  # no recording, or it cannot be read: the record then has no headset details
    started = float(meta["started_unix"])
    history, thresholds, calibration_problem = _calibration(folder)
    made = recordings(folder)
    # A start that failed (the hub keeps the folder and writes ``failure`` into session.json).
    failed_start = isinstance(meta.get("failure"), str)
    record = {
        **meta,
        "participant": meta["label"],
        "participant_uuid": meta["uuid"],
        "start_utc": _iso(started),
        "start_local": _iso(started, local=True),
        "headset": {"port": recorder.get("port"), "config": recorder.get("config")} if recorder else None,
        "software": _software(),
        "flow": _flow_from(folder, steps, saved),
        "timeline": timeline(folder),
        "channel_quality_history": history,
        "contact_thresholds_version": thresholds,
        "points_total": saved.points_total,
        # Earned of possible per task, from the trial events (``brain_mapper.scores``).
        "scores": scores.score_folder(folder),
        "recording": "eeg" in made,
        "recordings": made,
        # Every recording with the times and blocks of the events marked in it (a lost headset
        # link closes one and the next begins when the link is back).
        "recording_coverage": coverage(folder),
        # The main line's ``recordings`` (written by the hub while the session runs: each
        # set-aside recording with ``decoded_incl_lost``) is kept under its own key.
        **({"recordings_set_aside": meta["recordings"]} if _set_aside(meta) else {}),
        "complete": bool(complete) and saved.status == "finished" and reached_end(folder),
        # A session that did not reach its end is ended early unless the participant stopped it.
        "ended_early": failed_start or saved.status not in ("finished", "stopped"),
        "ending": "failed_start" if failed_start else "interrupted" if saved.status == "running" else saved.status,
        "ending_reason": meta["failure"] if failed_start else reason,
        "hook_errors": list(hook_errors or []),
        "not_zipped": list(not_zipped or []),
    }
    if calibration_problem is not None:
        record["calibration_problem"] = calibration_problem
    if is_replay(meta):
        raw = folder / "eeg" / "raw.bin"
        record["replay_of"] = {"name": replay_name, "raw_sha256": _sha256(raw) if raw.is_file() else None}
    progress.write_json(folder / "session.json", record)
    return record


# -- the zip -------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def members(folder: Path) -> Listing:
    """Every regular file of the folder at any depth, by its path in the zip.

    Symbolic links (to files or folders) are not followed and, with any other entry that is not a
    regular file, are listed in ``not_zipped`` with the reason. The only regular files left out are
    the temporary files of ``progress.write_json`` at the top of the folder, in the exact shape it
    makes (``OWN_TEMPORARY``); they are listed too. The analysis folders at the top (``ANALYSIS_DIR``: written by
    ``analysis/run.py`` after the zip, from it, and zipped on their own) are left out and listed. ``points.jsonl`` is also put into the zip as
    ``behavior/points.jsonl``, unless the folder has a file of that name itself. Anything that
    cannot be listed (a folder that cannot be read) raises ``PackageError``: a zip is never made
    from part of the folder.
    """
    listing = Listing()

    def refuse(error: OSError) -> None:
        raise PackageError(
            f"a folder inside it could not be read ({error.strerror or type(error).__name__}), "
            "so it was not zipped in part"
        ) from error

    def skip(rel: str, reason: str) -> None:
        listing.not_zipped.append({"path": rel, "reason": reason})

    for top, dirs, files in os.walk(folder, onerror=refuse, followlinks=False):
        here = Path(top)
        for name in sorted(dirs):
            if (here / name).is_symlink():
                skip((here / name).relative_to(folder).as_posix(), "a symbolic link, not followed")
        dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink())
        if here == folder:
            for name in [d for d in dirs if ANALYSIS_DIR.fullmatch(d)]:
                # The results of analysis/run.py, made from this zip after it was saved and kept
                # in their own <session>-analysis.zip (merge of night-analysis, 2026-11-26).
                skip(name, "analysis results, saved in their own -analysis.zip")
                dirs.remove(name)
        for name in sorted(files):
            path = here / name
            rel = path.relative_to(folder).as_posix()
            try:
                mode = os.lstat(path).st_mode
            except OSError as error:
                refuse(error)
            if stat.S_ISLNK(mode):
                skip(rel, "a symbolic link, not followed")
            elif not stat.S_ISREG(mode):
                skip(rel, "not a regular file")
            elif here == folder and OWN_TEMPORARY.fullmatch(name):
                skip(rel, "a temporary file of an interrupted write of session.json or progress.json")
            else:
                listing.files[rel] = path
    if "points.jsonl" in listing.files and "behavior/points.jsonl" not in listing.files:
        listing.files["behavior/points.jsonl"] = folder / "points.jsonl"
    return listing


def _base(folder: Path, meta: dict) -> str:
    return folder.name + ("_replay" if is_replay(meta) else "")


def _candidates(sessions: Path, base: str) -> Iterator[Path]:
    """``<base>.zip``, ``<base>-2.zip``, ``<base>-3.zip`` ... without end."""
    number = 1
    while True:
        yield sessions / (f"{base}.zip" if number == 1 else f"{base}-{number}.zip")
        number += 1


def partial_pattern(base: str) -> re.Pattern:
    """The names of this session's temporary zip files: ``.<base>.zip.partial-<8 hex digits>``."""
    return re.compile(rf"\.{re.escape(base)}\.zip\.partial-[0-9a-f]{{8}}")


def verify(path: Path, expected: dict[str, Path], crc_only: tuple[str, ...] = ()) -> bool:
    """Whether the zip at ``path`` holds exactly ``expected``, each member equal to its file.

    Reads every member to its end (``zipfile`` checks its CRC there) and compares its size and
    sha256 with the file. Members named in ``crc_only`` are only read back (their file has changed
    since, as ``session.json`` does when ``zip`` is added).
    """
    try:
        with zipfile.ZipFile(path) as z:
            if sorted(z.namelist()) != sorted(expected):
                return False
            for name, source in expected.items():
                info = z.getinfo(name)
                digest = hashlib.sha256()
                with z.open(info) as member:
                    for chunk in iter(lambda: member.read(CHUNK), b""):
                        digest.update(chunk)
                if name in crc_only:
                    continue
                if info.file_size != source.stat().st_size or digest.hexdigest() != _sha256(source):
                    return False
    except (OSError, zipfile.BadZipFile, KeyError, EOFError):
        return False
    return True


def _write(handle, expected: dict[str, Path]) -> None:
    """Writes the zip into the open file ``handle``, in chunks, syncs it and closes it."""
    with handle:
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for name, source in expected.items():
                z.write(source, name)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # best effort: the file itself is synced
    finally:
        os.close(fd)


def rename_exclusive(source: Path, target: Path) -> None:
    """Renames ``source`` to ``target`` only if ``target`` does not exist (``FileExistsError`` if it
    does): ``renamex_np(RENAME_EXCL)`` on macOS, ``renameat2(RENAME_NOREPLACE)`` on Linux. Elsewhere
    ``PackageError``, because a plain rename could replace a file."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        call = libc.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = call(os.fsencode(source), os.fsencode(target), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        call = libc.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = call(-100, os.fsencode(source), -100, os.fsencode(target), 0x1)  # AT_FDCWD, NOREPLACE
    else:
        raise PackageError("this system offers no way to name the zip without the risk of replacing a file")
    if result != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise FileExistsError(code, os.strerror(code), str(target))
        raise OSError(code, os.strerror(code), str(target))


def place(temp: Path, sessions: Path, base: str, link: bool = True) -> Path:
    """Gives the verified ``temp`` the first free name and returns it; never replaces a file.

    A hard link where the file system has them (``temp`` is then removed: the zip keeps its own
    name), else an exclusive rename of ``temp`` itself, so the final name never holds anything but
    the verified file. ``link=False`` goes straight to the rename.
    """
    for final in _candidates(sessions, base):
        if link:
            try:
                os.link(temp, final)
            except FileExistsError:
                continue
            except OSError as error:
                if error.errno not in (errno.ENOTSUP, errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP):
                    raise
                link = False  # this file system has no hard links
            else:
                try:
                    os.unlink(temp)  # the same file under a second name
                except OSError:
                    pass
                _sync_dir(sessions)
                return final
        try:
            rename_exclusive(temp, final)
        except FileExistsError:
            continue
        _sync_dir(sessions)
        return final
    raise AssertionError("unreachable")


@contextlib.contextmanager
def locked(folder: Path, wait: bool = True) -> Iterator[bool]:
    """Holds an exclusive lock on the session folder; yields ``False`` if ``wait`` is false and it is held."""
    if fcntl is None:
        yield True
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing it releases the lock


def before_zip(folder: Path, label: str | None = None) -> list[dict]:
    """Runs the ``BEFORE_ZIP`` hooks; returns ``[{"hook", "error"}]`` for each that raised (logged too)."""
    errors = []
    for hook in BEFORE_ZIP:
        try:
            hook(folder)
        except Exception as error:
            name = getattr(hook, "__qualname__", repr(hook))
            text = scrub(f"{type(error).__name__}: {error}", folder, label)
            LOG.error("a hook before the session zip failed (%s): %s", name, text)
            errors.append({"hook": name, "error": text})
    return errors


def finish(
    folder: Path,
    complete: bool,
    *,
    steps=None,
    replay_name: str | None = None,
    reason: str | None = None,
    held: bool = False,
) -> Path:
    """Completes ``session.json``, writes and checks the zip beside ``folder``, and records it.

    Returns the zip's path. Raises ``PackageError`` (in words, without a path) or ``OSError``;
    either way the folder is as complete as it was and a later call (the next start) finishes the
    job. ``held`` says the caller holds the folder's lock already (``locked``); otherwise it is
    taken here. The recording must be closed before this is called.
    """
    folder = Path(folder)
    context = contextlib.nullcontext(True) if held else locked(folder)
    with context:
        return _finish(folder, complete, steps, replay_name, reason)


def _finish(folder: Path, complete: bool, steps, replay_name, reason) -> Path:
    sessions = folder.parent
    meta = session_meta(folder)
    if meta.get("zip"):
        return sessions / meta["zip"]
    _progress(folder)  # checked before anything is written
    # A start that failed is kept exactly as it is (nothing is written into it before its zip).
    failed_start = isinstance(meta.get("failure"), str)
    hook_errors = before_zip(folder, meta["label"]) if "complete" not in meta and not failed_start else []
    describe(
        folder,
        complete,
        steps=steps,
        replay_name=replay_name,
        reason=reason,
        hook_errors=hook_errors,
        not_zipped=members(folder).not_zipped,
    )
    base = _base(folder, meta)
    listing = members(folder)
    expected = listing.files
    found = None
    for candidate in _candidates(sessions, base):
        if not candidate.exists():
            break
        if verify(candidate, expected):
            found = candidate  # placed before a crash, never recorded: it is this session's zip
            break
    if found is None:
        temp = sessions / f".{base}.zip.partial-{secrets.token_hex(4)}"
        handle = open(temp, "xb")  # exclusive: from here on the file is this attempt's own
        made = True
        try:
            _write(handle, expected)
            if not verify(temp, expected):
                raise PackageError("the zip did not read back the same as the session files")
            found = place(temp, sessions, base)
            made = False
        except BaseException as error:
            if made:
                LOG.error("the session zip could not be made: %s", scrub(f"{type(error).__name__}: {error}", folder, meta["label"]))
                try:
                    os.unlink(temp)  # this attempt's own temporary copy, never recorded data
                except OSError:
                    pass
            raise
    record = session_meta(folder)
    record["zip"] = found.name
    progress.write_json(folder / "session.json", record)
    return found


#: ``session.json`` key: events appended after the session was zipped (``count``, ``first_unix``,
#: ``last_unix``: server times of their arrival).
LATE = "late_events_after_zip"
#: ``session.json`` key: the additional zips made for them, ``[{"zip", "events", "hook_errors"}]``.
LATE_ZIPS = "late_zips"


def note_late(folder: Path, unix_time: float) -> None:
    """Counts one event appended to a zipped session's ``events.jsonl`` (``LATE`` in
    ``session.json``). The caller holds the folder's lock."""
    record = session_meta(folder)
    entry = dict(record.get(LATE) or {})
    entry["count"] = int(entry.get("count", 0)) + 1
    entry.setdefault("first_unix", unix_time)
    entry["last_unix"] = unix_time
    record[LATE] = entry
    progress.write_json(folder / "session.json", record)


def late_zip(folder: Path, held: bool = False) -> Path | None:
    """Makes an additional zip of a zipped session that took events after its zip (``LATE``), once
    for each new count: ``<base>-late.zip`` (then ``-late-2.zip`` ...), never replacing or removing
    the first zip or any other file. The ``BEFORE_ZIP`` hooks run again first (behavior files and
    scores take the late events in), ``scores`` in ``session.json`` is brought up to date, and the
    zip is verified like the first one before it gets its name; it is then listed in ``LATE_ZIPS``.
    Returns its path, or ``None`` when nothing is due. Raises ``PackageError`` or ``OSError``."""
    folder = Path(folder)
    context = contextlib.nullcontext(True) if held else locked(folder)
    with context:
        meta = session_meta(folder)
        late = meta.get(LATE)
        if not meta.get("zip") or not isinstance(late, dict):
            return None
        done = list(meta.get(LATE_ZIPS) or [])
        if done and done[-1].get("events") == late.get("count"):
            return None
        hook_errors = before_zip(folder, meta["label"])
        record = session_meta(folder)
        record["scores"] = scores.score_folder(folder)
        progress.write_json(folder / "session.json", record)
        sessions = folder.parent
        base = _base(folder, meta) + "-late"
        expected = members(folder).files
        temp = sessions / f".{base}.zip.partial-{secrets.token_hex(4)}"
        _write(open(temp, "xb"), expected)  # exclusive: this attempt's own file
        if not verify(temp, expected):
            raise PackageError("the additional zip did not read back the same as the session files")
        found = place(temp, sessions, base)
        record = session_meta(folder)
        record[LATE_ZIPS] = done + [{"zip": found.name, "events": late.get("count"), "hook_errors": hook_errors}]
        progress.write_json(folder / "session.json", record)
        return found


def summary(folder: Path) -> dict:
    """What the summary screen shows of a session, from its folder (no path in it)."""
    folder = Path(folder)
    meta = session_meta(folder)
    lines = awards(folder)
    started = float(meta["started_unix"])
    return {
        "label": meta["label"],
        "start_local": _iso(started, local=True),
        "started_unix": started,
        "points_total": lines[-1].get("total", 0) if lines else 0,
        "scores": meta["scores"] if isinstance(meta.get("scores"), dict) else scores.score_folder(folder),
        "recordings": recordings(folder),
        "awards": [
            {k: line.get(k) for k in ("step", "block", "reason", "amount", "total", "carried_from")} for line in lines
        ],
        "timeline": [
            {k: item.get(k) for k in ("step", "block", "part", "start_unix", "seconds", "outcome")}
            for item in timeline(folder)
            if item["block"] is not None or item.get("part") is not None
        ],
    }


# -- at start-up -----------------------------------------------------------------------------------


def in_words(error: BaseException) -> str:
    """A failure of ``finish`` in words, with no path in it."""
    if isinstance(error, PackageError):
        return str(error)
    if isinstance(error, OSError):
        return f"a file could not be read or written ({error.strerror or type(error).__name__})"
    return f"an unexpected problem ({type(error).__name__})"


def _leftovers(sessions: Path, base: str) -> list[Path]:
    pattern = partial_pattern(base)
    return sorted(p for p in sessions.iterdir() if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink())


def _tidy(folder: Path, base: str) -> tuple[int, bool]:
    """The crash leftovers of this session, removed if its recorded zip reads back and matches the folder."""
    sessions = folder.parent
    found = _leftovers(sessions, base)
    if not found:
        return 0, False
    meta = session_meta(folder)
    zip_path = sessions / meta["zip"] if meta.get("zip") else None
    if zip_path is None or not verify(zip_path, members(folder).files, crc_only=("session.json",)):
        return len(found), False
    for path in found:
        os.unlink(path)  # exact names of this session's temporary copies; its verified zip exists
    return len(found), True


def recover(data_dir: Path) -> list[Recovered]:
    """Packages every session folder in ``<data_dir>/sessions/`` that has no zip yet.

    A session whose ``progress.json`` still says ``running`` ended with the process: it is packaged
    with ``complete`` false. One whose ending was recorded but whose zip was not made (the process
    died while making it) is packaged as it would have been. A folder that cannot be read, or whose
    points ledger does not add up, is described in ``problem`` and left exactly as it is; so is a
    folder another brain-mapper process holds. Temporary zip files a crash left for a session are
    counted in ``leftovers`` and removed once the session's verified zip exists.
    """
    sessions = Path(data_dir) / "sessions"
    done: list[Recovered] = []
    try:
        folders = sorted(p for p in sessions.iterdir() if p.is_dir() and not p.name.startswith("."))
    except FileNotFoundError:
        return done
    for folder in folders:
        try:
            with locked(folder, wait=False) as mine:
                if not mine:
                    done.append(Recovered(folder.name, problem="another brain-mapper process is using it"))
                    continue
                meta = session_meta(folder)
                base = _base(folder, meta)
                made = None
                if not meta.get("zip"):
                    saved = _progress(folder)
                    ledger = folder / "points.jsonl"
                    try:
                        lines = points.read_awards(ledger) if ledger.exists() else []
                    except points.PointsError as error:
                        raise PackageError("its points.jsonl does not add up") from error
                    if (lines[-1]["total"] if lines else 0) != saved.points_total:
                        raise PackageError("its progress.json and its points.jsonl give different totals")
                    made = finish(folder, complete=saved.status == "finished", held=True).name
                count, removed = _tidy(folder, base)
                extra = late_zip(folder, held=True)
                if made is not None or count or extra is not None:
                    failed = isinstance(session_meta(folder).get("failure"), str)
                    done.append(
                        Recovered(
                            folder.name,
                            zip=made,
                            leftovers=count,
                            removed=removed,
                            failed_start=failed,
                            late_zip=extra.name if extra is not None else None,
                        )
                    )
        except Exception as error:  # every folder is tried; a failure is reported, not raised
            done.append(Recovered(folder.name, problem=in_words(error)))
    return done

"""The behaviour files of a session: ``behavior/<task>.csv``, one row per trial.

Built from ``events.jsonl`` and the recording's ``eeg/markers.jsonl``. Every
``<task>.trial`` event the page sent (see ``web/js/tasks/runner.js``) is one row.
Columns, in order:

* ``seq``, ``sample`` (the EEG sample of the trial event's marker, empty if it has
  none), ``recording`` (the recording folder that marker is in: ``eeg``, or ``eeg-lost-<n>``
  for a recording closed by a lost headset link; sample numbers start at 0 in each),
  ``unix_time``, ``block``, ``trial``, ``practice``;
* for each stimulus part ``<part>`` of the task (``stim`` first, then the others in
  sorted order) the part's first ``<task>.stim`` event of the trial:
  ``<part>_seq``, ``<part>_sample`` (its marker's sample: the arrival at the server),
  ``<part>_recording`` (the recording folder that marker is in, as ``recording``: after a lost
  headset link a trial's stimulus can be in ``eeg-lost-<n>`` and its response in ``eeg``),
  ``<part>_lag_ms`` (the server's arrival time minus the page's time of the event,
  ``unix_time * 1000 - (time_origin + browser_time)``: how late the sample stamp is)
  and ``<part>_off_ms`` (the first ``<task>.stim_off`` of that part after it, minus
  its onset, browser clock);
* ``resp_seq``, ``resp_sample``, ``resp_recording``, ``resp_lag_ms`` of the trial's last
  ``<task>.resp``;
* ``queued``: true if the trial event, a stimulus or the response was sent again after
  the connection was back (its lag is then the outage, and its sample must be corrected);
* the union of the payload keys of that task's trial events, in sorted order (these
  include ``<part>_browser_ms``, ``<part>_unix_ms``, ``resp_browser_ms``, ``resp_unix_ms``
  from the page). A payload key that is also one of the columns above is written as
  ``payload_<key>``; ``payload_block``, ``payload_trial`` and ``payload_practice`` are left
  out when they equal the fixed column in every row (the runner sends those in the payload).

``None`` is an empty cell, ``true``/``false`` and numbers are written as in JSON, lists
and objects as JSON text. A text cell, the JSON text of a list or object, and a column name
that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return gets a ``'`` in front,
so a spreadsheet does not read it as a formula. The server stores every payload key and part as
sent; a column name made from one that is not 1 to 48 of ``a-z 0-9 _`` starting with a letter is
written as ``x_`` and the name in lower case with every other character replaced by ``_``
(``delayMs`` is ``x_delayms``, ``=cmd`` is ``x__cmd``), a collision gets ``_2``, ``_3`` ..., and
``behavior/columns.json`` lists the original name of every renamed column of each file (``{}``
when none was renamed); it is written with the CSV files and never removed.

Each file is written to a new temporary file in the same folder and renamed over the old one; an
existing file with more rows than the new one is kept as it is. A task whose file cannot
be written is logged and the other tasks' files are still written.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import tempfile
from pathlib import Path

LOG = logging.getLogger("brain_mapper.behavior")

FOLDER = "behavior"
SUFFIX = ".trial"
FIRST = ["seq", "sample", "recording", "unix_time", "block", "trial", "practice"]
FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _guard(text: str) -> str:
    """``text`` with a ``'`` in front if a spreadsheet would read it as a formula."""
    return "'" + text if text.startswith(FORMULA) else text


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _guard(value)
    if isinstance(value, (bool, int, float)):
        return json.dumps(value, allow_nan=False)
    return _guard(json.dumps(value, allow_nan=False))


def _lines(path: Path) -> list[dict]:
    """The JSON objects of a JSON-lines file; a line that cannot be read is skipped and counted."""
    found: list[dict] = []
    bad = 0
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(item, dict):
            found.append(item)
        else:
            bad += 1
    if bad:
        LOG.warning(
            "%d lines of %s could not be read and were left out", bad, path.name
        )
    return found


def _recording_folders(folder: Path) -> list[str]:
    """The recording folders of the session: ``eeg-lost-<n>`` in order, then ``eeg``."""
    found = []
    for path in folder.iterdir() if folder.is_dir() else []:
        name = path.name
        if path.is_dir() and not path.is_symlink() and (
            name == "eeg" or (name.startswith("eeg-lost-") and name[9:].isdigit())
        ):
            found.append(name)
    return sorted(found, key=lambda n: (n == "eeg", int(n[9:]) if n != "eeg" else 0))


def _marks(folder: Path) -> dict[int, tuple[int, str]]:
    """``seq`` to the sample of its marker and the recording folder the marker is in (labels
    end in ``#<seq>``), over every recording of the session."""
    found: dict[int, tuple[int, str]] = {}
    for name in _recording_folders(folder):
        for marker in _lines(folder / name / "markers.jsonl"):
            label = marker.get("label")
            if not isinstance(label, str) or "#" not in label:
                continue
            tail = label.rsplit("#", 1)[1]
            if tail.isdigit() and isinstance(marker.get("sample"), int):
                found[int(tail)] = (marker["sample"], name)
    return found


def _samples(folder: Path) -> dict[int, int]:
    """``seq`` to the sample of its marker, over every recording of the session."""
    return {seq: sample for seq, (sample, _) in _marks(folder).items()}


def _rows_in(path: Path) -> int:
    """Data rows of an existing behaviour file (0 if there is none)."""
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", newline="") as stream:
        return max(0, sum(1 for _ in csv.reader(stream)) - 1)


def _replace(path: Path, text: str) -> None:
    """Writes ``text`` to a new temporary file of this call's own making beside ``path``
    (``tempfile.mkstemp``: ``.<name>.<random>.tmp``, created exclusively) and renames it over
    ``path``. If that fails, the temporary file it made, and only that, is removed."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def lag_ms(event: dict) -> float | None:
    """How late the server stamped ``event``: its arrival minus the page's time of it, in ms."""
    try:
        sent = float(event["time_origin"]) + float(event["browser_time"])
        return round(float(event["unix_time"]) * 1000 - sent, 3)
    except (KeyError, TypeError, ValueError):
        return None


def _trial_key(event: dict) -> tuple:
    p = event["payload"]
    return (
        json.dumps(p.get("block")),
        json.dumps(p.get("trial")),
        json.dumps(p.get("practice")),
    )


def _queued(event: dict | None) -> bool:
    return bool(event and event["payload"].get("queued") is True)


NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
COLUMNS_FILE = "columns.json"


def _neutral(name: str) -> str:
    """A column name as it is written: ``name`` if it is 1 to 48 of ``a-z 0-9 _`` starting with a
    letter, else ``x_`` and ``name`` in lower case with every other character replaced by ``_``
    (at most 48 characters)."""
    if NAME.fullmatch(name):
        return name
    return ("x_" + re.sub(r"[^a-z0-9_]", "_", name.lower()))[:48]


def _column_names(columns: list[str]) -> tuple[list[str], dict[str, str]]:
    """The written names of ``columns`` and the written name to original name of each one that
    changed. A name that follows the rule is kept; any other is made neutral, and gets ``_2``,
    ``_3`` ... (in column order) when that name is taken."""
    out: list[str] = []
    seen: set[str] = {c for c in columns if NAME.fullmatch(c)}
    renamed: dict[str, str] = {}
    for original in columns:
        if NAME.fullmatch(original):
            out.append(original)
            continue
        name = _neutral(original)
        if name in seen:
            i = 2
            while f"{name}_{i}" in seen:
                i += 1
            name = f"{name}_{i}"
        seen.add(name)
        out.append(name)
        if name != original:
            renamed[name] = original
    return out, renamed


def tables(folder: Path) -> dict[str, tuple[list[str], list[list[str]]]]:
    """Task name to ``(columns, rows)``, from the files of the session ``folder``; the columns
    as they are written (see ``_column_names``)."""
    return {task: (_column_names(columns)[0], rows) for task, (columns, rows) in _tables(folder).items()}


def _tables(folder: Path) -> dict[str, tuple[list[str], list[list[str]]]]:
    """Task name to ``(columns, rows)`` with the columns named from the events as they are."""
    folder = Path(folder)
    marks = _marks(folder)
    samples = {seq: sample for seq, (sample, _) in marks.items()}
    trials: dict[str, list[dict]] = {}
    parts: dict[
        str, dict[tuple, dict[str, dict]]
    ] = {}  # task -> trial -> part -> first stim
    offs: dict[str, dict[tuple, dict[str, list[dict]]]] = {}
    resps: dict[str, dict[tuple, dict]] = {}  # task -> trial -> last resp
    for event in _lines(folder / "events.jsonl"):
        name = event.get("name")
        if not isinstance(name, str) or "." not in name:
            continue
        if not isinstance(event.get("payload"), dict) or not isinstance(
            event.get("seq"), int
        ):
            continue
        task, _, kind = name.rpartition(".")
        if not task:
            continue
        if kind == "trial":
            trials.setdefault(task, []).append(event)
        elif kind == "stim":
            part = event["payload"].get("part", "stim")
            if isinstance(part, str):
                parts.setdefault(task, {}).setdefault(_trial_key(event), {}).setdefault(
                    part, event
                )
        elif kind == "stim_off":
            part = event["payload"].get("part", "stim")
            if isinstance(part, str):
                offs.setdefault(task, {}).setdefault(_trial_key(event), {}).setdefault(
                    part, []
                ).append(event)
        elif kind == "resp":
            resps.setdefault(task, {})[_trial_key(event)] = event
    out: dict[str, tuple[list[str], list[list[str]]]] = {}

    def recording_of(event: dict | None) -> str | None:
        """The recording folder of the event's marker (``None`` without one)."""
        if event is None or event["seq"] not in marks:
            return None
        return marks[event["seq"]][1]

    for task, events in trials.items():
        task_parts = parts.get(task, {})
        names = sorted(
            {p for found in task_parts.values() for p in found},
            key=lambda p: (p != "stim", p),
        )
        timing = [
            f"{p}_{field}"
            for p in names
            for field in ("seq", "sample", "recording", "lag_ms", "off_ms")
        ]
        timing += ["resp_seq", "resp_sample", "resp_recording", "resp_lag_ms", "queued"]
        fixed = FIRST + timing
        # ``queued`` (on an event sent again) is in the ``queued`` column, which also counts the
        # trial's stimuli and response.
        keys = sorted({k for e in events for k in e["payload"]} - {"queued"})
        columns = fixed + [f"payload_{k}" if k in fixed else k for k in keys]
        rows = []
        for event in sorted(events, key=lambda e: e["seq"]):
            payload = event["payload"]
            key = _trial_key(event)
            row = [
                str(event["seq"]),
                _cell(samples.get(event["seq"])),
                _cell(marks[event["seq"]][1] if event["seq"] in marks else None),
                _cell(event.get("unix_time")),
                _cell(payload.get("block", event.get("block"))),
                _cell(payload.get("trial")),
                _cell(payload.get("practice")),
            ]
            queued = _queued(event)
            for part in names:
                stim = task_parts.get(key, {}).get(part)
                off = None
                if stim is not None:
                    queued = queued or _queued(stim)
                    later = [
                        o
                        for o in offs.get(task, {}).get(key, {}).get(part, [])
                        if o["seq"] > stim["seq"]
                    ]
                    if later:
                        off = round(
                            float(later[0]["browser_time"])
                            - float(stim["browser_time"]),
                            3,
                        )
                row += [
                    _cell(stim["seq"] if stim else None),
                    _cell(samples.get(stim["seq"]) if stim else None),
                    _cell(recording_of(stim)),
                    _cell(lag_ms(stim) if stim else None),
                    _cell(off),
                ]
            resp = resps.get(task, {}).get(key)
            queued = queued or _queued(resp)
            row += [
                _cell(resp["seq"] if resp else None),
                _cell(samples.get(resp["seq"]) if resp else None),
                _cell(recording_of(resp)),
                _cell(lag_ms(resp) if resp else None),
                _cell(queued),
            ]
            row += [_cell(payload.get(k)) for k in keys]
            rows.append(row)
        # The runner sends `block`, `trial` and `practice` in the payload too: a payload column
        # that is the same as its fixed column in every row is left out. One that differs is kept
        # (and logged: the runner and the file disagree).
        dropped = set()
        for name in ("block", "trial", "practice"):
            if f"payload_{name}" not in columns:
                continue
            a, b = columns.index(name), columns.index(f"payload_{name}")
            if all(row[a] == row[b] for row in rows):
                dropped.add(b)
            else:
                LOG.warning("behavior file of %s: payload %s differs from the %s column", task, name, name)
        if dropped:
            columns = [c for i, c in enumerate(columns) if i not in dropped]
            rows = [[c for i, c in enumerate(row) if i not in dropped] for row in rows]
        out[task] = (columns, rows)
    return out


def write(folder: Path) -> list[Path]:
    """Writes ``behavior/<task>.csv`` for each task with trial events; returns the files written.

    An existing file with more rows than the new one is kept and left out of the list. A file
    that cannot be written is logged and left out; the other tasks' files are still written.
    Raises ``OSError`` only if the ``behavior`` folder cannot be made. The session's other
    files are only read.
    """
    folder = Path(folder)
    found = _tables(folder)
    if not found:
        return []
    out_dir = folder / FOLDER
    out_dir.mkdir(exist_ok=True)
    written: list[Path] = []
    names: dict[str, dict[str, str]] = {}
    for task in sorted(found):
        columns, rows = found[task]
        columns, renamed = _column_names(columns)
        if renamed:
            names[f"{task}.csv"] = renamed
        path = out_dir / f"{task}.csv"
        try:
            if _rows_in(path) > len(rows):
                LOG.warning(
                    "a behavior file with more rows than the event log gives was kept"
                )
                continue
            text = io.StringIO()
            writer = csv.writer(text, lineterminator="\n")
            writer.writerow([_guard(c) for c in columns])
            writer.writerows(rows)
            _replace(path, text.getvalue())
        except (OSError, ValueError) as error:
            LOG.error(
                "the behavior file of task %s could not be written (%s)",
                task,
                getattr(error, "strerror", None) or type(error).__name__,
            )
            continue
        written.append(path)
    # The original names of the columns that were renamed, beside the files: written every
    # time (``{}`` when none was renamed), never removed.
    listing = out_dir / COLUMNS_FILE
    try:
        _replace(listing, json.dumps(names, indent=2, sort_keys=True) + "\n")
    except OSError as error:
        LOG.error("the column list could not be written (%s)", error.strerror or type(error).__name__)
    return written


def before_zip(folder: Path) -> None:
    """The ``package.BEFORE_ZIP`` hook: ``write(folder)`` just before the session is zipped.

    A failure is logged here in words (``the behavior files could not be written``, the kind of
    error, no path) and raised again, so the zip names it in ``hook_errors`` and is still made.
    """
    try:
        write(folder)
    except Exception as error:
        LOG.error(
            "the behavior files could not be written (%s)",
            getattr(error, "strerror", None) or type(error).__name__,
        )
        raise

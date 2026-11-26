"""Running the analysis of a session: saving the results as new files and an additional zip,
and a background job with a progress value for the page.

Nothing that exists is changed, replaced or deleted. The results go into a new folder
``analysis/`` inside the session folder (``analysis-2/`` ... when that name is taken by an
attempt that did not finish), each file created exclusively. Then an additional zip of that
folder, ``<session folder>-analysis.zip`` (``-analysis-2.zip`` ... when taken), is written beside
the session's own zip, read back and given its name the way the session zip is
(``package.place``); the session zip is never touched.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from brain_mapper import package

from . import pipeline

LOG = logging.getLogger("brain_mapper")
RESULTS = "results.json"
WINDOWS = "windows.json"


def _dirs(folder: Path):
    number = 1
    while True:
        yield folder / ("analysis" if number == 1 else f"analysis-{number}")
        number += 1


def finished_dir(folder: Path) -> Path | None:
    """The last analysis folder of the session that holds results, or ``None``."""
    found = None
    for candidate in _dirs(Path(folder)):
        if not candidate.exists():
            return found
        if (candidate / RESULTS).is_file():
            found = candidate


def _new_dir(folder: Path) -> Path:
    for candidate in _dirs(folder):
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise AssertionError("unreachable")


def _write_new(path: Path, data: dict) -> None:
    with open(path, "x", encoding="utf-8") as handle:  # exclusive: never replaces a file
        json.dump(data, handle, separators=(",", ":"), allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())


def save(folder: Path, results: dict, windows: dict, zip_it: bool = True) -> tuple[Path, Path | None]:
    """Writes the results into a new analysis folder of ``folder`` (results last, so a folder with
    ``results.json`` is whole) and, with ``zip_it``, the additional zip beside the session folder.
    Returns the analysis folder and the zip (or ``None``)."""
    folder = Path(folder)
    with package.locked(folder):
        out = _new_dir(folder)
        _write_new(out / WINDOWS, windows)
        _write_new(out / RESULTS, {**results, "folder": out.name})
        made = None
        if zip_it:
            expected = {f"{out.name}/{WINDOWS}": out / WINDOWS, f"{out.name}/{RESULTS}": out / RESULTS}
            sessions = folder.parent
            base = f"{folder.name}-{out.name}"
            temp = sessions / f".{base}.zip.partial-{secrets.token_hex(4)}"
            package._write(open(temp, "xb"), expected)
            if not package.verify(temp, expected):
                raise package.PackageError("the results zip did not read back the same as the results")
            made = package.place(temp, sessions, base)
    return out, made


def analyze(folder: Path, out_folder: Path | None = None, progress=None, zip_it: bool = True) -> dict:
    """Computes and saves; with ``out_folder`` the files go there instead (no zip). Returns the results."""
    results, windows = pipeline.compute(Path(folder), progress)
    if out_folder is not None:
        out = Path(out_folder)
        out.mkdir(parents=True, exist_ok=True)
        _write_new(out / WINDOWS, windows)
        _write_new(out / RESULTS, results)
        return results
    save(folder, results, windows, zip_it=zip_it)
    return results


class Job:
    """The analysis of one session on a thread of its own (low priority, started after the zip).

    ``status()`` is ``{"state": "running"|"done"|"failed", "progress", "stage", "elapsed",
    "message"}``. A failure is caught, logged (paths, folder name and label replaced) and shown
    as "The results could not be computed."; the session folder and zip are not touched.
    """

    def __init__(self, folder: Path, label: str | None = None) -> None:
        self.folder = Path(folder)
        self._label = label
        self._lock = threading.Lock()
        self._state = "running"
        self._progress = 0.0
        self._stage = "Starting"
        self._message = None
        self._started = time.monotonic()
        self._ended: float | None = None
        self.thread = threading.Thread(target=self._run, name="analysis", daemon=True)

    def start(self) -> Job:
        self.thread.start()
        return self

    def _report(self, fraction: float, stage: str) -> None:
        with self._lock:
            self._progress = max(self._progress, min(1.0, float(fraction)))
            self._stage = stage

    def _run(self) -> None:
        try:
            if finished_dir(self.folder) is None:
                analyze(self.folder, progress=self._report)
            with self._lock:
                self._state, self._progress, self._stage = "done", 1.0, "Done"
        except Exception as error:  # every failure ends here: the session itself is untouched
            text = package.scrub(f"{type(error).__name__}: {error}", self.folder, self._label)
            LOG.error("the results of the session could not be computed: %s", text)
            with self._lock:
                self._state = "failed"
                self._message = (
                    f"The results could not be computed ({error})."
                    if isinstance(error, pipeline.NotComputable)
                    else "The results could not be computed."
                )
        finally:
            self._ended = time.monotonic()

    def status(self) -> dict:
        with self._lock:
            end = self._ended if self._ended is not None else time.monotonic()
            return {
                "state": self._state,
                "progress": round(self._progress, 3),
                "stage": self._stage,
                "elapsed": round(end - self._started, 1),
                "message": self._message,
            }

"""The scores of a session: for each task, the points earned of the points possible.

Computed here and nowhere else, from the ``<task>.trial`` events of ``events.jsonl`` alone, so the
page (through ``Session.state()["scores"]``), ``session.json``, ``behavior/scores.json`` in the zip
and a later re-scoring of the folder (``score_folder``) all give the same numbers. The rule of each
task (``RULES``) follows the task's own scoring function in ``web/js/tasks/<id>-logic.js``:

* ``scored_trials_correct`` (perception, flanker, spatial): a trial counts when the runner marked it
  ``scored`` (``status`` done, not practice: cut-short, abandoned, failed and practice trials do not
  count); it earns a point when ``correct`` is true. A trial with no response counts and earns
  nothing.
* ``nback_answerable_correct`` (2-back): as above, and only the letters the task's ``score`` uses:
  ``needs_response`` true, not ``tail``, not ``after_resume``, and a ``window_ms`` (when given) of
  at least the full 2400 ms. A letter earns a point when ``correct`` is true (``same`` on a target,
  ``different`` on any other letter); no answer earns nothing.
* ``words_list_recalled`` (words, immediate recall) and ``recall_both_lists_recalled`` (delayed
  recall): each ``scored`` trial is one recall; possible is the number of distinct studied words
  (``words``: the list just studied; ``lists``: both lists), earned the number of distinct studied
  words among ``typed`` (the entries, each trimmed and lower-cased as ``normalize`` in
  ``words-logic.js`` does; a repeat counts once, any other entry earns and costs nothing).

* ``video_10s_watched`` (the video round): one point for each full 10 s of the part of the video
  played that was watched, from the player's reports (``video.play``, ``video.progress``,
  ``video.uncover``, ``video.pause``, ``video.ended``, ``video.range_end``, each with the video
  position ``seconds``). Two reports in a row while playing (the first a ``play``, ``progress`` or
  ``uncover``), the second at most ``VIDEO_STEP_S`` later in the video and not before the first, mark
  the stretch between them as watched; a pause breaks the chain, and a seek (``video.seek``) while
  playing goes on from its ``to``. The part played (``start_s`` to
  ``end_s`` of the last ``video.ready``) is cut into 10 s bins from ``start_s``; a bin earns its point
  when the watched stretches cover all of it but at most ``VIDEO_SLACK_S`` (the spacing of the
  player's reports at the start). Stretches watched twice count once, so pausing, seeking back or
  watching again earns nothing twice. Possible: the number of full 10 s bins in the part played.

``Scorer`` keeps the counts as events come; ``score_events`` and ``score_folder`` feed it a whole log.
The result is ``{"tasks": {<task>: {"earned", "possible", "trials", "rule"}}, "total": {"earned",
"possible"}}``, with a task present once one of its trial events is in the log (``trials``: the trials,
or recalls, counted). Tasks without a rule here (``example``, ``timing``) are not scored.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from brain_mapper import behavior

LOG = logging.getLogger("brain_mapper.scores")

#: Task (the event name's prefix, the step's module) to the id of its rule.
RULES = {
    "perception": "scored_trials_correct",
    "flanker": "scored_trials_correct",
    "spatial": "scored_trials_correct",
    "nback": "nback_answerable_correct",
    "words": "words_list_recalled",
    "recall": "recall_both_lists_recalled",
}
#: The full answer window of a 2-back letter (``TIMING.responseWindowMs`` in nback-logic.js).
NBACK_WINDOW_MS = 2400
#: What JavaScript's ``String.prototype.trim`` removes: WhiteSpace (with every Zs) and LineTerminator.
JS_SPACE = (
    "\t\n\v\f\r              "
    "    　﻿"
)
FILE = "scores.json"
#: The video round: its task key (the module of its step), rule, bin length and the limits above.
VIDEO_TASK = "video"
VIDEO_RULE = "video_10s_watched"
VIDEO_BIN_S = 10.0
VIDEO_STEP_S = 2.5
VIDEO_SLACK_S = 0.5
#: Video events that report the position, and those after which the video is playing.
VIDEO_REPORTS = ("video.play", "video.progress", "video.uncover", "video.pause", "video.ended", "video.range_end")
VIDEO_PLAYING = ("video.play", "video.progress", "video.uncover")


class VideoWatch:
    """The watched stretches of the video round, from the player's reports."""

    def __init__(self) -> None:
        self.start: float | None = None
        self.end: float | None = None
        self.stretches: list[tuple[float, float]] = []
        self._last: float | None = None  # the position of the last report while playing

    def add(self, name: str, payload: dict) -> None:
        if name == "video.ready":
            start, end = payload.get("start_s"), payload.get("end_s")
            if _number(start) and _number(end) and start < end:
                self.start, self.end = float(start), float(end)
            self._last = None
            return
        if name not in VIDEO_REPORTS:
            if name == "video.seek":
                # Playing on from where the seek went (the page's seek to the start of the part
                # played); while not playing, nothing follows from it.
                to = payload.get("to")
                self._last = float(to) if self._last is not None and _number(to) else None
            return
        at = payload.get("seconds")
        if not _number(at):
            self._last = None
            return
        at = float(at)
        if self._last is not None and 0 < at - self._last <= VIDEO_STEP_S:
            self.stretches.append((self._last, at))
        self._last = at if name in VIDEO_PLAYING else None

    def possible(self) -> int:
        if self.start is None or self.end is None:
            return 0
        return int((self.end - self.start) // VIDEO_BIN_S)

    def earned(self) -> int:
        n = self.possible()
        if not n:
            return 0
        merged: list[list[float]] = []
        for a, b in sorted(self.stretches):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        earned = 0
        for k in range(n):
            lo = self.start + k * VIDEO_BIN_S
            hi = lo + VIDEO_BIN_S
            covered = sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in merged)
            if covered >= VIDEO_BIN_S - VIDEO_SLACK_S:
                earned += 1
        return earned


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def normalize(text: str) -> str:
    """An entry or a studied word as it is compared: ``normalize`` of words-logic.js."""
    return str(text).strip(JS_SPACE).lower()


def _counts(rule: str, payload: dict) -> tuple[int, int] | None:
    """``(earned, possible)`` of one trial, or ``None`` when it does not count."""
    if payload.get("scored") is not True:
        return None
    if rule == "scored_trials_correct":
        return (1 if payload.get("correct") is True else 0, 1)
    if rule == "nback_answerable_correct":
        window = payload.get("window_ms", NBACK_WINDOW_MS)
        if (
            payload.get("needs_response") is not True
            or payload.get("tail") is True
            or payload.get("after_resume") is True
            or isinstance(window, bool)
            or not isinstance(window, (int, float))
            or window < NBACK_WINDOW_MS
        ):
            return None
        return (1 if payload.get("correct") is True else 0, 1)
    if rule == "words_list_recalled":
        lists = [payload.get("words")]
    else:
        lists = payload.get("lists")
        if not isinstance(lists, list):
            return None
    typed = payload.get("typed")
    if not isinstance(typed, list) or not all(isinstance(t, str) for t in typed):
        return None
    studied: set[str] = set()
    for words in lists:
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            return None
        studied.update(normalize(w) for w in words)
    studied.discard("")
    if not studied:
        return None
    return (len(studied & {normalize(t) for t in typed}), len(studied))


class Scorer:
    """The scores of a log, kept up to date one event at a time."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._video: VideoWatch | None = None

    def add(self, event: dict) -> bool:
        """Counts ``event`` if it is a trial event of a task with a rule, or a report of the video
        player; returns whether it was one."""
        name = event.get("name") if isinstance(event, dict) else None
        if isinstance(name, str) and name.startswith("video."):
            payload = event.get("payload")
            if name == "video.ready" and self._video is None:
                self._video = VideoWatch()
            if self._video is None or not isinstance(payload, dict):
                return False
            self._video.add(name, payload)
            return True
        if not isinstance(name, str) or not name.endswith(".trial"):
            return False
        task = name[: -len(".trial")]
        rule = RULES.get(task)
        payload = event.get("payload")
        if rule is None or not isinstance(payload, dict):
            return False
        entry = self._tasks.setdefault(task, {"earned": 0, "possible": 0, "trials": 0, "rule": rule})
        counts = _counts(rule, payload)
        if counts is not None:
            entry["earned"] += counts[0]
            entry["possible"] += counts[1]
            entry["trials"] += 1
        return True

    def result(self) -> dict:
        found = dict(self._tasks)
        if self._video is not None:
            earned = self._video.earned()
            found[VIDEO_TASK] = {
                "earned": earned,
                "possible": self._video.possible(),
                "trials": earned,
                "rule": VIDEO_RULE,
            }
        tasks = {task: dict(entry) for task, entry in sorted(found.items())}
        return {
            "tasks": tasks,
            "total": {
                "earned": sum(t["earned"] for t in tasks.values()),
                "possible": sum(t["possible"] for t in tasks.values()),
            },
        }


def score_events(events) -> dict:
    """The scores of a whole log (event dicts as ``events.jsonl`` holds them)."""
    scorer = Scorer()
    for event in events:
        scorer.add(event)
    return scorer.result()


def read_events(path: Path) -> list[dict]:
    """The events of ``events.jsonl`` that can be read (a line cut off by a crash is left out)."""
    found: list[dict] = []
    if not Path(path).is_file():
        return found
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                found.append(item)
    return found


def score_folder(folder: Path) -> dict:
    """The scores of the session ``folder``, from its ``events.jsonl`` alone."""
    return score_events(read_events(Path(folder) / "events.jsonl"))


def write(folder: Path) -> Path:
    """Writes ``behavior/scores.json`` (replace-rename, as the behaviour files) and returns its path."""
    out_dir = Path(folder) / behavior.FOLDER
    out_dir.mkdir(exist_ok=True)
    path = out_dir / FILE
    behavior._replace(path, json.dumps(score_folder(folder), indent=2, sort_keys=True) + "\n")
    return path


def write_before_zip(folder: Path) -> None:
    """The ``package.BEFORE_ZIP`` hook: ``write(folder)``. A failure is logged in words (no path)
    and raised again, so the zip names it in ``hook_errors`` and is still made."""
    try:
        write(folder)
    except Exception as error:
        LOG.error(
            "the scores file could not be written (%s)",
            getattr(error, "strerror", None) or type(error).__name__,
        )
        raise

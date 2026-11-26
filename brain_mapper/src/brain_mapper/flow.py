"""The steps and blocks of a session (the table of spec 6.3 and 6.4).

A step is one screen of the session; a task, video, rest or recall step is
made of timed blocks. The task blocks have the lengths of spec 6.4, the video block
the part of the video played and a margin, the rest blocks the 3 minutes of spec 6.3.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Kind = Literal["setup", "task", "video", "rest", "recall", "summary"]
KINDS = ("setup", "task", "video", "rest", "recall", "summary")
#: Kinds that carry no blocks and so can earn no points.
NO_POINTS = ("setup", "summary")
#: Length of a placeholder block, in seconds.
PLACEHOLDER_SECONDS = 10.0


@dataclass(frozen=True)
class Block:
    id: str
    seconds: float
    points: int


#: A module name: what may follow ``export * as`` in ``web/js/tasks/index.js``.
MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
#: ``Step.module`` when it is not given: the step id is the module name.
SAME_AS_ID = "<id>"


@dataclass(frozen=True)
class Calibration:
    """The timed parts of a calibration step (spec 6.3): eyes open, eyes closed, then
    ``blink_cues`` blink cues ``blink_gap`` seconds apart, the first ``blink_lead`` seconds
    after the blinks part begins (the tone that ends eyes closed comes first)."""

    eyes_open: float = 20.0
    eyes_closed: float = 20.0
    blink_cues: int = 5
    blink_gap: float = 2.0
    blink_lead: float = 2.0

    @property
    def blinks(self) -> float:
        """Seconds of the blinks part: the lead, the cues, and one gap after the last."""
        return self.blink_lead + self.blink_cues * self.blink_gap

    def as_dict(self) -> dict:
        return {
            "eyes_open": self.eyes_open,
            "eyes_closed": self.eyes_closed,
            "blink_cues": self.blink_cues,
            "blink_gap": self.blink_gap,
            "blink_lead": self.blink_lead,
            "blinks": self.blinks,
        }


@dataclass(frozen=True)
class Video:
    """The video of a video step: its Vimeo id and the part of it that is played, from
    ``start_s`` to ``end_s`` (seconds of the video)."""

    id: int
    start_s: float
    end_s: float

    def as_dict(self) -> dict:
        return {"id": self.id, "start_s": self.start_s, "end_s": self.end_s}


@dataclass(frozen=True)
class Step:
    """``module`` names the page module (``web/js/tasks/index.js``) that draws a step whose kind
    has blocks (rest steps keep their own screens). Left out, it is the step id; ``None`` keeps
    the timed placeholder for the step even when a module of that name is registered."""

    id: str
    kind: Kind
    title: str
    blocks: list[Block] = field(default_factory=list)
    #: Set on the calibration step: it then has the sub-steps contact, eyes open, eyes closed
    #: and blinks (see ``engine``). A setup step without it is a single screen.
    calibration: Calibration | None = None
    module: str | None = SAME_AS_ID
    #: The video and the part of it played; a video step without one plays ``VIDEO``.
    video: Video | None = None
    #: Whether the page may end a block of this step early because the participant says they are
    #: done (``block_finished`` with the reason ``participant_done``; see ``engine``).
    may_finish_early: bool = False

    def __post_init__(self) -> None:
        if self.module == SAME_AS_ID:
            object.__setattr__(self, "module", self.id)
        if self.kind == "video" and self.video is None:
            object.__setattr__(self, "video", VIDEO)


#: Seconds of each task block (spec 6.4 for the lengths of perception and of one 2-back, flanker
#: and spatial block). 2-back, flanker and spatial memory have 2 blocks each,
#: so they last 100 s, 100 s and 120 s in all. A words block holds the longest study
#: phase (52.6 s: 2 s lead-in, 12 words of 3 s, 11 blanks of at most 1.2 s, at most 1.4 s to the
#: recall cue) and 90 s of recall, plus 2.4 s.
TASK_SECONDS = {"words": 145.0, "perception": 60.0, "nback": 50.0, "flanker": 50.0, "spatial": 60.0}
#: Blocks of each task step in the default flow.
TASK_BLOCKS = {"perception": 2, "nback": 2, "flanker": 2, "spatial": 2}
#: The rest periods of spec 6.3: 3 minutes each, eyes closed and eyes open.
REST_SECONDS = 180.0
#: The delayed free recall: a 2 s lead-in, then 120 s of recall (spec 6.3: 2 minutes), plus 3 s.
RECALL_SECONDS = 125.0
#: The video of the video round (spec 6.3: "3 min", Vimeo 19009646, 180 s as its player reports
#: it), played without its opening and its end: from 11.5 s, after the countdown and the title
#: "LXIV" (cut to the first picture at 11.2 s), to 173.5 s, the last picture before the cut to
#: black (173.7 s) that the closing credit ("Created by", from 174.5 s) and the dedication ("In
#: memory of", from 176.5 s) follow. Both times were read from the player's frames on 2026-11-25
#: (seek in 0.5 s and 0.1 s steps; contact sheets in docs/brain-mapper.md, "Video").
VIDEO = Video(19009646, 11.5, 173.5)
#: Room for the player to start once Start is pressed: the click-to-play time measured headed
#: on 2026-11-25 was 0.3 to 0.5 s; the margin is generous so that the end of the part played, not
#: the block's time, ends the round.
VIDEO_MARGIN_SECONDS = 3.0
#: The video block: the part played, in whole seconds, and the margin (162 + 3 = 165 s).
VIDEO_SECONDS = float(math.ceil(VIDEO.end_s - VIDEO.start_s) + VIDEO_MARGIN_SECONDS)


def _blocks(count: int, seconds: float = PLACEHOLDER_SECONDS) -> list[Block]:
    return [Block(f"block{i + 1}", seconds, 100) for i in range(count)]


def default_flow() -> list[Step]:
    """The order of spec 6.3, the task blocks of spec 6.4 (2 blocks each), the part of the video
    played, and the 3-minute rest periods of spec 6.3."""
    t = TASK_SECONDS
    n = TASK_BLOCKS
    words = t["words"]
    return [
        Step("participant", "setup", "Participant"),
        Step("device", "setup", "Device setup"),
        Step("calibration", "setup", "Calibration", calibration=Calibration()),
        Step(
            "words", "task", "Words", [Block("list1", words, 100), Block("list2", words, 100)],
            may_finish_early=True,
        ),
        Step("perception", "task", "Perception", _blocks(n["perception"], t["perception"])),
        Step("nback", "task", "2-back", _blocks(n["nback"], t["nback"])),
        Step("flanker", "task", "Flanker", _blocks(n["flanker"], t["flanker"])),
        Step("spatial", "task", "Spatial memory", _blocks(n["spatial"], t["spatial"])),
        Step("video", "video", "Video", [Block("video", VIDEO_SECONDS, 100)], may_finish_early=True),
        Step("rest_closed", "rest", "Rest, eyes closed", [Block("rest", REST_SECONDS, 100)]),
        Step("rest_open", "rest", "Rest, eyes open", [Block("rest", REST_SECONDS, 100)]),
        Step(
            "recall", "recall", "Delayed free recall", [Block("recall", RECALL_SECONDS, 100)],
            may_finish_early=True,
        ),
        Step("summary", "summary", "Summary"),
    ]


def validate(steps: list[Step]) -> list[Step]:
    """Returns ``steps`` or raises ``ValueError`` saying what is wrong."""
    if not steps:
        raise ValueError("a flow needs at least one step")
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValueError(f"duplicate step id {step.id!r}")
        seen.add(step.id)
        if step.kind not in KINDS:
            raise ValueError(f"step {step.id!r}: unknown kind {step.kind!r}")
        ids: set[str] = set()
        for block in step.blocks:
            if block.id in ids:
                raise ValueError(f"step {step.id!r}: duplicate block id {block.id!r}")
            ids.add(block.id)
            if not math.isfinite(block.seconds) or not block.seconds > 0:
                raise ValueError(
                    f"step {step.id!r}, block {block.id!r}: seconds must be a finite number > 0"
                )
            if block.points < 0:
                raise ValueError(f"step {step.id!r}, block {block.id!r}: negative points")
            if step.kind in NO_POINTS and block.points:
                raise ValueError(f"step {step.id!r} is a {step.kind} step and cannot have points")
        if step.kind in NO_POINTS and step.blocks:
            raise ValueError(f"step {step.id!r} is a {step.kind} step and cannot have blocks")
        if step.kind not in NO_POINTS and not step.blocks:
            raise ValueError(f"step {step.id!r} is a {step.kind} step and needs blocks")
        if step.calibration is not None:
            if step.kind != "setup":
                raise ValueError(f"step {step.id!r}: only a setup step can be a calibration")
            for name in ("eyes_open", "eyes_closed"):
                value = getattr(step.calibration, name)
                if not math.isfinite(value) or not value > 0:
                    raise ValueError(f"step {step.id!r}: calibration {name} must be a finite number > 0")
        if step.video is not None:
            if step.kind != "video":
                raise ValueError(f"step {step.id!r}: only a video step can have a video")
            v = step.video
            if not (math.isfinite(v.start_s) and math.isfinite(v.end_s) and 0 <= v.start_s < v.end_s):
                raise ValueError(f"step {step.id!r}: the video needs 0 <= start_s < end_s")
            if not 0 < v.id:
                raise ValueError(f"step {step.id!r}: the video id must be a whole number > 0")
    return steps


def rounds(steps: list[Step]) -> list[str]:
    """The ids of the rounds: the steps that have blocks (not setup, not summary)."""
    return [s.id for s in steps if s.kind not in NO_POINTS]


class EmptyChoice(ValueError):
    """``only`` is a list with no round in it."""


class RepeatedChoice(ValueError):
    """``only`` names a round more than once."""


def chosen(steps: list[Step], only) -> list[str] | None:
    """The rounds ``only`` names, in route order, or ``None`` when it is ``None``.

    ``only`` is a round id or a list (or tuple) of round ids. Raises ``EmptyChoice`` for an
    empty list, ``RepeatedChoice`` for a round named twice and ``ValueError`` for anything that
    is not a round of ``steps`` (naming the rounds).
    """
    if only is None:
        return None
    names = rounds(steps)
    wanted = [only] if isinstance(only, str) else only
    if not isinstance(wanted, (list, tuple)) or not all(isinstance(x, str) and x in names for x in wanted):
        raise ValueError(f"only: {only!r} is not a round of this route; the rounds are {', '.join(names)}")
    if not wanted:
        raise EmptyChoice("only: choose at least one round, or leave it out to play the whole route")
    if len(set(wanted)) != len(wanted):
        raise RepeatedChoice("only: a round is named more than once")
    return [name for name in names if name in wanted]


def select(steps: list[Step], only=None, start_at: str | None = None) -> list[Step]:
    """The steps of a session that plays only part of the route.

    The setup steps and the summary are always kept. ``only`` (a round id, or a list of them)
    keeps those rounds, in route order; ``start_at`` keeps that round and every round after it.
    Neither: all of ``steps``. Raises ``ValueError`` for both at once, for an id that is not a
    round (naming the ids that are), and for an empty or repeating list (see ``chosen``).
    """
    if only is not None and start_at is not None:
        raise ValueError("choose rounds or a round to start at, not both")
    names = rounds(steps)
    keep = chosen(steps, only)
    if start_at is not None and (not isinstance(start_at, str) or start_at not in names):
        raise ValueError(
            f"start_at: {start_at!r} is not a round of this route; the rounds are {', '.join(names)}"
        )
    if only is None and start_at is None:
        return list(steps)
    kept: list[Step] = []
    reached = False
    for step in steps:
        if step.kind in NO_POINTS:
            kept.append(step)
        elif keep is not None:
            if step.id in keep:
                kept.append(step)
        else:
            reached = reached or step.id == start_at
            if reached:
                kept.append(step)
    return kept


def _number(block: dict, key: str) -> float:
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, not {value!r}")
    return float(value)


def _whole(block: dict, key: str) -> int:
    value = block[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number, not {value!r}")
    return value


def load(path: Path) -> list[Step]:
    """Reads a flow file: a JSON list of steps with the fields of ``Step``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: a flow file is a JSON list of steps")
    steps = []
    for item in raw:
        try:
            blocks = [
                Block(str(b["id"]), _number(b, "seconds"), _whole(b, "points"))
                for b in item.get("blocks", [])
            ]
            calibration = None
            if item.get("calibration") is not None:
                timing = item["calibration"]
                if not isinstance(timing, dict) or set(timing) - {"eyes_open", "eyes_closed"}:
                    raise ValueError('calibration is {"eyes_open": seconds, "eyes_closed": seconds}')
                calibration = Calibration(**{k: _number(timing, k) for k in timing})
            module = item.get("module", SAME_AS_ID)
            if module != SAME_AS_ID and module is not None:
                if not isinstance(module, str) or not MODULE_NAME.fullmatch(module):
                    raise ValueError(
                        f"step {item.get('id')!r}: module must be null or 1 to 64 of "
                        f"A-Z a-z 0-9 _ not starting with a digit, not {module!r}"
                    )
            video = None
            if item.get("video") is not None:
                given = item["video"]
                if not isinstance(given, dict) or set(given) != {"id", "start_s", "end_s"}:
                    raise ValueError('video is {"id": vimeo id, "start_s": seconds, "end_s": seconds}')
                video = Video(_whole(given, "id"), _number(given, "start_s"), _number(given, "end_s"))
            early = item.get("may_finish_early", False)
            if not isinstance(early, bool):
                raise ValueError(f"may_finish_early must be true or false, not {early!r}")
            steps.append(
                Step(
                    str(item["id"]),
                    item["kind"],
                    str(item["title"]),
                    blocks,
                    calibration=calibration,
                    module=module,
                    video=video,
                    may_finish_early=early,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"{path}: bad step {item!r}: {error!r}") from error
    return validate(steps)

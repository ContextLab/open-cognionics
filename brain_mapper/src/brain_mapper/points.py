"""The points ledger: one line of ``points.jsonl`` per award.

A line holds ``seq`` (counting from 1), ``step``, ``block``, ``reason``,
``amount``, ``total`` (the total after the award), ``unix_time`` and ``sample``
(the number of samples the source had read, or ``null`` when no source is
attached). The ledger is append-only; reopening the file restores the total.

Two rules are kept here, whoever calls ``award``:

* a step of kind ``setup`` or ``summary`` can never be awarded points;
* nothing is awarded while the session engine's phase is ``running`` or
  ``paused``, so the total cannot change in the middle of a block.

The ledger of a session that continues an earlier one starts with one ``carried`` line
(``seq`` 1, ``amount`` 0, ``total`` the earlier session's final total, ``carried_from`` the
earlier session's folder name). It is the only line whose total is not the one before plus
its amount, and it is accepted only as the first line.

Nothing is ever awarded for a block that was skipped or stopped: no line is written.

The engine attaches ``phase`` and ``sample`` (callables) when it is created.
No amount is ever computed from an EEG measure: the caller names it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from brain_mapper.flow import NO_POINTS, Block, Step

#: Engine phases in which no award is possible.
BLOCKED_PHASES = ("running", "paused")


#: The reason of the first line of a continued session's ledger.
CARRIED = "carried"


class PointsError(ValueError):
    """The award is not allowed, or the ledger file cannot be read."""


def read_awards(path: Path) -> list[dict]:
    """The lines of the ledger at ``path``, checked; ``PointsError`` names the first line that fails.

    Every line's ``seq`` must count from 1 and its ``total`` must be the total before it plus
    its ``amount``; the one exception is a first line whose ``reason`` is ``carried``, which
    must have ``amount`` 0 and a whole ``total`` of at least 0. Nothing is written.
    """
    awards: list[dict] = []
    for number, text in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        try:
            award = json.loads(text)
            if award["seq"] != number:
                raise ValueError("seq does not count from 1")
            total, amount = award["total"], award["amount"]
            if award["reason"] == CARRIED:
                if number != 1:
                    raise ValueError("a carried line can only be the first line")
                if amount != 0 or isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise ValueError("a carried line has amount 0 and a whole total of at least 0")
            elif total != (awards[-1]["total"] if awards else 0) + amount:
                raise ValueError("seq or total does not follow from the lines before")
        except (ValueError, KeyError, TypeError) as error:
            raise PointsError(f"{path} line {number}: {error!r}") from error
        awards.append(award)
    return awards


class Ledger:
    """Appends awards to ``path`` and keeps the running total."""

    def __init__(self, path: Path, carried: tuple[int, str] | None = None) -> None:
        """Opens (and creates, with its folder) the ledger at ``path``.

        ``carried`` is ``(total, session)`` for a session that continues ``session``: a new
        ledger then starts with the ``carried`` line. For a ledger that exists already the
        first line must be that very line (``PointsError`` otherwise).
        """
        self.path = Path(path)
        self.phase: Callable[[], str] | None = None
        self.sample: Callable[[], int | None] | None = None
        self._awards: list[dict] = []
        self._unusable: str | None = None
        if self.path.exists():
            self._awards = read_awards(self.path)
            if carried is not None:
                first = self._awards[0] if self._awards else {}
                if (
                    first.get("reason") != CARRIED
                    or first.get("total") != carried[0]
                    or first.get("carried_from") != carried[1]
                ):
                    raise PointsError(f"{self.path} does not start with the line carried from {carried[1]}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8")
        if carried is not None and not self._awards:
            line = {
                "seq": 1,
                "step": None,
                "block": None,
                "reason": CARRIED,
                "amount": 0,
                "total": carried[0],
                "carried_from": carried[1],
                "unix_time": time.time(),
                "sample": None,
            }
            try:
                self._file.write(json.dumps(line, allow_nan=False) + "\n")
                self._file.flush()
            except BaseException:
                self._file.close()
                raise
            self._awards.append(line)

    @property
    def total(self) -> int:
        return self._awards[-1]["total"] if self._awards else 0

    @property
    def awards(self) -> list[dict]:
        """A copy of the awards so far, oldest first."""
        return [dict(a) for a in self._awards]

    def award(self, step: Step, block: Block, reason: str, amount: int) -> int:
        """Adds ``amount`` for ``block`` of ``step`` and returns the new total.

        If the line cannot be written and cannot be taken back out of the file
        either, the ledger is marked unusable: this and every later award raises
        ``PointsError``, and what is in memory is what is in the file.
        """
        if self._unusable is not None:
            raise PointsError(f"the ledger is unusable: {self._unusable}")
        if step.kind in NO_POINTS:
            raise PointsError(f"step {step.id!r} is a {step.kind} step: it earns no points")
        if self.phase is not None and self.phase() in BLOCKED_PHASES:
            raise PointsError(f"no points while the block is {self.phase()}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise PointsError(f"amount must be a whole number >= 0, not {amount!r}")
        if not isinstance(reason, str) or not reason:
            raise PointsError("an award needs a reason")
        award = {
            "seq": len(self._awards) + 1,
            "step": step.id,
            "block": block.id,
            "reason": reason,
            "amount": amount,
            "total": self.total + amount,
            "unix_time": time.time(),
            "sample": self.sample() if self.sample is not None else None,
        }
        start = None
        try:
            start = self._file.tell()
            self._file.write(json.dumps(award, allow_nan=False) + "\n")
            self._file.flush()
        except BaseException as error:
            try:
                if start is None:
                    raise ValueError("the end of the file is not known")
                self._file.seek(start)
                self._file.truncate(start)
            except (OSError, ValueError) as undo:
                self._unusable = f"{error!r}, and the line could not be taken back: {undo!r}"
                raise PointsError(f"the ledger is unusable: {self._unusable}") from error
            raise
        self._awards.append(award)
        return award["total"]

    def close(self) -> None:
        self._file.close()

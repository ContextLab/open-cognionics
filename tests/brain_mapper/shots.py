"""Where the browser tests save their screenshots.

The folder is ``$BRAIN_MAPPER_SHOTS_DIR/<name>`` when that environment variable is set, and
otherwise ``<name>`` under pytest's temporary directory for the run (``tmp_path_factory``; the
``_shots_base`` fixture in ``conftest.py`` records it). The folder is looked up when it is first
used, so a test module can name it at import time: ``SHOTS = ShotsDir("shots-night-engine")``.
"""

from __future__ import annotations

import os
from pathlib import Path

#: pytest's base temporary directory for this run, set by ``conftest._shots_base``.
BASE: list[Path] = []


def shots_root() -> Path:
    folder = os.environ.get("BRAIN_MAPPER_SHOTS_DIR")
    if folder:
        return Path(folder)
    if not BASE:
        raise RuntimeError("the screenshot folder is used before pytest's temporary directory is known")
    return BASE[0]


class ShotsDir:
    """A screenshot folder: ``mkdir`` and ``/`` act on ``shots_root() / name``."""

    def __init__(self, name: str) -> None:
        self.name = name

    def path(self) -> Path:
        return shots_root() / self.name

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        self.path().mkdir(parents=parents, exist_ok=exist_ok)

    def __truediv__(self, other: str) -> Path:
        return self.path() / other

    def __fspath__(self) -> str:
        return str(self.path())

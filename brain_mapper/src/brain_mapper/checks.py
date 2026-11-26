"""Start-up checks: what is needed for the demo to run, looked at without changing anything.

``run(settings)`` returns one ``Check`` per item, in a fixed order: the Python version, the
``open_cognionics`` package and its version, the page files, the three.js files, the data folder
and the dongle listing. Nothing is installed or opened: the data folder is only tried with a
small file that is removed again (a folder that does not exist yet is not made), and the dongle
listing is ``oc.find_headsets()``, which lists serial ports and opens none. No byte is written
to any device. ``brain-mapper --check`` prints the list; the server sends it to the start screen.

The page files and the three.js files are compared with ``web_files.json`` next to this module:
the sha256 of every file under ``web/``. The list is made by ``scripts/brain_mapper_web_hashes.py``,
which a developer runs after changing any file under ``web/`` (a test fails until it has been).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain_mapper.server import Settings

WEB = Path(__file__).resolve().parent / "web"
RECORDED = Path(__file__).resolve().parent / "web_files.json"
#: The files under this folder (of ``web/``) are the three.js files; all others are page files.
THREE = "vendor/three/"
MIN_PYTHON = (3, 10)
REGENERATE = "uv run python scripts/brain_mapper_web_hashes.py"


@dataclass(frozen=True)
class Check:
    """One start-up check: ``fix`` says what to do and is ``None`` when ``ok``."""

    id: str
    ok: bool
    text: str
    fix: str | None = None
    #: A warning: ``ok`` stays true (``--check`` still exits 0), the line starts with ``WARN``
    #: and has ``fix``. Only ``video_host`` gives one.
    warning: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "ok": self.ok, "text": self.text, "fix": self.fix}

    def line(self) -> str:
        """The line ``--check`` prints."""
        if self.warning:
            return f"WARN  {self.id}: {self.text} Fix: {self.fix}"
        if self.ok:
            return f"PASS  {self.id}: {self.text}"
        return f"FAIL  {self.id}: {self.text} Fix: {self.fix}"


def web_hashes(root: Path = WEB) -> dict[str, str]:
    """The sha256 of every file under ``root``, by path relative to it (with ``/``).

    Files whose name starts with a dot (such as ``.DS_Store``) are left out.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            out[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def recorded_hashes() -> dict[str, str]:
    """The list in ``web_files.json``."""
    return json.loads(RECORDED.read_text(encoding="utf-8"))["files"]


def _compare(names: list[str], recorded: dict[str, str], found: dict[str, str]) -> list[str]:
    """What differs, for the files in ``names``: ``"<file> (changed|missing|not in the list)"``."""
    problems = []
    for name in names:
        if name not in found:
            problems.append(f"{name} (missing)")
        elif name not in recorded:
            problems.append(f"{name} (not in the recorded list)")
        elif found[name] != recorded[name]:
            problems.append(f"{name} (changed)")
    return problems


def _files(id: str, what: str, wanted) -> Check:
    try:
        recorded = recorded_hashes()
    except (OSError, ValueError, KeyError) as error:
        return Check(
            id,
            False,
            f"the list of recorded {what} could not be read ({error}).",
            f"Reinstall brain-mapper, or make the list with: {REGENERATE}",
        )
    found = web_hashes()
    names = sorted(n for n in {*recorded, *found} if wanted(n))
    problems = _compare(names, recorded, found)
    if problems:
        return Check(
            id,
            False,
            f"{len(problems)} of {len(names)} {what} differ from the recorded list: "
            + ", ".join(problems)
            + ".",
            "Reinstall brain-mapper (uv sync) to restore them. If they were changed on "
            f"purpose, make a new list with: {REGENERATE}",
        )
    return Check(id, True, f"{len(names)} {what}, each with its recorded sha256.")


def _writable(folder: Path) -> Check:
    """Whether sessions can be saved in ``folder``; makes nothing that stays."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.is_dir():
        return Check(
            "data_folder",
            False,
            f"{folder} cannot be used: {probe} is not a folder.",
            "Choose another folder with --data-dir.",
        )
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".brain-mapper-check-"):
            pass
    except OSError as error:
        return Check(
            "data_folder",
            False,
            f"cannot write in {probe} ({error.strerror or error}).",
            "Make the folder writable, or choose another with --data-dir.",
        )
    where = "writable" if probe == folder else f"does not exist yet; {probe} is writable"
    return Check("data_folder", True, f"{folder} {where}.")


def _dongles(settings: Settings) -> Check:
    import open_cognionics as oc

    try:
        ports = oc.find_headsets()
    except Exception as error:  # listing ports is all this does
        return Check(
            "dongle",
            False,
            f"the serial ports could not be listed ({type(error).__name__}: {error}).",
            "Check that the operating system lets programs list serial ports.",
        )
    note = ""
    if settings.port is not None:
        note = f" The port given with --port ({settings.port}) is not opened by this check."
    if settings.replay is not None:
        note = " A replay needs no dongle."
    if not ports:
        return Check("dongle", True, "no headset dongle found." + note)
    names = ", ".join(p.path for p in ports)
    return Check("dongle", True, f"{len(ports)} headset dongle(s) found: {names}.{note}")


#: The host of the embedded video player, the one page content loaded from another host.
VIDEO_PLAYER = "https://player.vimeo.com/"


def video_host(url: str = VIDEO_PLAYER, timeout: float = 5.0) -> Check:
    """Whether the video player's host answers over HTTPS: any HTTP answer, whatever its status,
    means it can be reached. Only ``brain-mapper --check`` runs this (the server does not, so it
    starts without waiting on the network). Sends one request with no data of any session; a
    host that cannot be reached is a warning, never a failure, since only the video round needs it.
    """
    import urllib.error
    import urllib.request

    host = url.split("/")[2]
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "brain-mapper-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            status = answer.status
    except urllib.error.HTTPError as error:
        status = error.code  # an answer all the same: the host can be reached
    except (OSError, ValueError) as error:
        reason = getattr(error, "reason", None) or error
        return Check(
            "video_host",
            True,
            f"{host} could not be reached ({reason}). Only the video round needs it: it shows "
            "the video from there; every other part of the session works without the internet.",
            "Connect this computer to the internet before the video round, or skip that round.",
            warning=True,
        )
    return Check(
        "video_host",
        True,
        f"{host} answered (HTTP {status}); only the video round needs it.",
    )


def run(settings: Settings) -> list[Check]:
    """The checks, in order. Opens no port and writes nothing to any device."""
    items: list[Check] = []
    version = ".".join(str(n) for n in sys.version_info[:3])
    items.append(
        Check("python", True, f"Python {version}.")
        if sys.version_info >= MIN_PYTHON
        else Check(
            "python",
            False,
            f"Python {version} is too old.",
            f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
        )
    )
    try:
        import open_cognionics as oc

        items.append(Check("open_cognionics", True, f"open_cognionics {oc.__version__}."))
    except ImportError as error:
        items.append(
            Check(
                "open_cognionics",
                False,
                f"open_cognionics cannot be imported ({error}).",
                "Run uv sync in the repository to build and install it.",
            )
        )
    items.append(_files("page_files", "page files", lambda n: not n.startswith(THREE)))
    items.append(_files("three_js", "three.js files", lambda n: n.startswith(THREE)))
    items.append(_writable(Path(os.fspath(settings.data_dir))))
    if "open_cognionics" in sys.modules:
        items.append(_dongles(settings))
    else:
        items.append(Check("dongle", False, "the dongle listing needs open_cognionics.", "Fix the check above."))
    return items

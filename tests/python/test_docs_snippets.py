"""Runs the Python snippets of docs/quickstart.md against the pty device.

The snippets are read from the markdown file itself, so the documentation and
this test cannot drift. Every snippet runs in a fresh namespace, in a scratch
directory. The read and replay snippets read the ``session-*`` directory that
the record snippet writes, so that one directory carries over (and nothing
else). The record snippet is then run a second time in the same directory.

The one place a snippet is changed: ``oc.connect()`` (no port, which would
open the real dongle) becomes ``oc.connect(__pty_port__)``, the pseudo-terminal
of a fresh simulated headset. No test here opens a real serial port.
"""

import contextlib
import io
import re
from pathlib import Path

import pytest

from pty_device import PtyDevice, config_reply, onhead_stream

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
QUICKSTART = DOCS / "quickstart.md"
SERVE_RS = ROOT / "src" / "bin" / "serve.rs"

CONNECT = "oc.connect()"
CONNECT_WITH_PTY = "oc.connect(__pty_port__)"
KNOWN_LANGUAGES = {"python", "bash", "text", ""}


def doc_pages() -> list[Path]:
    """Every markdown page under docs/ except the superpowers plans."""
    return sorted(
        p for p in DOCS.rglob("*.md") if "superpowers" not in p.relative_to(DOCS).parts
    )


def fences(markdown: str, name: str) -> list[tuple[str, str]]:
    """(language, body) of every fenced block; fails on a fence not understood."""
    found = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("~~~"):
            pytest.fail(f"{name}:{i + 1}: ~~~ fence")
        if line.lstrip().startswith("```"):
            m = re.fullmatch(r"```(\w*)", line)
            if not m:
                pytest.fail(f"{name}:{i + 1}: indented or unrecognised fence {line!r}")
            language = m.group(1)
            if language not in KNOWN_LANGUAGES:
                pytest.fail(f"{name}:{i + 1}: unknown fence language {language!r}")
            body = []
            i += 1
            while i < len(lines) and lines[i] != "```":
                if lines[i].lstrip().startswith("```"):
                    pytest.fail(f"{name}:{i + 1}: indented closing fence")
                body.append(lines[i])
                i += 1
            if i == len(lines):
                pytest.fail(f"{name}: unclosed fence")
            found.append((language, "\n".join(body) + "\n"))
        i += 1
    return found


def blocks(path: Path, language: str) -> list[str]:
    return [b for lang, b in fences(path.read_text(), path.name) if lang == language]


def with_pty_port(code: str) -> str:
    """The single substitution: connect to the simulator, not the dongle."""
    return code.replace(CONNECT, CONNECT_WITH_PTY)


def test_substitution_changes_only_the_connect_calls():
    code = "with oc.connect() as h:\n    oc.connect('/dev/x')\n    oc.connect()\n"
    changed = with_pty_port(code)
    assert code.count(CONNECT) == 2
    assert changed.count(CONNECT_WITH_PTY) == 2
    assert changed.count(CONNECT) == 0
    assert changed.replace(CONNECT_WITH_PTY, CONNECT) == code


@pytest.mark.parametrize("path", doc_pages(), ids=lambda p: p.name)
def test_every_fence_is_understood(path):
    fences(path.read_text(), path.name)


@pytest.mark.parametrize(
    "path", [p for p in doc_pages() if p != QUICKSTART], ids=lambda p: p.name
)
def test_no_python_blocks_outside_the_quickstart(path):
    # Python blocks elsewhere would not be run by this file.
    assert not blocks(path, "python")


def run_snippet(block: str) -> tuple[str, bytes]:
    """Run one snippet against a fresh simulated headset: (stdout, bytes written)."""
    device = PtyDevice(onhead_stream(), config_reply())
    try:
        code = with_pty_port(block)
        assert code.count(CONNECT_WITH_PTY) == block.count(CONNECT)
        assert code.replace(CONNECT_WITH_PTY, CONNECT) == block
        namespace = {"__pty_port__": device.path}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(code, "quickstart.md", "exec"), namespace)
        return out.getvalue(), device.written()
    finally:
        device.hang_up()


def test_quickstart_snippets_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snippets = blocks(QUICKSTART, "python")
    assert len(snippets) == 6
    outputs = []
    written = []
    for block in snippets:
        out, wrote = run_snippet(block)
        outputs.append(out)
        written.append((CONNECT in block, wrote))

    find, stream, record, read, replay, contact = outputs
    import open_cognionics as oc

    assert find.splitlines()[0] == oc.__version__
    first = stream.splitlines()
    assert first[0].startswith("['F7', 'Fp1', 'Fp2'")
    assert first[1].startswith("500.0 3 ")
    assert int(first[-1].split()[0]) >= 500
    directory = record.strip()
    assert directory.startswith("session-")
    lines = read.splitlines()
    assert len(lines) == 4
    assert lines[1] == "P001 True"
    assert lines[2].startswith("[('eyes closed',")
    # The marker's sample and the sample at its time, from the saved clock:
    # the reads of the simulated headset come milliseconds apart.
    sample, at = (int(v) for v in lines[3].split())
    assert sample >= 500
    assert abs(at - sample) < 50
    rows = int(lines[0].split(",")[0].strip("("))
    assert rows >= 900
    assert int(replay.split()[1]) == rows

    # Bytes the library wrote to the simulated headset, snippet by snippet.
    assert [w for used, w in written if used] == [
        bytes([0x14]),
        bytes([0x14]),
        bytes([0x14, 0x11, 0x12]),
    ]
    assert [w for used, w in written if not used] == [b"", b"", b""]

    expected = ["clock.jsonl", "markers.jsonl", "meta.json", "raw.bin", "recording.bdf"]
    assert sorted(p.name for p in (tmp_path / directory).iterdir()) == expected
    assert len(outputs) == len(written) == 6

    # Run a second time, the record block writes a second directory.
    again, wrote = run_snippet(snippets[2])
    assert wrote == bytes([0x14])
    assert again.strip() != directory
    assert sorted(p.name for p in tmp_path.glob("session-*")) == sorted(
        [directory, again.strip()]
    )


def test_server_command_is_the_one_in_the_server_source():
    bash = "\n".join(blocks(QUICKSTART, "bash"))
    command = "cargo run --features server -- [serial-port] [http-port]"
    assert command in bash
    assert command in SERVE_RS.read_text()
    assert "uv sync" in bash

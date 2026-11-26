"""No code of brain_mapper deletes anything under a session folder; the only deletions are the temporary files listed here.

Every call that can delete a file or folder (``rmtree``, ``unlink``, ``os.remove``, ``rmdir``,
``removedirs``) in the package source must be on ``ALLOWED`` with a reason. A new one fails this
test until someone reads the reason and decides it is a file of this code's own making that is
not recorded data.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper"
NAMES = {"rmtree", "unlink", "rmdir", "removedirs"}

#: (file, function) -> why that call is allowed.
ALLOWED = {
    ("participants.py", "_write"): "the temporary file this code just wrote beside participants.json, when the replace failed",
    ("progress.py", "write_json"): "the temporary file this code just wrote beside progress.json or session.json, when the replace failed",
    ("behavior.py", "_replace"): "the temporary file this call made with tempfile.mkstemp beside a behaviour file (`.<name>.<random>.tmp`), when writing or renaming it failed",
    ("source.py", "_remove_temp"): "the temporary folder a replay of a zip was unpacked into by this code",
    # The session zip (package.py). Each is a file of the zip code's own making in sessions/,
    # beside the session folders and never inside one: `.<session>.zip.partial-<hex>`.
    ("package.py", "place"): "the verified partial zip this attempt wrote, after it was hard-linked to its final name (the same file under a second name)",
    ("package.py", "_finish"): "the partial zip this attempt opened exclusively (`xb`) and wrote, when writing or verifying it failed",
    ("package.py", "_tidy"): "partial zips of this session left by a crashed attempt (exact name pattern, regular files, no links), only once the session's recorded zip reads back and matches the folder",
}


def deletions():
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                attr = node.func.attr
                owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                if attr in NAMES or (attr == "remove" and owner == "os"):
                    found.append((path.name, function.name, node.lineno))
    return found


def test_every_call_that_can_delete_is_on_the_allow_list_with_a_reason():
    unlisted = [(f, fn, line) for f, fn, line in deletions() if (f, fn) not in ALLOWED]
    assert not unlisted, f"not on the allow-list: {unlisted}"
    assert all(reason.strip() for reason in ALLOWED.values())


def test_the_allow_list_has_no_entry_for_a_call_that_is_gone():
    present = {(f, fn) for f, fn, _ in deletions()}
    assert set(ALLOWED) <= present, set(ALLOWED) - present


def test_a_failed_start_keeps_its_folder_and_marks_it_failed():
    source = (PACKAGE / "server.py").read_text(encoding="utf-8")
    start = source.index("def _start_session")
    end = source.index("def _publish")
    assert "rmtree" not in source[start:end] and "_mark_failed" in source[start:end]


def test_nothing_that_sets_a_lost_link_s_recording_aside_deletes_it():
    source = (PACKAGE / "server.py").read_text(encoding="utf-8")
    start = source.index("def _link_lost")
    end = source.index("def _write_recordings")
    assert "rmtree" not in source[start:end] and "unlink" not in source[start:end]


def test_the_importer_neither_deletes_nor_replaces_nor_overwrites_anything():
    """``importer.py`` (saved session files): no call that deletes, renames over or truncates; the
    only file it opens for writing is opened with ``xb`` (exclusive create), and its one rename is
    ``package.rename_exclusive``, which fails rather than replace a file."""
    tree = ast.parse((PACKAGE / "importer.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    names = {
        (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        for node in calls
    }
    assert not names & (NAMES | {"remove", "replace", "rename", "renames", "move", "rmtree", "write_bytes", "write_text", "truncate", "extract", "extractall"})
    modes = [
        node.args[1].value
        for node in calls
        if getattr(node.func, "id", "") == "open" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
    ]
    assert set(modes) <= {"rb", "xb"} and "xb" in modes
    assert "rename_exclusive" in names

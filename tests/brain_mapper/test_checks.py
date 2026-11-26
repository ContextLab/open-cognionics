"""Start-up checks: ``checks.run``, ``brain-mapper --check`` and the list of recorded hashes.

Real files, the real command as a subprocess, a real pseudo-terminal device (to see that nothing
is written to it) and a copy of the installed package with one file changed. No test opens a
serial port; ``oc.find_headsets()`` only lists them, so these tests do not depend on whether a
dongle is plugged in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import open_cognionics as oc
from brain_mapper import checks
from brain_mapper.server import Settings

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "brain_mapper" / "src" / "brain_mapper"
COMMAND = Path(sys.executable).parent / "brain-mapper"
IDS = ["python", "open_cognionics", "page_files", "three_js", "data_folder", "dongle"]
#: The lines of ``--check``: the checks of ``checks.run``, then ``video_host`` (``checks.video_host``),
#: which is ``PASS`` with the internet and ``WARN`` without it, never ``FAIL``.
CHECK_IDS = [*IDS, "video_host"]
THREE = "web/vendor/three/build/three.module.js"


def settings(tmp_path, **kwargs) -> Settings:
    values = dict(data_dir=tmp_path / "data", port=None, replay=None, flow=None, http_port=1)
    values.update(kwargs)
    return Settings(**values)


@pytest.fixture
def package_copy(tmp_path):
    """A copy of the installed package that a test may change; ``env`` makes a process use it."""
    root = tmp_path / "copy"
    shutil.copytree(PACKAGE, root / "brain_mapper", ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ, PYTHONPATH=str(root))
    found = subprocess.run(
        [sys.executable, "-c", "import brain_mapper; print(brain_mapper.__file__)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert found == str(root / "brain_mapper" / "__init__.py"), found  # the copy is the one used
    return root / "brain_mapper", env


def run_check(args, env=None):
    return subprocess.run(
        [str(COMMAND), "--check", *args], capture_output=True, text=True, timeout=60, env=env
    )


# -- checks.run ----------------------------------------------------------------------------------


def test_run_gives_the_checks_in_order_and_a_good_setup_passes(tmp_path):
    items = checks.run(settings(tmp_path))
    assert [c.id for c in items] == IDS
    assert all(c.ok and c.fix is None and c.text for c in items), items
    by_id = {c.id: c for c in items}
    assert oc.__version__ in by_id["open_cognionics"].text
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in by_id["python"].text


def test_the_dongle_check_lists_ports_and_does_not_depend_on_what_is_plugged_in(tmp_path):
    ports = oc.find_headsets()
    check = {c.id: c for c in checks.run(settings(tmp_path))}["dongle"]
    assert check.ok
    if ports:
        assert f"{len(ports)} headset dongle(s) found" in check.text
        assert all(p.path in check.text for p in ports)
    else:
        assert "no headset dongle found" in check.text


def test_check_dicts_carry_the_four_fields(tmp_path):
    item = checks.run(settings(tmp_path))[0].as_dict()
    assert set(item) == {"id", "ok", "text", "fix"}


def test_run_never_opens_the_port_it_is_given_and_writes_no_byte_to_it(tmp_path, pty_device):
    before = set(os.listdir("/dev/fd"))
    items = checks.run(settings(tmp_path, port=pty_device.path))
    time.sleep(0.5)  # a request, if one were sent, would be logged by now
    assert pty_device.written() == b""
    assert set(os.listdir("/dev/fd")) == before  # no handle left open either
    dongle = {c.id: c for c in items}["dongle"]
    assert "--port" in dongle.text and pty_device.path in dongle.text


def test_run_does_not_make_the_data_folder(tmp_path):
    folder = tmp_path / "not" / "yet"
    check = {c.id: c for c in checks.run(settings(tmp_path, data_dir=folder))}["data_folder"]
    assert check.ok and "does not exist yet" in check.text
    assert not (tmp_path / "not").exists()


def test_a_data_folder_that_cannot_be_written_fails_with_a_fix(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        check = {c.id: c for c in checks.run(settings(tmp_path, data_dir=folder))}["data_folder"]
    finally:
        folder.chmod(0o700)
    assert not check.ok
    assert str(folder) in check.text and "--data-dir" in check.fix


def test_a_data_folder_that_is_a_file_fails(tmp_path):
    folder = tmp_path / "file"
    folder.write_text("x")
    check = {c.id: c for c in checks.run(settings(tmp_path, data_dir=folder / "inside"))}["data_folder"]
    assert not check.ok and "not a folder" in check.text


# -- the recorded list of hashes -----------------------------------------------------------------


def test_the_recorded_list_is_what_the_web_files_hash_to():
    """Fails when a file under ``web/`` was changed, added or removed without the list being made
    again. To make it again: ``uv run python scripts/brain_mapper_web_hashes.py`` (docs/brain-mapper.md)."""
    recorded = checks.recorded_hashes()
    found = checks.web_hashes()
    changed = sorted(n for n in recorded.keys() & found.keys() if recorded[n] != found[n])
    added = sorted(found.keys() - recorded.keys())
    removed = sorted(recorded.keys() - found.keys())
    assert not (changed or added or removed), (
        f"changed {changed}, not in the list {added}, missing {removed}: "
        f"run `{checks.REGENERATE}` and commit web_files.json"
    )


def test_the_script_that_makes_the_list_says_whether_it_is_current(tmp_path):
    script = REPO / "scripts" / "brain_mapper_web_hashes.py"
    current = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert current.returncode == 0, current.stderr


def test_the_list_covers_the_page_and_three_js():
    names = set(checks.recorded_hashes())
    assert {"index.html", "js/app.js", "js/screens/device.js", "vendor/three/build/three.module.js"} <= names


# -- brain-mapper --check ------------------------------------------------------------------------


def test_check_prints_one_line_per_check_and_exits_0(tmp_path, pty_device):
    result = run_check(["--data-dir", str(tmp_path / "data"), "--port", pty_device.path])
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == len(CHECK_IDS)
    assert [line.split(":")[0].split()[-1] for line in lines] == CHECK_IDS
    assert all(line.startswith("PASS  ") for line in lines[:-1])
    assert lines[-1].startswith(("PASS  video_host: ", "WARN  video_host: "))
    assert result.stderr == ""
    assert not (tmp_path / "data").exists()  # nothing is made
    assert pty_device.written() == b""  # and nothing is written to the headset


def test_check_with_the_default_data_folder_leaves_an_empty_home_empty(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home))
    result = run_check([], env=env)  # no --data-dir: the default, <home>/brain-mapper
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(line.startswith(("PASS  ", "FAIL  ", "WARN  video_host: ")) for line in result.stdout.splitlines())
    assert list(home.iterdir()) == []  # nothing made, not even the data folder


def test_check_with_a_changed_three_js_file_exits_1_and_names_the_file(tmp_path, package_copy):
    package, env = package_copy
    changed = package / THREE
    changed.write_bytes(changed.read_bytes() + b"\n// altered\n")
    result = run_check(["--data-dir", str(tmp_path / "data")], env=env)
    assert result.returncode == 1
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert len(failed) == 1 and "three_js" in failed[0]
    assert "vendor/three/build/three.module.js (changed)" in failed[0]
    assert "Fix:" in failed[0]
    assert len(result.stdout.splitlines()) == len(CHECK_IDS)


def test_check_with_a_changed_page_file_names_it_under_page_files(tmp_path, package_copy):
    package, env = package_copy
    page = package / "web" / "js" / "app.js"
    page.write_bytes(page.read_bytes() + b"\n// altered\n")
    result = run_check(["--data-dir", str(tmp_path / "data")], env=env)
    assert result.returncode == 1
    failed = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    assert len(failed) == 1 and "page_files" in failed[0] and "js/app.js (changed)" in failed[0]


def test_check_with_a_missing_and_an_added_file_names_both(tmp_path, package_copy):
    package, env = package_copy
    (package / "web" / "js" / "traces.js").unlink()
    (package / "web" / "js" / "extra.js").write_text("export {};\n")
    result = run_check(["--data-dir", str(tmp_path / "data")], env=env)
    assert result.returncode == 1
    line = next(x for x in result.stdout.splitlines() if x.startswith("FAIL"))
    assert "js/traces.js (missing)" in line and "js/extra.js (not in the recorded list)" in line


def test_check_with_a_data_folder_that_cannot_be_written_exits_1(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        result = run_check(["--data-dir", str(folder)])
    finally:
        folder.chmod(0o700)
    assert result.returncode == 1
    assert any(x.startswith("FAIL") and "data_folder" in x for x in result.stdout.splitlines())


def test_the_start_up_checks_go_to_the_page_over_the_websocket(app, pty_device, tmp_path, package_copy):
    from test_websocket import open_socket, read_until

    package, env = package_copy
    changed = package / "web" / "vendor" / "three" / "build" / "three.module.js"
    changed.write_bytes(changed.read_bytes() + b"\n// altered\n")
    a = app(port=pty_device.path, env=env)
    with open_socket(a) as ws:
        seen = read_until(ws, lambda m: m["t"] == "checks")
    items = seen[-1]["items"]
    assert [i["id"] for i in items] == IDS
    failed = [i for i in items if not i["ok"]]
    assert [i["id"] for i in failed] == ["three_js"]
    assert "three.module.js (changed)" in failed[0]["text"] and failed[0]["fix"]
    assert json.dumps(items)  # plain JSON


def test_a_good_start_sends_only_passing_checks(app, pty_device):
    from test_websocket import open_socket, read_until

    a = app(port=pty_device.path)
    with open_socket(a) as ws:
        seen = read_until(ws, lambda m: m["t"] == "checks")
    assert all(i["ok"] for i in seen[-1]["items"])

"""The session zip, continued: every file of the folder, sessions without EEG, what ``complete``
means, temporary files, the rename fallback, hooks that fail, what is logged, and the folder lock.

Sessions are real: the app on a pty device, a missing port or a silent device, or a replayed
recording through the real engine (``make_ended_folder``). Failures have real causes where one
exists (a file size limit in a subprocess, a FAT disk image without hard links, a lock held by
another process); where a test calls one function directly on real files, its docstring says so.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brain_mapper import package, scores
from brain_mapper.server import SessionRecord
from pty_device import PtyDevice
from test_device_browser import point
from test_engine import SHORT, accept, events_of, next_state, open_ws, send
from test_package import (  # noqa: F401  (device is a fixture)
    EEG,
    PACKET,
    TOP,
    device,
    files_of,
    make_ended_folder,
    wait_packaged,
    zip_contents,
    zips_in,
)
from test_participants import post_label, session_folder, stop
from test_progress import drive_to
from test_shell_browser import inprocess  # noqa: F401  (a fixture)

HERE = Path(__file__).parent


def wait_for(predicate, timeout: float = 30.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def expected_members(folder: Path) -> dict[str, bytes]:
    """Every regular file of the folder (no links), and ``points.jsonl`` again as ``behavior/points.jsonl``."""
    found = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if path.is_symlink() or any((folder / q).is_symlink() for q in rel.parents) or not path.is_file():
            continue
        found[rel.as_posix()] = path.read_bytes()
    if "points.jsonl" in found:
        found["behavior/points.jsonl"] = found["points.jsonl"]
    return found


def check_zip_is_the_folder(path: Path, folder: Path) -> dict[str, bytes]:
    """The zip holds exactly the folder's files, each byte for byte (``session.json`` before ``zip`` was added)."""
    content = zip_contents(path)
    on_disk = expected_members(folder)
    assert set(content) == set(on_disk)
    for name, data in content.items():
        if name != "session.json":
            assert data == on_disk[name], name
    saved = json.loads(on_disk["session.json"])
    assert {**json.loads(content["session.json"]), "zip": path.name} == saved
    return content


# -- Critical 1: every file of the folder --------------------------------------------------------


def test_every_file_of_the_folder_is_zipped_at_any_depth_and_links_are_reported_not_followed(tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    (folder / "notes" / "deep").mkdir(parents=True)
    (folder / "notes" / "deep" / "extra.txt").write_text("a file nobody planned for\n")
    (folder / "unexpected.bin").write_bytes(os.urandom(5000))
    (folder / ".hidden").write_text("hidden files are files too\n")
    (folder / ".tmp-1234abcd.json").write_text('{"half": ')  # a replace-rename cut short: not data
    # only that exact shape, and only at the top of the folder, is the writer's own: these are data
    (folder / ".tmp-notours.json").write_text('{"seven": "characters"}')
    (folder / "behavior").mkdir()
    (folder / "behavior" / ".tmp-1234abcd.json").write_text('{"a task": "file"}')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not part of the session\n")
    (folder / "link-to-file").symlink_to(outside / "secret.txt")
    (folder / "link-to-dir").symlink_to(outside)
    path = package.finish(folder, complete=False)
    content = zip_contents(path)
    assert {"notes/deep/extra.txt", "unexpected.bin", ".hidden", ".tmp-notours.json", "behavior/.tmp-1234abcd.json"} <= set(content)
    assert ".tmp-1234abcd.json" not in content
    assert not any(n.startswith("link-to") for n in content) and "secret.txt" not in "".join(content)
    on_disk = expected_members(folder)
    del on_disk[".tmp-1234abcd.json"]
    assert set(content) == set(on_disk)
    for name, body in content.items():
        if name != "session.json":
            assert body == on_disk[name], name
    meta = json.loads((folder / "session.json").read_text())
    left_out = [
        {"path": "link-to-dir", "reason": "a symbolic link, not followed"},
        {"path": "link-to-file", "reason": "a symbolic link, not followed"},
        {"path": ".tmp-1234abcd.json", "reason": "a temporary file of an interrupted write of session.json or progress.json"},
    ]
    assert sorted(meta["not_zipped"], key=str) == sorted(left_out, key=str)
    assert json.loads(content["session.json"])["not_zipped"] == meta["not_zipped"]
    assert (outside / "secret.txt").read_text() == "not part of the session\n"  # followed by nothing


def test_verification_fails_when_a_listed_file_is_not_in_the_zip_or_differs(tmp_path, recorded_session):
    """``package.verify`` called directly on a real zip and real files."""
    folder = make_ended_folder(tmp_path / "data", recorded_session)
    path = package.finish(folder, complete=False)
    listed = package.members(folder).files
    assert package.verify(path, listed, crc_only=("session.json",))
    extra = folder / "late.txt"
    extra.write_text("written after the zip\n")
    assert not package.verify(path, {**listed, "late.txt": extra}, crc_only=("session.json",))
    raw = folder / "eeg" / "raw.bin"
    copy = tmp_path / "raw-changed.bin"
    body = bytearray(raw.read_bytes())
    body[len(body) // 2] ^= 0xFF  # one byte, same size
    copy.write_bytes(bytes(body))
    assert not package.verify(path, {**listed, "eeg/raw.bin": copy}, crc_only=("session.json",))
    fewer = dict(listed)
    del fewer["events.jsonl"]
    assert not package.verify(path, fewer, crc_only=("session.json",))


def test_a_link_lost_before_the_device_step_and_found_again_puts_both_recordings_in_the_zip(app, tmp_path, device):  # noqa: F811
    first = device()
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    a = app(port=str(link), flow=SHORT)
    assert post_label(a, "P001")[0] == 200
    folder = session_folder(a)
    raw = folder / "eeg" / "raw.bin"
    wait_for(lambda: raw.is_file() and raw.stat().st_size > 20 * PACKET, what="the first recording")
    first.hang_up()
    wait_for(lambda: (folder / "eeg-lost-1" / "raw.bin").is_file(), what="eeg-lost-1")
    second = device()
    point(link, second.path)
    wait_for(lambda: raw.is_file() and raw.stat().st_size > 20 * PACKET, what="the second recording")
    stop(a)
    path = wait_packaged(folder, timeout=10)
    content = check_zip_is_the_folder(path, folder)
    lost = {n for n in content if n.startswith("eeg-lost-1/")}
    assert {"eeg-lost-1/raw.bin", "eeg-lost-1/meta.json"} <= lost
    assert content["eeg-lost-1/raw.bin"] and first.played_after_config().find(content["eeg-lost-1/raw.bin"]) >= 0
    assert second.played_after_config().find(content["eeg/raw.bin"]) >= 0
    meta = json.loads(content["session.json"])
    assert meta["recording"] is True and meta["recordings"] == ["eeg-lost-1", "eeg"]
    assert meta["complete"] is False


def test_a_folder_that_cannot_be_listed_fails_the_zip_and_is_left_intact(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    hidden = folder / "behavior" / "locked"
    hidden.mkdir(parents=True)
    (hidden / "task.csv").write_text("trial,answer\n")
    before = files_of(folder)
    before_meta = (folder / "session.json").read_bytes()
    hidden.chmod(0o000)  # the real cause: a subfolder that cannot be read
    try:
        with pytest.raises(package.PackageError, match="could not be read"):
            package.finish(folder, complete=False)
        assert zips_in(data / "sessions") == [] and [p.name for p in (data / "sessions").iterdir()] == [folder.name]
        assert (folder / "session.json").read_bytes() == before_meta  # nothing written yet
        # at the next start: reported in plain words, left as it is
        a = app(replay=recorded_session, flow=SHORT, data_dir=data)
        assert any(
            f"the session folder {folder.name} was not zipped (a folder inside it could not be read" in line for line in a.printed
        ), a.printed
        assert zips_in(data / "sessions") == []
    finally:
        hidden.chmod(0o700)
    # Every file as it was; the one file added is behavior/scores.json, which the BEFORE_ZIP hook
    # wrote before the listing failed.
    after = files_of(folder)
    assert json.loads(after.pop("behavior/scores.json")) == scores.score_folder(folder)
    assert after == before
    assert package.finish(folder, complete=False).name == f"{folder.name}_replay.zip"  # readable again: zipped whole


# -- Critical 2: a session whose link never came up ----------------------------------------------


@pytest.mark.parametrize("cause", ["missing port", "silent device"])
def test_a_session_whose_link_never_came_up_is_zipped_without_eeg(app, tmp_path, cause):
    silent = PtyDevice.silent_device() if cause == "silent device" else None
    try:
        port = silent.path if silent is not None else str(tmp_path / "no-such-port")
        a = app(port=port, flow=SHORT)
        assert post_label(a, "P001")[0] == 200  # accepted without a link
        folder = session_folder(a)
        with open_ws(a) as ws:  # at least one attempt to open the headset fails, and the page is told
            deadline = time.monotonic() + 30
            while True:
                assert time.monotonic() < deadline, "no failed attempt to open the headset"
                try:
                    message = json.loads(ws.recv(timeout=1))
                except TimeoutError:
                    continue
                if message.get("t") == "link" and message.get("state") == "connecting" and message.get("problem"):
                    break
        assert not (folder / "eeg").exists()
        stop(a)
        assert a.process.returncode == 0
    finally:
        if silent is not None:
            silent.hang_up()
    path = wait_packaged(folder, timeout=5)
    content = check_zip_is_the_folder(path, folder)
    assert not any(n.startswith("eeg") for n in content)
    assert {"session.json", "progress.json", "events.jsonl"} <= set(content)
    meta = json.loads(content["session.json"])
    assert meta["recording"] is False and meta["recordings"] == [] and meta["headset"] is None
    assert meta["complete"] is False and meta["ending"] != "finished"
    assert all(t["start_sample"] is None for t in meta["timeline"])


# -- Important 3: complete only when the participant finished the last step -----------------------


def test_a_source_that_ends_at_the_last_step_does_not_make_the_session_complete(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        drive_to(ws, "summary")  # every round played; Finish not pressed
    # The source ends: since 2026-11-25 a lost headset link pauses instead of ending, so the source
    # that ends here is the one the process stops (Ctrl-C).
    stop(a)
    folder = session_folder(a)
    meta = json.loads(zip_contents(wait_packaged(folder))["session.json"])
    events = events_of(folder)
    names = [e["name"] for e in events]
    assert "session_end" not in names and names[-1] == "source_ended"
    assert events[-1]["payload"]["ended_early"] is False and meta.get("failure") is None
    assert meta["complete"] is False


def advance_in_process(a, until) -> dict:
    """Advances the in-process session (as the page would) until ``until(state)``."""
    deadline = time.monotonic() + 90
    while True:
        state = a.hub.session.state()
        if until(state):
            return state
        assert time.monotonic() < deadline, state
        if state["phase"] in ("waiting", "between"):
            a.hub.session.advance()
        time.sleep(0.05)


def test_an_exit_whose_final_tick_fails_is_not_complete(inprocess, caplog):  # noqa: F811
    a = inprocess(SHORT)
    a.hub.accept("P001")
    folder = a.hub.folder
    advance_in_process(a, lambda s: s["step"] == "summary" and s["phase"] == "waiting")
    a.hub.session._events.close()  # the last write of the session fails, as with a full disk
    with caplog.at_level(logging.ERROR, logger="brain_mapper"):
        a.stop()  # as on Ctrl-C: the source stops, the final tick, the zip
    path = wait_packaged(folder, timeout=10)
    meta = json.loads(zip_contents(path)["session.json"])
    assert meta["complete"] is False
    assert "session_end" not in [e["name"] for e in events_of(folder)]


def test_a_finished_session_is_complete_only_with_its_session_end(tmp_path, recorded_session):
    """``package.describe`` called directly on a real folder: ``complete=True`` from the caller is
    not enough when the folder has no ``session_end`` (the folder was stopped)."""
    folder = make_ended_folder(tmp_path / "data", recorded_session)
    meta = package.describe(folder, complete=True)
    assert meta["complete"] is False


# -- Important 4: temporary files ----------------------------------------------------------------


def test_a_zip_the_disk_refuses_removes_its_own_temporary_file_and_the_next_start_makes_it(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    (folder / "behavior").mkdir()
    (folder / "behavior" / "noise.bin").write_bytes(os.urandom(2 << 20))  # does not compress
    before = files_of(folder)
    limit = 512 * 1024  # far above session.json, far below the zip
    done = subprocess.run(
        [sys.executable, str(HERE / "zip_fsize_driver.py"), str(folder), str(limit)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result["error"] is not None and result["error"]["errno"] == 27  # EFBIG: the size limit
    assert result["names"] == [folder.name]  # no zip, and the attempt's own temporary file is gone
    # the folder lost nothing: only session.json changed (its record, written before the limit)
    after = files_of(folder)
    assert set(after) == set(before)
    assert all(after[n] == before[n] for n in before if n != "session.json")
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    assert any(folder.name in line and "saved now" in line for line in a.printed)
    path = wait_packaged(folder, timeout=5)
    check_zip_is_the_folder(path, folder)


def test_leftovers_of_a_session_whose_zip_cannot_be_checked_are_reported_and_kept(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    package.finish(folder, complete=False)
    (folder / "late.txt").write_text("the folder changed after its zip was made\n")
    leftover = data / "sessions" / f".{folder.name}_replay.zip.partial-0a1b2c3d"
    leftover.write_bytes(b"PK half of a zip")
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    assert any(
        f"1 temporary zip file(s) of the session {folder.name}" in line and "left as they are" in line for line in a.printed
    )
    assert leftover.read_bytes() == b"PK half of a zip"


# -- Important 5: the rename fallback where hard links are not possible ---------------------------


@pytest.fixture
def fat_volume(tmp_path):
    """A real FAT32 disk image (made and mounted with hdiutil): a file system without hard links."""
    image, mount = tmp_path / "fat.dmg", tmp_path / "fat"
    subprocess.run(
        ["hdiutil", "create", "-size", "64m", "-fs", "MS-DOS FAT32", "-volname", "BMTEST", str(image)],
        check=True,
        capture_output=True,
    )
    mount.mkdir()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)], check=True, capture_output=True)
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(mount)], check=False, capture_output=True)


def test_on_a_disk_without_hard_links_the_zip_is_named_by_an_exclusive_rename(fat_volume, recorded_session):
    sessions_parent = fat_volume / "data"
    probe = fat_volume / "probe"
    probe.write_bytes(b"x")
    with pytest.raises(OSError):
        os.link(probe, fat_volume / "probe2")  # the real cause: this file system has no hard links
    probe.unlink()
    folder = make_ended_folder(sessions_parent, recorded_session)
    sessions = folder.parent
    squatter = sessions / f"{folder.name}_replay.zip"
    squatter.write_bytes(b"someone else's file")
    path = package.finish(folder, complete=False)
    assert path.name == f"{folder.name}_replay-2.zip"
    assert squatter.read_bytes() == b"someone else's file"
    check_zip_is_the_folder(path, folder)
    assert not [p for p in sessions.iterdir() if ".partial-" in p.name]


def test_an_exclusive_rename_never_replaces_a_file(tmp_path):
    """``package.rename_exclusive`` and ``package.place(link=False)`` called directly on real files."""
    source, taken = tmp_path / "source", tmp_path / "taken"
    source.write_bytes(b"verified zip")
    taken.write_bytes(b"already here")
    with pytest.raises(FileExistsError):
        package.rename_exclusive(source, taken)
    assert source.read_bytes() == b"verified zip" and taken.read_bytes() == b"already here"
    (tmp_path / "S.zip").write_bytes(b"one")
    (tmp_path / "S-2.zip").write_bytes(b"two")
    placed = package.place(source, tmp_path, "S", link=False)
    assert placed.name == "S-3.zip" and placed.read_bytes() == b"verified zip" and not source.exists()
    assert (tmp_path / "S.zip").read_bytes() == b"one" and (tmp_path / "S-2.zip").read_bytes() == b"two"


# -- Important 6 and 7: a hook that raises; nothing with the label in the log -----------------------


def test_a_hook_that_raises_does_not_block_the_zip_and_is_reported_without_the_label(inprocess, caplog):  # noqa: F811
    a = inprocess(SHORT)
    calls = []

    def broken_hook(folder: Path) -> None:
        calls.append("broken")
        raise RuntimeError(f"cannot write {folder / 'behavior' / 'words.csv'} for P001")

    def good_hook(folder: Path) -> None:
        calls.append("good")
        (folder / "behavior").mkdir(exist_ok=True)
        (folder / "behavior" / "words.csv").write_text("trial,answer\n1,a\n")

    registered = list(package.BEFORE_ZIP)  # behavior.before_zip; put back after the test
    package.BEFORE_ZIP[:] = [broken_hook, good_hook]
    try:
        with caplog.at_level(logging.INFO, logger="brain_mapper"):
            a.hub.accept("P001")
            folder = a.hub.folder
            advance_in_process(a, lambda s: s["step"] == "calibration")
            a.hub.session.stop()
            path = wait_packaged(folder, timeout=20)
            wait_for(lambda: (a.hub.record.message or {}).get("state") == "saved", what="the saved message")
    finally:
        package.BEFORE_ZIP[:] = registered
    assert calls == ["broken", "good"]
    content = zip_contents(path)
    assert "behavior/words.csv" in content  # the hook after the broken one ran
    meta = json.loads((folder / "session.json").read_text())
    assert len(meta["hook_errors"]) == 1
    error = meta["hook_errors"][0]
    assert error["hook"].endswith("broken_hook") and error["error"].startswith("RuntimeError: cannot write")
    assert "P001" not in error["error"] and str(folder) not in error["error"]
    notes = a.hub.record.message["notes"]
    assert any("broken_hook" in n and "failed" in n for n in notes)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "broken_hook" in logged and "RuntimeError" in logged
    assert "P001" not in logged and folder.name not in logged and "Traceback" not in logged
    assert all(r.exc_info is None for r in caplog.records)


def test_a_zip_that_cannot_be_made_is_logged_without_the_label_or_the_folder(inprocess, caplog):  # noqa: F811
    a = inprocess(SHORT)
    a.hub.accept("P001")
    folder = a.hub.folder
    sessions = folder.parent
    advance_in_process(a, lambda s: s["step"] == "calibration")
    os.chmod(sessions, 0o555)  # the real cause: nothing new can be made beside the folder
    try:
        with caplog.at_level(logging.INFO, logger="brain_mapper"):
            a.hub.session.stop()
            wait_for(lambda: (a.hub.record.message or {}).get("state") == "failed", what="the failed message")
    finally:
        os.chmod(sessions, 0o755)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "the session zip could not be made" in logged and "PermissionError" in logged
    assert "P001" not in logged and str(sessions) not in logged and "Traceback" not in logged
    assert all(r.exc_info is None for r in caplog.records)
    # the page is told where the folder is (the participant's own screen)
    assert folder.name in a.hub.record.message["msg"]


# -- Minor 9 and 11: the folder lock ------------------------------------------------------------------


HOLD = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


def test_a_lock_that_cannot_be_taken_is_told_to_the_page_and_the_log(inprocess, tmp_path, caplog):  # noqa: F811
    """``SessionRecord.claim`` and ``on_state`` called directly, with the lock held by a real second process."""
    a = inprocess(SHORT)
    a.host_port = ("127.0.0.1", a.port)  # what open_ws connects to
    a.hub.accept("P001")
    other = tmp_path / "held"
    other.mkdir()
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(other)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        record = SessionRecord(a.hub)
        record.claim(other)
        with open_ws(a) as ws, caplog.at_level(logging.WARNING, logger="brain_mapper"):
            next_state(ws)
            record.on_state({"phase": "waiting"})
            deadline = time.monotonic() + 10
            while True:
                message = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                if message.get("t") == "error" and message.get("kind") == "folder_not_locked":
                    break
        assert "could not be locked" in message["msg"] and "only one at a time" in message["msg"]
        assert any("could not be locked" in r.getMessage() for r in caplog.records)
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)


def test_a_second_process_on_the_same_data_folder_leaves_a_session_being_recorded_alone(app, pty_device, recorded_session):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    folder = session_folder(a)
    with open_ws(a) as ws:
        drive_to(ws, "words")
        before = json.loads((folder / "session.json").read_text())
        b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
        line = f"the session folder {folder.name} was not zipped (another brain-mapper process is using it)"
        assert any(line in printed for printed in b.printed), b.printed
        stop(b)
        assert json.loads((folder / "session.json").read_text()) == before
        assert zips_in(a.data_dir / "sessions") == []
        # the first process goes on recording and makes its own zip at the end
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["stopped"])
    path = wait_packaged(folder)
    check_zip_is_the_folder(path, folder)
    assert zips_in(a.data_dir / "sessions") == [path.name]


# -- calibration.json ------------------------------------------------------------------------------


def test_calibration_json_fills_the_quality_history_and_the_thresholds_version(tmp_path, recorded_session):
    """``calibration.json`` in the shape the calibration step writes it:
    ``{"states", "excluded", "thresholds_version", "unix_time"}``, each state good, marginal or bad.
    The same with a real contact check on the simulated headset, through the real server, is
    ``test_calibration_zip.test_a_real_contact_check_is_the_first_entry_of_the_quality_history_in_the_zip``."""
    folder = make_ended_folder(tmp_path / "data", recorded_session)
    calibration = {
        "states": {"Fp1": "good", "Fp2": "bad", "O1": "marginal"},
        "excluded": ["Fp2"],
        "thresholds_version": "contact-2026-11",
        "unix_time": 1764000000.5,
    }
    (folder / "calibration.json").write_text(json.dumps(calibration))
    path = package.finish(folder, complete=False)
    meta = json.loads(zip_contents(path)["session.json"])
    assert meta["channel_quality_history"] == [calibration]
    assert meta["contact_thresholds_version"] == "contact-2026-11"
    assert json.loads(zip_contents(path)["calibration.json"]) == calibration


def test_without_calibration_json_the_history_is_empty(tmp_path, recorded_session):
    folder = make_ended_folder(tmp_path / "data", recorded_session)
    meta = json.loads(zip_contents(package.finish(folder, complete=False))["session.json"])
    assert meta["channel_quality_history"] == [] and meta["contact_thresholds_version"] is None
    assert "calibration_problem" not in meta


def test_a_failure_while_a_label_is_accepted_is_logged_without_any_session_path_or_label(inprocess, caplog, tmp_path):  # noqa: F811
    """``Hub.log_failure`` with a real ``ProgressDamaged`` read from a damaged ``progress.json`` of a
    session labelled P002, while the hub's current session is P001's, and the label given as the
    accept route gives it."""
    from brain_mapper import progress
    from brain_mapper.server import make_session_folder

    a = inprocess(SHORT)
    a.hub.accept("P001")
    other = make_session_folder(a.settings.data_dir / "sessions", "P002")
    (other / "progress.json").write_text("{ not json")
    with pytest.raises(progress.ProgressDamaged) as caught:
        progress.read(other)
    assert str(other) in str(caught.value)  # the message holds the path, label and all
    with caplog.at_level(logging.ERROR, logger="brain_mapper"):
        a.hub.log_failure("accepting a participant label failed unexpectedly", caught.value, "P002")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ProgressDamaged" in logged and "<data>/..." in logged
    assert "P002" not in logged and "P001" not in logged and str(a.settings.data_dir) not in logged
    assert all(r.exc_info is None for r in caplog.records)

"""The main line's device-setup rules together with the session zip (merged here).

Real ``brain-mapper`` processes on simulated headsets (pseudo-terminal devices); nothing opens a
real serial port. What is checked:

- a failed start keeps its folder, marked ``failure`` in session.json; the next start zips it as
  a failed start (``ending`` ``failed_start``, the failure as ``ending_reason``), says so in one
  line, and changes nothing else in the folder;
- a stop at the device step before the link came up ends cleanly, with a zip and no EEG, after
  ``advance`` there was refused;
- the main line's ``recordings`` of session.json (the set-aside recordings, with
  ``decoded_incl_lost``) is not lost when the zip code describes the session: it is kept as
  ``recordings_set_aside`` beside the zip's own ``recordings`` (the folders that hold EEG).
"""

from __future__ import annotations

import json

from test_device_browser import point
from test_engine import SHORT, events_of, next_state, open_ws, send
from test_package import PACKET, device, next_package, wait_packaged, zip_contents  # noqa: F401  (device is a fixture)
from test_package_safety import check_zip_is_the_folder, wait_for
from test_participants import _read_only, post_label, session_folder, stop

from pty_device import PtyDevice, config_reply, onhead_stream


def files_of(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_a_failed_start_is_kept_and_the_next_start_zips_it_as_a_failed_start(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        with _read_only(a.data_dir):  # the participant list cannot be written: the start fails
            status, body = post_label(a, "P001")
        assert status == 500 and body["code"] == "write_failed"
    (folder,) = list((a.data_dir / "sessions").iterdir())
    meta = json.loads((folder / "session.json").read_text())
    assert "participant list" in meta["failure"]
    stop(a)
    before = files_of(folder)
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT, data_dir=a.data_dir)
        told = [line for line in b.printed if folder.name in line]
        assert told == [
            f"brain-mapper: the session folder {folder.name} is from a start that failed; "
            "it is kept as it is and saved as a zip now"
        ], b.printed
        path = wait_packaged(folder, timeout=10)
        stop(b)
    finally:
        dev.hang_up()
    content = check_zip_is_the_folder(path, folder)
    record = json.loads(content["session.json"])
    assert record["failure"] == meta["failure"]  # the main line's key, kept
    assert record["ending"] == "failed_start" and record["ending_reason"] == meta["failure"]
    assert record["complete"] is False and record["ended_early"] is True
    after = files_of(folder)
    assert {k: v for k, v in after.items() if k != "session.json"} == {k: v for k, v in before.items() if k != "session.json"}
    assert {k: v for k, v in json.loads(after["session.json"]).items() if k in meta} == meta  # every key kept


def test_a_stop_at_the_device_step_before_the_link_came_up_ends_with_a_zip(app, tmp_path):
    a = app(port=str(tmp_path / "no-such-port"), flow=SHORT)
    assert post_label(a, "P001")[0] == 200
    folder = session_folder(a)
    with open_ws(a) as ws:
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
        send(ws, cmd="advance")  # the main line: refused, no recording is open
        while True:
            message = json.loads(ws.recv(timeout=10))
            if message["t"] == "error":
                break
        assert message["kind"] == "refused" and "not live yet" in message["msg"]
        send(ws, cmd="stop")  # the zip line: a stop here is not refused for want of a recording
        final = next_state(ws, predicate=lambda m: m["phase"] == "done")
        assert final["stopped"] is True and final["failure"] is None
        saved = next_package(ws, "saved")
    path = wait_packaged(folder, timeout=10)
    assert saved["file"] == path.name
    content = check_zip_is_the_folder(path, folder)
    assert not any(name.startswith("eeg") for name in content)
    record = json.loads(content["session.json"])
    assert record["recording"] is False and record["ending"] == "stopped"
    stopped = [e for e in events_of(folder) if e["name"] == "stopped"]
    assert len(stopped) == 1 and stopped[0]["payload"].get("marked") is False


def test_the_set_aside_recordings_of_session_json_are_kept_beside_the_zips_recordings(app, tmp_path, device):  # noqa: F811
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
    during = json.loads((folder / "session.json").read_text())["recordings"]
    assert [r["folder"] for r in during] == ["eeg-lost-1"] and during[0]["decoded_incl_lost"] > 0
    stop(a)
    record = json.loads(zip_contents(wait_packaged(folder, timeout=10))["session.json"])
    assert record["recordings"] == ["eeg-lost-1", "eeg"]  # the zip's: the folders that hold EEG
    assert record["recordings_set_aside"] == during  # the main line's, with decoded_incl_lost
    assert json.loads((folder / "session.json").read_text())["recordings_set_aside"] == during

"""The session zip (``brain_mapper.package``): every way a session ends makes one, nothing is overwritten.

Everything is real: sessions run through the app on the pty device (or a replayed recording)
with the short flow, processes are stopped with real signals (SIGINT, SIGTERM, SIGKILL), and
the zips are read back with ``zipfile``, ``open_cognionics`` and MNE. Where a crash inside
``package.finish`` cannot be caused at an exact point without a hook in the product, the
on-disk state that crash leaves is built from a real session folder instead.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

import mne
import psutil
import pytest

import open_cognionics as oc
from brain_mapper import flow, package, progress, scores
from brain_mapper.engine import Session
from brain_mapper.points import Ledger
from brain_mapper.server import make_session_folder
from brain_mapper.source import ReplaySource
from pty_device import PtyDevice, config_reply, onhead_stream
from test_engine import SHORT, accept, events_of, markers_of, next_state, open_ws, run_until, send, session_folder_of
from test_participants import stop
from test_progress import drive_to
from test_shell_browser import inprocess  # noqa: F401  (a fixture)

#: The installed command. Defined here, as in test_checks.py, and not imported from ``conftest``:
#: with tests/python collected in the same run, ``import conftest`` finds that folder's conftest.
COMMAND = Path(sys.executable).parent / "brain-mapper"
PACKET = 75
RATE = 37_500
ZIP_NAME = re.compile(r"P001_\d{8}_\d{6}\.zip")
REPLAY_ZIP_NAME = re.compile(r"P001_\d{8}_\d{6}_replay\.zip")
EEG = {"eeg/raw.bin", "eeg/recording.bdf", "eeg/markers.jsonl", "eeg/meta.json", "eeg/clock.jsonl"}
TOP = {"session.json", "progress.json", "events.jsonl", "points.jsonl", "behavior/points.jsonl",
       "behavior/scores.json"}  # every file of the folder
#: Big enough that a zip held in memory, or a zip made on the event loop, would show.
BIG = 256 * 1024 * 1024


@pytest.fixture
def device():
    made: list[PtyDevice] = []

    def new() -> PtyDevice:
        dev = PtyDevice(onhead_stream(), config_reply())
        made.append(dev)
        return dev

    yield new
    for dev in made:
        dev.hang_up()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_packaged(folder: Path, timeout: float = 90.0) -> Path:
    """The zip named in ``session.json`` once it is recorded there (only after it was verified)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            meta = json.loads((folder / "session.json").read_text())
        except ValueError:
            meta = {}  # never half a file (replace-rename), but be plain about it
        if meta.get("zip"):
            return folder.parent / meta["zip"]
        assert time.monotonic() < deadline, f"no zip recorded for {folder.name}; session.json {meta}"
        time.sleep(0.1)


def zip_contents(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {name: z.read(name) for name in z.namelist()}


def files_of(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


#: The additional zip of a session's results (docs/brain-mapper-results.md), written beside the
#: session zip by the analysis thread after the session zip is saved, so at a moment a test
#: cannot pin: ``<folder>-analysis.zip``, ``<folder>-analysis-2.zip`` ...
ANALYSIS_ZIP = re.compile(r".+-analysis(-[0-9]+)?\.zip")


def zips_in(sessions: Path) -> list[str]:
    """The session zips in ``sessions`` (the results' additional zips are left out: see
    ``analysis_zips_in``)."""
    return sorted(
        p.name for p in sessions.iterdir() if p.is_file() and p.suffix == ".zip" and not ANALYSIS_ZIP.fullmatch(p.name)
    )


def analysis_zips_in(sessions: Path) -> list[str]:
    return sorted(p.name for p in sessions.iterdir() if p.is_file() and ANALYSIS_ZIP.fullmatch(p.name))


def play_through(ws) -> dict:
    """Advances once per new state until the session is over; returns the last state."""
    deadline = time.monotonic() + 120
    state = next_state(ws)
    while state["phase"] != "done":
        assert time.monotonic() < deadline, state
        if state["phase"] in ("waiting", "between"):
            send(ws, cmd="advance")
        state = next_state(ws, predicate=lambda m, s=state: m["rev"] > s["rev"])
    return state


def next_package(ws, state: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"no package message with state {state}"
        try:
            message = json.loads(ws.recv(timeout=left))
        except TimeoutError:
            continue
        if message.get("t") == "package" and message["state"] == state:
            return message


def check_raw_is_what_was_played(raw: bytes, device: PtyDevice) -> int:
    """``raw`` is one unbroken run of the bytes played after the configuration reply; returns where it ends."""
    played = device.played_after_config()
    assert raw and len(raw) % PACKET == 0
    at = played.find(raw)
    assert at >= 0, "raw.bin is not a contiguous run of what the device played"
    return at + len(raw)


def start_block(ws, step="words") -> dict:
    drive_to(ws, step)
    send(ws, cmd="advance")
    return next_state(ws, predicate=lambda m: m["phase"] == "running")


# -- a finished session ------------------------------------------------------------------------


def test_a_finished_session_is_zipped_beside_its_folder_and_the_zip_is_byte_faithful(app, pty_device, tmp_path):
    a = app(port=pty_device.path, flow=SHORT)
    answer = accept(a)
    with open_ws(a) as ws:
        final = play_through(ws)
    assert final["stopped"] is False and final["ended_early"] is False and final["failure"] is None
    folder = session_folder_of(a)
    path = wait_packaged(folder)
    sessions = a.data_dir / "sessions"
    assert path.parent == sessions and ZIP_NAME.fullmatch(path.name) and path.name == f"{folder.name}.zip"
    assert zips_in(sessions) == [path.name]
    # Beside it, at most the results' own zip of this session (it may not be written yet).
    assert analysis_zips_in(sessions) in ([], [f"{folder.name}-analysis.zip"])
    content = zip_contents(path)
    assert set(content) == TOP | EEG  # later stages' files are absent, not empty

    # the recording: what the device played, unbroken, from the label to the end of the session
    raw = content["eeg/raw.bin"]
    check_raw_is_what_was_played(raw, pty_device)
    events = events_of(folder)
    span = events[-1]["unix_time"] - events[0]["unix_time"]
    assert len(raw) / RATE >= span - 0.5
    # every file in the zip is the file in the folder, which is still there
    on_disk = files_of(folder)
    for name in TOP | EEG:
        source = "points.jsonl" if name == "behavior/points.jsonl" else name
        if name != "session.json":
            assert content[name] == on_disk[source], name
    assert json.loads((folder / "eeg" / "meta.json").read_text())["finished"] is True

    # the extracted eeg/ opens with the library and with MNE; the BDF header carries the label
    out = tmp_path / "out"
    with zipfile.ZipFile(path) as z:
        z.extractall(out)
    recording = oc.read_recording(out / "eeg")
    marked = [m["label"] for m in markers_of(folder / "eeg")]
    assert [m.label for m in recording.markers] == marked
    assert marked == [f"{e['step']}/{e['name']}#{e['seq']}" for e in events if e["payload"].get("marked", True)]
    bdf = mne.io.read_raw_bdf(out / "eeg" / "recording.bdf", verbose="error")
    # the BDF holds whole 1 s data records, the last one filled up
    assert recording.data.shape[0] <= bdf.n_times < recording.data.shape[0] + 500 and bdf.info["sfreq"] == 500
    assert b"P001" in (out / "eeg" / "recording.bdf").read_bytes()[:256]

    # session.json: what was there, and what the zip adds
    meta = json.loads(content["session.json"])
    store = json.loads((a.data_dir / "participants.json").read_text())
    assert meta["session"] == folder.name and meta["label"] == "P001" and meta["source"] == "live"
    assert meta["participant"] == "P001" and meta["participant_uuid"] == meta["uuid"] == answer["uuid"]
    assert store["participants"][0] == {"label": "P001", "uuid": answer["uuid"]}
    started = meta["started_unix"]
    assert datetime.fromisoformat(meta["start_utc"]).timestamp() == pytest.approx(started, abs=1)
    assert meta["start_utc"].endswith("+00:00")
    assert datetime.fromisoformat(meta["start_local"]).timestamp() == pytest.approx(started, abs=1)
    recorder = json.loads(content["eeg/meta.json"])
    assert meta["headset"] == {"port": pty_device.path, "config": recorder["config"]}
    assert set(meta["software"]) == {"brain_mapper", "open_cognionics"}
    assert all(isinstance(v, str) and v for v in meta["software"].values())
    assert meta["flow"] == [
        {"id": s.id, "kind": s.kind, "title": s.title, "blocks": [{"id": b.id, "seconds": b.seconds, "points": b.points} for b in s.blocks]}
        for s in flow.load(SHORT)
    ]
    assert meta["channel_quality_history"] == [] and meta["contact_thresholds_version"] is None
    ledger = [json.loads(x) for x in (folder / "points.jsonl").read_text().splitlines()]
    blocks = sum(len(s.blocks) for s in flow.load(SHORT))
    assert meta["points_total"] == ledger[-1]["total"] == 100 * blocks == final["points_total"]
    assert meta["complete"] is True and meta["ended_early"] is False and meta["ending"] == "finished"
    assert "zip" not in meta and "seed" not in meta
    # the timeline: every step and every block, in order, samples never going back
    timeline = meta["timeline"]
    assert [t["step"] for t in timeline if t["block"] is None] == [s.id for s in flow.load(SHORT)]
    assert [(t["step"], t["block"]) for t in timeline if t["block"] is not None] == [
        (s.id, b.id) for s in flow.load(SHORT) for b in s.blocks
    ]
    samples = [t["start_sample"] for t in sorted(timeline, key=lambda t: t["start_unix"])]
    assert all(isinstance(x, int) for x in samples) and samples == sorted(samples)
    for item in timeline:
        assert item["end_sample"] >= item["start_sample"] and item["seconds"] >= 0
        assert datetime.fromisoformat(item["start_utc"]).timestamp() == pytest.approx(item["start_unix"], abs=0.001)
        if item["block"] is not None:
            assert item["seconds"] == pytest.approx(2, abs=0.3) and item["outcome"] == "finished"
    # on disk, session.json is the same with the name of the verified zip added
    saved = json.loads((folder / "session.json").read_text())
    assert saved == {**meta, "zip": path.name}


# -- every other way a session ends ------------------------------------------------------------


def test_a_session_stopped_by_the_participant_is_zipped_and_marked_not_complete(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        start_block(ws)
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["stopped"])
    folder = session_folder_of(a)
    path = wait_packaged(folder)
    content = zip_contents(path)
    assert set(content) == TOP | EEG
    meta = json.loads(content["session.json"])
    assert meta["complete"] is False and meta["ended_early"] is False and meta["ending"] == "stopped"
    check_raw_is_what_was_played(content["eeg/raw.bin"], pty_device)
    assert content["eeg/raw.bin"] == (folder / "eeg" / "raw.bin").read_bytes()


def test_a_session_whose_headset_hangs_up_mid_block_is_zipped_with_every_byte_played(app, pty_device, tmp_path):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        start_block(ws)
        # A hang-up pauses the block (since 2026-11-25; until then it ended the
        # session); the process is then stopped (Ctrl-C), which ends the session early and zips it.
        pty_device.hang_up()
        next_state(ws, predicate=lambda m: m["phase"] == "paused", timeout=30)
    stop(a)
    folder = session_folder_of(a)
    path = wait_packaged(folder)
    content = zip_contents(path)
    meta = json.loads(content["session.json"])
    assert meta["ended_early"] is True and meta["complete"] is False and meta["ending"] == "ended_early"
    end = check_raw_is_what_was_played(content["eeg-lost-1/raw.bin"], pty_device)
    assert len(pty_device.played_after_config()) - end < PACKET  # at most a packet cut by the hang-up
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp_path / "out")
    assert oc.read_recording(tmp_path / "out" / "eeg-lost-1").data.shape[0] > 0


def test_a_session_ended_by_a_failure_is_zipped(inprocess):  # noqa: F811
    a = inprocess(SHORT)
    a.hub.accept("P001")
    a.hub.session.advance()  # participant
    a.hub.session.advance()  # device
    a.hub.session.advance()  # calibration
    a.hub.session.advance()  # the first block of words runs
    a.hub.session._events.close()  # the next write fails, as a full disk would
    a.wait_state(lambda s: s["phase"] == "done")
    path = wait_packaged(a.hub.folder)
    meta = json.loads(zip_contents(path)["session.json"])
    assert meta["ending"] == "failed" and meta["ended_early"] is True and meta["complete"] is False
    assert meta["ending_reason"].startswith("event log failed")


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_ctrl_c_or_sigterm_mid_block_closes_the_recording_and_zips_the_session(app, pty_device, sig):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        start_block(ws)
        folder = session_folder_of(a)
        played_before = len(pty_device.played_after_config())
        a.process.send_signal(sig)
        a.process.wait(timeout=30)
    assert a.process.returncode == 0
    meta = json.loads((folder / "session.json").read_text())
    path = folder.parent / meta["zip"]  # made before the process ended
    content = zip_contents(path)
    assert set(content) == TOP | EEG
    inside = json.loads(content["session.json"])
    assert inside["ended_early"] is True and inside["complete"] is False and inside["ending"] == "ended_early"
    assert json.loads(content["eeg/meta.json"])["finished"] is True  # the recording was closed first
    end = check_raw_is_what_was_played(content["eeg/raw.bin"], pty_device)
    assert end >= played_before - 0.25 * RATE  # all but what was in flight when the signal came
    names = [e["name"] for e in events_of(folder)]
    assert names[-1] == "source_ended"


def test_a_replay_session_is_saved_as_a_replay_zip_whose_raw_bin_is_the_sources(app, recorded_session):
    a = app(replay=recorded_session, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        drive_to(ws, "device")
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["stopped"])
    folder = session_folder_of(a)
    path = wait_packaged(folder)
    assert REPLAY_ZIP_NAME.fullmatch(path.name) and path.name == f"{folder.name}_replay.zip"
    content = zip_contents(path)
    assert set(content) == TOP | EEG
    assert content["eeg/raw.bin"] == (recorded_session / "raw.bin").read_bytes()
    meta = json.loads(content["session.json"])
    assert meta["source"] == "replay"
    assert meta["replay_of"] == {"name": recorded_session.name, "raw_sha256": sha256(recorded_session / "raw.bin")}


# -- the zip route --------------------------------------------------------------------------------


def get(a, path: str, host: str | None = None) -> tuple[int, bytes, dict]:
    h, port = a.host_port
    conn = http.client.HTTPConnection(h, port, timeout=30)
    try:
        conn.request("GET", path, headers={"Host": host or f"{h}:{port}"})
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


def test_the_zip_is_downloaded_only_by_the_participant_page_and_only_through_this_host(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a) as participant, open_ws(a, "onlooker") as onlooker:
        drive_to(participant, "device")
        status, _, _ = get(a, "/api/session/zip")
        assert status == 403  # no key: nothing is said about any session
        send(participant, cmd="stop")
        saved = next_package(participant, "saved")
        # The link message of a hang-up is sent to every client after the package message, so by
        # then the onlooker has been sent all of it: no package message at all. An onlooker cannot
        # download the zip and is not told its name, folder or summary (they hold the label).
        pty_device.hang_up()
        seen = [json.loads(onlooker.recv(timeout=30))]
        while not (seen[-1]["t"] == "link" and seen[-1]["state"] != "live"):
            seen.append(json.loads(onlooker.recv(timeout=30)))
    folder = session_folder_of(a)
    path = wait_packaged(folder)
    assert saved["file"] == path.name and saved["folder"] == str(a.data_dir / "sessions")
    assert not [m for m in seen if m["t"] == "package"], seen
    assert not [m for m in seen if path.name in json.dumps(m) or "/api/session/zip" in json.dumps(m)], seen
    link = saved["download"]
    assert link.startswith("/api/session/zip?key=") and "P001" not in link
    status, body, headers = get(a, link)
    assert status == 200 and body == path.read_bytes()
    assert headers["content-type"] == "application/zip" and path.name in headers["content-disposition"]
    host, port = a.host_port
    assert get(a, link, host=f"evil.example:{port}")[0] == 403
    assert get(a, link, host=f"{host}:{port + 1}")[0] == 403
    assert get(a, "/api/session/zip?key=" + "x" * 24)[0] == 403
    assert get(a, link.replace("key=", "nokey="))[0] == 403


def test_a_reloaded_participant_page_is_told_the_zip_again(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    with open_ws(a, page="pageone") as ws:
        drive_to(ws, "device")
        send(ws, cmd="stop")
        first = next_package(ws, "saved")
    with open_ws(a, page="pageone") as again:
        second = next_package(again, "saved")
    assert second["download"] == first["download"] and second["file"] == first["file"]
    assert second["summary"]["label"] == "P001" and second["summary"]["points_total"] == 0


# -- the process dies: the next start completes the zip ----------------------------------------


def test_a_session_killed_mid_block_is_zipped_at_the_next_start(app, device):
    dev = device()
    a = app(port=dev.path, flow=SHORT)
    accept(a)
    with open_ws(a) as ws:
        start_block(ws)
        folder = session_folder_of(a)
        while (folder / "eeg" / "raw.bin").stat().st_size < 10 * PACKET:
            time.sleep(0.05)
        a.process.kill()
        a.process.wait()
    sessions = a.data_dir / "sessions"
    assert zips_in(sessions) == []
    before = files_of(folder)
    old_meta = json.loads(before["session.json"])
    assert json.loads(before["progress.json"])["status"] == "running"

    b = app(port=device().path, flow=SHORT, data_dir=a.data_dir)
    told = [line for line in b.printed if folder.name in line]
    assert len(told) == 1 and "P001" not in told[0].replace(folder.name, "")
    assert not any("P001" in line for line in b.printed if line not in told)
    (name,) = zips_in(sessions)
    assert name == f"{folder.name}.zip"
    content = zip_contents(sessions / name)
    assert set(content) == TOP | EEG
    # behavior/scores.json is written just before the zip (a BEFORE_ZIP hook), from events.jsonl.
    assert json.loads(content["behavior/scores.json"]) == scores.score_folder(folder)
    for member in TOP | EEG:
        source = "points.jsonl" if member == "behavior/points.jsonl" else member
        if member not in ("session.json", "behavior/scores.json"):
            assert content[member] == before[source], member  # the bytes written before the kill
    meta = json.loads(content["session.json"])
    assert meta["complete"] is False and meta["ended_early"] is True and meta["ending"] == "interrupted"
    assert {k: meta[k] for k in old_meta} == old_meta  # every key kept
    after = files_of(folder)
    made = ("session.json", "behavior/scores.json")  # written by the zip
    assert {k: v for k, v in after.items() if k not in made} == {k: v for k, v in before.items() if k not in made}
    assert json.loads(after["session.json"]) == {**meta, "zip": name}
    # and the start after that has nothing left to do
    stop(b)
    c = app(port=device().path, flow=SHORT, data_dir=a.data_dir)
    assert not any(folder.name in line for line in c.printed)
    assert zips_in(sessions) == [name]


def make_ended_folder(data: Path, recorded_session: Path, now: float | None = None, label: str = "P001") -> Path:
    """A real session folder as the server leaves it: a replayed recording, one block of words, stopped."""
    folder = make_session_folder(data / "sessions", label, now)
    source = ReplaySource(recorded_session)
    source.open()
    source.subscribe(100_000)
    try:
        source.start_recording(folder / "eeg", label, "uuid-1")
        progress.write_json(
            folder / "session.json",
            {"session": folder.name, "label": label, "uuid": "uuid-1", "started_unix": time.time(), "source": "replay"},
        )
        ledger = Ledger(folder / "points.jsonl")
        session = Session(flow.load(SHORT), source, ledger, folder=folder, meta={"uuid": "uuid-1", "seed": 7})
        for _ in range(4):
            session.advance()  # the three setup steps, then the first block of words
        run_until(session, lambda s: s["phase"] == "between")
        session.stop()
        session.close()
        ledger.close()
    finally:
        source.stop()
    return folder


def test_a_name_that_is_taken_gets_a_number_and_the_file_there_is_left_alone(tmp_path, recorded_session):
    data = tmp_path / "data"
    now = time.time()
    first = make_ended_folder(data, recorded_session, now)
    second = make_ended_folder(data, recorded_session, now)  # same participant, same second
    assert second.name == f"{first.name}-2"
    sessions = data / "sessions"
    squatter = sessions / f"{first.name}_replay.zip"
    squatter.write_bytes(b"not this session's zip")
    one = package.finish(first, complete=False)
    two = package.finish(second, complete=False)
    assert one.name == f"{first.name}_replay-2.zip" and two.name == f"{second.name}_replay.zip"
    assert squatter.read_bytes() == b"not this session's zip"
    # finishing again changes nothing and makes no other zip
    stamp = {p.name: p.read_bytes() for p in sessions.iterdir() if p.is_file()}
    assert package.finish(first, complete=False) == one
    assert {p.name: p.read_bytes() for p in sessions.iterdir() if p.is_file()} == stamp


def test_a_crash_after_session_json_and_before_the_zip_is_completed_by_the_next_start(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    package.describe(folder, complete=False)  # the first step of finish; the process "died" here
    described = (folder / "session.json").read_bytes()
    assert "complete" in json.loads(described) and "zip" not in json.loads(described)
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    assert any(folder.name in line for line in a.printed)
    path = wait_packaged(folder, timeout=5)
    assert zip_contents(path)["session.json"] == described  # the record already written is the one zipped
    assert json.loads((folder / "session.json").read_text()) == {**json.loads(described), "zip": path.name}


def test_a_crash_mid_zip_leaves_a_partial_file_that_is_never_taken_for_the_zip_and_goes_once_the_zip_exists(
    app, tmp_path, recorded_session
):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    made = package.finish(folder, complete=False)
    whole = made.read_bytes()
    # The state a crash in the middle of writing leaves: session.json described, no zip, half a temporary file.
    meta = json.loads((folder / "session.json").read_text())
    del meta["zip"]
    progress.write_json(folder / "session.json", meta)
    made.unlink()
    partial = data / "sessions" / f".{folder.name}_replay.zip.partial-0123abcd"
    partial.write_bytes(whole[: len(whole) // 2])
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    assert any(folder.name in line for line in a.printed)
    path = wait_packaged(folder, timeout=5)
    assert path.name == f"{folder.name}_replay.zip" and path.read_bytes() != whole[: len(whole) // 2]
    assert set(zip_contents(path)) == TOP | EEG
    # the leftover is reported in a plain line and removed, now that the session's verified zip exists
    assert any(
        f"1 temporary zip file(s) of the session {folder.name}" in line and "were removed" in line for line in a.printed
    )
    assert not partial.exists()


def test_a_crash_after_the_zip_was_placed_and_before_it_was_recorded_reuses_that_zip(tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    made = package.finish(folder, complete=False)
    meta = json.loads((folder / "session.json").read_text())
    del meta["zip"]
    progress.write_json(folder / "session.json", meta)
    whole = made.read_bytes()
    assert package.finish(folder, complete=False) == made  # the verified zip already there is taken
    assert made.read_bytes() == whole and zips_in(data / "sessions") == [made.name]


def test_a_real_kill_while_the_zip_is_being_written_is_completed_by_the_next_start(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    folder = make_ended_folder(data, recorded_session)
    (folder / "behavior").mkdir()
    big = folder / "behavior" / "big.bin"
    with open(big, "wb") as handle:
        for _ in range(BIG // (1 << 20)):
            handle.write(os.urandom(1 << 20))
    big_sha = sha256(big)
    sessions = data / "sessions"
    args = [str(COMMAND), "--no-browser", "--data-dir", str(data), "--http-port", "0", "--replay", str(recorded_session)]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while True:  # the zip is being written once its temporary file has grown
            partials = [p for p in sessions.iterdir() if ".partial-" in p.name]
            if partials and partials[0].stat().st_size > 1 << 20:
                break
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        process.kill()
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
    assert zips_in(sessions) == []
    left = {p.name: p.stat().st_size for p in sessions.iterdir() if ".partial-" in p.name}
    assert len(left) == 1
    # files that only look alike are never touched: another session's leftover, a name that does
    # not end in 8 hex digits, a name without the leading dot
    others = {
        ".P002_20200101_000000_replay.zip.partial-0123abcd": b"another session",
        f".{folder.name}_replay.zip.partial-notes": b"not a leftover",
        f"{folder.name}_replay.zip.partial-0123abcd": b"no leading dot",
    }
    for name, body in others.items():
        (sessions / name).write_bytes(body)
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    assert any(folder.name in line for line in a.printed)
    path = wait_packaged(folder, timeout=5)
    with zipfile.ZipFile(path) as z:
        assert set(z.namelist()) == TOP | EEG | {"behavior/big.bin"}
        digest = hashlib.sha256()
        with z.open("behavior/big.bin") as member:
            for chunk in iter(lambda: member.read(1 << 20), b""):
                digest.update(chunk)
    assert digest.hexdigest() == big_sha
    # the killed run's leftover is reported and removed; nothing else is
    assert any(
        f"1 temporary zip file(s) of the session {folder.name}" in line and "were removed" in line for line in a.printed
    )
    assert not any((sessions / name).exists() for name in left)
    assert {name: (sessions / name).read_bytes() for name in others} == others


def test_a_damaged_folder_is_reported_in_plain_words_and_left_alone(app, tmp_path, recorded_session):
    data = tmp_path / "data"
    good = make_ended_folder(data, recorded_session)
    bad = make_ended_folder(data, recorded_session)
    (bad / "progress.json").write_text("{not json")
    odd = data / "sessions" / "Q9_20260101_000000"
    odd.mkdir()
    (odd / "session.json").write_text("[1, 2")
    before_bad, before_odd = files_of(bad), files_of(odd)
    a = app(replay=recorded_session, flow=SHORT, data_dir=data)
    wait_packaged(good, timeout=5)
    for folder in (bad, odd):
        (line,) = [x for x in a.printed if folder.name in x]
        assert "left as it is" in line and str(data) not in line
    assert files_of(bad) == before_bad and files_of(odd) == before_odd
    assert zips_in(data / "sessions") == [f"{good.name}_replay.zip"]


# -- large recordings ---------------------------------------------------------------------------

PEAK = """
import resource, sys
from pathlib import Path
from brain_mapper import package
package.finish(Path(sys.argv[1]), complete=False)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(peak if sys.platform == "darwin" else peak * 1024)
"""


def test_zipping_streams_the_files_instead_of_holding_them_in_memory(tmp_path, recorded_session):
    folder = make_ended_folder(tmp_path / "data", recorded_session)
    (folder / "behavior").mkdir()
    with open(folder / "behavior" / "big.bin", "wb") as handle:
        for _ in range(BIG // (1 << 20)):
            handle.write(os.urandom(1 << 20))
    script = tmp_path / "peak.py"
    script.write_text(PEAK)
    done = subprocess.run([sys.executable, str(script), str(folder)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    peak = int(done.stdout.strip())
    assert peak < 128 * 1024 * 1024, f"peak memory {peak / 2**20:.0f} MB while zipping a {BIG // 2**20} MB file"
    assert zipfile.ZipFile(wait_packaged(folder, timeout=1)).getinfo("behavior/big.bin").file_size == BIG


def test_a_large_zip_does_not_hold_up_the_server(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    accept(a)
    folder = session_folder_of(a)
    (folder / "behavior").mkdir()
    with open(folder / "behavior" / "big.bin", "wb") as handle:
        for _ in range(BIG // (1 << 20)):
            handle.write(os.urandom(1 << 20))
    server = psutil.Process(a.process.pid)
    baseline = server.memory_info().rss
    answered: list[float] = []
    peak = [baseline]
    with open_ws(a) as ws, open_ws(a, "onlooker") as watcher:
        drive_to(ws, "device")
        send(ws, cmd="stop")
        next_package(ws, "making")
        making_at = time.monotonic()
        states: list[float] = []
        done = threading.Event()

        def watch() -> None:
            while not done.is_set():
                try:
                    message = json.loads(watcher.recv(timeout=0.5))
                except TimeoutError:
                    continue
                if message["t"] == "state":
                    states.append(time.monotonic())

        reader = threading.Thread(target=watch, daemon=True)
        reader.start()
        while not json.loads((folder / "session.json").read_text()).get("zip"):
            t = time.monotonic()
            status, body, _ = get(a, "/api/health")
            assert status == 200 and json.loads(body)["ok"] is True
            answered.append(time.monotonic() - t)
            peak[0] = max(peak[0], server.memory_info().rss)
        saved_at = time.monotonic()
        done.set()
        reader.join()
        next_package(ws, "saved")
    assert saved_at - making_at > 1.0  # long enough that a blocked loop would have shown
    assert answered and max(answered) < 1.0, answered
    # the once-a-second state kept coming to the other client while the zip was made
    times = [making_at, *states, saved_at]
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert max(gaps) < 2.5, gaps
    assert peak[0] - baseline < 96 * 1024 * 1024

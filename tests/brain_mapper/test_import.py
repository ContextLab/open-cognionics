"""Loading a saved session zip on the Participant ID screen (``POST /api/import``, ``importer``).

Everything is real: the zips are made by real sessions of the app on simulated headsets (pseudo-
terminal devices, short flow), or by the app before this feature existed (the fixture
``fixtures/previous-version-session.zip``, made by ``scripts/brain_mapper_import_fixture.py`` on
commit b13a896, with every channel sample zero). Hostile files are built here with ``zipfile`` and
``struct``. Requests go to the running server over HTTP; nothing is mocked.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from test_engine import SHORT, accept, next_state, open_ws, send, session_folder_of
from test_package import start_block, wait_packaged, zip_contents
from test_participants import stop

from brain_mapper import importer
from pty_device import PtyDevice, config_reply, onhead_stream

FIXTURE = Path(__file__).parent / "fixtures" / "previous-version-session.zip"
COMMAND = Path(sys.executable).parent / "brain-mapper"


# -- helpers ---------------------------------------------------------------------------------------


def upload(a, data, *, origin="same", host=None, length=True, content_type="application/zip"):
    """``POST /api/import`` with ``data`` (bytes, or an iterable of chunks sent chunked)."""
    host_name, port = a.host_port
    connection = http.client.HTTPConnection(host_name, port, timeout=120)
    connection.putrequest("POST", "/api/import", skip_host=True)
    connection.putheader("Host", host or f"{host_name}:{port}")
    if origin == "same":
        origin = f"http://{host_name}:{port}"
    if origin is not None:
        connection.putheader("Origin", origin)
    connection.putheader("Content-Type", content_type)
    try:
        if isinstance(data, bytes) and length:
            connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
        else:
            connection.putheader("Transfer-Encoding", "chunked")
            connection.endheaders()
            for chunk in data:
                connection.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            connection.send(b"0\r\n\r\n")
    except (BrokenPipeError, ConnectionResetError):
        pass  # the server answered and closed before the whole body was sent
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    try:
        return response.status, json.loads(payload)
    except ValueError:
        return response.status, payload


def post_participant(a, body: dict):
    host_name, port = a.host_port
    connection = http.client.HTTPConnection(host_name, port, timeout=60)
    data = json.dumps(body).encode()
    connection.request(
        "POST",
        "/api/participant",
        body=data,
        headers={"Content-Type": "application/json", "Origin": f"http://{host_name}:{port}"},
    )
    response = connection.getresponse()
    answer = json.loads(response.read())
    connection.close()
    return response.status, answer


def tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root`` (and its mode), by relative path."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def session_json(folder: Path) -> dict:
    return json.loads((folder / "session.json").read_text())


def new_folder(a, before: set[str]) -> Path:
    found = [p for p in (a.data_dir / "sessions").iterdir() if p.is_dir() and p.name not in before]
    assert len(found) == 1, found
    return found[0]


def folders(a) -> set[str]:
    sessions = a.data_dir / "sessions"
    return {p.name for p in sessions.iterdir() if p.is_dir()} if sessions.exists() else set()


def stop_after_participant(a) -> None:
    """Leaves the participant step and stops the session (it is then zipped)."""
    with open_ws(a) as ws:
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
        send(ws, cmd="stop")
        next_state(ws, predicate=lambda m: m["stopped"])


def rebuilt(source: bytes, changes: dict) -> bytes:
    """``source`` (a zip) with members replaced (``bytes``) or removed (``None``)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
        for info in z.infolist():
            if info.filename in changes:
                if changes[info.filename] is not None:
                    w.writestr(info.filename, changes[info.filename])
            else:
                w.writestr(info.filename, z.read(info.filename))
    return out.getvalue()


def with_record(source: bytes, change) -> bytes:
    with zipfile.ZipFile(io.BytesIO(source)) as z:
        record = json.loads(z.read("session.json"))
    change(record)
    return rebuilt(source, {"session.json": json.dumps(record).encode()})


def plus_member(source: bytes, info: zipfile.ZipInfo, data: bytes = b"x") -> bytes:
    buffer = io.BytesIO(source)
    with zipfile.ZipFile(buffer, "a") as z:
        z.writestr(info, data)
    return buffer.getvalue()


def lying_size(real: int = 20 << 20, claimed: int = 1000) -> bytes:
    """The fixture with one more member that inflates to ``real`` bytes while its headers say ``claimed``."""
    buffer = io.BytesIO(FIXTURE.read_bytes())
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("eeg/extra.bin", bytes(real))
        offset = z.getinfo("eeg/extra.bin").header_offset
    data = bytearray(buffer.getvalue())
    # Local header: uncompressed size at offset 22; its central record: at offset 24.
    assert data[offset : offset + 4] == b"PK\x03\x04"
    struct.pack_into("<I", data, offset + 22, claimed)
    central = data.rfind(b"PK\x01\x02")
    assert data[central + 46 : central + 46 + len("eeg/extra.bin")] == b"eeg/extra.bin"
    struct.pack_into("<I", data, central + 24, claimed)
    return bytes(data)


def over_the_total_cap() -> bytes:
    """The fixture with three honest members of 3/8 of ``importer.MAX_TOTAL`` each (each under the
    member cap, together over the total cap)."""
    buffer = io.BytesIO(FIXTURE.read_bytes())
    block = bytes(16 << 20)
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for number in range(3):
            info = zipfile.ZipInfo(f"eeg/part{number}.bin")
            info.compress_type = zipfile.ZIP_DEFLATED
            with z.open(info, "w", force_zip64=True) as member:
                left = importer.MAX_TOTAL * 3 // 8
                while left:
                    part = block[: min(left, len(block))]
                    member.write(part)
                    left -= len(part)
    return buffer.getvalue()


def over_the_member_cap() -> bytes:
    """A zip with one honest member one byte longer than ``importer.MAX_MEMBER``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        info = zipfile.ZipInfo("eeg/raw.bin")
        info.compress_type = zipfile.ZIP_DEFLATED
        with z.open(info, "w", force_zip64=True) as member:
            block = bytes(16 << 20)
            left = importer.MAX_MEMBER + 1
            while left:
                part = block[: min(left, len(block))]
                member.write(part)
                left -= len(part)
    return buffer.getvalue()


# -- a whole chain: the previous version's zip, sample point 2, then 3 ---------------------------------


def test_a_zip_of_the_previous_version_starts_sample_point_2_and_that_zip_starts_sample_point_3(app, tmp_path):
    fixture = FIXTURE.read_bytes()
    with zipfile.ZipFile(FIXTURE) as z:
        old = json.loads(z.read("session.json"))
    assert "sample_point" not in old and "previous_sessions" not in old  # made before the fields existed

    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=dev.path, flow=SHORT)
        status, answer = upload(a, fixture)
        assert status == 200, answer
        assert answer["label"] == "P001" and answer["sample_point"] == 1 and answer["next_sample_point"] == 2
        assert answer["rounds_done"] == answer["rounds_total"] == 9 and answer["complete"] is True
        assert answer["started_unix"] == old["started_unix"] and answer["continue"] is None
        kept = a.data_dir / "imported" / f"{sha256(fixture)}.zip"
        assert kept.read_bytes() == fixture  # kept byte for byte
        assert folders(a) == set()  # nothing unpacked anywhere

        status, body = post_participant(a, {"label": "P001", "import": answer["token"]})
        assert status == 200 and body["uuid"] == old["uuid"]  # the same person, by the file's identifier
        second = session_folder_of(a)
        meta = session_json(second)
        assert meta["sample_point"] == 2 and meta["uuid"] == old["uuid"]
        assert meta["previous_sessions"] == [
            {
                "session_id": old["session_id"],
                "session": old["session"],
                "started_unix": old["started_unix"],
                "start_utc": meta["previous_sessions"][0]["start_utc"],
                "sample_point": 1,
                "zip_sha256": sha256(fixture),
            }
        ]
        assert meta["imported_from"] == {
            "file": f"imported/{sha256(fixture)}.zip",
            "sha256": sha256(fixture),
            "session": old["session"],
            "session_id": old["session_id"],
            "sample_point": 1,
        }
        assert meta["session_id"] != old["session_id"]
        participants = json.loads((a.data_dir / "participants.json").read_text())["participants"]
        assert participants == [{"label": "P001", "uuid": old["uuid"]}]
        stop_after_participant(a)
        zip2 = wait_packaged(second)
        record2 = json.loads(zip_contents(zip2)["session.json"])
        assert record2["sample_point"] == 2 and record2["previous_sessions"] == meta["previous_sessions"]
        assert record2["imported_from"] == meta["imported_from"]
        stop(a)
    finally:
        dev.hang_up()

    # On a third computer (a new data folder): the second zip starts sample point 3.
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT)
        second_zip = zip2.read_bytes()
        status, answer = upload(b, second_zip)
        assert status == 200, answer
        assert answer["sample_point"] == 2 and answer["next_sample_point"] == 3
        status, body = post_participant(b, {"label": "P001", "import": answer["token"]})
        assert status == 200 and body["uuid"] == old["uuid"]
        meta3 = session_json(session_folder_of(b))
        assert meta3["sample_point"] == 3
        assert [(e["session_id"], e["sample_point"]) for e in meta3["previous_sessions"]] == [
            (old["session_id"], 1),
            (meta["session_id"], 2),
        ]
        assert meta3["previous_sessions"][0]["zip_sha256"] == sha256(fixture)
        assert meta3["previous_sessions"][1]["zip_sha256"] == sha256(second_zip)
        assert meta3["imported_from"]["sha256"] == sha256(second_zip)
        stop(b)
    finally:
        dev.hang_up()


# -- stopped and ended-early sessions; the continue offer ------------------------------------------


def test_stopped_and_ended_early_zips_are_accepted_and_continue_is_offered_only_for_that_unfinished_folder(app):
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=dev.path, flow=SHORT)
        accept(a, "Stopper")
        with open_ws(a) as ws:
            start_block(ws)  # words, block 1 running
            next_state(ws, predicate=lambda m: m["phase"] == "between")
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m: m["phase"] == "between" and m["blocks_done"] == 2)
            send(ws, cmd="advance")  # on to perception, waiting
            next_state(ws, predicate=lambda m: m["step"] == "perception")
            send(ws, cmd="stop")
            next_state(ws, predicate=lambda m: m["stopped"])
        stopped_folder = session_folder_of(a)
        stopped_zip = wait_packaged(stopped_folder).read_bytes()
        stop(a)
    finally:
        dev.hang_up()

    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT, data_dir=a.data_dir)
        before = tree_hashes(a.data_dir / "sessions")
        status, answer = upload(b, stopped_zip)
        assert status == 200, answer
        assert answer["ending"] == "stopped" and answer["rounds_done"] == 1 and answer["complete"] is False
        # The same session's folder is here, unfinished: the existing continue offer comes with it.
        assert answer["continue"]["session"] == stopped_folder.name
        assert answer["continue"]["next_step"] == "perception" and answer["continue"]["points_total"] == 200
        assert answer["next_sample_point"] == 2
        assert tree_hashes(a.data_dir / "sessions") == before  # nothing changed by the import
        status, body = post_participant(b, {"label": "Stopper", "continues": stopped_folder.name})
        assert status == 200
        continued = new_folder(b, {stopped_folder.name})
        meta = session_json(continued)
        assert meta["continues"] == stopped_folder.name
        assert meta["sample_point"] == 1 and meta["previous_sessions"] == []  # the same sample point
        changed = tree_hashes(a.data_dir / "sessions")
        assert {k: v for k, v in changed.items() if k in before} == before  # the earlier folder is as it was
        # The ended-early session: the headset hangs up during a block, which pauses it (since
        # 2026-11-25; until then the hang-up ended the session), and the app is then
        # stopped (Ctrl-C), which ends the session early.
        with open_ws(b) as ws:
            start_block(ws, "perception")
            dev.hang_up()
            next_state(ws, predicate=lambda m: m["phase"] == "paused", timeout=30)
        stop(b)
        assert session_json(continued)["ended_early"] is True
        ended_zip = wait_packaged(continued).read_bytes()
    finally:
        dev.hang_up()

    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        c = app(port=dev.path, flow=SHORT)  # another computer: no folder of that session here
        status, answer = upload(c, ended_zip)
        assert status == 200, answer
        assert answer["ending"] == "ended_early" and answer["continue"] is None
        assert answer["label"] == "Stopper" and answer["next_sample_point"] == 2
        status, body = post_participant(c, {"label": "Stopper", "import": answer["token"]})
        assert status == 200
        meta = session_json(session_folder_of(c))
        assert meta["sample_point"] == 2 and meta["imported_from"]["sha256"] == sha256(ended_zip)
        stop(c)
    finally:
        dev.hang_up()


# -- refusals ----------------------------------------------------------------------------------------


def hostile_files() -> dict[str, bytes]:
    fixture = FIXTURE.read_bytes()
    symlink = zipfile.ZipInfo("eeg/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    duplicate = io.BytesIO(fixture)
    with zipfile.ZipFile(duplicate, "a") as z:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            z.writestr("session.json", b"{}")
    return {
        "a text file": b"label,P001\nthis is not a zip\n",
        "an empty file": b"",
        "a truncated zip": fixture[: len(fixture) // 2],
        "a zip with ../ in a name": plus_member(fixture, zipfile.ZipInfo("../outside.txt")),
        "a zip with a nested ../": plus_member(fixture, zipfile.ZipInfo("eeg/../../outside.txt")),
        "a zip with an absolute name": plus_member(fixture, zipfile.ZipInfo("/tmp/outside.txt")),
        "a zip with a drive letter": plus_member(fixture, zipfile.ZipInfo("C:/outside.txt")),
        "a zip with a backslash": plus_member(fixture, zipfile.ZipInfo("eeg\\..\\outside.txt")),
        "a zip with a symlink member": plus_member(fixture, symlink, b"/etc/passwd"),
        "a zip with a name twice": duplicate.getvalue(),
        "a member that inflates past its stated size": lying_size(),
        "a member over the size cap": over_the_member_cap(),
        "members over the total size cap": over_the_total_cap(),
        "a zip without session.json": rebuilt(fixture, {"session.json": None}),
        "a label that is a number": with_record(fixture, lambda r: r.update(label=123)),
        "a huge label": with_record(fixture, lambda r: r.update(label="A" * 100_000)),
        "a uuid that is not one": with_record(fixture, lambda r: r.update(uuid="not-a-uuid")),
        "a start time that is text": with_record(fixture, lambda r: r.update(started_unix="yesterday")),
        "a huge title": with_record(fixture, lambda r: r["flow"][3].update(title="T" * 1_000_000)),
        "a sample point that is true": with_record(fixture, lambda r: r.update(sample_point=True)),
        "previous sessions that are text": with_record(fixture, lambda r: r.update(previous_sessions="x" * 5000)),
        "session.json that is a list": rebuilt(fixture, {"session.json": b"[1, 2, 3]"}),
        "session.json nested very deep": rebuilt(fixture, {"session.json": b"[" * 100_000 + b"]" * 100_000}),
    }


def test_hostile_and_foreign_files_are_refused_in_plain_words_and_nothing_is_kept(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    for what, data in hostile_files().items():
        status, answer = upload(a, data)
        assert status in (400, 413), (what, status, answer)
        assert answer["code"] in ("not_a_zip", "damaged", "unsafe", "not_ours"), (what, answer)
        words = answer["error"]
        assert "outside" not in words and "passwd" not in words and "AAAA" not in words and "TTTT" not in words
        assert "P001" not in words and "{" not in words, (what, words)
        assert "token" not in answer and "label" not in answer
    # A declared length over the limit is refused before the body is read...
    host_name, port = a.host_port
    connection = http.client.HTTPConnection(host_name, port, timeout=30)
    connection.putrequest("POST", "/api/import", skip_host=True)
    connection.putheader("Host", f"{host_name}:{port}")
    connection.putheader("Origin", f"http://{host_name}:{port}")
    connection.putheader("Content-Type", "application/zip")
    connection.putheader("Content-Length", str(importer.MAX_UPLOAD + 1))
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413 and json.loads(response.read())["code"] == "too_large"
    connection.close()
    # ... and a body sent without a length is abandoned once it passes the limit.
    block = bytes(8 << 20)
    chunks = (block for _ in range(importer.MAX_UPLOAD // len(block) + 2))
    status, answer = upload(a, chunks, length=False)
    assert status == 413 and answer["code"] == "too_large"
    # Nothing was kept or made in the data folder.
    assert not (a.data_dir / "imported").exists() and folders(a) == set()
    assert not (a.data_dir / "participants.json").exists()
    # The server still answers, and a good file is still accepted.
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 200 and answer["label"] == "P001"


def test_a_post_from_another_origin_or_with_a_foreign_host_is_refused(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    fixture = FIXTURE.read_bytes()
    host_name, port = a.host_port
    assert upload(a, fixture, origin="http://evil.example")[0] == 403
    assert upload(a, fixture, origin=None)[0] == 403
    assert upload(a, fixture, origin=f"http://127.0.0.1:{port + 1}")[0] == 403
    assert upload(a, fixture, host="evil.example")[0] == 403
    assert upload(a, fixture, host=f"evil.example:{port}")[0] == 403
    assert not (a.data_dir / "imported").exists()
    # A GET is not an upload.
    connection = http.client.HTTPConnection(host_name, port, timeout=10)
    connection.request("GET", "/api/import")
    assert connection.getresponse().status == 405
    connection.close()


def test_a_label_used_here_by_another_person_is_not_merged_with_the_file_s_person(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    other = accept(a, "P001")["uuid"]  # someone else typed P001 on this computer
    with zipfile.ZipFile(FIXTURE) as z:
        assert json.loads(z.read("session.json"))["uuid"] != other
    before = tree_hashes(a.data_dir)
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 409 and answer["code"] in ("participant_conflict", "already_set"), answer
    stop(a)
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT, data_dir=a.data_dir)
        status, answer = upload(b, FIXTURE.read_bytes())
        assert status == 409 and answer["code"] == "participant_conflict", answer
        assert "P001" not in answer["error"]
        assert not (a.data_dir / "imported").exists()
        assert {k: v for k, v in tree_hashes(a.data_dir).items() if k in before and not k.startswith("sessions/")} == {
            k: v for k, v in before.items() if not k.startswith("sessions/")
        }
        stop(b)
    finally:
        dev.hang_up()


def test_a_token_is_needed_and_the_label_must_be_the_file_s(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 200
    assert post_participant(a, {"label": "P001", "import": "f" * 32})[1]["code"] == "unknown_import"
    assert post_participant(a, {"label": "P002", "import": answer["token"]})[1]["code"] == "import_mismatch"
    assert post_participant(a, {"label": "P001", "import": 5})[1]["code"] == "bad_body"
    assert post_participant(a, {"label": "P001", "import": answer["token"], "continues": "x"})[1]["code"] == "bad_selection"
    assert folders(a) == set()
    assert post_participant(a, {"label": "P001", "import": answer["token"]})[0] == 200
    # Once a session runs, a file is not loaded any more.
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 409 and answer["code"] == "already_set"


# -- nothing of the label in the log; nothing to the onlooker ------------------------------------


def test_the_label_is_in_no_log_line_and_the_onlooker_is_told_nothing_of_the_import(tmp_path):
    label = "Zq7Xlabel"
    dev = PtyDevice(onhead_stream(), config_reply())
    log = tmp_path / "log.txt"
    data = tmp_path / "data"

    def start():
        earlier = log.read_text().count("brain-mapper at ") if log.exists() else 0
        handle = open(log, "a")
        process = subprocess.Popen(
            [str(COMMAND), "--no-browser", "--data-dir", str(data), "--http-port", "0", "--port", dev.path, "--flow", str(SHORT)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 30
        while log.read_text().count("brain-mapper at ") == earlier:
            assert time.monotonic() < deadline and process.poll() is None, log.read_text()
            time.sleep(0.1)
        url = [line for line in log.read_text().splitlines() if line.startswith("brain-mapper at ")][-1]
        return process, handle, url.removeprefix("brain-mapper at ")

    class A:
        def __init__(self, url):
            self.url = url
            self.data_dir = data

        @property
        def host_port(self):
            host, _, port = self.url.removeprefix("http://").partition(":")
            return host, int(port)

    try:
        process, handle, url = start()
        a = A(url)
        accept(a, label)
        folder = session_folder_of(a)
        stop_after_participant(a)
        made = wait_packaged(folder).read_bytes()
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
        handle.close()
        shutil.rmtree(data / "sessions")  # the test's own folder: a new computer for the second run
        (data / "participants.json").unlink()

        process, handle, url = start()
        a = A(url)
        with open_ws(a, role="onlooker") as onlooker:
            status, answer = upload(a, made)
            assert status == 200 and answer["label"] == label
            refused = upload(a, b"not a zip " + label.encode())
            assert refused[0] == 400
            status, body = post_participant(a, {"label": label, "import": answer["token"]})
            assert status == 200
            seen = []
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                try:
                    seen.append(onlooker.recv(timeout=0.5))
                except TimeoutError:
                    continue
        assert seen, "the onlooker got no message at all"
        for text in seen:
            assert label not in text and answer["token"] not in text and "Saved session" not in text
            assert sha256(made) not in text
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
        handle.close()
    finally:
        dev.hang_up()
    text = log.read_text()
    assert "brain-mapper at " in text
    assert label not in text, text


def test_a_new_session_typed_on_the_same_computer_is_the_next_sample_point(app, pty_device, recorded_session):
    a = app(port=pty_device.path, flow=SHORT)
    first = accept(a, "P007")["uuid"]
    folder1 = session_folder_of(a)
    meta1 = session_json(folder1)
    assert meta1["sample_point"] == 1 and meta1["previous_sessions"] == []
    stop(a)
    b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
    assert accept(b, "P007")["uuid"] == first
    meta2 = session_json(new_folder(b, {folder1.name}))
    assert meta2["sample_point"] == 2 and "imported_from" not in meta2
    assert [(e["session_id"], e["session"], e["sample_point"]) for e in meta2["previous_sessions"]] == [
        (meta1["session_id"], folder1.name, 1)
    ]


# -- one identifier, one label ----------------------------------------------------------------------


def relabelled(label: str) -> bytes:
    """The fixture as if its person had been saved under ``label`` (the same identifier)."""

    def change(record):
        record["session"] = record["session"].replace("P001", label, 1)
        record["label"] = record["participant"] = label

    return with_record(FIXTURE.read_bytes(), change)


def write_participants(data_dir: Path, entries: list[dict]) -> None:
    """``participants.json`` as brain-mapper writes it (as another run on this folder left it)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "participants.json").write_text(json.dumps({"version": 1, "participants": entries}, indent=2) + "\n")


def fixture_uuid() -> str:
    with zipfile.ZipFile(FIXTURE) as z:
        return json.loads(z.read("session.json"))["uuid"]


def test_a_file_whose_identifier_is_stored_here_under_another_label_is_refused_when_loaded(app):
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=dev.path, flow=SHORT)
        status, answer = upload(a, relabelled("Q123"))
        assert status == 200 and answer["label"] == "Q123"
        assert post_participant(a, {"label": "Q123", "import": answer["token"]})[0] == 200
        stop(a)
    finally:
        dev.hang_up()
    assert json.loads((a.data_dir / "participants.json").read_text())["participants"] == [
        {"label": "Q123", "uuid": fixture_uuid()}
    ]
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT, data_dir=a.data_dir)
        before = tree_hashes(a.data_dir)
        status, answer = upload(b, FIXTURE.read_bytes())  # P001, the same identifier
        assert status == 409 and answer["code"] == "participant_conflict", answer
        assert "P001" not in answer["error"] and "Q123" not in answer["error"] and "token" not in answer
        assert tree_hashes(a.data_dir) == before  # nothing written, no session folder changed
        stop(b)
    finally:
        dev.hang_up()


def test_an_identifier_stored_under_another_label_after_loading_is_refused_when_starting(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 200
    # Another run on this folder has since saved the same identifier under another label.
    write_participants(a.data_dir, [{"label": "Q123", "uuid": fixture_uuid()}])
    before = tree_hashes(a.data_dir)
    status, body = post_participant(a, {"label": "P001", "import": answer["token"]})
    assert status == 409 and body["code"] == "participant_conflict", body
    assert "P001" not in body["error"] and "Q123" not in body["error"]
    assert folders(a) == set() and tree_hashes(a.data_dir) == before


def test_a_file_whose_label_and_identifier_are_both_stored_here_is_accepted(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    write_participants(a.data_dir, [{"label": "Q9", "uuid": "0f0e0d0c-0b0a-4908-8706-050403020100"}, {"label": "P001", "uuid": fixture_uuid()}])
    status, answer = upload(a, FIXTURE.read_bytes())
    assert status == 200, answer
    status, body = post_participant(a, {"label": "P001", "import": answer["token"]})
    assert status == 200 and body["uuid"] == fixture_uuid()
    stored = json.loads((a.data_dir / "participants.json").read_text())["participants"]
    assert stored[0] == {"label": "P001", "uuid": fixture_uuid()} and len(stored) == 2


def test_two_files_of_one_identifier_under_two_labels_sent_at_once_load_at_most_once(app, pty_device):
    import threading

    a = app(port=pty_device.path, flow=SHORT)
    files = [FIXTURE.read_bytes(), relabelled("Q123")]
    winners: list[str] = []
    for _ in range(3):  # a few rounds, each with both sent at the same moment
        results: list = [None, None]
        gate = threading.Barrier(2)

        def send_one(i):
            gate.wait()
            results[i] = upload(a, files[i])

        threads = [threading.Thread(target=send_one, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        codes = sorted(status for status, _ in results)
        assert codes == [200, 409], results
        refused = next(answer for status, answer in results if status == 409)
        assert refused["code"] == "participant_conflict"
        winners.append(next(answer["label"] for status, answer in results if status == 200))
    assert len(set(winners)) == 1  # the identifier stays with the label that loaded first
    # Whichever label won, only that one can start a session; the identifier stays with one label.
    status, answer = upload(a, files[0])
    other_status, other = upload(a, files[1])
    assert sorted([status, other_status]) == [200, 409]

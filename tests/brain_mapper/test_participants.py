"""The participant ID: the store, the two routes, the page, and when recording starts.

Everything is real: files in a temporary folder, the ``brain-mapper`` command as a
subprocess on a pty device or a replayed recording, and Chromium. The labels are
made up (``P001``, ``P002``, ``P003``).
"""

from __future__ import annotations

import http.client
import json
import signal
import threading
import uuid
from pathlib import Path

import pytest

from brain_mapper.participants import RULE, IdentityTaken, Store, StoreDamaged, valid
from test_engine import SHORT, events_of, markers_of, next_state, open_ws, send

SCREEN = "#screen"
WEB = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "web"

#: Labels that must be refused everywhere: path pieces, empty, too long, spaces,
#: control characters and characters that only look like letters or digits.
HOSTILE = [
    "../x",
    "..",
    ".",
    "a/b",
    "a\\b",
    "/etc/passwd",
    "~",
    "",
    "a" * 33,
    "a b",
    " P001",
    "P001 ",
    "P001\n",
    "\nP001",
    "a\x00b",
    "a\tb",
    "a\x1bb",
    "a\x7fb",
    "P001.json",
    "P:001",
    "Р001",  # Cyrillic Er, which looks like P
    "P００１",  # fullwidth digits
    "Ｐ001",  # fullwidth P
    "P٠١",  # Arabic-Indic digits
    "café",
    "P001‮",  # right-to-left override
    "P001​",  # zero-width space
    "K001",  # Kelvin sign, which casefolds to k
    "-x",  # a first character that is not a letter or digit is refused (awkward on a command line)
    "_x",
    "-",
    "_",
    "--help",
]
GOOD = ["P001", "p_1", "a-b", "A", "0", "x" * 32, "9_-"]


# -- valid ----------------------------------------------------------------------------


@pytest.mark.parametrize("label", GOOD)
def test_valid_accepts_one_to_32_ascii_letters_digits_underscores_and_hyphens(label):
    assert valid(label) is True


@pytest.mark.parametrize("label", HOSTILE)
def test_valid_refuses_hostile_and_malformed_labels(label):
    assert valid(label) is False


@pytest.mark.parametrize("label", [None, 1, 1.5, b"P001", ["P001"], {"label": "P001"}])
def test_valid_refuses_what_is_not_a_string(label):
    assert valid(label) is False


def test_the_rule_is_given_in_words():
    assert "1 to 32" in RULE and "letters" in RULE and "digits" in RULE


# -- the store -------------------------------------------------------------------------


def test_the_same_label_gives_the_same_identifier_across_two_stores_on_one_file(tmp_path):
    path = tmp_path / "participants.json"
    first = Store(path).identify("P001")
    second = Store(path).identify("P001")
    assert first == second
    assert uuid.UUID(first).version == 4 and str(uuid.UUID(first)) == first


def test_two_labels_give_different_identifiers(tmp_path):
    store = Store(tmp_path / "participants.json")
    assert store.identify("P001") != store.identify("P002")


def test_two_machines_with_the_same_label_get_different_identifiers(tmp_path):
    assert Store(tmp_path / "a.json").identify("P001") != Store(tmp_path / "b.json").identify("P001")


def test_labels_are_most_recently_used_first(tmp_path):
    store = Store(tmp_path / "participants.json")
    assert store.labels() == []
    for label in ("P001", "P002", "P003"):
        store.identify(label)
    assert store.labels() == ["P003", "P002", "P001"]
    store.identify("P001")
    assert Store(tmp_path / "participants.json").labels() == ["P001", "P003", "P002"]


def test_using_a_label_again_keeps_its_identifier(tmp_path):
    store = Store(tmp_path / "participants.json")
    ids = {label: store.identify(label) for label in ("P001", "P002")}
    store.identify("P001")
    assert {label: store.identify(label) for label in ("P001", "P002")} == ids


def test_the_file_is_written_by_replace_rename_and_leaves_nothing_else_behind(tmp_path):
    path = tmp_path / "participants.json"
    store = Store(path)
    store.identify("P001")
    inode = path.stat().st_ino
    store.identify("P002")
    assert path.stat().st_ino != inode  # a new file was renamed over the old one
    assert [p.name for p in tmp_path.iterdir()] == ["participants.json"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [p["label"] for p in stored["participants"]] == ["P002", "P001"]


def test_a_missing_file_is_an_empty_list_and_is_created_by_the_first_label(tmp_path):
    path = tmp_path / "deeper" / "participants.json"
    store = Store(path)
    assert store.labels() == []
    assert not path.exists()
    store.identify("P001")
    assert path.is_file()


@pytest.mark.parametrize("label", HOSTILE)
def test_the_store_refuses_a_hostile_label_and_writes_nothing(label, tmp_path):
    path = tmp_path / "participants.json"
    with pytest.raises(ValueError, match="1 to 32"):
        Store(path).identify(label)
    assert list(tmp_path.iterdir()) == []


def test_one_store_gives_one_identifier_to_threads_using_the_same_label(tmp_path):
    store = Store(tmp_path / "participants.json")
    found: list[str] = []
    threads = [threading.Thread(target=lambda: found.append(store.identify("P001"))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(found)) == 1 and len(found) == 8


GOOD_ID = str(uuid.uuid4())
DAMAGED = {
    "not json": b"{ this is not json",
    "empty file": b"",
    "binary": b"\xff\xfe\x00\x01",
    "a list": b'["P001"]',
    "no participants key": b'{"version": 1}',
    "entry without uuid": b'{"version": 1, "participants": [{"label": "P001"}]}',
    "uuid that is not one": b'{"version": 1, "participants": [{"label": "P001", "uuid": "abc"}]}',
    "label that is not valid": (
        b'{"version": 1, "participants": [{"label": "../x", "uuid": "%s"}]}' % GOOD_ID.encode()
    ),
    "label twice": (
        b'{"version": 1, "participants": [{"label": "P001", "uuid": "%s"},'
        b' {"label": "P001", "uuid": "%s"}]}' % (GOOD_ID.encode(), str(uuid.uuid4()).encode())
    ),
    "unknown version": b'{"version": 2, "participants": []}',
}


@pytest.mark.parametrize("name", list(DAMAGED))
def test_a_damaged_file_is_reported_in_words_and_never_overwritten(name, tmp_path):
    path = tmp_path / "participants.json"
    path.write_bytes(DAMAGED[name])
    store = Store(path)
    with pytest.raises(StoreDamaged) as error:
        store.labels()
    text = str(error.value)
    assert "participants.json" in text and "not been changed" in text
    assert "Traceback" not in text
    with pytest.raises(StoreDamaged):
        store.identify("P001")
    with pytest.raises(StoreDamaged):
        Store(path).identify("P002")
    assert path.read_bytes() == DAMAGED[name]
    assert [p.name for p in tmp_path.iterdir()] == ["participants.json"]


def test_a_file_that_cannot_be_read_is_reported_and_left_alone(tmp_path):
    path = tmp_path / "participants.json"
    path.mkdir()  # a folder where the file should be: reading it fails with an OS error
    with pytest.raises(StoreDamaged, match="participants.json"):
        Store(path).labels()
    assert path.is_dir()


# -- the routes --------------------------------------------------------------------------


def request(a, method, path, body=None, host=None, origin="same", raw=None):
    """One real HTTP request; returns ``(status, parsed JSON body)``."""
    host_name, port = a.host_port
    connection = http.client.HTTPConnection(host_name, port, timeout=10)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host or f"{host_name}:{port}")
    if origin == "same":
        origin = f"http://{host_name}:{port}"
    if origin is not None and method != "GET":
        connection.putheader("Origin", origin)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    if method != "GET":
        connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(len(data)))
    connection.endheaders(data)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    try:
        return response.status, json.loads(payload)
    except ValueError:
        return response.status, payload


def post_label(a, label):
    return request(a, "POST", "/api/participant", {"label": label})


def files_under(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


def session_folder(a) -> Path:
    found = [p for p in (a.data_dir / "sessions").iterdir() if p.is_dir()]
    assert len(found) == 1, found
    return found[0]


def stop(a) -> None:
    """Ends the app with Ctrl-C so the recording is completed."""
    a.process.send_signal(signal.SIGINT)
    a.process.wait(timeout=15)


def test_get_lists_the_remembered_labels_most_recent_first(app, pty_device, recorded_session):
    a = app(port=pty_device.path, flow=SHORT)
    assert request(a, "GET", "/api/participants") == (200, {"labels": []})
    assert post_label(a, "P001")[0] == 200
    stop(a)
    # a session belongs to its first label, so each further label comes with a later run
    for label in ("P002", "P003"):
        b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
        assert post_label(b, label)[0] == 200
        stop(b)
    c = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
    assert request(c, "GET", "/api/participants") == (200, {"labels": ["P003", "P002", "P001"]})


def test_post_returns_the_label_and_a_stable_identifier_and_the_file_is_on_disk(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    status, body = post_label(a, "P001")
    assert status == 200 and set(body) == {"label", "uuid"} and body["label"] == "P001"
    assert str(uuid.UUID(body["uuid"])) == body["uuid"]
    assert request(a, "GET", "/api/participants") == (200, {"labels": ["P001"]})
    stored = json.loads((a.data_dir / "participants.json").read_text(encoding="utf-8"))
    assert stored["participants"][0]["label"] == "P001"
    assert stored["participants"][0]["uuid"] == body["uuid"]
    # the same label again: the same answer
    assert post_label(a, "P001") == (200, body)


def test_the_same_label_keeps_its_identifier_in_a_later_run_on_the_same_data_dir(
    app, pty_device, recorded_session
):
    a = app(port=pty_device.path, flow=SHORT)
    _, first = post_label(a, "P001")
    stop(a)
    b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
    assert request(b, "GET", "/api/participants") == (200, {"labels": ["P001"]})
    assert post_label(b, "P001") == (200, first)


def test_a_second_label_in_a_session_that_has_one_is_refused_with_the_first_named(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    _, first = post_label(a, "P001")
    status, body = post_label(a, "P002")
    assert status == 409 and body["code"] == "already_set" and "P001" in body["error"]
    assert request(a, "GET", "/api/participants") == (200, {"labels": ["P001"]})
    assert post_label(a, "P001") == (200, first)


# -- nothing is recorded until a label is accepted ---------------------------------------


def test_nothing_is_saved_before_a_label_is_accepted(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        first = next_state(ws)
        assert (first["step"], first["phase"]) == ("participant", "waiting")
        assert first["rev"] == 0 and first["ended_early"] is False and first["points_total"] == 0
        next_state(ws)  # the heartbeat: the session has not started and the state is still sent
        assert files_under(a.data_dir / "sessions") == []
        assert not (a.data_dir / "sessions" / "working").exists()
        assert [p for p in (a.data_dir / "sessions").iterdir()] == []
        assert not (a.data_dir / "participants.json").exists()


def test_a_command_before_a_label_is_refused_and_changes_nothing(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        send(ws, cmd="advance")
        send(ws, cmd="event", name="x", payload={}, browser_time=1.0, time_origin=1.0)
        errors = []
        while len(errors) < 2:
            message = json.loads(ws.recv(timeout=10))
            if message["t"] == "error":
                errors.append(message)
        assert all(e["kind"] == "refused" and "participant" in e["msg"] for e in errors)
        assert next_state(ws)["step"] == "participant"
    assert files_under(a.data_dir / "sessions") == []


def test_accepting_a_label_creates_the_session_folder_and_records_with_the_label(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        assert files_under(a.data_dir / "sessions") == []
        status, body = post_label(a, "P001")
        assert status == 200
        folder = session_folder(a)
        assert folder.name.startswith("P001_") and not (a.data_dir / "sessions" / "working").exists()
        meta = json.loads((folder / "eeg" / "meta.json").read_text(encoding="utf-8"))
        assert meta["participant"] == "P001" and meta["participant_uuid"] == body["uuid"]
        # the first marker is the start of the first step, made when the session began
        assert [m["label"] for m in markers_of(folder / "eeg")] == ["participant/step_start#1"]
        assert [e["name"] for e in events_of(folder)] == ["step_start"]
        # markers and events from now on land in it
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
        send(ws, cmd="event", name="clicked", payload={"x": 1}, browser_time=1.0, time_origin=2.0)
        next_state(ws, predicate=lambda m: m["rev"] >= 3)
    labels = [m["label"] for m in markers_of(folder / "eeg")]
    assert labels[:3] == ["participant/step_start#1", "device/step_start#2", "device/clicked#3"]
    assert [e["name"] for e in events_of(folder)] == ["step_start", "step_start", "clicked"]
    assert (folder / "eeg" / "raw.bin").is_file()


def test_the_bdf_header_carries_the_label(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    post_label(a, "P001")
    folder = session_folder(a)
    stop(a)
    header = (folder / "eeg" / "recording.bdf").read_bytes()[:256]
    assert header[8:88].decode("ascii").startswith("P001 ")


def test_a_replay_session_gets_its_folder_when_the_label_is_accepted(app, recorded_session):
    a = app(replay=recorded_session, flow=SHORT)
    assert files_under(a.data_dir / "sessions") == []
    status, body = post_label(a, "P001")
    assert status == 200
    folder = session_folder(a)
    assert (folder / "eeg" / "markers.jsonl").is_file() and (folder / "events.jsonl").is_file()
    assert (folder / "eeg" / "raw.bin").is_file()


def test_a_label_after_the_link_ended_is_refused_and_nothing_is_saved(app, recorded_session):
    a = app(replay=recorded_session, flow=SHORT)
    with open_ws(a) as ws:
        while True:
            message = json.loads(ws.recv(timeout=60))
            if message["t"] == "link" and message["state"] == "ended":
                break
    status, body = post_label(a, "P001")
    assert status == 409 and body["code"] == "link_ended" and "ended" in body["error"]
    assert files_under(a.data_dir / "sessions") == []
    assert not (a.data_dir / "participants.json").exists()


# -- hostile labels never reach a path -----------------------------------------------------


def snapshot(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_the_post_refuses_every_hostile_label_with_the_rule_and_creates_nothing(
    app, pty_device, tmp_path
):
    """One app start for all cases; every case is checked on its own."""
    a = app(port=pty_device.path, flow=SHORT)
    before = snapshot(tmp_path)
    for label in HOSTILE:
        status, body = post_label(a, label)
        assert status == 400, label
        assert body["code"] == "invalid_label" and RULE in body["error"], label
        assert snapshot(tmp_path) == before, label
        assert files_under(a.data_dir / "sessions") == [], label
        assert not (a.data_dir / "participants.json").exists(), label
    assert request(a, "GET", "/api/participants") == (200, {"labels": []})


@pytest.mark.parametrize(
    "raw, wanted_status, wanted_code",
    [
        (b"", 400, "bad_body"),
        (b"not json", 400, "bad_body"),
        (b"[]", 400, "bad_body"),
        (b'"P001"', 400, "bad_body"),
        (b"{}", 400, "bad_body"),
        (b'{"label": 1}', 400, "bad_body"),
        (b'{"label": null}', 400, "bad_body"),
        (b'{"label": ["P001"]}', 400, "bad_body"),
        (b'{"other": "P001"}', 400, "bad_body"),
        (b'{"label": ""}', 400, "invalid_label"),
        # longer than the 1024 bytes that are read: refused as too long (before round 1 this
        # was a 400 for an invalid label; the answer is now the more exact 413)
        (b'{"label": "' + b"a" * 100_000 + b'"}', 413, "body_too_large"),
    ],
)
def test_the_post_refuses_a_body_that_is_not_a_label_object(
    raw, wanted_status, wanted_code, app, pty_device
):
    a = app(port=pty_device.path, flow=SHORT)
    status, body = request(a, "POST", "/api/participant", raw=raw)
    assert status == wanted_status and "error" in body
    assert body["code"] == wanted_code
    assert files_under(a.data_dir / "sessions") == []
    assert not (a.data_dir / "participants.json").exists()


def test_the_new_routes_check_host_and_origin(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    _, port = a.host_port
    for host in (f"example.com:{port}", "example.com", f"127.0.0.1.example.com:{port}"):
        assert request(a, "GET", "/api/participants", host=host)[0] == 403
        assert request(a, "POST", "/api/participant", {"label": "P001"}, host=host)[0] == 403
    for origin in (None, "http://example.com", f"http://127.0.0.1:{port + 1}", "null"):
        assert request(a, "POST", "/api/participant", {"label": "P001"}, origin=origin)[0] == 403
    assert files_under(a.data_dir / "sessions") == []
    assert not (a.data_dir / "participants.json").exists()
    assert post_label(a, "P001")[0] == 200


# -- a damaged participants.json -----------------------------------------------------------


def test_a_damaged_file_stops_the_routes_with_a_plain_message_and_is_not_overwritten(
    app, pty_device, tmp_path
):
    data = tmp_path / "damaged-data"
    data.mkdir()
    (data / "participants.json").write_bytes(b"{ this is not json")
    a = app(port=pty_device.path, flow=SHORT, data_dir=data)
    status, body = request(a, "GET", "/api/participants")
    assert status == 500 and body["code"] == "store_damaged"
    assert "participants.json" in body["error"] and "not been changed" in body["error"]
    assert body["labels"] == []
    status, body = post_label(a, "P001")
    assert status == 500 and body["code"] == "store_damaged" and "participants.json" in body["error"]
    assert (data / "participants.json").read_bytes() == b"{ this is not json"
    assert files_under(data / "sessions") == []
    assert [p.name for p in data.iterdir() if p.name != "sessions"] == ["participants.json"]


# -- the page ---------------------------------------------------------------------------------


def begin(a, page) -> None:
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')


def test_typing_a_label_and_continuing_reaches_the_device_screen_and_the_label_is_offered_next_time(
    app, pty_device, recorded_session, page, browser
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    assert page.locator("#participant-label").count() == 1
    assert page.locator(".remembered button").count() == 0
    page.fill("#participant-label", "P001")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert session_folder(a).name.startswith("P001_")
    stop(a)
    b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
    context = browser.new_context()
    try:
        second = context.new_page()
        begin(b, second)
        buttons = second.locator(".remembered button")
        assert buttons.all_inner_texts() == ["P001"]
        buttons.first.click()
        assert second.input_value("#participant-label") == "P001"
        second.click("#advance")
        second.wait_for_selector(f'{SCREEN}[data-screen="device"]')
        assert json.loads((b.data_dir / "participants.json").read_text())["participants"][0][
            "uuid"
        ] == json.loads((a.data_dir / "participants.json").read_text())["participants"][0]["uuid"]
    finally:
        context.close()


def test_the_screen_says_where_the_list_is_kept(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    text = page.inner_text(f"{SCREEN} .note")
    assert "file on this computer" in text


@pytest.mark.parametrize("label", ["a b", "../x", "a" * 33, "Р001"])
def test_the_page_shows_the_rule_for_a_refused_label_and_stays_on_the_screen(
    label, app, pty_device, page
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page.fill("#participant-label", label)
    page.click("#advance")
    page.wait_for_function(
        "document.getElementById('participant-problem').textContent.includes('1 to 32')"
    )
    assert page.get_attribute(f"{SCREEN}", "data-screen") == "participant"
    assert page.get_attribute("#participant-label", "aria-invalid") == "true"
    assert files_under(a.data_dir / "sessions") == []
    assert not (a.data_dir / "participants.json").exists()


def test_an_empty_label_is_refused_on_the_page(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page.click("#advance")
    page.wait_for_function(
        "document.getElementById('participant-problem').textContent.includes('1 to 32')"
    )
    assert files_under(a.data_dir / "sessions") == []


def test_a_damaged_participants_file_is_explained_on_the_page_and_not_overwritten(
    app, pty_device, page, tmp_path
):
    data = tmp_path / "damaged-data"
    data.mkdir()
    (data / "participants.json").write_bytes(b"[1, 2")
    a = app(port=pty_device.path, flow=SHORT, data_dir=data)
    begin(a, page)
    page.wait_for_function(
        "document.getElementById('participant-problem').textContent.includes('participants.json')"
    )
    text = page.inner_text("#participant-problem")
    assert "not been changed" in text
    # Continue is switched off: pressing it again could not help
    assert page.is_disabled("#advance")
    assert page.get_attribute(f"{SCREEN}", "data-screen") == "participant"
    assert (data / "participants.json").read_bytes() == b"[1, 2"
    assert files_under(data / "sessions") == []


def test_the_page_never_puts_the_label_into_the_address_or_a_log_line(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    requested: list[str] = []
    page.on("request", lambda r: requested.append(r.url))
    messages: list[str] = []
    page.on("console", lambda m: messages.append(m.text))
    begin(a, page)
    page.fill("#participant-label", "P001")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert not any("P001" in url for url in requested)
    assert not any("P001" in text for text in messages)
    assert all(url.startswith(a.url) or url.startswith("ws://127.0.0.1") for url in requested)


# -- fix round 1: peek, labels' first character -----------------------------------------------


def test_peek_gives_the_identifier_a_label_has_or_will_get_and_writes_nothing(tmp_path):
    path = tmp_path / "participants.json"
    store = Store(path)
    fresh = store.peek("P001")
    assert str(uuid.UUID(fresh)) == fresh and not path.exists()
    assert store.identify("P001", fresh=fresh) == fresh  # a new label takes the identifier it was shown
    assert store.peek("P001") == fresh  # an existing one keeps its own
    assert store.identify("P001", fresh=str(uuid.uuid4())) == fresh
    with pytest.raises(ValueError):
        store.peek("../x")


# -- the body limit ---------------------------------------------------------------------------


def raw_exchange(a, head_lines, chunks, stop_when_answered=True):
    """Sends a request on a raw socket, chunk by chunk, until the server has answered.

    Returns ``(status line, bytes of the body sent before the answer was seen)``.
    """
    import select
    import socket

    host, port = a.host_port
    sock = socket.create_connection((host, port), timeout=10)
    sent = 0
    answer = b""
    try:
        sock.sendall(
            ("\r\n".join(["POST /api/participant HTTP/1.1", f"Host: {host}:{port}",
                          f"Origin: http://{host}:{port}", "Content-Type: application/json",
                          *head_lines]) + "\r\n\r\n").encode()
        )
        for chunk in chunks:
            ready, _, _ = select.select([sock], [], [], 0)
            if ready:
                break
            try:
                sock.sendall(chunk)
            except OSError:
                break  # the server closed after answering
            sent += len(chunk)
        sock.settimeout(10)
        while True:  # the whole answer: the head, then as many body bytes as it declares
            head, _, rest = answer.partition(b"\r\n\r\n")
            if b"\r\n\r\n" in answer:
                length = int(
                    next(
                        (row.split(b":")[1] for row in head.lower().split(b"\r\n") if row.startswith(b"content-length")),
                        b"0",
                    )
                )
                if len(rest) >= length:
                    break
            more = sock.recv(65536)
            if not more:
                break
            answer += more
    finally:
        sock.close()
    return answer.split(b"\r\n", 1)[0].decode(), answer, sent


def test_a_declared_5_mb_body_is_refused_before_it_is_sent(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    total = 5 * 1024 * 1024
    chunks = (b"x" * 4096 for _ in range(total // 4096))
    status, answer, sent = raw_exchange(a, [f"Content-Length: {total}"], chunks)
    assert status.split()[1] == "413", status
    assert b"body_too_large" in answer
    # (what the client had sent is only what the socket buffers took before the answer came back)
    assert sent < total // 2, f"the server answered only after {sent} bytes were sent"
    assert files_under(a.data_dir / "sessions") == []


def test_a_5_mb_body_with_no_length_is_cut_off_after_the_limit(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    total = 5 * 1024 * 1024

    def chunked():
        for _ in range(total // 4096):
            yield b"1000\r\n" + b"x" * 4096 + b"\r\n"

    status, answer, sent = raw_exchange(a, ["Transfer-Encoding: chunked"], chunked())
    assert status.split()[1] == "413", status
    assert b"body_too_large" in answer
    # The client's sends are counted when the kernel takes them, and the socket buffers on both
    # ends hold some hundreds of KB; the server answered long before the 5 MB were sent.
    assert sent < total // 2, f"the server answered only after {sent} bytes were sent"
    assert files_under(a.data_dir / "sessions") == []
    assert request(a, "GET", "/api/participants") == (200, {"labels": []})  # still serving


def test_a_body_just_within_the_limit_is_read(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    padding = " " * 900
    status, body = request(a, "POST", "/api/participant", raw=('{"label": "P001"%s}' % padding).encode())
    assert status == 200 and body["label"] == "P001"


# -- races and failures in accepting a label ---------------------------------------------------


def post_together(posts):
    """Runs ``posts`` (callables) at the same moment on threads; returns their results."""
    barrier = threading.Barrier(len(posts))
    results = [None] * len(posts)

    def run(i):
        barrier.wait()
        results[i] = posts[i]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(posts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def test_two_labels_at_the_same_moment_give_one_session_and_one_refusal(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    results = post_together([lambda: post_label(a, "P001"), lambda: post_label(a, "P002")])
    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409]
    winner = next(r[1]["label"] for r in results if r[0] == 200)
    loser = next(r[1] for r in results if r[0] == 409)
    assert loser["code"] == "already_set" and winner in loser["error"]
    folder = session_folder(a)
    assert folder.name.startswith(winner + "_")
    assert request(a, "GET", "/api/participants") == (200, {"labels": [winner]})
    assert json.loads((folder / "session.json").read_text())["label"] == winner


def test_the_same_label_twice_at_once_gives_one_session(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    results = post_together([lambda: post_label(a, "P001") for _ in range(6)])
    assert {r[0] for r in results} == {200}
    assert len({r[1]["uuid"] for r in results}) == 1
    folder = session_folder(a)
    assert len(markers_of(folder / "eeg")) == 1  # one session start, not six


def test_session_folders_taken_in_the_same_second_get_a_suffix_and_never_the_same_name(tmp_path):
    """The naming and the exclusive mkdir, with a fixed clock, in one process."""
    import time

    from brain_mapper.server import make_session_folder

    sessions = tmp_path / "sessions"
    now = time.mktime((2026, 11, 25, 12, 30, 5, 0, 0, -1))
    first = make_session_folder(sessions, "P001", now)
    second = make_session_folder(sessions, "P001", now)
    third = make_session_folder(sessions, "P001", now)
    other = make_session_folder(sessions, "P002", now)
    assert [p.name for p in (first, second, third, other)] == [
        "P001_20261125_123005",
        "P001_20261125_123005-2",
        "P001_20261125_123005-3",
        "P002_20261125_123005",
    ]
    assert all(p.is_dir() and p.parent == sessions for p in (first, second, third, other))
    # a name that is already there (a folder of an earlier run) is skipped, never reused
    (first / "keep.txt").write_text("earlier run")
    assert make_session_folder(sessions, "P001", now).name == "P001_20261125_123005-4"
    assert (first / "keep.txt").read_text() == "earlier run"
    # the same label a second later gets the plain name again
    assert make_session_folder(sessions, "P001", now + 1).name == "P001_20261125_123006"


def test_two_sessions_started_on_one_data_folder_one_after_the_other_do_not_share_a_folder(
    app, recorded_session
):
    """One process per data folder: two runs one after the other, the same label."""
    first = app(replay=recorded_session, flow=SHORT)
    assert post_label(first, "P001")[0] == 200
    stop(first)
    second = app(replay=recorded_session, flow=SHORT, data_dir=first.data_dir)
    assert post_label(second, "P001")[0] == 200
    folders = sorted(p for p in (first.data_dir / "sessions").iterdir() if p.is_dir())
    assert len(folders) == 2 and folders[0].name != folders[1].name
    assert all(f.name.startswith("P001_") for f in folders)
    # Ctrl-C ended the first session (and zipped it), which saves a source_ended event with no marker
    for folder, names in zip(folders, (["step_start", "source_ended"], ["step_start"])):
        assert [e["name"] for e in events_of(folder)] == names
        assert len(markers_of(folder / "eeg")) == 1
        assert json.loads((folder / "session.json").read_text())["session"] == folder.name
    # the second session writes only into its own folder: it takes its own step
    with open_ws(second) as ws:
        next_state(ws)
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
    counts = sorted(len(events_of(f)) for f in folders)
    assert counts == [2, 2], counts
    assert [e["name"] for e in events_of(folders[0])] == ["step_start", "source_ended"]  # untouched
    assert [(e["step"], e["name"]) for e in events_of(folders[1])][-1] == ("device", "step_start")


def test_a_device_that_never_opens_still_gets_its_label_accepted_and_no_hang(app, tmp_path):
    # Before: 409 source_not_ready ("could not be opened"), nothing saved. Decision 1: the label is
    # accepted without a live link, so the person reaches the screen that helps with the link.
    import time

    a = app(port=str(tmp_path / "no-such-device"), flow=SHORT)
    started = time.monotonic()
    status, body = post_label(a, "P001")
    assert time.monotonic() - started < 10
    assert status == 200 and body["label"] == "P001"
    folder = session_folder(a)
    assert (folder / "session.json").is_file() and not (folder / "eeg").exists()  # no recording yet
    assert (a.data_dir / "participants.json").exists()


def test_a_device_that_is_slow_to_open_is_waited_for_for_one_attempt_and_the_label_is_then_accepted(tmp_path):
    # Before: after ready_wait the label was refused (409 source_not_ready, "not connected yet"),
    # hub.participant and hub.session stayed None and nothing was saved. Now it is accepted.
    import time

    from brain_mapper.server import Hub, Settings
    from pty_device import PtyDevice

    dev = PtyDevice.silent_device()  # accepts the connection, never says a byte
    settings = Settings(
        data_dir=tmp_path, port=dev.path, replay=None, flow=SHORT, http_port=1, ready_wait=1.0
    )
    hub = Hub(settings)
    hub.start()
    try:
        started = time.monotonic()
        result = hub.accept("P001")
        waited = time.monotonic() - started
        assert 0.9 <= waited < 5
        assert result["label"] == "P001"
        assert hub.participant is not None and hub.session is not None
        assert hub.session.device_pending() and hub.folder is not None
        assert not (hub.folder / "eeg").exists()
        assert (hub.folder / "session.json").is_file()
    finally:
        hub.stop()
        dev.hang_up()


def test_a_label_after_a_headset_hang_up_is_accepted_and_waits_for_the_link(app, pty_device):
    # Before: 409 link_ended and nothing saved. A live link lost before the device step is tried
    # again (decision 2), so the label is accepted (decision 1) and its recording waits.
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        while True:
            message = json.loads(ws.recv(timeout=20))
            if message["t"] == "link" and message["state"] == "live":
                break
        pty_device.hang_up()
        while True:
            message = json.loads(ws.recv(timeout=20))
            if message["t"] == "link" and message["state"] == "connecting" and message["problem"]:
                break
    status, body = post_label(a, "P001")
    assert status == 200 and body["label"] == "P001"
    folder = session_folder(a)
    assert (folder / "session.json").is_file() and not (folder / "eeg").exists()


def _read_only(path: Path):
    import contextlib

    @contextlib.contextmanager
    def locked():
        path.chmod(0o500)
        try:
            yield
        finally:
            path.chmod(0o700)

    return locked()


def test_a_recording_that_cannot_start_leaves_nothing_and_can_be_tried_again(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        with _read_only(a.data_dir / "sessions"):  # a real cause: the folder cannot be written
            status, body = post_label(a, "P001")
            assert status == 500 and body["code"] == "recording_failed"
            assert "try again" in body["error"]
        assert list((a.data_dir / "sessions").iterdir()) == []
        assert not (a.data_dir / "participants.json").exists()  # the label is not listed
        send(ws, cmd="advance")  # still no session
        error = next(m for m in iter(lambda: json.loads(ws.recv(timeout=10)), None) if m["t"] == "error")
        assert error["kind"] == "refused"
        # the participant tries again, and now it works
        status, body = post_label(a, "P001")
        assert status == 200
    assert session_folder(a).name.startswith("P001_")
    assert request(a, "GET", "/api/participants") == (200, {"labels": ["P001"]})


def open_files_of(pid: int) -> str:
    import subprocess

    return subprocess.run(
        ["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True, check=False
    ).stdout


def test_a_label_that_cannot_be_stored_takes_the_session_back_and_can_be_tried_again(app, pty_device):
    a = app(port=pty_device.path, flow=SHORT)
    with open_ws(a) as ws:
        next_state(ws)
        with _read_only(a.data_dir):  # the sessions folder is writable, the list's folder is not
            status, body = post_label(a, "P001")
            assert status == 500 and body["code"] == "write_failed"
            # Before: no folder was left ("no half-made session folder"). Now nothing under a
            # session folder is ever deleted: the folder is kept and marked failed.
            kept = list((a.data_dir / "sessions").iterdir())
            assert len(kept) == 1
            failed = json.loads((kept[0] / "session.json").read_text())
            assert "participant list" in failed["failure"] and failed["label"] == "P001"
            # nothing the failed session opened is still open in the server
            assert "/sessions/P001_" not in open_files_of(a.process.pid)
        assert not (a.data_dir / "participants.json").exists()
        send(ws, cmd="advance")
        error = next(m for m in iter(lambda: json.loads(ws.recv(timeout=10)), None) if m["t"] == "error")
        assert error["kind"] == "refused"
        status, body = post_label(a, "P001")
        assert status == 200
        send(ws, cmd="advance")
        next_state(ws, predicate=lambda m: m["step"] == "device")
    both = sorted(p for p in (a.data_dir / "sessions").iterdir() if p.is_dir())
    assert len(both) == 2 and both[0] == kept[0]  # the new session has a folder of its own
    assert "failure" in json.loads((kept[0] / "session.json").read_text())  # the old one untouched
    folder = next(p for p in both if p != kept[0])
    assert "failure" not in json.loads((folder / "session.json").read_text())
    assert [e["name"] for e in events_of(folder)] == ["step_start", "step_start"]  # a fresh session


# -- session.json and Hub.participant -------------------------------------------------------------


def check_session_json(folder: Path, label, ident, kind, before, after):
    data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert set(data) == {"session", "label", "uuid", "started_unix", "source", "session_id", "sample_point", "previous_sessions"}
    assert data["sample_point"] == 1 and data["previous_sessions"] == []  # the person's first session here
    assert isinstance(data["session_id"], str) and label not in data["session_id"]
    assert data["session"] == folder.name and data["label"] == label and data["uuid"] == ident
    assert data["source"] == kind
    assert isinstance(data["started_unix"], float) and before - 1 <= data["started_unix"] <= after + 1
    assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []  # no temp file left


def test_session_json_holds_the_label_and_identifier_of_a_live_session(app, pty_device):
    import time

    a = app(port=pty_device.path, flow=SHORT)
    before = time.time()
    _, body = post_label(a, "P001")
    check_session_json(session_folder(a), "P001", body["uuid"], "live", before, time.time())
    meta = json.loads((session_folder(a) / "eeg" / "meta.json").read_text())
    assert meta["participant_uuid"] == body["uuid"]


def test_session_json_holds_the_label_and_identifier_of_a_replay_session(app, recorded_session):
    import time

    a = app(replay=recorded_session, flow=SHORT)
    before = time.time()
    _, body = post_label(a, "P002")
    folder = session_folder(a)
    check_session_json(folder, "P002", body["uuid"], "replay", before, time.time())
    # the replayed recording keeps its own meta.json, so session.json is where the label is
    meta = json.loads((folder / "eeg" / "meta.json").read_text())
    assert meta["participant"] == "P001"


def test_hub_participant_is_none_until_a_label_is_accepted(pty_device, tmp_path):
    from brain_mapper.server import Hub, Settings

    hub = Hub(Settings(tmp_path, pty_device.path, None, SHORT, 1))
    hub.start()
    try:
        assert hub.participant is None and hub.session is None
        assert hub.ready.wait(20)
        answer = hub.accept("P001")
        assert hub.participant == ("P001", answer["uuid"])
        assert hub.folder is not None and hub.folder.name.startswith("P001_")
    finally:
        hub.stop()


# -- the page: focus, Enter, alert, case hint, notes ------------------------------------------------


def test_enter_in_the_field_submits(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page.fill("#participant-label", "P001")
    page.press("#participant-label", "Enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert session_folder(a).name.startswith("P001_")


def test_an_error_returns_focus_to_the_field_and_the_alert_stays_in_the_page(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    alert = page.locator("#participant-problem")
    assert alert.get_attribute("role") == "alert" and alert.count() == 1
    assert alert.get_attribute("hidden") is None and alert.inner_text() == ""
    page.fill("#participant-label", "a b")
    page.click("#advance")
    page.wait_for_function("document.getElementById('participant-problem').textContent.includes('1 to 32')")
    assert page.evaluate("document.activeElement.id") == "participant-label"
    assert alert.get_attribute("hidden") is None and alert.count() == 1
    # fixing the label clears the words but keeps the same element
    page.fill("#participant-label", "P001")
    page.press("#participant-label", "Enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')


SAMPLE_FOCUS = """() => { window.__focus = []; window.__sampling = true;
  const tick = () => {
    const screen = document.getElementById('screen').dataset.screen;
    if (screen === 'participant') window.__focus.push(document.activeElement === document.body ? 'body' : document.activeElement.id);
    if (window.__sampling) requestAnimationFrame(tick);
  };
  tick(); }"""


def test_focus_is_never_dropped_to_the_page_while_the_label_is_sent(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    # a refused label: focus returns to the field
    page.fill("#participant-label", "a b")
    page.focus("#advance")
    page.evaluate(SAMPLE_FOCUS)
    page.keyboard.press("Enter")
    page.wait_for_function("document.getElementById('participant-problem').textContent.includes('1 to 32')")
    page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    assert page.evaluate("document.activeElement.id") == "participant-label"
    seen = page.evaluate("window.__focus")
    assert len(seen) > 2 and "body" not in seen, seen
    # an accepted label: focus stays on Continue until the screen changes
    page.fill("#participant-label", "P001")
    page.focus("#advance")
    page.evaluate("window.__focus = []")
    page.evaluate(
        """() => { window.__disabled = []; const b = document.getElementById('advance');
          new MutationObserver(() => window.__disabled.push(b.disabled))
            .observe(b, { attributes: true }); }"""
    )
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    page.evaluate("window.__sampling = false")
    seen = page.evaluate("window.__focus")
    assert "body" not in seen, seen
    assert seen and set(seen) <= {"advance", "participant-label"}, seen
    # the button was never really disabled while the label was sent
    assert all(disabled is False for disabled in page.evaluate("window.__disabled"))


def test_the_page_says_the_label_cannot_be_changed_during_the_session(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    assert "cannot be changed during the session" in page.inner_text(f"{SCREEN} .participant-main")


def test_a_label_that_differs_from_a_remembered_one_only_by_case_gets_a_plain_hint(
    app, pty_device, recorded_session, page
):
    a = app(port=pty_device.path, flow=SHORT)
    post_label(a, "P001")
    stop(a)
    b = app(replay=recorded_session, flow=SHORT, data_dir=a.data_dir)
    begin(b, page)
    hint = page.locator("#participant-hint")
    assert hint.count() == 1 and hint.inner_text() == ""
    page.fill("#participant-label", "p001")
    page.wait_for_function("document.getElementById('participant-hint').textContent.includes('P001')")
    text = hint.inner_text()
    assert "P001 was used before on this computer" in text and "different" in text
    page.fill("#participant-label", "P001")  # the same label: nothing to say
    page.wait_for_function("document.getElementById('participant-hint').textContent === ''")
    page.fill("#participant-label", "P002")
    assert hint.inner_text() == ""


def test_the_participant_screen_names_the_rule_with_a_first_character(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    text = page.inner_text("#participant-rule")
    assert "1 to 32" in text and "first" in text


# -- a lost source: before a label and during a session ------------------------------------------------


def test_a_source_lost_before_a_label_is_tried_again_and_nothing_is_saved(app, tmp_path, page):
    # Before: the ended screen with "disconnected" in #ended-reason, and no label field. Now the
    # link is tried again: a second headset plugged in at the same port is asked for its
    # configuration (0x14) and the link is live again.
    from pty_device import PtyDevice, config_reply, onhead_stream

    first = PtyDevice(onhead_stream(), config_reply())
    second = PtyDevice(onhead_stream(), config_reply())
    link = tmp_path / "headset-port"
    link.symlink_to(first.path)
    try:
        a = app(port=str(link), flow=SHORT)
        with open_ws(a) as ws:
            while json.loads(ws.recv(timeout=20)).get("state") != "live":
                pass
            first.hang_up()
            while True:
                m = json.loads(ws.recv(timeout=20))
                if m["t"] == "link" and m["state"] == "connecting" and m["problem"]:
                    break
            assert second.written() == b""  # not asked yet
            tmp = tmp_path / "headset-port.new"
            tmp.symlink_to(second.path)
            tmp.replace(link)
            while True:
                m = json.loads(ws.recv(timeout=30))
                if m["t"] == "link" and m["state"] == "live":
                    break
        assert b"\x14" in second.written()  # the retry asked the new headset for its configuration
        page.goto(a.url)
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]', timeout=30_000)
        assert page.locator("#ended-reason").count() == 0
        assert files_under(a.data_dir / "sessions") == []
    finally:
        first.hang_up()
        second.hang_up()


def test_a_source_lost_on_the_device_screen_keeps_the_session_and_shows_the_dropped_tip(app, pty_device, page):
    # Before: the ended screen ("disconnected" in #ended-reason, a source_ended event). Until the
    # device step is passed the link is tried again; after it the session ends (test_device_browser).
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page.fill("#participant-label", "P001")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    pty_device.hang_up()
    page.wait_for_selector('#device-tip[data-tip="dropped"]', timeout=30_000)
    assert page.locator(f'{SCREEN}[data-screen="device"]').count() == 1
    folder = session_folder(a)
    names = [e["name"] for e in events_of(folder)]
    assert names[0] == "step_start" and "source_ended" not in names


def test_the_installed_package_serves_the_participant_screen_module(app, pty_device):
    import urllib.request

    a = app(port=pty_device.path, flow=SHORT)
    response = urllib.request.urlopen(a.url + "/static/js/screens/participant.js")
    body = response.read()
    assert response.status == 200 and "javascript" in response.headers["Content-Type"]
    assert b"participant-label" in body and len(body) > 1000


# -- the folder sync after a rename is best effort ----------------------------------------------


def test_a_folder_that_cannot_be_synced_is_logged_once_and_does_not_fail(tmp_path, caplog):
    import logging

    import brain_mapper.participants as module

    module._folder_sync_logged = False
    gone = tmp_path / "no-such-folder"  # a real unusable folder: it does not exist
    with caplog.at_level(logging.WARNING, logger="brain_mapper"):
        module.sync_folder(gone)
        module.sync_folder(gone)
    messages = [r.getMessage() for r in caplog.records if "could not be synced" in r.getMessage()]
    assert len(messages) == 1 and str(gone) in messages[0]
    module.sync_folder(tmp_path)  # a usable folder: no error, nothing logged again
    assert len([r for r in caplog.records if "could not be synced" in r.getMessage()]) == 1


def test_an_identifier_belongs_to_one_label_and_is_found_by_it(tmp_path):
    store = Store(tmp_path / "participants.json")
    ident = store.identify("P001")
    assert store.label_of(ident) == "P001" and store.label_of(str(uuid.uuid4())) is None
    before = (tmp_path / "participants.json").read_bytes()
    with pytest.raises(IdentityTaken):
        store.identify("P002", fresh=ident)  # the same person under a second label
    assert (tmp_path / "participants.json").read_bytes() == before  # nothing written
    assert store.identify("P001", fresh=ident) == ident  # the same label again is the same person

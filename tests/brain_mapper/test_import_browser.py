"""The Participant ID screen with a saved session file, in real Chromium on the real server.

The zips are made by real sessions on simulated headsets (short flow). The file is chosen with the
file input, or dropped on the screen (a real ``drop`` event carrying the file's bytes). Screenshots
(900 x 900) go to the test's folder, or to ``$BRAIN_MAPPER_SHOTS`` when that is set.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path

from test_engine import SHORT, accept, next_state, open_ws, send, session_folder_of
from test_package import play_through, start_block, wait_packaged
from test_participants import stop
from test_shell_browser import SCREEN, begin

from pty_device import PtyDevice, config_reply, onhead_stream


def shots(tmp_path: Path) -> Path:
    folder = Path(os.environ.get("BRAIN_MAPPER_SHOTS") or tmp_path / "shots")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def day(unix: float) -> str:
    moment = datetime.fromtimestamp(unix)
    return f"{moment:%B} {moment.day}, {moment.year}"


def text_of(page, selector: str) -> str:
    return page.locator(selector).inner_text().strip()


def drop_file(page, name: str, data: bytes, mime: str = "application/zip") -> None:
    """Drops a file with ``data`` on the import box, as a person dragging it there would."""
    page.evaluate(
        """([name, b64, mime]) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const transfer = new DataTransfer();
            transfer.items.add(new File([bytes], name, { type: mime }));
            const box = document.getElementById("import-box");
            box.dispatchEvent(new DragEvent("dragover", { dataTransfer: transfer, bubbles: true, cancelable: true }));
            box.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true }));
        }""",
        [name, base64.b64encode(data).decode(), mime],
    )


def test_a_finished_session_s_zip_fills_in_the_id_and_starts_sample_point_2(app, page, tmp_path):
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=dev.path, flow=SHORT)
        accept(a, "Brw1")
        first = session_folder_of(a)
        with open_ws(a) as ws:
            assert play_through(ws)["phase"] == "done"
        made = wait_packaged(first)
        original = json.loads((first / "session.json").read_text())
        stop(a)
    finally:
        dev.hang_up()

    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT)  # another computer
        page.set_viewport_size({"width": 900, "height": 900})
        begin(b, page)
        page.fill("#participant-label", "Other1")
        page.screenshot(path=str(shots(tmp_path) / "participant-import-before.png"))
        page.set_input_files("#import-file", str(made))
        page.wait_for_function("document.getElementById('import-summary').textContent.startsWith('Saved session from')")
        assert text_of(page, "#import-summary") == f"Saved session from {day(original['started_unix'])}: 9 rounds completed"
        assert page.input_value("#participant-label") == "Brw1"
        assert page.eval_on_selector("#participant-label", "e => e.readOnly") is True
        assert text_of(page, "#import-label-note") == "The ID in the file, Brw1, is used instead of Other1."
        assert text_of(page, "#import-point") == "A new session is saved as sample point 2 for this person."
        assert text_of(page, "#advance") == "Start a new session for this person"
        assert page.locator("#import-continue").is_hidden() and page.locator("#continue-offer").is_hidden()
        assert page.locator("#start-over").is_hidden()
        page.screenshot(path=str(shots(tmp_path) / "participant-import-loaded.png"))
        # The label goes nowhere in an address.
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=30_000)
        assert not any("Brw1" in url for url in requests)
        meta = json.loads((session_folder_of(b) / "session.json").read_text())
        assert meta["label"] == "Brw1" and meta["uuid"] == original["uuid"]
        assert meta["sample_point"] == 2
        assert [e["session_id"] for e in meta["previous_sessions"]] == [original["session_id"]]
        kept = b.data_dir / meta["imported_from"]["file"]
        assert kept.read_bytes() == made.read_bytes()
        stop(b)
    finally:
        dev.hang_up()


def test_a_dropped_zip_of_an_unfinished_session_here_offers_to_continue_it(app, page, tmp_path):
    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        a = app(port=dev.path, flow=SHORT)
        accept(a, "Brw2")
        with open_ws(a) as ws:
            start_block(ws)
            next_state(ws, predicate=lambda m: m["phase"] == "between")
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m: m["phase"] == "between" and m["blocks_done"] == 2)
            send(ws, cmd="advance")
            next_state(ws, predicate=lambda m: m["step"] == "perception")
            send(ws, cmd="stop")
            next_state(ws, predicate=lambda m: m["stopped"])
        stopped = session_folder_of(a)
        data = wait_packaged(stopped).read_bytes()
        stop(a)
    finally:
        dev.hang_up()

    dev = PtyDevice(onhead_stream(), config_reply())
    try:
        b = app(port=dev.path, flow=SHORT, data_dir=a.data_dir)
        page.set_viewport_size({"width": 900, "height": 900})
        begin(b, page)
        drop_file(page, "notes.txt", b"this is not a zip", "text/plain")
        page.wait_for_function("document.getElementById('import-problem').textContent.length > 0")
        assert text_of(page, "#import-problem").startswith("This file is not a zip file.")
        assert page.input_value("#participant-label") == ""
        page.screenshot(path=str(shots(tmp_path) / "participant-import-refused.png"))
        drop_file(page, "session.zip", data)
        page.wait_for_function("document.getElementById('import-summary').textContent.startsWith('Saved session from')")
        assert re.fullmatch(r"Saved session from .+: 1 round completed", text_of(page, "#import-summary"))
        assert text_of(page, "#import-problem") == ""
        assert page.input_value("#participant-label") == "Brw2"
        assert text_of(page, "#import-label-note") == ""  # nothing was typed
        assert text_of(page, "#import-continue") == "Continue from Perception"
        # The offer no longer shows the old ledger total (2026-11-25: earned-of-possible or nothing).
        assert "stopped before Perception." in text_of(page, "#continue-offer")
        assert "points" not in text_of(page, "#continue-offer")
        page.screenshot(path=str(shots(tmp_path) / "participant-import-continue.png"))
        before = {p.name for p in (a.data_dir / "sessions").iterdir() if p.is_dir()}
        page.click("#import-continue")
        page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=30_000)
        (new,) = [p for p in (a.data_dir / "sessions").iterdir() if p.is_dir() and p.name not in before]
        meta = json.loads((new / "session.json").read_text())
        assert meta["continues"] == stopped.name and meta["sample_point"] == 1 and "imported_from" not in meta
        stop(b)
    finally:
        dev.hang_up()


def test_type_an_id_instead_sets_the_file_aside(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    fixture = Path(__file__).parent / "fixtures" / "previous-version-session.zip"
    page.set_input_files("#import-file", str(fixture))
    page.wait_for_function("document.getElementById('import-summary').textContent.startsWith('Saved session from')")
    assert page.input_value("#participant-label") == "P001"
    page.click("#import-clear")
    assert page.input_value("#participant-label") == "" and text_of(page, "#import-summary") == ""
    assert page.eval_on_selector("#participant-label", "e => e.readOnly") is False
    assert text_of(page, "#advance") == "Continue"
    page.fill("#participant-label", "P009")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]', timeout=30_000)
    meta = json.loads((session_folder_of(a) / "session.json").read_text())
    assert meta["label"] == "P009" and "imported_from" not in meta and meta["sample_point"] == 1

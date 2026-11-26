"""The summary screen in a real Chromium: what the session was, where its zip is, and its download.

The app runs on the pty device or a replayed recording with the short flow; the download is a
real Playwright download compared byte for byte with the zip on disk.
"""

from __future__ import annotations

import json
import os
import stat
import time

from playwright.sync_api import Error as PlaywrightError

from test_device_browser import to_device
from test_engine import SHORT
from test_package import TOP, EEG, wait_packaged, zip_contents
from test_shell_browser import SCREEN, begin, press, screen_of
from test_skip_browser import answer_yes, ask, folder_of, to_between_after_one_block

#: The block of the session record about the results computed from the recording (results.js).
RESULTS = "#results-entry"


def finish_the_route(page) -> None:
    """Presses Continue, Start and Finish until the session is over."""
    deadline = time.monotonic() + 150
    while True:
        assert time.monotonic() < deadline, screen_of(page)
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
        last = screen_of(page)[0] == "summary"
        press(page)
        if last:
            break
    page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")


def scores_total(meta: dict) -> str:
    """The session total of ``session.json``'s scores as the page writes it."""
    total = meta["scores"]["total"]
    return f"{total['earned']} of {total['possible']}" if total["possible"] else "\u2014"


def check_record(page, a, folder, tmp_path) -> None:
    """The summary part shared by every ending, and its download."""
    path = wait_packaged(folder)
    page.wait_for_selector("#summary-download", state="visible", timeout=30_000)
    text = page.inner_text("#summary-record")
    meta = json.loads((folder / "session.json").read_text())
    assert "P001" in page.inner_text("#summary-who")
    assert meta["start_local"][:10] in page.get_attribute("#summary-who", "data-date")
    # The points are the scores of session.json (earned of possible), one row per task round.
    total = scores_total(meta)
    assert page.inner_text("#summary-points").strip() == (
        "Total: nothing scored" if total == "\u2014" else f"Total: {total} points"
    )
    # The video has a score row too since 2026-11-25 (was: kinds "task" and "recall" only).
    rounds = [s for s in meta["flow"] if s["kind"] in ("task", "recall", "video")]
    assert rounds and page.locator("#summary-scores li").count() == len(rounds)
    blocks = [t for t in meta["timeline"] if t["block"] is not None]
    assert page.locator("#summary-timeline tbody tr").count() == len(blocks)
    assert path.name in page.inner_text("#summary-zip") and str(path.parent) in page.inner_text("#summary-zip")
    assert page.locator(f"#summary-record {RESULTS}").count() == 1 and "Your results" in text
    assert page.inner_text("#summary-recording").strip() == "The recording is saved on this computer."
    href = page.get_attribute("#summary-download", "href")
    assert "P001" not in href
    with page.expect_download() as info:
        page.click("#summary-download")
    download = info.value
    saved = tmp_path / "downloaded.zip"
    download.save_as(saved)
    assert saved.read_bytes() == path.read_bytes()
    assert download.suggested_filename == path.name
    assert set(zip_contents(saved)) == TOP | EEG


def test_the_summary_of_a_finished_session_shows_the_total_the_timeline_and_downloads_the_zip(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    finish_the_route(page)
    folder = folder_of(a)
    assert page.locator(f'{SCREEN}[data-screen="summary"]').count() == 1
    check_record(page, a, folder, tmp_path)
    meta = json.loads((folder / "session.json").read_text())
    assert page.inner_text("#hud-points").strip() == scores_total(meta)
    assert "finished" in page.inner_text("#summary-text")


def test_the_summary_after_a_stop_shows_the_record_and_downloads_the_zip(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert "stopped by the participant" in page.inner_text("#summary-text")
    check_record(page, a, folder_of(a), tmp_path)
    assert page.locator("#stopped-rounds").is_visible()


def test_a_session_that_ended_early_shows_the_record_and_downloads_the_zip(app, recorded_session, page, tmp_path):
    a = app(replay=recorded_session, flow=SHORT)
    begin(a, page)
    press(page)  # the label; the recording runs out a few seconds later
    deadline = time.monotonic() + 60
    while screen_of(page)[0] != "ended":
        assert time.monotonic() < deadline, screen_of(page)
        button = page.locator("#advance:not([disabled])")
        if button.count():
            try:
                press(page, timeout=5000)
            except PlaywrightError:
                # The one reason a press may not land: the recording ran out under it and the
                # button went with the screen. Anything else fails the test here.
                page.wait_for_selector(f'{SCREEN}[data-screen="ended"]', timeout=5000)
        page.wait_for_timeout(100)
    assert "end of recording" in page.inner_text("#ended-reason")
    check_record(page, a, folder_of(a), tmp_path)


def test_a_zip_that_cannot_be_made_is_told_in_plain_words_and_the_folder_stays_complete(app, pty_device, page):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    folder = folder_of(a)
    sessions = folder.parent
    mode = stat.S_IMODE(os.stat(sessions).st_mode)
    os.chmod(sessions, 0o555)  # nothing new can be made beside the folder
    try:
        ask(page, "stop-here")
        answer_yes(page)
        page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
        page.wait_for_function("document.getElementById('summary-zip').dataset.state === 'failed'", timeout=30_000)
        words = page.inner_text("#summary-zip")
        assert "could not be made" in words and folder.name in words and "next time" in words
        assert page.locator("#summary-download").count() == 0
        assert [p.name for p in sessions.iterdir()] == [folder.name]
        meta = json.loads((folder / "session.json").read_text())
        assert "zip" not in meta and meta["complete"] is False
        assert json.loads((folder / "eeg" / "meta.json").read_text())["finished"] is True
        assert (folder / "eeg" / "raw.bin").stat().st_size > 0
    finally:
        os.chmod(sessions, mode)


def test_a_session_whose_headset_never_linked_says_that_no_eeg_was_recorded(app, page, tmp_path):
    a = app(port=str(tmp_path / "no-such-port"), flow=SHORT)
    to_device(a, page)  # the label is accepted without a link; the device screen waits for one
    ask(page, "stop-here")
    asked = page.inner_text("#leave-text")
    assert "no EEG is being recorded" in asked and "recording is saved" not in asked
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    folder = folder_of(a)
    path = wait_packaged(folder)
    page.wait_for_selector("#summary-download", state="visible", timeout=30_000)
    assert not (folder / "eeg").exists()
    words = page.inner_text("#summary-recording")
    assert "No EEG was recorded" in words and "never came up" in words
    screen = page.inner_text("#screen")
    assert "recording is saved" not in screen and page.locator(RESULTS).count() == 0
    with page.expect_download() as info:
        page.click("#summary-download")
    saved = tmp_path / "downloaded.zip"
    info.value.save_as(saved)
    assert saved.read_bytes() == path.read_bytes()
    assert not any(name.startswith("eeg") for name in zip_contents(saved))

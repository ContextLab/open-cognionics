"""A silent headset link during calibration (see ``test_silent_link.py``): during the contact
check the check starts again from its beginning; during settling the screen says that the
headset connection was lost, never that the contact check "did not go off", and eyes open starts
by itself once the link is back and the data show the check off.

The real ``brain-mapper`` on the simulated headset that answers the contact-check bytes, made
silent with ``go_silent`` (the pty stays open), and a real Chromium.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from brain_mapper.engine import OFF_LIMIT_S
from brain_mapper.source import SILENT
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from test_calibration_browser import pass_signal_check, to_calibration
from test_engine import SHORT, events_of, session_folder_of
from test_link_pause import ALLOWED, Plug, until
from test_shell_browser import SCREEN
from test_whole_session_browser import contact_device
from shots import ShotsDir


SHOTS = ShotsDir("shots-fix-night")


def lost_events(folder: Path) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == "link_lost"]


def back_events(folder: Path) -> list[dict]:
    return [e for e in events_of(folder) if e["name"] == "link_back"]


def calibration_flow(tmp_path: Path) -> Path:
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": 4.0, "eyes_closed": 2.0}
    path = tmp_path / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path


def test_silence_during_the_contact_check_and_during_settling(app, page, tmp_path):
    """The contact check starts again from its beginning; during settling the screen says the
    headset connection was lost (never that the check "did not go off"), and eyes open starts by
    itself once the link is back and the data show the check off."""
    plug = Plug(tmp_path, make=contact_device)
    device = plug.device
    try:
        a = app(port=str(plug.link), flow=calibration_flow(tmp_path))
        page.set_viewport_size({"width": 900, "height": 900})
        to_calibration(a, page)
        folder = session_folder_of(a)
        device.go_silent()
        assert until(lambda: lost_events(folder), "link_lost", 10)[0]["payload"]["reason"] == SILENT
        starts = until(lambda: [e for e in events_of(folder) if e["name"] == "contact_start"][1:], "contact_start 2")
        assert starts[0]["payload"]["restarted"] == "link_lost"
        device.speak_again()
        until(lambda: back_events(folder), "link_back", 40)
        # Continue once the check that is on now has been measured.
        deadline = time.monotonic() + 40
        while True:
            assert time.monotonic() < deadline, "the contact check was never left"
            if page.locator("#advance:not([disabled])").count():
                page.click("#advance")
            try:
                page.wait_for_selector(f'{SCREEN}[data-substep="settling"]', timeout=300)
                break
            except PlaywrightTimeout:
                continue
        device.go_silent()
        assert "settling_start" in [e["name"] for e in events_of(folder)]
        assert "eyes_open_start" not in [e["name"] for e in events_of(folder)]
        until(lambda: lost_events(folder)[1:], "link_lost 2", 10)
        page.wait_for_function(
            "document.getElementById('calib-text').textContent.includes('headset connection was lost')",
            timeout=10_000,
        )
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "settling-link-lost.png"))
        # Longer than the time after which a check that stays on is reported as not off.
        page.wait_for_timeout((OFF_LIMIT_S + 1.0) * 1000)
        assert page.get_attribute(SCREEN, "data-substep") == "settling"
        assert not page.locator("#calib-not-off").is_visible()
        assert "contact_not_off" not in [e["name"] for e in events_of(folder)]
        device.speak_again()
        until(lambda: back_events(folder)[1:], "link_back 2", 40)
        pass_signal_check(page)
        page.wait_for_selector(f'{SCREEN}[data-substep="eyes_open"]', timeout=30_000)
        assert set(plug.written()) <= ALLOWED
    finally:
        plug.close()

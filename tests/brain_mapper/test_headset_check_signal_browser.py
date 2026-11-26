"""The headset check of the paused screen runs the signal check too, by the same path as the
calibration (the contact check, the settling wait with the tone off, then ``quality.SIGNAL_S`` of
data looked at by ``quality.check``). "Check the headset" on
the paused screen also runs the signal check.

Real Chromium, the real server and the simulated headset playing the synthetic streams of
``test_signal_check_browser`` (a 125 Hz tone with the check on; Fp1 noisy with it off).
"""

from __future__ import annotations

import json

from brain_mapper import quality
from test_engine import events_of, session_folder_of
from test_link_pause import ALLOWED, until, wait_paused
from test_pause_browser import PROMPT_BETWEEN, flow_file, running_with_trials, task
from test_shell_browser import begin
from test_signal_check_browser import make_device
from test_whole_session_browser import drive


def test_check_the_headset_from_the_paused_screen_also_runs_the_signal_check(app, page, tmp_path):
    page.set_viewport_size({"width": 1512, "height": 982})
    device = make_device({"Fp1": "noisy"})
    try:
        a = app(port=device.path, flow=flow_file(tmp_path, [task("flanker", "Flanker", 2, 40)]))
        begin(a, page)
        drive(page, a, running_with_trials(a, "flanker", 1), timeout=180)
        folder = session_folder_of(a)
        page.evaluate("document.exitFullscreen()")
        wait_paused(page)
        page.click("#fs-check")
        page.wait_for_selector("#hc-count[data-good]", timeout=30_000)
        assert page.inner_text("#hc-count") == "20 of 20 contacts are good"
        page.click("#hc-done")
        page.wait_for_selector("#fs-check-panel", state="hidden", timeout=10_000)
        # After the settling wait the signal check runs: Resume waits for it, and says why.
        until(lambda: [e for e in events_of(folder) if e["name"] == "signal_start"], "signal_start", 30)
        assert page.is_disabled("#fs-return") and page.is_disabled("#fs-check")
        page.wait_for_function("document.getElementById('fs-wait').textContent.includes('signal')")
        # The result is shown on the paused screen, by the same words as the calibration's.
        page.wait_for_selector("#fs-signal:not([hidden])", timeout=30_000)
        text = page.inner_text("#fs-signal")
        assert text.startswith("Signal check: 20 of 20 contacts good; 1 channel noisy: Fp1"), text
        page.wait_for_selector("#fs-return:not([disabled])", timeout=10_000)
        events = events_of(folder)
        order = [
            e["name"]
            for e in events
            if e["name"] in ("headset_check_start", "headset_check_end", "headset_check_settled", "signal_start", "signal_end")
        ]
        assert order == ["headset_check_start", "headset_check_end", "headset_check_settled", "signal_start", "signal_end"]
        start = next(e for e in events if e["name"] == "signal_start")
        assert start["payload"]["for"] == "headset_check" and start["payload"]["seconds"] == quality.SIGNAL_S
        end = next(e for e in events if e["name"] == "signal_end")
        assert end["payload"]["not_fine"] == ["Fp1"] and end["payload"]["fine"] == 19
        # calibration.json: the signal result is part of the headset check's entry.
        saved = json.loads((folder / "calibration.json").read_text())
        check = saved["checks"][-1]
        assert check["kind"] == "headset_check" and check["signal"]["not_fine"] == ["Fp1"]
        assert check["signal"]["version"] == quality.VERSION
        # Resume goes on as after any pause in a block: the between screen (for flanker, which
        # starts on Enter, its own prompt for the next block), then the next block.
        page.click("#fs-return")
        page.wait_for_selector(PROMPT_BETWEEN, timeout=10_000)
        assert "ended by the pause" in page.inner_text("#task-ready")
        assert page.is_hidden("#fs-signal")
        assert set(device.written()) <= ALLOWED
    finally:
        device.hang_up()

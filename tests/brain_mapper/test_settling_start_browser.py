"""Start pressed right after the calibration is skipped, while the headset's contact check is
still going off (the documented settling time, ``engine._refuse_while_check_goes_off``): the page
shows that the round is about to start instead of a refusal, and the round starts by itself once
the data show the check off.

The real server on the simulated headset that answers the contact-check bytes, a real Chromium.
"""

from __future__ import annotations

from test_engine import events_of, session_folder_of
from test_shell_browser import SCREEN
from test_whole_session_browser import contact_device

WATCH = """() => {
  window.__notices = [];
  window.__waiting = [];
  new MutationObserver(() => {
    const n = document.getElementById('notice');
    if (n && !n.hidden && n.textContent) window.__notices.push(n.textContent);
    const b = document.getElementById('advance');
    if (b && b.dataset.waiting === 'true') window.__waiting.push(b.textContent);
  }).observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true});
}"""


def test_start_during_the_settling_time_waits_and_starts_instead_of_refusing(app, page):
    device = contact_device()
    try:
        a = app(port=device.path)
        page.goto(a.url)
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
        page.click('.route-pick[data-step="rest_open"]')  # a round whose Start comes at once
        page.wait_for_selector("#play-only", state="visible")
        page.click("#play-only")
        page.click("#fs-enter")
        page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
        page.fill("#participant-label", "SETTLE1")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
        page.wait_for_selector("#skip-round", state="visible", timeout=30_000)
        page.click("#skip-round")
        page.click("#leave-yes")
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="waiting"]', timeout=30_000)
        page.wait_for_selector("#advance", state="visible")
        page.evaluate(WATCH)
        page.click("#advance")  # at once: the check went off moments ago
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="running"]', timeout=20_000)
        notices = page.evaluate("window.__notices")
        waiting = page.evaluate("window.__waiting")
        assert not any("can start in a moment" in n for n in notices), notices
        assert waiting and all(w == "Starting in a moment" for w in waiting), waiting
        folder = session_folder_of(a)
        names = [e["name"] for e in events_of(folder)]
        assert names.count("block_start") == 1
    finally:
        device.hang_up()

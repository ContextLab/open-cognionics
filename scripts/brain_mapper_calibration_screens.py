"""Saves one PNG per part of the brain-mapper calibration step.

    uv run python scripts/brain_mapper_calibration_screens.py <out-dir> [--size 900x900]

A pseudo-terminal device plays the recorded contact-check fixture
(``tests/fixtures/impedance_on_2s.bin``, looped) while the app has the check on, and the on-head
recording (``onhead_10s.bin``) once it is off, to the real app, run with the short flow and
calibration sub-steps of 3 s, and Chromium walks the step with the made-up label P001:
``contact.png`` (the states the recording gives), ``contact-focused.png`` (the first pill
pressed), ``eyes-open.png``, ``eyes-closed.png``, ``blink-cue.png`` (a cue lit) and
``done.png``. The contact states are those the recorded bytes give; no state is made up, so a
screen with every contact good, for example, cannot be made from this recording.
``<out-dir>`` must be outside the repository. Never opens a real serial port.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_mapper_screens as screens  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import FIXTURES, PtyDevice, config_reply, onhead_stream  # noqa: E402

SECONDS = 3.0


def calibration_flow(work: Path) -> Path:
    steps = json.loads(screens.SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": SECONDS, "eyes_closed": SECONDS}
    path = work / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path


def settle(page) -> None:
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(300)


def walk(browser, url: str, out: Path, size: dict) -> None:
    context = browser.new_context(viewport=size)
    page = context.new_page()
    page.goto(url)
    screens.wait_for(page, screen="start")
    screens.enter(page)
    page.fill("#participant-label", "P001")
    page.click("#advance")
    screens.wait_for(page, screen="device")
    page.wait_for_selector("#advance:not([disabled])", timeout=60_000)
    page.click("#advance")
    page.wait_for_selector('#screen[data-substep="contact"]', timeout=60_000)
    page.wait_for_selector("#contact-count[data-good]", timeout=60_000)
    page.wait_for_timeout(1500)  # the view's camera and the font settle
    settle(page)
    page.screenshot(path=out / "contact.png")
    pill = page.locator("#contact-pills .contact-pill").first
    if pill.count():
        pill.click()
        page.wait_for_timeout(1500)  # the camera move
        page.screenshot(path=out / "contact-focused.png")
    page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
    page.click("#advance")
    page.wait_for_selector('#screen[data-substep="eyes_open"]', timeout=30_000)
    settle(page)
    page.screenshot(path=out / "eyes-open.png")
    page.wait_for_selector('#screen[data-substep="eyes_closed"]', timeout=30_000)
    settle(page)
    page.screenshot(path=out / "eyes-closed.png")
    page.wait_for_selector('#screen[data-substep="blinks"]', timeout=30_000)
    page.wait_for_selector("#blink-cue[data-on='true']", timeout=30_000)
    page.screenshot(path=out / "blink-cue.png")
    page.wait_for_selector('#screen[data-substep="done"]', timeout=60_000)
    settle(page)
    page.screenshot(path=out / "done.png")
    context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--size", default="900x900")
    args = parser.parse_args()
    out = args.out.resolve()
    if out == REPO or REPO in out.parents:
        parser.error("the output folder must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    width, height = (int(v) for v in args.size.split("x"))
    work = Path(tempfile.mkdtemp(prefix="brain-mapper-calibration-screens-"))
    # Answers 0x11 and 0x12 as the headset does: the check-on recording while the check is on.
    device = PtyDevice(
        onhead_stream(), config_reply(), contact_stream=(FIXTURES / "impedance_on_2s.bin").read_bytes(), contact_on=True
    )
    flow = calibration_flow(work)
    original = screens.SHORT
    screens.SHORT = flow  # start_app passes the flow it names
    process = None
    try:
        process, url = screens.start_app(work / "data", port=device.path)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                walk(browser, url, out, {"width": width, "height": height})
            finally:
                browser.close()
    finally:
        screens.SHORT = original
        if process is not None:
            screens.stop_app(process)
        device.hang_up()
        shutil.rmtree(work, ignore_errors=True)
    for name in sorted(p.name for p in out.glob("*.png")):
        print(out / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

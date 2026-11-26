"""Saves one PNG per screen of the brain-mapper participant page.

    uv run python scripts/brain_mapper_screens.py <out-dir> [--size 1440x900]

Starts a pseudo-terminal device and the app with the short flow
(``tests/brain_mapper/flows/short.json``), walks the session in Chromium with
Playwright and writes ``<nn>-<screen>.png``: the start screen, the setup
screens, a round mid-block, the full-screen-lost overlay, the between-blocks
screen, both rest screens and the summary. A second pass with reduced motion
saves the between-blocks screen as ``rm-between.png``; a third plays a short
real recording to its end and saves the ended screen. ``<out-dir>`` must be
outside the repository (the script refuses a path inside it). Never opens a
real serial port.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "python"))

from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

import open_cognionics as oc  # noqa: E402

SHORT = REPO / "tests" / "brain_mapper" / "flows" / "short.json"
COMMAND = Path(sys.executable).parent / "brain-mapper"
PRINTED = "brain-mapper at "
SCREEN = "#screen"


def start_app(data_dir: Path, port: str | None = None, replay: Path | None = None):
    args = [str(COMMAND), "--no-browser", "--http-port", "0", "--data-dir", str(data_dir)]
    args += ["--flow", str(SHORT)]
    args += ["--port", port] if port else ["--replay", str(replay)]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        if line.startswith(PRINTED):
            return process, line.strip().removeprefix(PRINTED)
    raise RuntimeError("brain-mapper did not print its address")


def stop_app(process: subprocess.Popen) -> None:
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def wait_for(page, step: str | None = None, screen: str | None = None, phase: str | None = None):
    selector = SCREEN
    if screen:
        selector += f'[data-screen="{screen}"]'
    if step:
        selector += f'[data-step="{step}"]'
    if phase:
        selector += f'[data-phase="{phase}"]'
    page.wait_for_selector(selector, timeout=60_000)


def press(page) -> None:
    page.wait_for_selector("#advance:not([disabled])", state="visible")
    field = page.locator("#participant-label")
    if field.count() and not field.input_value():
        field.fill("P001")  # nothing goes on from the participant screen without a label
    page.click("#advance")


def begin(page, url: str) -> None:
    page.goto(url)
    wait_for(page, screen="start")
    page.wait_for_function("document.fonts.status === 'loaded'")


def enter(page) -> None:
    page.click("#fs-enter")
    page.wait_for_function("document.fullscreenElement !== null")
    wait_for(page, screen="participant")


def main_pass(browser, url: str, out: Path, size: dict) -> None:
    context = browser.new_context(viewport=size)
    page = context.new_page()
    begin(page, url)
    page.screenshot(path=out / "01-start.png")
    enter(page)
    for number, name in ((2, "participant"), (3, "device"), (4, "calibration")):
        wait_for(page, screen=name)
        page.screenshot(path=out / f"{number:02d}-{name}.png")
        press(page)
    wait_for(page, step="words", phase="waiting")
    press(page)
    wait_for(page, step="words", phase="running")
    page.screenshot(path=out / "05-placeholder.png")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector("#fs-lost", state="visible")
    wait_for(page, phase="paused")
    page.screenshot(path=out / "06-fs-lost.png")
    page.click("#fs-return")
    page.wait_for_selector("#fs-lost", state="hidden")
    wait_for(page, screen="between")
    page.wait_for_function("document.getElementById('hud-points').textContent.trim() === '100'")
    page.wait_for_timeout(300)
    page.screenshot(path=out / "07-between.png")
    press(page)
    for step, number in (("rest_closed", 8), ("rest_open", 9)):
        while not page.locator(f'{SCREEN}[data-step="{step}"][data-phase="waiting"]').count():
            if page.locator("#advance:not([disabled])").count():
                page.click("#advance")
            page.wait_for_timeout(50)
        press(page)
        wait_for(page, step=step, phase="running")
        page.screenshot(path=out / f"{number:02d}-{step.replace('_', '-')}.png")
        wait_for(page, step=step, phase="between")
        press(page)
    while not page.locator(f'{SCREEN}[data-screen="summary"]').count():
        if page.locator("#advance:not([disabled])").count():
            page.click("#advance")
        page.wait_for_timeout(50)
    page.screenshot(path=out / "10-summary.png")
    context.close()


def reduced_pass(browser, url: str, out: Path, size: dict) -> None:
    context = browser.new_context(viewport=size, reduced_motion="reduce")
    page = context.new_page()
    begin(page, url)
    enter(page)
    for _ in range(3):
        press(page)
    wait_for(page, step="words", phase="waiting")
    press(page)
    wait_for(page, screen="between")
    page.screenshot(path=out / "rm-between.png")
    context.close()


def ended_pass(browser, out: Path, size: dict, work: Path) -> None:
    """Plays a 6 s recording to its end; the session ends early with the reason."""
    dev = PtyDevice(onhead_stream(), config_reply())
    recording = work / "recording"
    try:
        with oc.connect(dev.path) as headset:
            with headset.record(recording, participant="P001"):
                time.sleep(6)
    finally:
        dev.hang_up()
    process, url = start_app(work / "replay-data", replay=recording)
    try:
        context = browser.new_context(viewport=size)
        page = context.new_page()
        begin(page, url)
        enter(page)
        deadline = time.monotonic() + 60
        while not page.locator(f'{SCREEN}[data-screen="ended"]').count():
            if time.monotonic() > deadline:
                raise RuntimeError("the replay did not end the session")
            if page.locator("#advance:not([disabled])").count():
                press(page)
            page.wait_for_timeout(100)
        page.screenshot(path=out / "11-ended.png")
        context.close()
    finally:
        stop_app(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--size", default="1440x900", help="viewport, WIDTHxHEIGHT")
    args = parser.parse_args()
    out = args.out_dir.expanduser().resolve()
    if out == REPO or REPO in out.parents:
        print(f"refusing to write screenshots inside the repository: {out}", file=sys.stderr)
        return 2
    width, height = (int(x) for x in args.size.split("x"))
    size = {"width": width, "height": height}
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="brain-mapper-screens-"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for name in ("main", "reduced"):
                dev = PtyDevice(onhead_stream(), config_reply())
                process, url = start_app(work / f"data-{name}", port=dev.path)
                try:
                    if name == "main":
                        main_pass(browser, url, out, size)
                    else:
                        reduced_pass(browser, url, out, size)
                finally:
                    stop_app(process)
                    dev.hang_up()
            ended_pass(browser, out, size, work)
            browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"screenshots in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

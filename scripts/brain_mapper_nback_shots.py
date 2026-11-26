"""900x900 screenshots of the 2-back task (instructions and each state of a trial).

    uv run python scripts/brain_mapper_nback_shots.py <out-dir>

Starts brain-mapper on a simulated headset (a pseudo-terminal device, never a real serial port)
with the test flow ``tests/brain_mapper/flows/nback.json`` and plays the practice in Chromium.
"""

import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests" / "brain_mapper"), str(ROOT / "tests" / "python")]

from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

from conftest import COMMAND, PRINTED, App, _Lines  # noqa: E402
from test_shell_browser import SCREEN, advance_until, begin, server_state  # noqa: E402
from test_task_nback import lists_of  # noqa: E402

FLOW = ROOT / "tests" / "brain_mapper" / "flows" / "nback.json"


def shoot_while(page, selector: str, path: Path, seconds: float = 5.0) -> None:
    """Screenshots until one is taken with ``selector`` on screen before and after it (the
    feedback is up for only 400 ms)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if page.locator(selector).count():
            page.screenshot(path=str(path))
            if page.locator(selector).count():
                return
    raise AssertionError(f"never caught {selector} on screen")


def awaiting(page, trial: int) -> None:
    page.wait_for_selector(f'#task-area[data-trial="{trial}"][data-awaiting="true"]', timeout=20_000)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    dev = PtyDevice(onhead_stream(), config_reply())
    data = Path(tempfile.mkdtemp(prefix="nback-shots-"))
    args = [str(COMMAND), "--no-browser", "--data-dir", str(data), "--http-port", "0",
            "--flow", str(FLOW), "--port", dev.path]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = _Lines(process.stdout).wait_for(PRINTED, timeout=30)
        a = App(line.removeprefix(PRINTED), data, process)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(viewport={"width": 900, "height": 900}).new_page()
            begin(a, page)
            advance_until(page, "nback", "waiting")
            page.wait_for_selector("#task-instructions", state="visible")
            page.screenshot(path=str(out / "instructions.png"))
            practice = lists_of(page, server_state(a)["seed"], "block1", "practice")
            page.click("#practice")
            awaiting(page, 0)
            page.wait_for_selector("#task-panel .nback-letter", state="visible")
            page.screenshot(path=str(out / "letter.png"))
            page.wait_for_selector("#task-panel:empty", state="attached")
            page.screenshot(path=str(out / "fixation.png"))
            # Trial 2: the right key; trial 3: the wrong key; trial 4: no answer.
            for trial, name in ((2, "feedback-right"), (3, "feedback-wrong")):
                awaiting(page, trial)
                same = practice[trial]["target"] == (name == "feedback-right")
                page.keyboard.press("j" if same else "f")
                shoot_while(page, ".nback-feedback", out / f"{name}.png")
            awaiting(page, 4)
            shoot_while(page, ".nback-feedback:text('No response')", out / "feedback-no-answer.png", 10.0)
            page.wait_for_selector("#advance", state="visible", timeout=60_000)
            page.click("#advance")
            awaiting(page, 1)
            page.evaluate("document.exitFullscreen()")
            page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
            page.screenshot(path=str(out / "paused.png"))
            page.click("#fs-return")
            page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
            page.screenshot(path=str(out / "between.png"))
            browser.close()
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
        dev.hang_up()


if __name__ == "__main__":
    main(Path(sys.argv[1]))

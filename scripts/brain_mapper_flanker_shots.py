"""900x900 screenshots of the flanker task: the instructions, the fixation cross, congruent and
incongruent arrows, the three practice feedbacks, the blank gap, the end of practice and the
between-blocks screen after block 1.

    uv run python scripts/brain_mapper_flanker_shots.py <out-dir>

Starts ``brain-mapper`` on a simulated headset (a pseudo-terminal, never a real serial port) with
the test flow ``tests/brain_mapper/flows/flanker.json`` and a new temporary data folder.
"""

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests" / "brain_mapper"), str(ROOT / "tests" / "python")]

from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

from conftest import COMMAND, PRINTED, App, _Lines  # noqa: E402
from test_task_flanker import (  # noqa: E402
    AREA,
    FIELD,
    FLOW,
    KEY,
    OTHER,
    PANEL,
    awaiting_selector,
    to_instructions,
)


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix="shots-flanker-"))
    dev = PtyDevice(onhead_stream(), config_reply())
    args = [str(COMMAND), "--no-browser", "--data-dir", str(data), "--http-port", "0"]
    args += ["--flow", str(FLOW), "--port", dev.path]
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    a = App(
        _Lines(process.stdout).wait_for(PRINTED, timeout=30).removeprefix(PRINTED),
        data,
        process,
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(
                viewport={"width": 900, "height": 900}
            ).new_page()
            to_instructions(a, page)
            page.screenshot(path=str(out / "instructions.png"))
            page.click("#practice")
            seen = set()
            for i in range(6):
                page.wait_for_selector(
                    f'{FIELD}[data-phase="fixation"][data-trial="{i}"]', timeout=15_000
                )
                if i == 0:
                    page.screenshot(path=str(out / "fixation.png"))
                page.wait_for_selector(awaiting_selector(i, True), timeout=15_000)
                field = page.locator(FIELD)
                name = (
                    "arrows-congruent"
                    if field.get_attribute("data-congruent") == "true"
                    else "arrows-incongruent"
                )
                if name not in seen:
                    seen.add(name)
                    page.screenshot(path=str(out / f"{name}.png"))
                target = field.get_attribute("data-target")
                if i == 1:
                    page.keyboard.press(KEY[OTHER[target]])
                elif i != 2:  # trial 2: no key, "No response"
                    page.keyboard.press(KEY[target])
                page.wait_for_selector(
                    f'{FIELD}[data-phase="feedback"][data-trial="{i}"]', timeout=5000
                )
                if i < 3:
                    text = (
                        page.inner_text(f"{FIELD} .flanker-feedback")
                        .lower()
                        .replace(" ", "-")
                    )
                    page.screenshot(path=str(out / f"feedback-{text}.png"))
                page.wait_for_selector(
                    f'{AREA}[data-trial="{i}"] {PANEL}:empty', timeout=5000
                )
                if i == 0:
                    page.screenshot(path=str(out / "blank-gap.png"))
            page.wait_for_selector("#advance", state="visible", timeout=15_000)
            page.screenshot(path=str(out / "practice-over.png"))
            page.click("#advance")
            page.wait_for_selector('#screen[data-screen="between"]', timeout=90_000)
            page.screenshot(path=str(out / "between.png"))
            browser.close()
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
        dev.hang_up()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: brain_mapper_flanker_shots.py <out-dir>")
    main(Path(sys.argv[1]))

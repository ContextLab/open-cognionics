"""Saves PNGs of the Words and Delayed recall screens at 900x900.

    uv run python scripts/brain_mapper_words_screens.py <out-dir>

Starts a pseudo-terminal device and the app with ``tests/brain_mapper/flows/words.json``, plays
the session in Chromium with Playwright and writes ``instructions.png``, ``fixation.png``,
``word.png``, ``blank.png``, ``recall_logged.png`` (the field's sign that an entry was just
logged), ``recall.png`` (three entries made, none listed, one still in the field),
``between.png``, ``recall_instructions.png``, ``delayed_recall_logged.png`` and
``delayed_recall.png``. ``<out-dir>`` must be
outside the repository (the script refuses a path inside it). Never opens a real serial port.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "python"))

from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

FLOW = REPO / "tests" / "brain_mapper" / "flows" / "words.json"
COMMAND = Path(sys.executable).parent / "brain-mapper"
PRINTED = "brain-mapper at "
SCREEN = "#screen"


def start_app(data_dir: Path, port: str) -> tuple[subprocess.Popen, str]:
    args = [str(COMMAND), "--no-browser", "--http-port", "0", "--data-dir", str(data_dir),
            "--flow", str(FLOW), "--port", port]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        if line.startswith(PRINTED):
            # Keep reading so that the pipe never fills.
            threading.Thread(target=lambda: [None for _ in process.stdout], daemon=True).start()
            return process, line.strip().removeprefix(PRINTED)
    raise RuntimeError("brain-mapper did not print its address")


def advance_to(page, step: str) -> None:
    """Clicks Continue until the page shows ``step`` waiting (the label is typed on the way)."""
    target = f'{SCREEN}[data-step="{step}"][data-phase="waiting"]'
    while not page.locator(target).count():
        field = page.locator("#participant-label")
        if field.count():
            field.fill("P001")
            page.click("#advance")
            page.wait_for_function("document.getElementById('screen').dataset.screen !== 'participant'")
            continue
        page.click("#advance", timeout=5000)
        page.wait_for_timeout(200)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="folder for the PNGs (outside the repository)")
    out = parser.parse_args().out.resolve()
    if out == REPO or REPO in out.parents:
        raise SystemExit(f"{out} is inside the repository; give a folder outside it")
    out.mkdir(parents=True, exist_ok=True)
    device = PtyDevice(onhead_stream(), config_reply())
    data = Path(tempfile.mkdtemp(prefix="brain-mapper-words-shots-"))
    process, url = start_app(data, device.path)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 900})
            shot = lambda name: page.screenshot(path=str(out / f"{name}.png"))  # noqa: E731
            page.goto(url)
            page.click("#fs-enter")
            advance_to(page, "words")
            page.wait_for_selector("#task-instructions", state="visible")
            shot("instructions")
            page.click("#advance")
            page.wait_for_selector("#words-fixation", state="visible")
            shot("fixation")
            page.wait_for_selector("#words-word", state="visible")
            shot("word")
            page.wait_for_function("document.getElementById('words-word') === null")
            shot("blank")
            page.wait_for_selector("#recall-input:not([disabled])", state="visible", timeout=45_000)
            for text in ("anchor", "Bottle"):
                page.keyboard.type(text)
                page.keyboard.press("Enter")
            page.keyboard.type("dragon")
            page.keyboard.press("Enter")
            page.wait_for_selector("#recall-input.words-logged")
            shot("recall_logged")
            page.wait_for_selector("#recall-input:not(.words-logged)")
            page.keyboard.type("garden")
            shot("recall")
            page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=45_000)
            shot("between")
            page.click("#advance")
            page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]')
            page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=45_000)
            page.click("#advance")
            page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]')
            page.wait_for_selector("#task-instructions", state="visible")
            shot("recall_instructions")
            page.click("#advance")
            page.wait_for_selector("#recall-input:not([disabled])", state="visible", timeout=15_000)
            for text in ("contest", "feature"):
                page.keyboard.type(text)
                page.keyboard.press("Enter")
            page.wait_for_selector("#recall-input.words-logged")
            shot("delayed_recall_logged")
            page.wait_for_selector("#recall-input:not(.words-logged)")
            shot("delayed_recall")
            browser.close()
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
        device.hang_up()
    print(f"wrote the screenshots to {out}")


if __name__ == "__main__":
    main()

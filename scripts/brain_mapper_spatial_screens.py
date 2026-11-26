"""Screenshots (900x900) of the spatial memory task: the instructions and each state of a trial.

Usage: ``uv run python scripts/brain_mapper_spatial_screens.py <out-dir>``

Starts a pty device (``tests/python/pty_device.py``) and ``brain-mapper`` with the test flow
``tests/brain_mapper/flows/spatial.json`` and a temporary data folder, plays the practice in a
headless Chromium and saves ``01-instructions.png`` to ``10-between.png`` (``08-block-lit.png`` shows
the filled squares of a block trial, ``09-block-probe.png`` its probe). The out-dir must be
outside the repository (the script refuses a path inside it). Never opens a real serial port.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "python"))
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

FLOW = ROOT / "tests" / "brain_mapper" / "flows" / "spatial.json"
PRINTED = "brain-mapper at "


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    out = parser.parse_args().out_dir.resolve()
    if out == ROOT or ROOT in out.parents:
        sys.exit("the out-dir must be outside the repository")
    out.mkdir(parents=True, exist_ok=True)
    dev = PtyDevice(onhead_stream(), config_reply())
    data = Path(tempfile.mkdtemp(prefix="spatial-screens-"))
    command = Path(sys.executable).parent / "brain-mapper"
    proc = subprocess.Popen(
        [str(command), "--no-browser", "--data-dir", str(data), "--http-port", "0",
         "--port", dev.path, "--flow", str(FLOW)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        url = next(line.strip() for line in proc.stdout if line.startswith(PRINTED))
        url = url.removeprefix(PRINTED)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 900})
            page.goto(url)
            page.click("#fs-enter")
            page.wait_for_selector('#screen[data-screen="participant"]')
            page.fill("#participant-label", "P001")
            page.click("#advance")
            page.wait_for_selector("#task-instructions", state="visible")
            page.screenshot(path=out / "01-instructions.png")
            page.click("#practice")
            for i, part in enumerate(["fixation", "lit", "retention", "probe"]):
                page.wait_for_selector(
                    f'#task-area[data-trial="0"] #spatial-grid[data-part="{part}"]', timeout=20_000
                )
                page.screenshot(path=out / f"0{i + 2}-{part}.png")
            page.keyboard.press("j")
            page.wait_for_selector('#spatial-grid[data-part="feedback"]')
            page.screenshot(path=out / "06-feedback.png")
            for trial in (1, 2):
                page.wait_for_selector(
                    f'#task-area[data-trial="{trial}"][data-awaiting="true"]', timeout=20_000
                )
                page.keyboard.press("j")
            page.wait_for_selector("#advance", state="visible", timeout=20_000)
            page.screenshot(path=out / "07-after-practice.png")
            page.click("#advance")
            page.wait_for_selector(
                '#task-area[data-practice="false"] #spatial-grid[data-part="lit"]', timeout=20_000
            )
            page.screenshot(path=out / "08-block-lit.png")
            page.wait_for_selector(
                '#task-area[data-practice="false"] #spatial-grid[data-part="probe"]', timeout=20_000
            )
            page.screenshot(path=out / "09-block-probe.png")
            page.wait_for_selector('#screen[data-screen="between"]', timeout=90_000)
            page.screenshot(path=out / "10-between.png")
            browser.close()
    finally:
        proc.terminate()
        proc.wait()
        dev.hang_up()
        shutil.rmtree(data, ignore_errors=True)
    print(f"saved to {out}")


if __name__ == "__main__":
    main()

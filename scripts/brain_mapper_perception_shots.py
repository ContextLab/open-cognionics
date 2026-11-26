"""Screenshots of the perception task (instructions, then the fixation, grating, blank and
feedback of the first practice trial, then the between-blocks screen) on the simulated headset,
and the grating as the task draws it at a tilt of 8 and of 1 degree clockwise (grating-8deg.png,
grating-1deg.png: drawn onto the task's canvas during the first practice trial's blank, with the
page's own colors). Run from the repository:

    uv run python scripts/brain_mapper_perception_shots.py <out-dir> [W H]

For the screenshots only, every setTimeout delay in the page is made 8 times longer so that each
part of a trial (gap, fixation, grating, blank, feedback) stays on screen long enough to capture.
"""

import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(WT / "tests" / "brain_mapper"), str(WT / "tests" / "python")]

from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

from conftest import COMMAND, PRINTED, App, _Lines  # noqa: E402
from test_engine import session_folder_of  # noqa: E402
from test_shell_browser import advance_until, begin  # noqa: E402

FLOW = WT / "tests" / "brain_mapper" / "flows" / "perception.json"
SLOW = """
const real = window.setTimeout.bind(window);
window.setTimeout = (fn, ms, ...rest) => real(fn, (ms || 0) * 8, ...rest);
"""


def start(data: Path, port: str) -> App:
    args = [str(COMMAND), "--no-browser", "--data-dir", str(data), "--http-port", "0",
            "--flow", str(FLOW), "--port", port]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = _Lines(process.stdout).wait_for(PRINTED, timeout=30)
    return App(line.removeprefix(PRINTED), data, process)


def last_stim(folder: Path):
    lines = (folder / "events.jsonl").read_text().split("\n")[:-1]
    stims = [json.loads(x) for x in lines if '"perception.stim"' in x]
    return stims[-1]["payload"] if stims else None


def wait_what(folder: Path, what: str, trial: int, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = last_stim(folder)
        if p and p["part"] == what and p["trial"] == trial:
            return p
        time.sleep(0.03)
    raise AssertionError(f"no {what} for trial {trial}")

#: Draws the grating with the task's drawing module and the page's colors (ground: the body's
#: background; light: --stim) at a tilt, phase 0, onto the task's visible canvas.
DRAW_AT = """
async (tilt) => {
  const D = await import('/static/js/tasks/perception-draw.js');
  const canvas = document.getElementById('perception-canvas');
  const ctx = document.createElement('canvas').getContext('2d');
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--stim').trim();
  const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const light = hexRgb(ctx.fillStyle);
  const ground = getComputedStyle(document.body).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number);
  const off = document.createElement('canvas');
  const info = D.drawGrating(off, {sizeCss: canvas.getBoundingClientRect().width,
                                  dpr: window.devicePixelRatio || 1, tiltDeg: tilt, phaseDeg: 0,
                                  background: ground, light});
  canvas.getContext('2d').drawImage(off, 0, 0);
  return {background: D.hex(info.background), peak: D.hex(info.peak)};
}
"""


def main(out: Path, w: int, h: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="shots-perception-"))
    dev = PtyDevice(onhead_stream(), config_reply())
    a = start(root / "data", dev.path)
    tag = f"{w}x{h}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_context(viewport={"width": w, "height": h}).new_page()
            begin(a, page)
            advance_until(page, "perception", "waiting")
            page.wait_for_selector("#task-instructions", state="visible")
            page.wait_for_function("document.fonts.status === 'loaded'")
            page.screenshot(path=str(out / f"instructions-{tag}.png"))
            folder = session_folder_of(a)
            page.add_init_script(SLOW)
            page.evaluate(SLOW)
            page.click("#practice")
            for what in ("fixation", "grating"):
                p = wait_what(folder, what, 0)
                page.screenshot(path=str(out / f"practice-{what}-{tag}.png"))
            wait_what(folder, "blank", 0)
            page.screenshot(path=str(out / f"practice-blank-{tag}.png"))
            for tilt in (8, 1):
                print(f"grating at {tilt} deg:", page.evaluate(DRAW_AT, tilt))
                page.screenshot(path=str(out / f"grating-{tilt}deg.png"))
            page.keyboard.press("f" if p["side"] == "left" else "j")
            wait_what(folder, "feedback", 0)
            page.screenshot(path=str(out / f"practice-feedback-{tag}.png"))
            # The rest of practice (answer F each time), then block 1 to the between-blocks screen.
            ready = (
                "(() => { const b = document.getElementById('advance');"
                " const a = document.getElementById('task-area');"
                " return (b && b.offsetParent) || (a && a.dataset.awaiting === 'true'); })()"
            )
            while not page.locator("#advance:visible").count():
                page.wait_for_function(ready, timeout=120_000)
                if page.locator("#advance:visible").count():
                    break
                page.keyboard.press("f")
                page.wait_for_function(
                    "document.getElementById('task-area').dataset.awaiting !== 'true'", timeout=120_000
                )
            page.click("#advance")
            page.wait_for_selector('#screen[data-screen="between"]', timeout=180_000)
            page.screenshot(path=str(out / f"between-{tag}.png"))
            browser.close()
    finally:
        a.process.send_signal(signal.SIGINT)
        a.process.wait(timeout=30)
        dev.hang_up()


if __name__ == "__main__":
    size = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (900, 900)
    main(Path(sys.argv[1]), *size)

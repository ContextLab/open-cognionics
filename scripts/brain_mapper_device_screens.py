"""Saves one PNG per state of the brain-mapper device screen, and the text of every tip.

    uv run python scripts/brain_mapper_device_screens.py <out-dir> [--size 900x900]

Each state is reached with a real pseudo-terminal device (or a missing path, or a replayed
recording), the real app and Chromium: ``live``, ``no-data`` (a silent device), ``open-error`` (a
path that does not exist), ``no-configuration`` (a device that streams and never answers),
``dropped`` (a device that hangs up), ``lost-samples`` (a stream that skips samples) and
``replay``; ``start-check-failed`` is the start screen of an app run from a copy of the package
with one three.js file changed. ``no-dongle`` and ``several-dongles`` cannot be reached without
leaving ``--port`` out (which would open a plugged-in dongle), so their tip, made by the page's
own tip function from the problem the real ``oc`` errors give, is put into the screen of the
``open-error`` state. ``tips.txt`` lists every tip text. ``<out-dir>`` must be outside the
repository. Never opens a real serial port.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain_mapper_screens import enter, start_app, stop_app, wait_for  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from pty_device import PtyDevice, config_reply, onhead_stream  # noqa: E402

import open_cognionics as oc  # noqa: E402
from brain_mapper.source import describe  # noqa: E402

PACKAGE = REPO / "brain_mapper" / "src" / "brain_mapper"


def to_device(page, url: str) -> None:
    page.goto(url)
    wait_for(page, screen="start")
    enter(page)
    page.fill("#participant-label", "P001")
    page.click("#advance")
    wait_for(page, screen="device")


def settle(page, tip: str | None = None, live: bool = False, rows: int = 0) -> None:
    if tip:
        page.wait_for_selector(f'#device-tip[data-tip="{tip}"]:not([hidden])', timeout=60_000)
    if live:
        page.wait_for_selector('#device-link[data-state="live"]', timeout=30_000)
        page.wait_for_function("document.getElementById('device-rate').dataset.hz !== ''", timeout=30_000)
    if rows:
        page.wait_for_function(f"+document.getElementById('device-traces').dataset.rows > {rows}")
    page.wait_for_function("document.fonts.status === 'loaded'")
    page.wait_for_timeout(300)


def one(browser, size, out: Path, work: Path, name: str, device=None, port=None, replay=None, **wait) -> None:
    process, url = start_app(work / f"data-{name}", port=port or (device.path if device else None), replay=replay)
    try:
        context = browser.new_context(viewport=size)
        page = context.new_page()
        to_device(page, url)
        settle(page, **wait)
        page.screenshot(path=out / f"{name}.png")
        context.close()
    finally:
        stop_app(process)
        if device is not None:
            device.hang_up()


def tips_text(page) -> str:
    """Every tip, as the page would show it: the table of the spec, one block each."""
    none = describe(_error(oc.NoHeadset, []))
    several = describe(_error(oc.SeveralHeadsets, [
        oc.PortInfo("/dev/cu.example-A", "Cognionics Quick-20", None),
        oc.PortInfo("/dev/cu.example-B", "Cognionics Quick-20", None),
    ]))
    rows = [
        ("No dongle found", "connecting", none.as_dict()),
        ("More than one dongle", "connecting", several.as_dict()),
        ("Port cannot be opened", "connecting", {"code": "open_error", "kind": "OpenError", "msg": "could not open the port: No such file or directory", "ports": []}),
        ("Dongle found, nothing received", "connecting", {"code": "no_data", "kind": "NoData", "msg": "", "ports": []}),
        ("Data arriving, no configuration reply", "connecting", {"code": "no_configuration", "kind": "NoConfiguration", "msg": "", "ports": []}),
        ("Link dropped (before the device step is passed)", "connecting", {"code": "disconnected", "kind": "Disconnected", "msg": "", "ports": [], "dropped": True}),
    ]
    lines = []
    for seen, state, problem in rows:
        link = {"state": state, "reason": "", "problem": {"dropped": False, "attempt": 1, "retry_s": 2.0, **problem}}
        tip = page.evaluate("async link => (await import('/static/js/tips.js')).chooseTip(link, null)", link)
        lines.append(f"{seen}\n  [{tip['code']}] {tip['text']}\n")
    ended = page.evaluate(
        "async () => (await import('/static/js/tips.js')).chooseTip({state:'ended', problem:null}, null, true)"
    )
    lines.append(f"Link dropped (after the device step: the session has ended)\n  [{ended['code']}] {ended['text']}\n")
    lost = page.evaluate(
        "async () => (await import('/static/js/tips.js')).chooseTip({state:'live', problem:null}, {lost10: 12})"
    )
    lines.append(f"Samples being lost (12 in the last 10 seconds, as an example)\n  [{lost['code']}] {lost['text']}\n")
    fault = page.evaluate(
        "async () => (await import('/static/js/tips.js')).chooseTip("
        "{state:'ended', reason:'ValueError: example', fault:true, problem:null}, null, true)"
    )
    lines.append(f"An error inside brain-mapper (the hub thread failed)\n  [{fault['code']}] {fault['text']}\n")
    return "\n".join(lines)


def _error(kind, ports):
    try:
        oc.select_headset(ports)
    except kind as error:
        return error
    raise RuntimeError("select_headset did not raise")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--size", default="900x900", help="viewport, WIDTHxHEIGHT")
    args = parser.parse_args()
    out = args.out_dir.expanduser().resolve()
    if out == REPO or REPO in out.parents:
        print(f"refusing to write screenshots inside the repository: {out}", file=sys.stderr)
        return 2
    width, height = (int(x) for x in args.size.split("x"))
    size = {"width": width, "height": height}
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="brain-mapper-device-screens-"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            one(browser, size, out, work, "live", PtyDevice(onhead_stream(), config_reply()), live=True, rows=300)
            one(browser, size, out, work, "no-data", PtyDevice.silent_device(), tip="no_data")
            one(browser, size, out, work, "open-error", port=str(work / "no-such-port"), tip="open_error")
            one(browser, size, out, work, "no-configuration", PtyDevice(onhead_stream(), None), tip="no_configuration")
            one(browser, size, out, work, "lost-samples", PtyDevice.lossy_device(10), tip="lost_samples", rows=300)
            # dropped: the device hangs up while the screen is shown
            device = PtyDevice(onhead_stream(), config_reply())
            process, url = start_app(work / "data-dropped", port=device.path)
            try:
                page = browser.new_context(viewport=size).new_page()
                to_device(page, url)
                settle(page, live=True, rows=200)
                device.hang_up()
                settle(page, tip="dropped")
                page.screenshot(path=out / "dropped.png")
            finally:
                stop_app(process)
            # a replay
            recording = work / "recording"
            device = PtyDevice(onhead_stream(), config_reply())
            try:
                with oc.connect(device.path) as headset:
                    with headset.record(recording, participant="P001"):
                        time.sleep(15)
            finally:
                device.hang_up()
            one(browser, size, out, work, "replay", replay=recording, live=True, rows=300)
            # the start screen with a failed start-up check: a copy of the package, one file changed
            copy = work / "copy"
            shutil.copytree(PACKAGE, copy / "brain_mapper", ignore=shutil.ignore_patterns("__pycache__"))
            changed = copy / "brain_mapper" / "web" / "vendor" / "three" / "build" / "three.module.js"
            changed.write_bytes(changed.read_bytes() + b"\n// changed for the screenshot\n")
            device = PtyDevice(onhead_stream(), config_reply())
            os.environ["PYTHONPATH"] = str(copy)
            process, url = start_app(work / "data-check", port=device.path)
            os.environ.pop("PYTHONPATH")
            try:
                page = browser.new_context(viewport=size).new_page()
                page.goto(url)
                wait_for(page, screen="start")
                page.wait_for_selector("#checks-failed:not([hidden])")
                page.wait_for_function("document.fonts.status === 'loaded'")
                page.screenshot(path=out / "start-check-failed.png")
                (out / "tips.txt").write_text(tips_text(page), encoding="utf-8")
            finally:
                stop_app(process)
                device.hang_up()
            # the two states that need --port left out: their tips, in the screen of a missing port
            process, url = start_app(work / "data-two", port=str(work / "no-such-port"))
            try:
                page = browser.new_context(viewport=size).new_page()
                to_device(page, url)
                settle(page, tip="open_error")
                for name, error, ports in (
                    ("no-dongle", oc.NoHeadset, []),
                    ("several-dongles", oc.SeveralHeadsets, [
                        oc.PortInfo("/dev/cu.example-A", "Cognionics Quick-20", None),
                        oc.PortInfo("/dev/cu.example-B", "Cognionics Quick-20", None),
                    ]),
                ):
                    problem = describe(_error(error, ports)).as_dict()
                    link = {"state": "connecting", "reason": "", "problem": {"dropped": False, "attempt": 1, "retry_s": 2.0, **problem}}
                    tip = page.evaluate("async link => (await import('/static/js/tips.js')).chooseTip(link, null)", link)
                    page.evaluate(
                        "t => { const e = document.getElementById('device-tip'); e.textContent = t; e.hidden = false; }",
                        tip["text"],
                    )
                    page.screenshot(path=out / f"{name}.png")
            finally:
                stop_app(process)
            browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The signal check after the contact check, in a real Chromium over the real websocket, against
the simulated headset (``tests/python/pty_device.py``) playing synthetic streams made here.

The streams are packets in the headset's format (``docs/protocol.md``, section 2) built from
arrays: 20 EEG channels of 1/f-like noise of about 10 uV in 1 to 45 Hz, periodic over the 10 s
the device plays in a loop (no jump where it starts again). With the contact check on (after
``0x11``) the device plays the same signal with a 125 Hz tone of 0.3 mV (a good contact) and the
packets say the check is on. Some tests make channels bad in the stream with the check off: at
the converter's limit (clipped), constant (flat) or 150 uV of noise (noisy).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from brain_mapper import contact, quality
from test_calibration_browser import (
    SCREEN,
    last_check_on_before,
    marker_sample,
    to_calibration,
    wait_until,
)
from test_engine import SHORT, events_of
from test_onlooker_browser import open_onlooker
from test_participants import session_folder

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from pty_device import PtyDevice, config_reply  # noqa: E402
from shots import ShotsDir  # noqa: E402

FS = 500
N = 5000  # 10 s, played in a loop
VPC = 9.934107462565105e-08  # volts per count at gain 3 (docs/protocol.md, "Scaling")
LABELS = ["F7", "Fp1", "Fp2", "F8", "F3", "Fz", "F4", "C3", "Cz", "P8", "P7", "Pz", "P4", "T3", "P3", "O1", "O2", "C4", "T4", "A2"]
SHOTS = ShotsDir("shots-night-quality")
SIZES = [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)]


def periodic_noise(rms: float, seed: int, channels: int = 20) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal((N, channels)), axis=0)
    freqs = np.fft.rfftfreq(N, 1 / FS)
    spectrum[0] = 0
    spectrum[1:] /= np.sqrt(freqs[1:])[:, None]
    x = np.fft.irfft(spectrum, n=N, axis=0)
    passed = quality.band_pass(x, FS)
    return x * rms / passed.std(axis=0)


def encode(volts: np.ndarray, check_on: bool) -> bytes:
    """Packets of 75 bytes: sync, counter (the device rewrites it), 23 channels of 3 bytes (the
    3 accelerometer channels 0), the contact-check status, the battery byte, the trigger."""
    counts = np.clip(np.round(volts / VPC).astype(np.int64), -(2**23), 2**23 - 8)
    u = (counts >> 3) & 0x1FFFFF
    b = np.stack([((u >> 14) & 0x7F) << 1, ((u >> 7) & 0x7F) << 1, (u & 0x7F) << 1], axis=-1)
    out = bytearray()
    status = 0x11 if check_on else 0x00
    for k in range(len(volts)):
        out += bytes([0xFF, 0]) + bytes(b[k].astype(np.uint8).ravel()) + bytes(9) + bytes([status, 105, 0, 0])
    assert len(out) == 75 * len(volts)
    return bytes(out)


def streams(bad: dict[str, str] | None = None, tone_v: float = 0.3e-3) -> tuple[bytes, bytes]:
    """The check-off and check-on streams, with the channels of ``bad`` made clipped, flat or noisy
    in the check-off stream; ``tone_v`` is the tone's amplitude with the check on."""
    x = periodic_noise(10e-6, seed=1)
    tone = tone_v * np.sin(2 * np.pi * 125 * np.arange(N) / FS)[:, None]
    on = x + tone
    off = x.copy()
    for name, how in (bad or {}).items():
        c = LABELS.index(name)
        if how == "clipped":
            off[:, c] = quality.full_scale(VPC)
        elif how == "flat":
            off[:, c] = 2e-3
        elif how == "noisy":
            off[:, c] = periodic_noise(150e-6, seed=7 + c, channels=1)[:, 0]
    return encode(off, False), encode(on, True)


def calibration_flow(tmp_path) -> Path:
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": 3.0, "eyes_closed": 3.0}
    path = tmp_path / "calibration-flow.json"
    path.write_text(json.dumps(steps))
    return path


def make_device(bad=None, tone_v=0.3e-3) -> PtyDevice:
    off, on = streams(bad, tone_v)
    return PtyDevice(off, config_reply(), contact_stream=on, contact_on=True)


@pytest.fixture
def calib_flow(tmp_path) -> Path:
    """The calibration flow of the calibration tests (``test_calibration_browser.calib_flow``)."""
    return calibration_flow(tmp_path)


@pytest.fixture
def bad_device():
    dev = make_device({"Fp1": "noisy", "Cz": "clipped", "O2": "flat"})
    yield dev
    dev.hang_up()


@pytest.fixture
def clean_device():
    dev = make_device()
    yield dev
    dev.hang_up()


def calibration_events(folder) -> list[dict]:
    return [e for e in events_of(folder) if e["step"] == "calibration"]


def names(folder) -> list[str]:
    return [e["name"] for e in calibration_events(folder)]


def continue_from_contact(page) -> None:
    page.wait_for_selector("#contact-count[data-good]", timeout=30_000)
    page.wait_for_selector("#advance:not([disabled])", timeout=10_000)
    page.click("#advance")


def wait_signal_done(page) -> None:
    page.wait_for_selector(f'{SCREEN}[data-substep="signal_done"]', timeout=40_000)
    page.wait_for_selector("#signal-headline[data-done]", timeout=5_000)


def test_the_streams_decode_to_what_was_encoded():
    """The synthetic packets decode, by the formula of docs/protocol.md (section 2), to the signal
    they were made from (the browser tests below then read them through the library)."""
    off, _ = streams({"Cz": "clipped"})
    # the counts of each channel, decoded as docs/protocol.md says
    data = np.frombuffer(off, dtype=np.uint8).reshape(N, 75)
    ch = data[:, 2 : 2 + 60].reshape(N, 20, 3).astype(np.int64)
    raw = ((ch[..., 0] << 24) | (ch[..., 1] << 17) | (ch[..., 2] << 10)).astype(np.uint32).view(np.int32) >> 8
    volts = raw * VPC
    expected = np.clip(np.round(periodic_noise(10e-6, seed=1) / VPC), -(2**23), 2**23 - 8) * VPC
    keep = [k for k in range(20) if LABELS[k] != "Cz"]
    assert np.max(np.abs(volts[:, keep] - expected[:, keep])) <= 8 * VPC
    assert volts[0, LABELS.index("Cz")] >= quality.RAIL_SHARE * quality.full_scale(VPC)


def test_bad_channels_are_named_on_screen_and_in_calibration_json(app, bad_device, calib_flow, page, browser):
    a = app(port=bad_device.path, flow=calib_flow)
    onlooker_context, onlooker = open_onlooker(browser, a)
    to_calibration(a, page)
    folder = session_folder(a)
    page.wait_for_selector("#contact-count[data-good]", timeout=30_000)
    assert not page.locator("#contact-no-tone").is_visible()  # the tone is there
    continue_from_contact(page)
    # the signal check runs with the tone off, after the settling wait, with a countdown
    page.wait_for_selector(f'{SCREEN}[data-substep="signal"]', timeout=30_000)
    page.wait_for_function("/\\d+ s left/.test(document.getElementById('signal-left').textContent)")
    assert page.locator("#signal-panel").is_visible()
    assert not page.locator("#advance").is_visible()
    wait_signal_done(page)
    events = calibration_events(folder)
    order = [e["name"] for e in events]
    assert order.index("contact_end") < order.index("settling_start") < order.index("signal_start") < order.index("signal_end")
    # the window starts after the last sample with the check on plus the settling wait
    start = marker_sample(folder, "signal_start")
    last_on, rate = last_check_on_before(folder, start)
    assert start - last_on >= contact.settle_samples(rate)
    # The exception first, in plain words (since 2026-11-26; before:
    # "20 of 20 contacts good; 1 channel noisy: Fp1; 1 channel clipped: Cz; 1 channel flat: O2").
    headline = page.inner_text("#signal-headline")
    assert headline == "Fp1 is noisy. Cz is clipped. O2 is flat. All 20 contacts are good.", headline
    assert page.locator("#signal-warn").is_visible()
    assert "not fine" in page.inner_text("#signal-warn")
    # Channels of more than one state not fine: Continue says so.
    assert page.inner_text("#advance") == "Continue with channels that are not fine"
    # The full list is behind "All channels", closed at first; opened, every status is in full.
    assert not page.locator("#signal-list").is_visible()
    page.click("#signal-all summary")
    assert page.locator("#signal-list").is_visible()
    clipped = page.evaluate(
        "[...document.querySelectorAll('#signal-list .signal-words')].filter((e) => e.scrollWidth > e.clientWidth + 1).length"
    )
    assert clipped == 0
    for name, state, word in (("Fp1", "noisy", "Noisy"), ("Cz", "clipped", "Clipped"), ("O2", "flat", "Flat"), ("F3", "fine", "Fine")):
        item = page.locator(f'#signal-list li[data-contact="{name}"]')
        assert item.get_attribute("data-signal") == state
        assert item.inner_text().endswith(f"Good · {word}")
    # the fit view: the worse of contact and signal
    drawn = page.evaluate(
        "() => { const h = window.__calibration.view.headset; return Object.fromEntries(h.contacts.map((c) => [c.name, h.state(c.name)])); }"
    )
    assert drawn["Fp1"] == "marginal" and drawn["Cz"] == "bad" and drawn["O2"] == "bad" and drawn["F3"] == "good"
    # calibration.json: the existing fields as before, the new one added
    saved = json.loads((folder / "calibration.json").read_text())
    assert {"states", "excluded", "thresholds_version", "unix_time"} <= set(saved)
    assert saved["thresholds_version"] == contact.THRESHOLDS.version
    signal = saved["signal"]
    assert signal["not_fine"] == ["Fp1", "Cz", "O2"]
    assert signal["states"]["Fp1"] == "noisy" and signal["states"]["Cz"] == "clipped" and signal["states"]["O2"] == "flat"
    assert sum(1 for s in signal["states"].values() if s == "fine") == 17
    assert signal["version"] == quality.VERSION and signal["seconds"] == quality.SIGNAL_S
    end = [e for e in events if e["name"] == "signal_end"][0]
    assert end["payload"]["not_fine"] == ["Fp1", "Cz", "O2"] and end["payload"]["fine"] == 17
    # the onlooker shows the result as counts (its line is not changed by the change of
    # 2026-11-26, which concerns the operator's screen; before, both said the same)
    onlooker.wait_for_function("(document.getElementById('ol-signal') || {}).textContent?.includes('1 channel clipped: Cz')", timeout=10_000)
    assert onlooker.inner_text("#ol-signal") == (
        "Signal check: 20 of 20 contacts good; 1 channel noisy: Fp1; 1 channel clipped: Cz; 1 channel flat: O2"
    )
    SHOTS.mkdir(parents=True, exist_ok=True)
    onlooker.screenshot(path=str(SHOTS / "onlooker-signal-bad.png"))
    # Continue is allowed: eyes open follows
    page.wait_for_selector("#advance:not([disabled])", timeout=5_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-substep="eyes_open"]', timeout=10_000)
    assert names(folder)[-1] == "eyes_open_start"
    assert set(bad_device.written()) <= {0x11, 0x12, 0x14}
    onlooker_context.close()


def test_check_again_repeats_both(app, bad_device, calib_flow, page):
    a = app(port=bad_device.path, flow=calib_flow)
    to_calibration(a, page)
    folder = session_folder(a)
    continue_from_contact(page)
    wait_signal_done(page)
    ons = bytes(b for b in bad_device.written() if b != 0x14).count(0x11)
    page.click("#check-again")
    page.wait_for_selector(f'{SCREEN}[data-substep="contact"]', timeout=10_000)
    wait_until(lambda: bytes(b for b in bad_device.written() if b != 0x14).count(0x11) == ons + 1, what="0x11 again", page=page)
    continue_from_contact(page)
    wait_signal_done(page)
    got = names(folder)
    assert got.count("contact_start") == 2 and got.count("contact_end") == 2
    assert got.count("signal_start") == 2 and got.count("signal_end") == 2
    saved = json.loads((folder / "calibration.json").read_text())
    assert saved["signal"]["not_fine"] == ["Fp1", "Cz", "O2"]


def test_all_fine_shows_no_warning_and_continue_works(app, clean_device, calib_flow, page):
    a = app(port=clean_device.path, flow=calib_flow)
    to_calibration(a, page)
    folder = session_folder(a)
    continue_from_contact(page)
    wait_signal_done(page)
    # Before 2026-11-26: "20 of 20 contacts good; all 20 channels fine".
    assert page.inner_text("#signal-headline") == "All 20 channels look fine. All 20 contacts are good."
    assert page.inner_text("#advance") == "Continue"
    assert not page.locator("#signal-warn").is_visible()
    assert page.locator("#signal-list li.is-off").count() == 0
    saved = json.loads((folder / "calibration.json").read_text())
    assert saved["signal"]["not_fine"] == []
    page.wait_for_selector("#advance:not([disabled])", timeout=5_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-substep="eyes_open"]', timeout=10_000)


def test_a_link_drop_during_the_signal_check_pauses_and_the_part_restarts(app, clean_device, calib_flow, page):
    a = app(port=clean_device.path, flow=calib_flow)
    to_calibration(a, page)
    folder = session_folder(a)
    continue_from_contact(page)
    page.wait_for_selector(f'{SCREEN}[data-substep="signal"]', timeout=30_000)
    clean_device.go_silent()
    wait_until(lambda: "link_lost" in [e["name"] for e in events_of(folder)], timeout=20, what="link_lost", page=page)
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=10_000)
    assert "signal_end" not in names(folder)
    clean_device.speak_again()
    wait_until(lambda: "link_back" in [e["name"] for e in events_of(folder)], timeout=40, what="link_back", page=page)
    page.wait_for_selector("#fs-return:not([disabled])", timeout=10_000)
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]', timeout=10_000)
    wait_signal_done(page)
    events = calibration_events(folder)
    restarted = [e for e in events if e["name"] == "part_restarted"]
    assert [(e["payload"]["part"], e["payload"]["reason"]) for e in restarted] == [("signal", "link_lost")]
    starts = [e for e in events if e["name"] == "signal_start"]
    assert len(starts) == 2 and starts[1]["seq"] > restarted[0]["seq"]
    end = [e for e in events if e["name"] == "signal_end"]
    assert len(end) == 1 and end[0]["seq"] > starts[1]["seq"]
    # the result counts only data from after the restart: 6 s of them
    assert json.loads((folder / "calibration.json").read_text())["signal"]["seconds"] == quality.SIGNAL_S


@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
def test_layout_of_the_signal_check_at_four_sizes(app, bad_device, calib_flow, browser, size):
    w, h = size
    context = browser.new_context(viewport={"width": w, "height": h})
    page = context.new_page()
    try:
        a = app(port=bad_device.path, flow=calib_flow)
        to_calibration(a, page)
        continue_from_contact(page)
        page.wait_for_selector(f'{SCREEN}[data-substep="signal"]', timeout=30_000)
        page.wait_for_function("/\\d+ s left/.test(document.getElementById('signal-left').textContent)")
        SHOTS.mkdir(parents=True, exist_ok=True)
        if size in [(3840, 1600), (1512, 982)]:
            page.screenshot(path=str(SHOTS / f"signal-running-{w}x{h}.png"))
        wait_signal_done(page)
        page.wait_for_function("window.__calibration.view && !window.__calibration.loading")
        page.click("#signal-all summary")  # the full list, behind "All channels", must fit open too
        page.wait_for_timeout(500)
        if size in [(3840, 1600), (1512, 982)]:
            page.screenshot(path=str(SHOTS / f"signal-done-{w}x{h}.png"))
        boxes = page.evaluate(
            """() => {
              const ids = ["signal-headline", "signal-warn", "signal-list", "calib-view", "advance", "check-again"];
              const out = {};
              for (const id of ids) { const r = document.getElementById(id).getBoundingClientRect(); out[id] = [r.left, r.top, r.right, r.bottom]; }
              const items = [...document.querySelectorAll("#signal-list li")].map((e) => { const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; });
              const font = parseFloat(getComputedStyle(document.querySelector("#signal-list li")).fontSize);
              return { out, items, font, sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight, iw: innerWidth, ih: innerHeight };
            }"""
        )
        assert boxes["sw"] <= boxes["iw"] and boxes["sh"] <= boxes["ih"], (w, h, boxes)
        for name, (left, top, right, bottom) in list(boxes["out"].items()) + [(f"item{k}", b) for k, b in enumerate(boxes["items"])]:
            assert 0 <= left and right <= w and 0 <= top and bottom <= h, (w, h, name, (left, top, right, bottom))
            assert right > left and bottom > top, (w, h, name)
        assert len(boxes["items"]) == 20
        # Before 2026-11-26: 14 to 17 px; the larger type is about 30 px at 3840 x 1600.
        assert 14 <= boxes["font"] <= 32
        # in a centered column: nothing at the far edges of a wide screen
        left = min(b[0] for b in boxes["out"].values())
        right = max(b[2] for b in boxes["out"].values())
        assert abs((left + right) / 2 - w / 2) < 0.1 * w, (w, left, right)
    finally:
        context.close()


def test_a_contact_check_without_any_tone_says_so_and_the_signal_check_still_names_bad_channels(app, calib_flow, page):
    """The packets say the check is on but no tone comes:
    every contact reads good, the contact check says the states may be wrong, and the signal
    check names the noisy channel."""
    device = make_device({"Pz": "noisy"}, tone_v=0.0)
    try:
        a = app(port=device.path, flow=calib_flow)
        to_calibration(a, page)
        page.wait_for_selector("#contact-count[data-good]", timeout=30_000)
        page.wait_for_selector("#contact-no-tone:visible", timeout=10_000)
        assert "tone is not showing on any electrode" in page.inner_text("#contact-no-tone")
        assert page.inner_text("#contact-count").startswith("20 of 20 contacts are good")
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "contact-no-tone.png"))
        continue_from_contact(page)
        wait_signal_done(page)
        # Before 2026-11-26: "20 of 20 contacts good; 1 channel noisy: Pz".
        assert page.inner_text("#signal-headline") == "Pz is noisy. All 20 contacts are good."
        assert page.inner_text("#advance") == "Continue with a noisy channel"
    finally:
        device.hang_up()

"""The layout of every screen outside a running task, from real layout boxes in real Chromium.

The screens are checked at 3840 x 1600 CSS px (a wide display, full screen); they are also
checked at 1920 x 1080, 1512 x 982 and 1024 x 768. Each test walks a real session (the real
server, the simulated headset of ``tests/python/pty_device.py``) to a screen and then, at each
size, measures the page with ``MEASURE``:

- nothing is outside the viewport, and the page scrolls neither way;
- the content (the union of the boxes of the screen's visible elements) lies in a band of at
  most ``BAND`` px (``WIDE_BAND`` at 3840 px wide) centered on the viewport, and its center is within a tolerance of the
  viewport's center; the HUD's three parts the same (``HUD_BAND``);
- the HUD is inside the viewport and overlaps no element of the screen;
- every visible line of body text is 16 to 44 px; every heading at most 80 px (the larger type
  of 2026-11-26); every text at least
  4.5:1 against the ground it is drawn on.

``unstyled()`` checks the stylesheets as loaded: every element of the screen that has a class has
at least one rule that applies at this size and matches it through that class. This is what a
stylesheet cut short by a lost brace fails (device setup and calibration were unstyled at every
width above 760 px on the night of 2026-11-25), and ``KEY`` checks that some elements of each
screen have the computed styles their screen depends on.

With ``$BRAIN_MAPPER_SHOTS`` set, a screenshot of each screen at each size is saved there.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path


from test_engine import SHORT, accept, open_ws, session_folder_of
from test_import_browser import drop_file
from test_link_pause import Plug, until
from test_package import play_through, wait_packaged
from test_participants import stop
from test_shell_browser import SCREEN, advance_until, begin, press
from test_whole_session_browser import contact_device

SIZES = [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)]
#: The widest the content of a screen may be, centered (2400 px at 3840 x 1600: the wider column
#: of 2026-11-26).
BAND = 1440
WIDE_BAND = {3840: 2400}
#: The widest the onlooker window's content may be (a display for someone watching, not a participant screen).
ONLOOKER_BAND = 1800
#: The widest the HUD may be, centered.
HUD_BAND = 1600
#: How far the center of the content may be from the center of the viewport.
CENTER_TOLERANCE = 0.03
NAVY = "rgb(18, 24, 38)"
#: Numbers shown as figures, not read as text: the points, the link figures, the contact count.
FIGURES = (
    "#hud-points, .stat-value, #between-gained, .contact-count, #time-left, #ol-points"
)

MEASURE = r"""
({band, hudBand, tol, figures}) => {
  const FIGURES = figures;
  const vw = innerWidth, vh = innerHeight;
  const problems = [];
  const name = (e) => e.id ? '#' + e.id : (e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).join('.') : ''));
  const shown = (e) => {
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(e);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0.01;
  };
  const doc = document.documentElement;
  if (doc.scrollWidth > vw + 1) problems.push(`the page is ${doc.scrollWidth} px wide in a ${vw} px window`);
  if (doc.scrollHeight > vh + 1) problems.push(`the page is ${doc.scrollHeight} px high in a ${vh} px window`);
  for (const e of [document.body, doc]) {
    if (e.scrollHeight > e.clientHeight + 1 && getComputedStyle(e).overflowY !== 'visible' && e === document.body)
      problems.push('the body scrolls');
  }

  // What is shown: the pause overlay when it is up, else the screen; the onlooker page its root.
  const overlay = document.getElementById('fs-lost');
  let roots;
  if (overlay && shown(overlay)) roots = [overlay.querySelector('.card')];
  else if (document.getElementById('ol-root')) roots = [document.getElementById('ol-root')];
  else roots = [...document.getElementById('screen').children].filter(shown);
  // The 3-D view and the video player draw inside their own boxes; their insides are not measured.
  const inside = (e) => e.closest('.scene3d .labels, .video-stage iframe, svg *');
  const boxes = [];
  for (const root of roots) {
    for (const e of [root, ...root.querySelectorAll('*')]) {
      if (inside(e) || !shown(e)) continue;
      const r = e.getBoundingClientRect();
      boxes.push([e, r]);
      if (r.left < -1 || r.top < -1 || r.right > vw + 1 || r.bottom > vh + 1)
        problems.push(`${name(e)} is cut off: ${Math.round(r.left)},${Math.round(r.top)} to ${Math.round(r.right)},${Math.round(r.bottom)}`);
    }
  }
  const union = (list) => list.reduce((u, [, r]) => ({
    left: Math.min(u.left, r.left), right: Math.max(u.right, r.right),
    top: Math.min(u.top, r.top), bottom: Math.max(u.bottom, r.bottom)}),
    {left: Infinity, right: -Infinity, top: Infinity, bottom: -Infinity});
  const centered = (u, width, what) => {
    const lo = (vw - Math.min(width, vw)) / 2 - 1, hi = (vw + Math.min(width, vw)) / 2 + 1;
    if (u.left < lo || u.right > hi)
      problems.push(`${what} spans ${Math.round(u.left)} to ${Math.round(u.right)}, outside the centered ${width} px band (${Math.round(lo)} to ${Math.round(hi)})`);
    const off = Math.abs((u.left + u.right) / 2 - vw / 2);
    if (off > Math.max(8, tol * vw)) problems.push(`${what} is ${Math.round(off)} px off center`);
  };
  const content = union(boxes);
  if (boxes.length) centered(content, band, 'the content');

  const hud = document.getElementById('hud');
  let hudBox = null;
  if (hud && shown(hud) && !(overlay && shown(overlay))) {
    const parts = [...hud.children].filter(shown).map((e) => [e, e.getBoundingClientRect()]);
    hudBox = union(parts);
    centered(hudBox, hudBand, 'the HUD');
    if (hudBox.top < 0) problems.push(`the HUD is cut off at the top (${Math.round(hudBox.top)})`);
    for (const [e, r] of boxes) {
      for (const [p, q] of parts) {
        if (r.left < q.right && q.left < r.right && r.top < q.bottom && q.top < r.bottom)
          problems.push(`${name(e)} overlaps the HUD's ${name(p)}`);
      }
    }
  }

  const parse = (c) => (c.match(/[\d.]+/g) || [0, 0, 0, 0]).map(Number).concat([1]).slice(0, 4);
  const over = (top, under) => [0, 1, 2].map((i) => top[i] * top[3] + under[i] * (1 - top[3])).concat([1]);
  const groundOf = (e) => {
    const layers = [];
    for (let n = e; n; n = n.parentElement) {
      const bg = parse(getComputedStyle(n).backgroundColor);
      if (bg[3] > 0) layers.push(bg);
      if (bg[3] >= 1) break;
    }
    let ground = parse(getComputedStyle(document.body).backgroundColor);
    for (const layer of layers.reverse()) ground = over(layer, ground);
    return ground;
  };
  const lum = (c) => {
    const [r, g, b] = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  // Type: body text 16 to 44 px, headings at most 80 px.
  const sizes = [];
  const texts = [...boxes];
  if (hudBox) for (const e of hud.querySelectorAll('*')) if (shown(e)) texts.push([e, e.getBoundingClientRect()]);
  for (const [e] of texts) {
    const own = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    if (!own) continue;
    const px = parseFloat(getComputedStyle(e).fontSize);
    const heading = e.closest('h1, h2');
    const figure = e.closest(FIGURES);
    if (heading) {
      if (px > 80) problems.push(`${name(e)} is set at ${px} px (a heading is at most 80 px)`);
      if (px < 16) problems.push(`${name(e)} is set at ${px} px (under 16 px)`);
    } else if (figure) {
      if (px < 16 || px > 160) problems.push(`${name(e)} is set at ${px} px (a figure is 16 to 160 px)`);
    } else if (px < 16 || px > 44) {
      problems.push(`${name(e)} "${e.textContent.trim().slice(0, 30)}" is set at ${px} px (body text is 16 to 44 px)`);
    }
    sizes.push(px);
    // Readable: at least 4.5:1 against the ground it is drawn on (rgba grounds laid over the page).
    const fg = parse(getComputedStyle(e).color);
    const ratio = contrast(fg, groundOf(e));
    if (ratio < 4.5) problems.push(`${name(e)} "${e.textContent.trim().slice(0, 30)}" has a contrast of ${ratio.toFixed(2)}:1`);
  }
  return {problems, content, hud: hudBox, vw, vh};
}
"""

UNSTYLED = r"""
(rootSelector) => {
  const active = [];
  const walk = (rules) => {
    for (const r of rules) {
      if (r instanceof CSSStyleRule) {
        active.push(r.selectorText);
        if (r.cssRules && r.cssRules.length) walk(r.cssRules);
      } else if (r instanceof CSSMediaRule) {
        if (matchMedia(r.conditionText).matches) walk(r.cssRules);
      } else if (typeof CSSSupportsRule !== 'undefined' && r instanceof CSSSupportsRule) {
        if (CSS.supports(r.conditionText)) walk(r.cssRules);
      } else if (r.cssRules) {
        walk(r.cssRules);
      }
    }
  };
  for (const sheet of document.styleSheets) walk(sheet.cssRules);
  const split = (s) => {
    const out = []; let depth = 0, start = 0;
    for (let i = 0; i < s.length; i++) {
      if (s[i] === '(') depth++;
      else if (s[i] === ')') depth--;
      else if (s[i] === ',' && depth === 0) { out.push(s.slice(start, i).trim()); start = i + 1; }
    }
    out.push(s.slice(start).trim());
    return out;
  };
  const parts = active.flatMap(split);
  const strip = (p) => p
    .replace(/::?(before|after|placeholder|marker|selection|-webkit-[\w-]+)/g, '')
    .replace(/:(hover|active|focus-visible|focus-within|focus|visited)/g, '');
  const matches = (e, p) => { try { return p.trim() !== '' && e.matches(p); } catch (err) { return false; } };
  const missing = [];
  const root = document.querySelector(rootSelector);
  for (const e of [root, ...root.querySelectorAll('[class]')]) {
    if (!e.classList || !e.classList.length) continue;
    for (const c of e.classList) {
      const re = new RegExp('\\.' + CSS.escape(c) + '(?![\\w-])');
      if (!parts.some((p) => re.test(p) && matches(e, strip(p)))) {
        missing.push(`${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''} .${c}`);
      }
    }
  }
  return [...new Set(missing)];
}
"""

#: Computed styles each screen depends on: (selector, property, expected value).
KEY = {
    "device": [
        (".device-steps", "list-style-type", "none"),
        (".device-steps", "display", "grid"),
        (".device-stats", "display", "flex"),
        (".device", "display", "grid"),
    ],
    "calib-contact": [
        (".calib", "display", "grid"),
        (".calib-view", "grid-row-start", "1"),
        (".calib-legend", "list-style-type", "none"),
        (".calib-panel", "display", "flex"),
    ],
    "between": [(".between", "display", "grid")],
}


def shots_dir() -> Path | None:
    folder = os.environ.get("BRAIN_MAPPER_SHOTS")
    if not folder:
        return None
    Path(folder).mkdir(parents=True, exist_ok=True)
    return Path(folder)


def settle(page) -> None:
    """Two animation frames: the layout of the new size is done and painted."""
    page.evaluate(
        "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


def check(
    page,
    name: str,
    tmp_path: Path | None = None,
    key: str | None = None,
    root: str = SCREEN,
    band: int = BAND,
) -> None:
    """Measures the page at each size (and checks its stylesheets at 1920 x 1080); restores the
    first viewport. Raises with every problem found at every size."""
    problems: list[str] = []
    folder = shots_dir()
    # The viewport is set through the DevTools protocol: a window in full screen cannot be
    # resized, and leaving full screen would pause a block.
    cdp = page.context.new_cdp_session(page)
    for width, height in SIZES:
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        settle(page)
        found = page.evaluate(
            MEASURE,
            {
                "band": WIDE_BAND.get(width, band) if band == BAND else band,
                "hudBand": HUD_BAND,
                "tol": CENTER_TOLERANCE,
                "figures": FIGURES,
            },
        )
        problems += [f"{name} at {width}x{height}: {p}" for p in found["problems"]]
        # Through the protocol too: Playwright's own screenshot puts back its own viewport first.
        if folder is not None:
            shot = cdp.send("Page.captureScreenshot", {"format": "png"})
            (folder / f"{name}-{width}x{height}.png").write_bytes(
                base64.b64decode(shot["data"])
            )
        if (width, height) == (1920, 1080):
            missing = page.evaluate(UNSTYLED, root)
            problems += [f"{name}: no rule styles {m}" for m in missing]
            background = page.evaluate(
                "getComputedStyle(document.body).backgroundColor"
            )
            if background != NAVY:
                problems.append(
                    f"{name}: the page ground is {background}, not the dark ground"
                )
            for selector, prop, value in KEY.get(key or name, []):
                got = page.eval_on_selector(
                    selector,
                    f"e => getComputedStyle(e).getPropertyValue({json.dumps(prop)})",
                )
                if got != value:
                    problems.append(
                        f"{name}: {selector} has {prop} {got!r}, not {value!r}"
                    )
    cdp.send("Emulation.clearDeviceMetricsOverride")
    cdp.detach()
    assert not problems, "\n".join(problems)


def layout_flow(tmp_path: Path) -> Path:
    """The short flow with a calibration of 6 s eyes open and closed, 6 s word blocks (time to
    leave full screen inside one) and 1 s blocks elsewhere."""
    steps = json.loads(SHORT.read_text())
    for step in steps:
        if step["id"] == "calibration":
            step["calibration"] = {"eyes_open": 6.0, "eyes_closed": 6.0}
        for block in step["blocks"]:
            block["seconds"] = (
                6 if step["id"] in ("words", "rest_closed", "rest_open") else 1
            )
    path = tmp_path / "layout-flow.json"
    path.write_text(json.dumps(steps))
    return path


def substep(page, name: str, timeout: float = 30_000) -> None:
    page.wait_for_selector(
        f'{SCREEN}[data-screen="calibration"][data-substep="{name}"]', timeout=timeout
    )


def test_every_screen_from_the_start_to_the_summary_fits_at_four_sizes(
    app, page, tmp_path
):
    dev = contact_device()
    try:
        a = app(port=dev.path, flow=layout_flow(tmp_path))
        page.goto(a.url + "/?test=1")
        page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
        page.wait_for_function("document.fonts.status === 'loaded'")
        check(page, "start", tmp_path)
        page.click('.route-pick[data-step="words"]')
        page.wait_for_selector("#play-only", state="visible")
        check(page, "start-one-round-marked", tmp_path)
        page.click(
            '.route-pick[data-step="words"]'
        )  # unmarked again: the whole route is played
        page.wait_for_selector("#play-only", state="hidden")
        page.click("#fs-enter")
        page.wait_for_function("document.fullscreenElement !== null")
        page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
        check(page, "participant", tmp_path)
        page.fill("#participant-label", "LAYOUT1")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        page.wait_for_function(
            "/\\d/.test(document.getElementById('device-rate').textContent)",
            timeout=30_000,
        )
        check(page, "device", tmp_path)
        page.click("#advance")
        substep(page, "contact")
        page.wait_for_selector("#contact-count[data-good]", timeout=30_000)
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        check(page, "calib-contact", tmp_path)
        page.click("#advance")
        # Since the signal check (night-quality) the settling wait leads into the signal check and
        # its result, which waits for Continue; then the timed parts follow. (The 6 s signal part
        # itself can be over before a check of the settling screen at four sizes is.)
        for sub in ("settling", "signal_done", "eyes_open", "eyes_closed", "blinks", "done"):
            substep(page, sub, timeout=60_000)
            if sub == "signal_done":
                page.wait_for_selector("#signal-headline[data-done]", timeout=10_000)
            check(page, f"calib-{sub}", tmp_path)
            if sub == "signal_done":
                page.wait_for_selector("#advance:not([disabled])", timeout=10_000)
                page.click("#advance")
        assert page.evaluate("document.fullscreenElement !== null")
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-step="words"][data-phase="waiting"]')
        press(page)
        page.wait_for_selector(f'{SCREEN}[data-step="words"][data-phase="running"]')
        page.evaluate("document.exitFullscreen()")
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
        page.wait_for_selector("#fs-lost", state="visible")
        check(page, "paused-fullscreen", tmp_path, root="#fs-lost")
        page.click("#fs-return")
        page.wait_for_selector("#fs-lost", state="hidden")
        page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=30_000)
        check(page, "between", tmp_path)
        advance_until(page, "rest_closed", "waiting")
        check(page, "rest-closed-waiting", tmp_path)
        press(page)
        page.wait_for_selector(
            f'{SCREEN}[data-step="rest_closed"][data-phase="running"]'
        )
        check(page, "rest-closed-running", tmp_path)
        page.wait_for_selector(
            f'{SCREEN}[data-step="rest_closed"][data-phase="between"]', timeout=20_000
        )
        check(page, "rest-over", tmp_path)
        press(page)
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="waiting"]')
        press(page)
        page.wait_for_selector(f'{SCREEN}[data-step="rest_open"][data-phase="running"]')
        check(page, "rest-open-running", tmp_path)
        page.wait_for_selector(
            f'{SCREEN}[data-step="rest_open"][data-phase="between"]', timeout=20_000
        )
        advance_until(page, "summary", "waiting")
        page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
        check(page, "summary-waiting", tmp_path)
        page.click("#advance")
        page.wait_for_selector("#summary-download", state="visible", timeout=30_000)
        check(page, "summary-finished", tmp_path)
    finally:
        dev.hang_up()


def test_device_tips_fit_at_four_sizes(app, page, tmp_path):
    """The tip of a port that cannot be opened, and the tip of a link that dropped."""
    missing = tmp_path / "no-such-port"
    a = app(port=str(missing))
    begin(a, page)
    page.fill("#participant-label", "LAYOUT2")
    page.click("#advance")
    page.wait_for_selector(
        '#device-tip[data-tip="open_error"]:not([hidden])', timeout=30_000
    )
    check(page, "device-tip-open-error", tmp_path, key="device")
    stop(a)

    (tmp_path / "plug").mkdir()
    plug = Plug(tmp_path / "plug")
    try:
        b = app(port=str(plug.link))
        begin(b, page)
        page.fill("#participant-label", "LAYOUT3")
        page.click("#advance")
        page.wait_for_selector("#advance:not([disabled])", timeout=30_000)
        plug.unplug()
        page.wait_for_selector(
            '#device-tip[data-tip="dropped"]:not([hidden])', timeout=30_000
        )
        check(page, "device-tip-dropped", tmp_path, key="device")
    finally:
        plug.close()


def test_the_link_lost_pause_fits_at_four_sizes(app, page, tmp_path):
    steps = json.loads(SHORT.read_text())
    for step in steps:
        for block in step["blocks"]:
            block["seconds"] = 120
    flow = tmp_path / "long.json"
    flow.write_text(json.dumps(steps))
    (tmp_path / "plug").mkdir()
    plug = Plug(tmp_path / "plug")
    try:
        a = app(port=str(plug.link), flow=flow)
        begin(a, page)
        advance_until(page, "words", "waiting")
        press(page)
        page.wait_for_selector(f'{SCREEN}[data-step="words"][data-phase="running"]')
        folder = session_folder_of(a)
        plug.unplug()
        until(
            lambda: any(e.get("name") == "link_lost" for e in _events(folder)),
            "link_lost",
        )
        page.wait_for_selector("#fs-return[disabled]", timeout=10_000)
        check(page, "paused-link-lost", tmp_path, root="#fs-lost")
    finally:
        plug.close()


def _events(folder: Path) -> list[dict]:
    path = folder / "events.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )


def test_the_summary_after_a_stop_and_after_an_early_end_fits_at_four_sizes(
    app, pty_device, recorded_session, page, tmp_path
):
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=30_000)
    page.wait_for_selector(".record-zip .btn", timeout=30_000)
    check(page, "summary-stopped", tmp_path)
    stop(a)

    b = app(replay=recorded_session, flow=SHORT)
    begin(b, page)
    deadline_ms = 90_000
    page.wait_for_function(
        """() => {
          const s = document.getElementById('screen');
          if (s.dataset.screen === 'ended') return true;
          const b = document.querySelector('#advance:not([disabled])');
          const f = document.getElementById('participant-label');
          if (f && !f.value) { f.value = 'P001'; f.dispatchEvent(new Event('input', {bubbles: true})); }
          if (b) b.click();
          return false;
        }""",
        polling=500,
        timeout=deadline_ms,
    )
    page.wait_for_selector(".record-zip .btn", timeout=30_000)
    check(page, "summary-ended-early", tmp_path)


def test_the_participant_screen_with_a_loaded_file_fits_at_four_sizes(
    app, page, tmp_path
):
    dev = contact_device()
    try:
        a = app(port=dev.path, flow=SHORT)
        accept(a, "Lay4")
        first = session_folder_of(a)
        with open_ws(a) as ws:
            assert play_through(ws)["phase"] == "done"
        made = wait_packaged(first)
        stop(a)
    finally:
        dev.hang_up()
    dev = contact_device()
    try:
        b = app(port=dev.path, flow=SHORT)
        begin(b, page)
        page.set_input_files("#import-file", str(made))
        page.wait_for_function(
            "document.getElementById('import-summary').textContent.startsWith('Saved session from')"
        )
        check(page, "participant-loaded", tmp_path)
        drop_file(page, "notes.txt", b"this is not a zip", "text/plain")
        page.wait_for_function(
            "document.getElementById('import-problem').textContent.length > 0"
        )
        check(page, "participant-file-refused", tmp_path)
    finally:
        dev.hang_up()


def test_the_onlooker_window_fits_at_four_sizes(
    app, pty_device, page, browser, tmp_path
):
    from test_onlooker_browser import open_onlooker

    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    context, look = open_onlooker(browser, a)
    try:
        check(look, "onlooker", tmp_path, root="#ol-root", band=ONLOOKER_BAND)
    finally:
        context.close()


def test_the_unstyled_class_check_finds_a_stylesheet_cut_short(
    app, pty_device, page, tmp_path
):
    """The check itself: the device screen with the stylesheets as they were on the night of
    2026-11-25 (shell.css with the rest of the file after the record styles inside a rule for
    windows up to 760 px, and no layout.css) names the device classes."""
    a = app(port=pty_device.path, flow=SHORT)
    begin(a, page)
    page.fill("#participant-label", "LAYOUT5")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False},
    )
    assert page.evaluate(UNSTYLED, SCREEN) == []
    page.evaluate(
        """async () => {
          const link = document.querySelector('link[href$="shell.css"]');
          const text = await (await fetch(link.href)).text();
          const cut = text.indexOf('/* Device setup');
          const style = document.createElement('style');
          style.textContent = text.slice(0, cut) + '@media (max-width: 760px) {' + text.slice(cut) + '}';
          link.replaceWith(style);
          document.querySelector('link[href$="layout.css"]').remove();
        }"""
    )
    missing = page.evaluate(UNSTYLED, SCREEN)
    assert "ol#device-steps .device-steps" in missing and "div .device" in missing, (
        missing
    )


def test_the_video_round_while_its_player_loads_and_when_it_cannot_load_fits_at_four_sizes(
    app, pty_device, page, tmp_path
):
    """Without the internet (requests to the player's host refused, as the video tests do) the
    round shows its loading state and then, after its 15 s limit, the problem and Try again."""
    from urllib.parse import urlsplit

    from test_video_browser import to_video, video_flow

    a = app(port=pty_device.path, flow=video_flow(tmp_path, 30))
    page.context.route(
        lambda url: urlsplit(url).netloc == "player.vimeo.com",
        lambda route: route.abort("internetdisconnected"),
    )
    to_video(a, page)
    check(page, "video-loading", tmp_path)
    page.wait_for_selector("#video-problem", state="visible", timeout=25_000)
    check(page, "video-error", tmp_path)

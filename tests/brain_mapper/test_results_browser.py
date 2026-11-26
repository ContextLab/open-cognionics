"""The results screen in a real Chromium, from a session recorded from the simulated headset.

The session plays a flow with calibration and two 40 s rest rounds (``flows/results.json``). After the zip is saved the analysis runs on its own thread; the page
shows its progress, then "See your results", and every figure is checked against the files the
analysis wrote into the session folder. The session zip is hashed when it is saved and again
once the results are in. The layout is measured at four screen sizes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from test_package import wait_packaged
from test_shell_browser import SCREEN, begin, press, screen_of
from test_skip_browser import answer_yes, ask, folder_of
from shots import ShotsDir

RESULTS_FLOW = Path(__file__).parent / "flows" / "results.json"
NO_ROUND_FLOW = Path(__file__).parent / "flows" / "results-none.json"
SIZES = [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)]
SHOTS = ShotsDir("shots-night-analysis")


def finish_the_route(page) -> None:
    """Presses Continue, Start and Finish until the session is over (``test_summary_browser``'s
    helper, with room for blocks of 40 s)."""
    deadline = time.monotonic() + 400
    while True:
        assert time.monotonic() < deadline, screen_of(page)
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=90_000)
        last = screen_of(page)[0] == "summary"
        press(page)
        if last:
            break
    page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_results(folder: Path, timeout: float = 120.0) -> Path:
    deadline = time.monotonic() + timeout
    while not (folder / "analysis" / "results.json").exists():
        assert time.monotonic() < deadline, "no results written"
        time.sleep(0.2)
    return folder / "analysis"


def clean_stream() -> bytes:
    """60 s of headset packets (looped by the simulated headset): 10 uV of independent 1/f noise
    per channel, with C3 and P3 sharing an 8-13 Hz source a quarter cycle apart, so that every
    round has enough clean recording for links and windows, and some links to draw."""
    import synthetic_eeg as syn

    rng = np.random.default_rng(60)
    x = syn.noise(rng, 60)
    source = syn.narrowband(rng, 60 * syn.FS, 10.5, 5.0) * 15
    x[:, syn.ch("C3")] += source
    x[:, syn.ch("P3")] += syn.lagged(source, 10.5)
    return syn.encode(x)


def expected_links(band, good) -> int:
    """The links of results.json the page must draw: those above the surrogate threshold."""
    if band["threshold"] is None:
        return 0
    m = np.array([[np.nan if v is None else v for v in row] for row in band["matrix"]])
    index = [i for i, g in enumerate(good) if g]
    return sum(1 for a, i in enumerate(index) for j in index[a + 1 :] if np.isfinite(m[i, j]) and m[i, j] > band["threshold"])


NO_GROUPS = "The links in this round and band do not split into groups more than shuffled links do."
NO_LINKS = "No link in this round and band is stronger than in time-shifted copies of the same recording."


def check_network(page, band, good) -> None:
    """The view draws exactly the links above the threshold, and colors groups only when the
    analysis found Q above the shuffled graphs' mean plus 2 SD."""
    stage = page.locator("#results-stage")
    links = expected_links(band, good)
    assert int(stage.get_attribute("data-links")) == links == band["links"]
    grouped = bool(band["grouped"] and links)
    assert stage.get_attribute("data-grouped") == str(grouped).lower()
    info = page.inner_text("#results-net-info")
    if not links:
        assert info == NO_LINKS
    elif not grouped:
        assert info == NO_GROUPS
    else:
        assert "split into groups more than shuffled links do" in info


def layout_problems(page, width: int, height: int) -> list[str]:
    """What breaks the layout rules: anything outside the window, a page that scrolls, a column
    that is not centered, text smaller than 12 px."""
    return page.evaluate(
        """([W, H]) => {
          const out = [];
          const root = document.getElementById('results');
          const doc = document.scrollingElement;
          if (doc.scrollHeight > innerHeight + 1 || doc.scrollWidth > innerWidth + 1) out.push('the page scrolls');
          if (root.scrollHeight > root.clientHeight + 1) out.push('the results screen overflows');
          for (const node of root.querySelectorAll('h1,h2,p,button,figcaption,svg,canvas,.chip,.legend li,input')) {
            if (node.closest('[hidden]') || !node.getClientRects().length) continue;
            const r = node.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            if (r.left < -0.5 || r.top < -0.5 || r.right > W + 0.5 || r.bottom > H + 0.5)
              out.push(`${node.tagName}#${node.id || ''} ${node.className && node.className.baseVal === undefined ? node.className : ''} outside: ${JSON.stringify([r.left, r.top, r.right, r.bottom].map(Math.round))}`);
            if (node.tagName !== 'svg' && node.tagName !== 'CANVAS' && node.tagName !== 'INPUT') {
              const size = parseFloat(getComputedStyle(node).fontSize);
              if (size < 12) out.push(`${node.tagName} text ${size}px`);
            }
          }
          for (const t of root.querySelectorAll('svg text')) {
            const size = parseFloat(t.getAttribute('font-size'));
            if (size < 9.5) out.push(`chart text ${size}px`);
          }
          for (const box of root.querySelectorAll('.results-side, .results-fig, .results-page'))
            if (box.scrollHeight > box.clientHeight + 1 || box.scrollWidth > box.clientWidth + 1)
              out.push(`${box.className} cuts off its content`);
          const head = root.querySelector('.results-head').getBoundingClientRect();
          const left = head.left, right = W - head.right;
          if (Math.abs(left - right) > 2) out.push(`not centered: ${Math.round(left)} vs ${Math.round(right)}`);
          if (W >= 1920 && head.width > 1700) out.push(`column too wide: ${Math.round(head.width)}`);
          return out;
        }""",
        [width, height],
    )


@pytest.fixture(scope="module")
def finished(tmp_path_factory, browser):
    """One whole session on the simulated headset playing ``clean_stream()``, its results computed
    and the page on the summary."""
    # The ``app`` fixture is per test; this one session is shared by the tests of this module, so
    # it is started here the same way (the simulated headset, a temporary data folder).
    from conftest import COMMAND, PRINTED, _Lines
    from pty_device import PtyDevice, config_reply

    tmp = tmp_path_factory.mktemp("results")
    device = PtyDevice(clean_stream(), config_reply())
    data_dir = tmp / "data"
    process = subprocess.Popen(
        [str(COMMAND), "--no-browser", "--data-dir", str(data_dir), "--http-port", "0", "--port", device.path, "--flow", str(RESULTS_FLOW)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = _Lines(process.stdout)
    url = lines.wait_for(PRINTED, timeout=30).removeprefix(PRINTED)

    class A:
        pass

    a = A()
    a.url, a.data_dir, a.printed = url, data_dir, lines.seen
    context = browser.new_context(viewport={"width": 1512, "height": 982})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        begin(a, page)
        finish_the_route(page)
        folder = folder_of(a)
        zip_path = wait_packaged(folder)
        zip_hash = sha(zip_path)
        page.wait_for_selector("#results-entry", timeout=30_000)
        seen = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = page.get_attribute("#results-entry", "data-state")
            value = page.get_attribute("#results-progress", "aria-valuenow")
            seen.append((state, value, page.inner_text("#results-status")))
            if state == "done":
                break
            time.sleep(0.1)
        out = wait_results(folder)
        yield {"a": a, "page": page, "folder": folder, "zip": zip_path, "zip_hash": zip_hash, "out": out, "seen": seen, "lines": lines, "errors": errors}
    finally:
        context.close()
        process.terminate()
        process.wait(timeout=20)
        device.hang_up()


def test_the_summary_shows_progress_then_opens_results_and_the_zip_is_untouched(finished):
    page, folder, seen = finished["page"], finished["folder"], finished["seen"]
    print("states seen on the page:", seen[:3], "...", seen[-1])
    assert seen[-1][0] == "done"
    running = [s for s in seen if s[0] == "running"]
    # the entry starts as a progress bar; while it runs the line says how far and how long
    assert seen[0][0] == "running" and seen[0][2]
    for _, value, text in running[1:]:
        assert text.startswith("Computing the results") and value is not None
    # the line names the stage and round being computed
    if len(running) > 1:
        assert any(word in text for _, _, text in running[1:] for word in ("Filtering", "Links", "Power spectra", "Reading")), running
    # the entry comes before the points and the timeline
    above = page.evaluate(
        "(() => { const e = document.getElementById('results-entry').getBoundingClientRect();"
        " const g = document.querySelector('.record-grid'); return !g || e.bottom <= g.getBoundingClientRect().top + 1; })()"
    )
    assert above
    assert page.is_enabled("#results-open")
    assert sha(finished["zip"]) == finished["zip_hash"]
    # the additional zip beside the first holds the results, and nothing else changed
    extra = folder.parent / f"{folder.name}-analysis.zip"
    assert extra.exists()
    meta = json.loads((folder / "session.json").read_text())
    assert meta["zip"] == finished["zip"].name


def test_every_figure_is_drawn_from_this_session_s_files(finished, tmp_path):
    page, folder, out = finished["page"], finished["folder"], finished["out"]
    results = json.loads((out / "results.json").read_text())
    windows = json.loads((out / "windows.json").read_text())
    meta = json.loads((folder / "session.json").read_text())
    page.click("#results-open")
    page.wait_for_selector("#results[data-step='points']")
    # 1. points: the scores of session.json as the summary shows them, not recomputed. (This
    # flow has rest rounds only, which are not scored: the tasks are being changed on other
    # branches tonight, and the scores have their own tests.)
    tasks = meta["scores"]["tasks"]
    bars = page.locator("#results-points rect.bar")
    assert bars.count() == sum(1 for t in tasks.values() if t["possible"] > 0)
    for k in range(bars.count()):
        bar = bars.nth(k)
        entry = tasks[bar.get_attribute("data-round")]
        assert int(bar.get_attribute("data-earned")) == entry["earned"] and int(bar.get_attribute("data-possible")) == entry["possible"]
    summary_rows = page.locator("#summary-scores li[data-step]")
    figure_text = page.inner_text("#results-points")
    if summary_rows.count() == 0:
        assert "No round was scored in this session." in figure_text
    for k in range(summary_rows.count()):
        what = summary_rows.nth(k).locator(".what").inner_text()
        how = summary_rows.nth(k).locator(".how").inner_text()
        assert what in figure_text
        why = summary_rows.nth(k).locator(".why")
        assert (why.inner_text() if why.count() else how) in figure_text
    # 2. rhythms: one spectrum per round with one, and the band shares of results.json
    page.click("#results-next")
    page.wait_for_selector("#results[data-step='rhythms']")
    with_spectrum = [c for c in results["conditions"] if c["spectrum"]]
    assert with_spectrum, results["conditions"]
    assert page.locator("#results-spectrum path.spec").count() == len(with_spectrum)
    for c in results["conditions"]:
        shares = page.locator(f"#results-bands rect.share[data-condition='{c['id']}']")
        if c["relative"]:
            got = {shares.nth(k).get_attribute("data-band"): float(shares.nth(k).get_attribute("data-share")) for k in range(shares.count())}
            assert got == c["relative"]
        else:
            assert shares.count() == 0
            assert "too little clean data" in page.inner_text("#results-bands")
    # 3. eyes closed and open: the ratio of results.json at every good electrode, or why not
    page.click("#results-next")
    page.wait_for_selector("#results[data-step='eyes']")
    ratio = results["alpha_ratio"]
    print("alpha ratio of the simulated session:", ratio)
    assert not finished["errors"], finished["errors"]
    if "error" in ratio:
        assert ratio["error"] in page.inner_text("#results-eyes")
        assert page.locator("#results-eyes svg").count() == 0
    else:
        labels = results["recording"]["labels"]
        bars = page.locator("#results-eyes rect.ratio")
        expected = {labels[i]: v for i, v in enumerate(ratio["per_channel"]) if v is not None and v > 0}
        got = {bars.nth(k).get_attribute("data-channel"): float(bars.nth(k).get_attribute("data-ratio")) for k in range(bars.count())}
        assert got == expected, page.evaluate("document.getElementById('results') && document.getElementById('results').outerHTML.slice(0, 3000)")
        headline = page.inner_text("#results-eyes-headline")
        assert f"about {ratio['posterior_median']:.1f} times" in headline
        assert f"from {ratio['closed_kept_segments']} clean 2 s segments with the eyes closed and {ratio['open_kept_segments']} with them open" in headline
        few = min(ratio["closed_kept_segments"], ratio["open_kept_segments"]) < results["parameters"]["min_segments_connectivity"]
        assert ("short, so this number rests on few segments" in page.inner_text("#results-eyes-note")) == few
    # 4. networks: this session's connectivity, per band and round, and the windows over time
    page.click("#results-next")
    page.wait_for_selector("#results-stage[data-ready='true']", timeout=30_000)
    stage = page.locator("#results-stage")
    condition = stage.get_attribute("data-condition")
    entry = next(c for c in results["conditions"] if c["id"] == condition)
    good = results["channels"]["good"]
    assert entry["connectivity"], entry
    check_network(page, entry["connectivity"]["alpha"], good)
    page.click(".chip[data-band='theta']")
    assert stage.get_attribute("data-band") == "theta"
    check_network(page, entry["connectivity"]["theta"], good)
    # every round has a chip; one without enough clean recording says so
    for c in results["conditions"]:
        page.click(f".chip[data-condition='{c['id']}']")
        if c["connectivity"] is None:
            assert "Not enough clean recording in this round to estimate links." in page.inner_text("#results-now")
            assert stage.get_attribute("data-links") == "0"
        else:
            check_network(page, c["connectivity"]["theta"], good)
    page.click(f".chip[data-condition='{condition}']")
    frames = [f for f in windows["frames"] if f["bands"]["theta"]]
    assert frames, "the simulated session has 40 s windows"
    page.click("#results-play")
    page.wait_for_function("document.getElementById('results-stage').dataset.frame !== undefined")
    first = float(stage.get_attribute("data-t"))
    assert first == frames[0]["t"]
    band = frames[0]["bands"]["theta"]
    assert int(stage.get_attribute("data-links")) == band["links"]
    assert stage.get_attribute("data-grouped") == str(bool(band["grouped"] and band["links"])).lower()
    if len(frames) > 1:
        page.wait_for_function("Number(document.getElementById('results-stage').dataset.frame) >= 1", timeout=10_000)
        assert float(stage.get_attribute("data-t")) == frames[int(stage.get_attribute("data-frame"))]["t"]
    assert "into the recording" in page.inner_text("#results-now")
    assert not finished["errors"], finished["errors"]
    page.click("#results-next")  # "Back to the summary"
    page.wait_for_selector("#results", state="detached")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')


def test_the_results_screen_fits_every_screen_size(finished, tmp_path):
    page = finished["page"]
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems = {}
    # A window in full screen cannot be resized: leave it (the session is over; nothing pauses).
    page.evaluate("document.fullscreenElement && document.exitFullscreen()")
    page.wait_for_function("document.fullscreenElement === null")
    for width, height in SIZES:
        page.keyboard.press("Escape")  # back on the summary, so each size also shows the entry
        page.set_viewport_size({"width": width, "height": height})
        if page.locator("#results").count() == 0:
            if (width, height) in ((3840, 1600), (1512, 982)):
                shot = tmp_path / f"summary-entry-{width}x{height}.png"
                page.locator("#results-entry").scroll_into_view_if_needed()
                page.screenshot(path=str(shot))
                shutil.copy(shot, SHOTS / shot.name)
            page.click("#results-open")
        for step in ("points", "rhythms", "eyes", "networks"):
            page.click(f".results-tab[data-step='{step}']")
            page.wait_for_selector(f"#results[data-step='{step}']")
            if step == "networks":
                page.wait_for_selector("#results-stage[data-ready='true']", timeout=30_000)
            page.wait_for_timeout(300)  # the charts redraw on the resize observer's next frame
            found = layout_problems(page, width, height)
            if found:
                problems[(width, height, step)] = found
            if (width, height) in ((3840, 1600), (1512, 982)):
                shot = tmp_path / f"results-{step}-{width}x{height}.png"
                page.screenshot(path=str(shot))
                shutil.copy(shot, SHOTS / shot.name)
    page.keyboard.press("Escape")
    page.set_viewport_size({"width": 1512, "height": 982})
    assert not problems, problems


def test_a_session_with_nothing_to_analyze_says_the_results_could_not_be_computed(app, pty_device, page, tmp_path):
    a = app(port=pty_device.path, flow=NO_ROUND_FLOW)
    begin(a, page)
    # through the participant and device steps to the rest round's instructions, then stop there:
    # the recording holds no part of a round
    deadline = time.monotonic() + 120
    while screen_of(page)[1] != "rest_closed":
        assert time.monotonic() < deadline, screen_of(page)
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=60_000)
        press(page)
        page.wait_for_timeout(300)
    folder = folder_of(a)
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    zip_path = wait_packaged(folder)
    before = sha(zip_path)
    page.wait_for_selector("#results-entry[data-state='failed']", timeout=60_000)
    text = page.inner_text("#results-status")
    assert text.startswith("The results could not be computed")
    assert page.locator("#results-open").is_hidden()
    assert not (folder / "analysis").exists()
    assert sha(zip_path) == before
    # (the log line of the failure, without the label, is checked in test_analysis.py)

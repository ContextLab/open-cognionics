"""The Words and Delayed recall screens at 3840x1600 and at 1920x1080, 1512x982 and
1024x768, from real layout boxes in a real Chromium against the real server: the instruction
screen, a word, the recall view, the end-recall question and the waiting line are inside the
viewport, centered, never at the far edges, with no scroll; the word is about 0.09 of the
viewport height. Screenshots at 3840x1600 and 1512x982 go to the folder named by
``BRAIN_MAPPER_SHOTS`` when it is set.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from test_engine import session_folder_of
from test_shell_browser import SCREEN, advance_until, begin
from test_task_words import recall_field_ready, type_entry, wait_events
from test_task_words_finish import CONFIRM_GUARD_MS, focused, one_list_flow

SIZES = [(3840, 1600), (1920, 1080), (1512, 982), (1024, 768)]
SHOT_SIZES = {(3840, 1600), (1512, 982)}

#: Boxes of the named elements (when present and shown), the viewport and the scroll size.
BOXES = """(ids) => {
  const out = {};
  for (const id of ids) {
    const e = document.getElementById(id);
    if (!e || !e.getClientRects().length) { out[id] = null; continue; }
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    out[id] = {x: r.x, y: r.y, w: r.width, h: r.height, font: parseFloat(s.fontSize),
               color: s.color, text: e.textContent};
  }
  const doc = document.scrollingElement;
  return {boxes: out, vw: innerWidth, vh: innerHeight, sh: doc.scrollHeight, sw: doc.scrollWidth,
          bg: getComputedStyle(document.body).backgroundColor};
}"""


def shoot(page, name: str, size: tuple[int, int]) -> None:
    folder = os.environ.get("BRAIN_MAPPER_SHOTS")
    if folder and size in SHOT_SIZES:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}-{size[0]}x{size[1]}.png"))


def check(page, size, ids: list[str], name: str, min_font: float = 14.0) -> dict:
    page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    got = page.evaluate(BOXES, ids)
    check_boxes(page, size, got, name, min_font)
    return got


def check_boxes(page, size, got: dict, name: str, min_font: float = 14.0) -> None:
    vw, vh = got["vw"], got["vh"]
    assert (vw, vh) == size
    assert got["sh"] <= vh and got["sw"] <= vw, (name, size, got)  # no scroll
    for id_, box in got["boxes"].items():
        assert box is not None, (name, size, id_)
        # Inside the viewport, and never against its far edges (a 2 % margin at least).
        assert box["x"] >= 0.02 * vw - 0.5 and box["x"] + box["w"] <= 0.98 * vw + 0.5, (name, size, id_, box)
        assert box["y"] >= 0 and box["y"] + box["h"] <= vh + 0.5, (name, size, id_, box)
        # Centered.
        assert abs(box["x"] + box["w"] / 2 - vw / 2) <= 2, (name, size, id_, box)
        assert box["font"] >= min_font, (name, size, id_, box)
    shoot(page, name, size)


@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
def test_words_and_recall_screens_fit_and_are_centered(app, pty_device, page, tmp_path, size):
    # The page is in full screen, where the window cannot be resized: one session per size.
    page.set_viewport_size({"width": size[0], "height": size[1]})
    a = app(port=pty_device.path, flow=one_list_flow(tmp_path, 75, 30))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.wait_for_selector("#task-instructions", state="visible")
    check(page, size, ["task-title", "task-instructions", "advance"], "instructions")
    page.keyboard.press("Enter")
    wait_events(folder, "words.stim", 1, timeout=15, part="word")
    page.wait_for_selector("#words-word", state="visible")
    got = check(page, size, ["words-word"], "word")
    word = got["boxes"]["words-word"]
    # About 0.09 of the viewport height, white on the dark page.
    assert 0.07 * size[1] <= word["font"] <= 0.09 * size[1] + 0.5, (size, word)
    assert word["color"] == "rgb(255, 255, 255)" and got["bg"] == "rgb(18, 24, 38)"
    recall_field_ready(page, timeout=70_000)
    recall_ids = ["recall-cue", "recall-prompt", "recall-input", "recall-ask", "recall-time"]
    type_entry(page, "draft", enter=False)
    got = check(page, size, recall_ids, "recall")
    b = got["boxes"]
    # Top to bottom: cue, prompt, field, finish control, clock.
    tops = [b[i]["y"] for i in recall_ids]
    assert tops == sorted(tops), (size, tops)
    assert max(v["w"] for v in b.values()) <= 1200
    page.keyboard.press("Tab")
    assert focused(page) == "recall-done"
    page.keyboard.press("Enter")
    page.wait_for_selector("#recall-confirm", state="visible")
    check(page, size, ["recall-confirm", "recall-input", "recall-time"], "confirm")
    page.wait_for_timeout(CONFIRM_GUARD_MS + 100)  # pacing past the guard, as a person would
    page.keyboard.press("Enter")
    wait_events(folder, "words.recall_end", 1, timeout=10)
    # The waiting line stays only until the engine ends the block on the participant's
    # block_finished, which can be the next frame: its boxes are read in the frame it is drawn.
    waiting = page.wait_for_function(
        f"""() => {{
          if (document.getElementById('screen').dataset.screen === 'between') return 'between';
          if (!document.getElementById('words-wait')) return false;
          return ({BOXES})(['words-wait']);
        }}"""
    ).json_value()
    if waiting != "between":
        check_boxes(page, size, waiting, "waiting")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)

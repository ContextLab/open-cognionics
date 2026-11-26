"""The Words and Delayed recall rounds in a real Chromium, against the real server.

Most tests start ``brain-mapper`` on a pseudo-terminal device with ``flows/words.json`` (Words:
two blocks of 64 s, so at most 52.6 s of study and the rest, about 11 s, of recall cut by the
block's end; Delayed recall, kind ``recall``, 20 s); one plays Delayed recall in the default flow
(one block of 125 s: a 2 s lead-in and 120 s of recall). They drive the page with
the keyboard as a participant would, and read the DOM, the server's messages, ``events.jsonl``,
the recording's markers and ``behavior/words.csv`` / ``behavior/recall.csv``. The studied words are
taken from the ``words.stim`` events (part ``word``) the page logged.
"""

from __future__ import annotations

import csv
import json
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import brain_mapper
from test_engine import events_of, open_ws, session_folder_of
from test_progress import device, drive_to, request  # noqa: F401  (device is a fixture)
from test_shell_browser import SCREEN, advance_until, begin, server_state
from test_tasks_browser import samples_of, wait_for_file, wait_until
from test_calibration_browser import contact_stream
from pty_device import PtyDevice, config_reply, onhead_stream

FLOW = Path(__file__).parent / "flows" / "words.json"


@pytest.fixture
def answering():
    """A pty device that answers the contact-check bytes as the headset does (0x11: the recording
    made with the check on; 0x12: the on-head one), for the default flow's calibration step."""
    dev = PtyDevice(onhead_stream(), config_reply(), contact_stream=contact_stream(), contact_on=True)
    yield dev
    dev.hang_up()

def _own_conftest():
    """This folder's conftest.py, loaded by its path: with tests/python collected in the same run,
    ``import conftest`` finds that folder's conftest instead (as test_package.py notes)."""
    spec = importlib.util.spec_from_file_location("brain_mapper_conftest", Path(__file__).with_name("conftest.py"))
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # its dataclass (App) looks itself up there
        spec.loader.exec_module(module)
    return module


_conftest = _own_conftest()
PRINTED, App, _Lines = _conftest.PRINTED, _conftest.App, _conftest._Lines
#: One display frame (60 Hz) plus 5 ms.
TOLERANCE_MS = 17 + 5
#: Records, before every paint, any frame in which the runner's "no more trials" text or an
#: empty, hidden stage would be seen on a Words or Delayed recall screen.
WATCH_PAINTS = """() => {
  window.__bad_paints = [];
  const look = () => {
    const area = document.getElementById('task-area');
    if (area && (area.dataset.task === 'words' || area.dataset.task === 'recall')) {
      const message = document.getElementById('task-message');
      const stage = document.getElementById('task-stage');
      if (message && message.textContent) window.__bad_paints.push(message.textContent);
      if (area.dataset.mode === 'over' && stage && stage.hidden) window.__bad_paints.push('hidden stage');
    }
    requestAnimationFrame(look);
  };
  requestAnimationFrame(look);
}"""
PENDING = "sessionStorage.getItem('brain-mapper-task/pending')"


def named(folder: Path, name: str, part: str | None = None) -> list[dict]:
    return [
        e for e in events_of(folder)
        if e["name"] == name and (part is None or e["payload"].get("part") == part)
    ]


def wait_events(folder: Path, name: str, count: int, timeout: float = 40.0, part=None) -> list[dict]:
    wait_until(lambda: len(named(folder, name, part)) >= count, f"{count} x {name}", timeout)
    return named(folder, name, part)


def studied(folder: Path, block: str) -> list[str]:
    stims = [e["payload"] for e in named(folder, "words.stim", "word") if e["payload"]["block"] == block]
    return [p["word"] for p in sorted(stims, key=lambda p: p["position"])]


def type_entry(page, text: str, enter: bool = True) -> None:
    page.keyboard.type(text)
    if enter:
        page.keyboard.press("Enter")


#: The delayed recall instructions and prompt.
DELAYED_TEXT = (
    "Remember those lists of words you learned earlier? Type any words you remembered from "
    "either list, in any order."
)
#: The line that says how a list starts, and how delayed recall starts.
# Before 2026-11-26: "Press Enter when you are ready to see the words.", at the end of the instructions;
# now the same prompt as the other tasks, on its own line just above Start.
ENTER_LINE = "Press Enter to start."
WORDS_INSTRUCTIONS = (
    "Watch 12 words, one at a time. Remember as many as you can. "
    "Then type the words you remember, pressing Enter after each word."
)
START_LINE = "Press Enter to start."
#: The children of the recall view, in order.
RECALL_VIEW = ["recall-cue", "recall-prompt", "recall-input", "recall-finish", "recall-time"]


def no_entries_shown(page, field: str = "") -> None:
    """The recall view holds only the prompt, the field (with ``field`` in it) and the clock: no
    list of entries, nothing typed earlier, nothing that says right or wrong."""
    assert page.locator("#recall-entries").count() == 0
    kids = page.evaluate("[...document.getElementById('words-recall').children].map((e) => e.id)")
    assert kids == RECALL_VIEW
    assert page.input_value("#recall-input") == field


#: Records each time the field shows that an entry was logged: when the sign came on and went
#: off (``performance.now``), and how the field looked while it was on.
WATCH_LOGGED = """() => {
  const input = document.getElementById('recall-input');
  window.__logged = [];
  let on = null;
  new MutationObserver(() => {
    const lit = input.classList.contains('words-logged');
    if (lit && on === null) {
      const s = getComputedStyle(input);
      on = {on: performance.now(), border: s.borderTopColor, shadow: s.boxShadow, value: input.value};
    } else if (!lit && on !== null) {
      on.off = performance.now();
      window.__logged.push(on);
      on = null;
    } else if (lit && on !== null) {
      // a new entry while the sign is on: it starts again
      on.off = performance.now();
      window.__logged.push(on);
      const s = getComputedStyle(input);
      on = {on: performance.now(), border: s.borderTopColor, shadow: s.boxShadow, value: input.value};
    }
  }).observe(input, {attributes: true, attributeFilter: ['class', 'data-logged']});
}"""


def check_logged_signs(page, count: int) -> None:
    """``count`` signs, each the same look, each under 300 ms, each with the field already empty."""
    page.wait_for_function(f"window.__logged && window.__logged.length >= {count}", timeout=5000)
    signs = page.evaluate("window.__logged")
    assert len(signs) == count, signs
    rest = page.evaluate(
        "(() => { const s = getComputedStyle(document.getElementById('recall-input'));"
        " return [s.borderTopColor, s.boxShadow]; })()"
    )
    for sign in signs:
        assert 0 < sign["off"] - sign["on"] < 300, sign
        assert sign["value"] == ""
        assert (sign["border"], sign["shadow"]) == (signs[0]["border"], signs[0]["shadow"])
        assert (sign["border"], sign["shadow"]) != tuple(rest)  # it is seen


def inside(page, selector: str, container: str | None = None) -> None:
    """``selector`` lies inside the viewport, and inside ``container`` when one is named."""
    box = page.locator(selector).bounding_box()
    size = page.viewport_size
    assert box is not None, selector
    outer = (
        page.locator(container).bounding_box()
        if container
        else {"x": 0, "y": 0, "width": size["width"], "height": size["height"]}
    )
    assert box["x"] >= outer["x"] - 0.5 and box["y"] >= outer["y"] - 0.5, (selector, box, outer)
    assert box["x"] + box["width"] <= outer["x"] + outer["width"] + 0.5, (selector, box, outer)
    assert box["y"] + box["height"] <= outer["y"] + outer["height"] + 0.5, (selector, box, outer)


def no_scroll(page) -> None:
    """The page does not scroll: nothing reaches past the viewport."""
    sizes = page.evaluate(
        "[document.scrollingElement.scrollHeight, innerHeight,"
        " document.scrollingElement.scrollWidth, innerWidth]"
    )
    assert sizes[0] <= sizes[1] and sizes[2] <= sizes[3], sizes


def recall_field_ready(page, timeout: float = 70_000) -> None:
    """The field is up and focused; study takes up to 52.6 s."""
    page.wait_for_selector("#recall-input:not([disabled])", state="visible", timeout=timeout)
    page.wait_for_function("document.activeElement && document.activeElement.id === 'recall-input'")


def watch_server(page) -> list[dict]:
    """Every message the server sends the page, as parsed JSON (filled as they arrive)."""
    seen: list[dict] = []

    def on_socket(ws):
        def on_frame(payload):
            try:
                seen.append(json.loads(payload))
            except (TypeError, ValueError):
                pass

        ws.on("framereceived", on_frame)

    page.on("websocket", on_socket)
    return seen


def check_delivery(page, seen: list[dict]) -> None:
    """No event was refused and every event the page sent was acknowledged."""
    assert not [m for m in seen if m.get("t") == "error"], [m for m in seen if m.get("t") == "error"]
    assert not [m for m in seen if m.get("t") == "ack" and m.get("refused")]
    wait_until(lambda: page.evaluate(PENDING) in ("[]", None), "every event acknowledged")


def check_markers(folder: Path) -> None:
    events = events_of(folder)
    samples = samples_of(folder)
    ours = [e for e in events if e["name"].split(".")[0] in ("words", "recall")]
    assert ours
    for event in ours:
        assert event["seq"] in samples, event
    ordered = [samples[e["seq"]] for e in ours]
    assert ordered == sorted(ordered) and ordered[-1] > ordered[0]


def check_timing(folder: Path, blocks=("list1", "list2"), cut_allowed: bool = False) -> tuple[float, float]:
    """Every onset and offset against the list's jittered schedule: the word at position p is due
    at the fixation frame F + 2000 ms + the words and blanks before it since the position n that
    fixation led to (each word 3000 ms, each blank the `blank_ms` logged with the word before it,
    800-1200 ms) and goes off 3000 ms after that; the runner's `due_ms` is that time and
    `late_frames` is 0. Returns the worst onset error and the worst duration error (ms)."""
    events = events_of(folder)
    onset_errors: list[float] = []
    duration_errors: list[float] = []
    checked = 0
    for block in blocks:
        mine = [e for e in events if e["name"].startswith("words.") and e["payload"].get("block") == block]
        fixations = [e for e in mine if e["name"] == "words.stim" and e["payload"]["part"] == "fixation"]
        stims = [e for e in mine if e["name"] == "words.stim" and e["payload"]["part"] == "word"]
        offs = [e for e in mine if e["name"] == "words.stim_off" and e["payload"]["part"] == "word"]
        assert [x["payload"]["position"] for x in stims] == list(range(1, 13))
        assert fixations and (cut_allowed or len(fixations) == 1)
        blanks = {x["payload"]["position"]: x["payload"]["blank_ms"] for x in stims}
        assert all(800 <= blanks[p] <= 1200 for p in range(1, 12)) and 1200 <= blanks[12] <= 1400
        for stim in stims:
            position = stim["payload"]["position"]
            fixation = [f for f in fixations if f["seq"] < stim["seq"]][-1]
            first = fixation["payload"]["next_position"]
            assert fixation["payload"]["lead_in_ms"] == 2000
            due = (fixation["browser_time"] + 2000 + (position - first) * 3000
                   + sum(blanks[p] for p in range(first, position)))
            assert abs(stim["payload"]["due_ms"] - due) < 1e-6
            assert stim["payload"]["late_frames"] == 0
            onset_errors.append(stim["browser_time"] - due)
            [off] = [o for o in offs if o["seq"] > stim["seq"]][:1]
            if "due_ms" not in off["payload"]:  # taken off by the runner: a pause cut the word
                assert cut_allowed
                continue
            assert abs(off["payload"]["due_ms"] - (due + 3000)) < 1e-6
            assert off["payload"]["late_frames"] == 0
            duration_errors.append(off["browser_time"] - stim["browser_time"] - 3000)
            checked += 1
    assert checked >= (10 if cut_allowed else 20)
    worst_onset = max(abs(x) for x in onset_errors)
    worst_duration = max(abs(x) for x in duration_errors)
    assert worst_onset <= TOLERANCE_MS, (worst_onset, onset_errors)
    assert worst_duration <= TOLERANCE_MS, (worst_duration, duration_errors)
    return worst_onset, worst_duration


def rows_of(path: Path) -> list[dict]:
    wait_for_file(path)
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_words_and_delayed_recall_are_played_logged_scored_and_written_out(app, pty_device, page):
    page.set_viewport_size({"width": 1024, "height": 768})
    seen = watch_server(page)
    a = app(port=pty_device.path, flow=FLOW)
    begin(a, page)
    advance_until(page, "words", "waiting")
    page.wait_for_selector("#task-instructions", state="visible")
    assert page.inner_text("#task-instructions") == WORDS_INSTRUCTIONS
    assert page.inner_text("#task-enter-line") == ENTER_LINE
    assert page.inner_text("#task-legend") == ""  # no legend: the text on screen says what Enter does
    assert page.locator("#practice").count() == 0  # no practice: Start at once
    no_scroll(page)
    folder = session_folder_of(a)
    page.evaluate(WATCH_PAINTS)
    page.keyboard.press("Enter")  # Enter starts the list

    # List 1: fixation, 12 words, then the recall field, all in the stimulus area.
    page.wait_for_selector("#task-panel #words-fixation", state="visible")
    page.wait_for_selector("#task-panel #words-word", state="visible")
    inside(page, "#words-word", "#task-panel")
    inside(page, "#task-panel")
    no_scroll(page)
    page.wait_for_function("document.getElementById('words-word') === null")
    recall_field_ready(page)
    page.evaluate(WATCH_LOGGED)
    field = page.locator("#recall-input")
    for attribute, value in (("autocomplete", "off"), ("autocorrect", "off"),
                             ("autocapitalize", "off"), ("spellcheck", "false")):
        assert field.get_attribute(attribute) == value
    assert field.get_attribute("maxlength") == "64"
    assert page.inner_text("#recall-prompt") == (
        "Type the words you remember, in any order. Press Enter after each word."
    )
    list1 = studied(folder, "list1")
    assert len(list1) == 12
    # Mixed case and spaces, a repeat, an intrusion of F and J letters (no task keys), a blank entry.
    script = [f"  {list1[0].upper()} ", list1[4], list1[0], "fjfj", "", list1[11].capitalize()]
    for text in script:
        type_entry(page, text)
    type_entry(page, "draft", enter=False)  # still in the field when the block ends
    # Each entry that was logged (the repeat too, not the blank one) gives the same short sign;
    # nothing typed is listed or judged.
    check_logged_signs(page, 5)
    no_entries_shown(page, field="draft")
    no_scroll(page)
    for selector in ("#recall-prompt", "#recall-input", "#recall-time"):
        inside(page, selector, "#task-panel")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    # No "List 2 of 2" in the text: the card's heading already says "Block 2 of 2".
    assert page.inner_text("#between-instructions") == f"{WORDS_INSTRUCTIONS} {ENTER_LINE}"
    assert page.inner_text("#between-next") == "Block 2 of 2"
    page.keyboard.press("Enter")  # Enter on the between screen starts list 2

    # List 2: only a word of list 1 (an intrusion from the other list).
    page.wait_for_selector("#words-word", state="visible", timeout=15_000)
    recall_field_ready(page)
    type_entry(page, list1[5])
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    page.click("#advance")

    # Delayed recall (kind recall): words of both lists, a repeat, an intrusion.
    page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]', timeout=15_000)
    page.wait_for_selector("#task-instructions", state="visible")
    assert page.inner_text("#task-instructions") == f"{DELAYED_TEXT} {START_LINE}"
    page.keyboard.press("Enter")
    recall_field_ready(page)
    assert page.inner_text("#recall-prompt") == DELAYED_TEXT
    list2 = studied(folder, "list2")
    assert not set(list1) & set(list2)
    delayed = [list2[2], list1[1].upper(), "zebraq", f" {list2[2]}"]
    page.evaluate(WATCH_LOGGED)
    for text in delayed:
        type_entry(page, text)
    check_logged_signs(page, len(delayed))
    no_entries_shown(page)
    no_scroll(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=15_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    check_delivery(page, seen)

    # The trial records.
    trials = named(folder, "words.trial")
    assert [t["payload"]["list"] for t in trials] == [1, 2]
    assert all(t["payload"]["status"] == "done" and t["payload"]["scored"] for t in trials)
    first, second = (t["payload"] for t in trials)
    assert not [k for k in first if k.startswith(("param_", "result_"))]
    assert first["words"] == list1 and second["words"] == list2
    assert first["typed"] == [x for x in script if x != ""]
    assert [(m["word"], m["position"]) for m in first["matched"]] == [
        (list1[0], 1), (list1[4], 5), (list1[11], 12),
    ]
    assert first["repeats"] == [list1[0]] and first["intrusions"] == ["fjfj"]
    assert first["unsubmitted"] == "draft"
    assert [s["position"] for s in first["shown"]] == list(range(1, 13))
    assert first["intrusions_tagged"] == [{"text": "fjfj", "other_list": False}]
    assert second["typed"] == [list1[5]] and second["matched"] == []
    assert second["intrusions"] == [list1[5]]
    assert second["intrusions_tagged"] == [
        {"text": list1[5], "other_list": True, "word": list1[5], "list": 1, "position": 6},
    ]
    assert second["unsubmitted"] is None
    assert page.evaluate("window.__bad_paints") == []
    unsub = named(folder, "words.unsubmitted")
    assert [(u["payload"]["text"], u["payload"]["ended"]) for u in unsub] == [("draft", "block_end")]
    # Each recall was cut by the block's end (this flow's blocks are shorter than study + 90 s).
    ends = [e["payload"] for e in named(folder, "words.recall_end")]
    assert [(e["list"], e["ended_by"]) for e in ends] == [(1, "block_end"), (2, "block_end")]
    assert all(t["ended_by"] == "block_end" for t in (first, second))
    for t in (first, second):
        assert len(t["blanks_ms"]) == 11 and 1200 <= t["cue_delay_ms"] <= 1400
    # Responses: one per non-blank entry, first key before Enter, both after the recall start.
    resps = [e["payload"] for e in named(folder, "words.resp") if e["payload"]["block"] == "list1"]
    assert [r["text"] for r in resps] == first["typed"]
    for r in resps:
        assert 0 < r["first_key_ms"] < r["enter_ms"] < 12000
    # Delayed recall: the lists rebuilt from the seed are the studied ones.
    [rec] = named(folder, "recall.trial")
    r = rec["payload"]
    assert r["status"] == "done" and r["lists"] == [list1, list2]
    assert [(m["word"], m["list"], m["position"]) for m in r["matched"]] == [
        (list2[2], 2, 3), (list1[1], 1, 2),
    ]
    assert r["intrusions"] == ["zebraq"] and r["repeats"] == [f" {list2[2]}"]
    assert [e["payload"]["text"] for e in named(folder, "recall.resp")] == delayed
    assert len(named(folder, "recall.recall_start")) == 1
    # Markers and timing.
    check_markers(folder)
    onset, duration = check_timing(folder)
    print(f"worst onset error {onset:.2f} ms, worst duration error {duration:.2f} ms")
    # The behavior files: clean column names, the typed entries.
    rows = rows_of(folder / "behavior" / "words.csv")
    # No field of this task is renamed.
    assert not [c for c in rows[0] if c.startswith(("param_", "result_", "payload_"))]
    assert [json.loads(row["typed"]) for row in rows] == [first["typed"], [list1[5]]]
    assert [json.loads(row["intrusions"]) for row in rows] == [["fjfj"], [list1[5]]]
    assert [json.loads(row["repeats"]) for row in rows] == [[list1[0]], []]
    assert [row["unsubmitted"] for row in rows] == ["draft", ""]
    rows = rows_of(folder / "behavior" / "recall.csv")
    assert len(rows) == 1 and json.loads(rows[0]["lists"]) == [list1, list2]
    assert json.loads(rows[0]["intrusions"]) == ["zebraq"]


def test_a_pause_in_study_goes_on_with_the_next_word_and_typed_words_survive_a_pause(
    app, pty_device, page, tmp_path
):
    # The pause rule of 2026-11-25: a pause abandons the block it is in, and Resume goes
    # on with the next block. Before: one list, Resume went on with the next word of the same
    # list and with its recall. Now: a pause in list 1's study ends list 1 (its words shown so
    # far stay in the data, none is shown again); list 2 follows from its start; a pause in list
    # 2's recall keeps the unsubmitted text (logged once) and the round is left from the pause.
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [{"id": "list1", "seconds": 90, "points": 100}, {"id": "list2", "seconds": 90, "points": 100}]
    flow = tmp_path / "words.json"
    flow.write_text(json.dumps(raw))
    page.set_viewport_size({"width": 1920, "height": 1080})
    seen = watch_server(page)
    a = app(port=pty_device.path, flow=flow)
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.click("#advance")
    page.wait_for_selector("#words-word", state="visible", timeout=15_000)
    inside(page, "#words-word", "#task-panel")
    inside(page, "#task-panel")
    wait_events(folder, "words.stim", 3, part="word")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    # Nothing of the trial stays in the panel under the pause overlay.
    page.wait_for_function("document.getElementById('task-panel').childElementCount === 0")
    page.keyboard.press("KeyF")  # paused, not in a trial: nothing is logged for it
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
    shown1 = [e["payload"] for e in named(folder, "words.stim", "word") if e["block"] == "list1"]
    positions = [s["position"] for s in shown1]
    assert 3 <= len(positions) <= 4 and positions == list(range(1, len(positions) + 1)), positions
    trials = [e["payload"] for e in named(folder, "words.trial")]
    assert trials[0]["status"] == "abandoned" and trials[0]["reason"] == "fullscreen_lost"
    page.click("#advance")  # list 2, from its start
    recall_field_ready(page)
    stims = [e["payload"] for e in named(folder, "words.stim", "word") if e["block"] == "list2"]
    assert [s["position"] for s in stims] == list(range(1, 13))  # each word of list 2 once
    # No word of list 1 is shown again after the pause.
    assert [e["payload"]["position"] for e in named(folder, "words.stim", "word") if e["block"] == "list1"] == positions
    # Recall: one word, then a pause with text in the field: the entry is kept (and logged once).
    words = [s["word"] for s in stims]
    type_entry(page, words[0])
    no_entries_shown(page)
    for selector in ("#recall-prompt", "#recall-input", "#recall-time"):
        inside(page, selector, "#task-panel")
    type_entry(page, "half", enter=False)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    wait_events(folder, "words.unsubmitted", 1, timeout=10)
    [unsub] = named(folder, "words.unsubmitted")
    assert unsub["payload"]["text"] == "half" and unsub["payload"]["ended"] == "abandoned"
    starts = [(e["block"], e["payload"]["segment"]) for e in named(folder, "words.recall_start")]
    assert starts == [("list2", 1)]
    # Skip from inside the task (the paused screen). Both lists were played (a block a pause
    # ended counts as played), so the round counts as finished.
    page.click("#fs-skip-round")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-step="recall"]', timeout=15_000)
    assert server_state(a)["outcomes"]["words"] == "finished"
    ends = [e for e in events_of(folder) if e["name"] == "block_end" and e["step"] == "words"]
    assert [(e["block"], e["payload"].get("ended_by")) for e in ends] == [("list1", "interrupted"), ("list2", "interrupted")]
    resps = [e["payload"]["text"] for e in named(folder, "words.resp")]
    assert resps == [words[0]]
    assert all(t["status"] == "abandoned" for t in (e["payload"] for e in named(folder, "words.trial")))
    assert [e["payload"]["text"] for e in named(folder, "words.unsubmitted")] == ["half"]  # logged once
    assert not named(folder, "words.stray")
    check_markers(folder)
    check_delivery(page, seen)
    # List 2 was shown whole, on its schedule (list 1 was ended by the pause after 3 words, so its
    # schedule is not whole; its words were checked above); the recall cue cut by the pause was
    # taken off by the runner.
    check_timing(folder, blocks=("list2",), cut_allowed=True)


def test_a_continued_session_rebuilds_the_same_lists_for_delayed_recall(
    app, page, device  # noqa: F811
):
    first = app(port=device().path, flow=FLOW)
    begin(first, page)
    advance_until(page, "words", "waiting")
    earlier = session_folder_of(first)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]', timeout=15_000)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    list1, list2 = studied(earlier, "list1"), studied(earlier, "list2")
    seed = json.loads((earlier / "progress.json").read_text())["seed"]
    first.process.send_signal(signal.SIGINT)
    first.process.wait(timeout=15)

    second = app(port=device().path, flow=FLOW, data_dir=first.data_dir)
    begin(second, page)
    page.fill("#participant-label", "P001")
    page.wait_for_selector("#continue-offer", state="visible", timeout=15_000)
    page.click("#advance")  # "Continue from Delayed free recall": the participant step is the only setup
    page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]', timeout=15_000)
    later = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    progress = json.loads((later / "progress.json").read_text())
    assert progress["continues"] == earlier.name and progress["seed"] == seed
    page.click("#advance")
    recall_field_ready(page)
    for text in (list1[3], list2[7]):
        type_entry(page, text)
    wait_events(later, "recall.resp", 2)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    [rec] = named(later, "recall.trial")
    assert rec["payload"]["lists"] == [list1, list2]
    assert [(m["list"], m["position"]) for m in rec["payload"]["matched"]] == [(1, 4), (2, 8)]
    assert rec["payload"]["intrusions"] == []


def test_delayed_recall_alone_in_the_default_flow_uses_the_lists_studied_earlier(
    app, page, device, answering  # noqa: F811
):
    # Session 1 studies both lists (the words flow) and stops before its recall.
    first = app(port=device().path, flow=FLOW)
    begin(first, page)
    advance_until(page, "words", "waiting")
    earlier = session_folder_of(first)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]', timeout=15_000)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    list1, list2 = studied(earlier, "list1"), studied(earlier, "list2")
    seed = json.loads((earlier / "progress.json").read_text())["seed"]
    first.process.send_signal(signal.SIGINT)
    first.process.wait(timeout=15)

    # Session 2: the default flow, only the delayed recall round (125 s), chosen on the start screen.
    second = app(port=answering.path, data_dir=first.data_dir)
    seen = watch_server(page)
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="recall"]')
    page.wait_for_selector("#play-only", state="visible")
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    # The default flow's calibration step (contact check, eyes open and closed, blinks) is played
    # as a participant plays it: on a device that answers the contact-check bytes.
    advance_until(page, "recall", "waiting", timeout=180)
    later = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    progress = json.loads((later / "progress.json").read_text())
    assert progress["only"] == "recall" and progress["seed"] == seed
    page.wait_for_selector("#task-instructions", state="visible")
    assert page.inner_text("#task-instructions") == f"{DELAYED_TEXT} {START_LINE}"
    page.click("#advance")
    recall_field_ready(page)
    [start] = named(later, "block_start")[-1:]
    assert start["payload"] == {"block": "recall", "seconds": 125.0}
    # The recall clock: 120 s from the cue (after the 2 s lead-in), not the block's 125 s.
    remaining = float(page.get_attribute("#recall-time", "data-remaining"))
    assert 115 < remaining <= 120
    [rs] = named(later, "recall.recall_start")
    assert rs["payload"]["recall_ms"] == 120000
    for text in (list2[0], list1[6].upper(), "notaword"):
        type_entry(page, text)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=140_000)
    [rec] = named(later, "recall.trial")
    r = rec["payload"]
    assert r["status"] == "done" and r["lists"] == [list1, list2]
    assert r["ended_by"] == "time" and abs(r["recall_ms"] - 120000) <= TOLERANCE_MS
    assert [(m["list"], m["position"]) for m in r["matched"]] == [(2, 1), (1, 7)]
    assert r["intrusions"] == ["notaword"]
    check_delivery(page, seen)
    page.wait_for_selector("#advance:not([disabled])")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]', timeout=15_000)
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    second.process.send_signal(signal.SIGINT)
    second.process.wait(timeout=15)

    # Session 3: from Delayed recall on (start_at), in the words flow (15 s): the same lists.
    third = app(port=device().path, flow=FLOW, data_dir=first.data_dir)
    status, _ = request(third, "POST", "/api/participant", {"label": "P001", "start_at": "recall"})
    assert status == 200
    assert server_state(third)["plan"] == ["participant", "recall", "summary"]
    assert server_state(third)["seed"] == seed
    # No screen sends start_at: the participant step is passed over a participant socket (the
    # label is already accepted), then the page opens at the recall round and enters full screen.
    with open_ws(third) as ws:
        drive_to(ws, "recall")
    page.goto(third.url)
    page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]', timeout=15_000)
    page.wait_for_selector("#fs-return", state="visible")
    page.click("#fs-return")
    page.wait_for_function("document.fullscreenElement !== null")
    page.wait_for_selector("#task-instructions", state="visible")
    page.click("#advance")
    recall_field_ready(page)
    known = {earlier.name, later.name}
    newest = next(p for p in (third.data_dir / "sessions").iterdir() if p.is_dir() and p.name not in known)
    for text in (list1[0], list2[11]):
        type_entry(page, text)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    [rec] = named(newest, "recall.trial")
    assert rec["payload"]["lists"] == [list1, list2]
    assert [(m["list"], m["position"]) for m in rec["payload"]["matched"]] == [(1, 1), (2, 12)]


def test_keys_pressed_during_study_leave_every_onset_and_offset_on_the_schedule(
    app, pty_device, page, tmp_path
):
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [{"id": "list1", "seconds": 60, "points": 100}]
    flow = tmp_path / "words.json"
    flow.write_text(json.dumps(raw))
    a = app(port=pty_device.path, flow=flow)
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.click("#advance")
    # Keys while a word is up and in the gap after it, for every word of the list.
    keys = ["KeyX", "KeyF", "KeyJ", "Space", "Digit1", "Enter"]
    pressed = {"word": 0, "gap": 0}
    for position in range(1, 13):
        page.wait_for_selector("#words-word", state="visible", timeout=10_000)
        page.keyboard.press(keys[position % len(keys)])
        pressed["word"] += 1
        page.wait_for_function("document.getElementById('words-word') === null", timeout=10_000)
        if position < 12:
            page.keyboard.press(keys[(position + 3) % len(keys)])
            pressed["gap"] += 1
    recall_field_ready(page)
    assert pressed == {"word": 12, "gap": 11}
    events = events_of(folder)
    [fixation] = [e for e in events if e["name"] == "words.stim" and e["payload"]["part"] == "fixation"]
    stims = [e for e in events if e["name"] == "words.stim" and e["payload"]["part"] == "word"]
    offs = [e for e in events if e["name"] == "words.stim_off" and e["payload"]["part"] == "word"]
    [cue] = [e for e in events if e["name"] == "words.stim" and e["payload"]["part"] == "cue"]
    assert [s["payload"]["position"] for s in stims] == list(range(1, 13)) and len(offs) == 12
    anchor = fixation["browser_time"]
    blanks = [s["payload"]["blank_ms"] for s in stims]
    # The jitter is the one the session seed gives for this block, and it is in the trial record.
    [trial] = wait_events(folder, "words.trial", 1, timeout=30)
    assert blanks == trial["payload"]["blanks_ms"] + [trial["payload"]["cue_delay_ms"]]
    # The logged frame_ms is a rounded estimate (16.7 at 60 Hz); over ~3000 frames its error adds
    # up to whole frames, so the grid period is taken from the run: first from the mean word
    # duration (180 frames, unambiguous), then over the whole list. A frame lost to a key moves
    # an event by a whole period (~16.7 ms).
    ups = [off["browser_time"] - stim["browser_time"] for stim, off in zip(stims, offs)]
    rough = (sum(ups) / len(ups)) / round(3000 / offs[-1]["payload"]["frame_ms"])
    last = offs[-1]["browser_time"] - anchor
    period = last / round(last / rough)
    assert abs(period - offs[-1]["payload"]["frame_ms"]) < 0.5
    due_on = anchor + 2000
    for k, (stim, off) in enumerate(zip(stims, offs)):
        for event, due in ((stim, due_on), (off, due_on + 3000)):
            assert abs(event["payload"]["due_ms"] - due) < 1e-6
            assert event["payload"]["late_frames"] == 0
            # On the frame grid of the anchor, in the frame nearest its due time.
            elapsed = event["browser_time"] - anchor
            frames = round((due - anchor) / period)
            assert abs(elapsed - frames * period) <= 1.0, (k, event["name"], elapsed, period)
        # Every word up for the same whole number of frames (3000 ms); each blank as drawn.
        up = off["browser_time"] - stim["browser_time"]
        assert abs(up - round(3000 / period) * period) <= 1.0, (k, up)
        nxt = stims[k + 1]["browser_time"] if k < 11 else cue["browser_time"]
        gap = nxt - off["browser_time"]
        # The gap is the drawn blank, both of its ends on the frame nearest their due times on the
        # anchor's grid: a whole number of frames, each end rounded on its own.
        frames_gap = round((due_on + 3000 + blanks[k] - anchor) / period) - round((due_on + 3000 - anchor) / period)
        assert abs(gap - frames_gap * period) <= 1.0, (k, gap, blanks[k], frames_gap)
        assert abs(frames_gap * period - blanks[k]) <= period, (k, frames_gap, blanks[k])
        due_on += 3000 + blanks[k]
    # The recall cue (and the recall clock's start) on the schedule too: after the cue delay.
    assert abs(cue["payload"]["due_ms"] - due_on) < 1e-6
    assert cue["payload"]["late_frames"] == 0
    # The presses were taken as stray keys (one per trial), not as anything else.
    assert len(named(folder, "words.stray")) == 1


def test_two_sessions_with_the_same_seed_in_one_tab_each_study_the_whole_list(
    app, page, device, tmp_path  # noqa: F811
):
    # A 2 s rest round first, so that a session stopped inside Words can be continued at Words.
    raw = json.loads(FLOW.read_text())
    raw.insert(1, {"id": "rest_open", "kind": "rest", "title": "Rest",
                   "blocks": [{"id": "rest", "seconds": 2, "points": 100}]})
    flow = tmp_path / "words-after-rest.json"
    flow.write_text(json.dumps(raw))
    first = app(port=device().path, flow=flow)
    begin(first, page)
    advance_until(page, "words", "waiting")
    earlier = session_folder_of(first)
    page.click("#advance")
    wait_events(earlier, "words.stim", 3, part="word")
    # Session 1 ends mid-list by Ctrl-C, with no pause. (Before 2026-11-26 it was paused and then
    # stopped; under the pause rule a pause ends the list and counts it as played, so the
    # continued session would go on with list 2. A block cut by the end of the session is not
    # played, so session 2 continues at list 1, which this test is about.)
    # What this tab kept for session 1 (the words state among it).
    kept = page.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
    assert any("/words/task/list_list1" in k for k in kept), kept
    first.process.send_signal(signal.SIGINT)
    first.process.wait(timeout=15)
    shown_before = [e["payload"]["word"] for e in named(earlier, "words.stim", "word")]

    # Session 2 continues it with the same seed. A server cannot take the same port again at once
    # (the closed connections hold it), so it runs on another port, and the tab's storage is
    # carried over to that origin as it was before the page loads: the same tab state.
    second = app(port=device().path, flow=flow, data_dir=first.data_dir)
    page.add_init_script(
        "if (!sessionStorage.getItem('carried')) {"
        f" for (const [k, v] of Object.entries({json.dumps(kept)})) sessionStorage.setItem(k, v);"
        " sessionStorage.setItem('carried', '1'); }"
    )
    page.goto(second.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    assert all(page.evaluate("(k) => sessionStorage.getItem(k)", k) == v for k, v in kept.items())
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    page.fill("#participant-label", "P001")
    page.wait_for_selector("#continue-offer", state="visible", timeout=15_000)
    page.click("#advance")  # Continue from Words
    page.wait_for_selector(f'{SCREEN}[data-step="words"][data-phase="waiting"]', timeout=15_000)
    later = next(p for p in (second.data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    assert json.loads((later / "progress.json").read_text())["seed"] == json.loads(
        (earlier / "progress.json").read_text()
    )["seed"]
    page.click("#advance")
    recall_field_ready(page)
    stims = [e["payload"] for e in named(later, "words.stim", "word")]
    assert [s["position"] for s in stims] == list(range(1, 13))
    assert [s["word"] for s in stims][: len(shown_before)] == shown_before  # the same seed
    assert [e["payload"]["next_position"] for e in named(later, "words.stim", "fixation")] == [1]
    # A stop during recall logs the text still in the field.
    type_entry(page, "half", enter=False)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-stop-here")
    page.click("#fs-leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-phase="done"]', timeout=15_000)
    [unsub] = named(later, "words.unsubmitted")
    assert unsub["payload"]["text"] == "half" and unsub["payload"]["block"] == "list1"


def test_a_word_list_that_cannot_be_loaded_pauses_the_block_and_says_what_to_do(
    pty_device, page, tmp_path
):
    # A copy of the package without its word pool, run in place of the installed one.
    package = tmp_path / "pkg"
    shutil.copytree(Path(brain_mapper.__file__).parent, package / "brain_mapper")
    (package / "brain_mapper" / "web" / "assets" / "wordpool.txt").unlink()
    data = tmp_path / "data"
    args = [sys.executable, "-c", "import sys; from brain_mapper.cli import main; sys.exit(main(sys.argv[1:]))",
            "--no-browser", "--data-dir", str(data), "--http-port", "0",
            "--port", str(pty_device.path), "--flow", str(FLOW)]
    env = {**os.environ, "PYTHONPATH": str(package)}
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    try:
        url = _Lines(process.stdout).wait_for(PRINTED, timeout=30).removeprefix(PRINTED)
        a = App(url, data, process)
        with urllib.request.urlopen(f"{url}/api/flow") as r:  # the copy is the one serving
            assert r.status == 200
        try:
            urllib.request.urlopen(f"{url}/static/assets/wordpool.txt")
            raise AssertionError("the copy still serves the pool")
        except urllib.error.HTTPError as error:
            assert error.code == 404
        # Every text the problem element showed, recorded as it is put in the page.
        page.add_init_script(
            "window.__problems = []; new MutationObserver(() => {"
            " const p = document.getElementById('words-problem');"
            " if (p && !window.__problems.includes(p.textContent)) window.__problems.push(p.textContent);"
            " }).observe(document, { subtree: true, childList: true });"
        )
        begin(a, page)
        advance_until(page, "words", "waiting")
        folder = session_folder_of(a)
        page.click("#advance")
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=15_000)
        [problem] = page.evaluate("window.__problems")
        assert problem.startswith("The word list could not be loaded, so this round cannot be played.")
        assert "Skip this round" in problem and "Stop here" in problem
        assert "could not be loaded" in page.inner_text("#fs-lost-why")
        [failed] = named(folder, "words.pool_failed")
        assert "404" in failed["payload"]["error"]
        assert not named(folder, "words.stim")
        # The message was in the panel until the runner took it off for the pause.
        [trial] = named(folder, "words.trial")
        assert trial["payload"]["status"] == "abandoned"
        assert trial["payload"]["reason"] == "the word list could not be loaded"
        # Resume: the pause ended list 1 (the pause rule of 2026-11-25), so the between screen
        # comes; list 2 tries the pool again, and its block pauses again.
        page.click("#fs-return")
        page.wait_for_selector(f'{SCREEN}[data-phase="between"]', timeout=15_000)
        page.click("#advance")
        page.wait_for_function(
            "document.getElementById('words-problem') !== null"
            " || document.getElementById('screen').dataset.phase === 'paused'",
            timeout=15_000,
        )
        wait_events(folder, "words.pool_failed", 2, timeout=15)
        page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=15_000)
        ends = [e for e in events_of(folder) if e["name"] == "block_end" and e["step"] == "words"]
        assert [(e["block"], e["payload"].get("ended_by")) for e in ends] == [("list1", "interrupted"), ("list2", "interrupted")]
        page.click("#fs-skip-round")
        page.click("#fs-leave-yes")
        page.wait_for_selector(f'{SCREEN}[data-step="recall"]', timeout=15_000)
        # Before: "skipped". Both lists were ended by a pause, and a block a pause ends counts as
        # played (since 2026-11-26), so the round counts as finished, with no word studied.
        assert server_state(a)["outcomes"]["words"] == "finished"
        assert not named(folder, "words.stim")
    finally:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()



def test_one_enter_starts_exactly_one_list(app, pty_device, page, tmp_path):
    """runner.js and words.js both handle Enter: one press on
    the instruction screen and one on the between screen each send one ``advance`` and start one
    list, and nothing else."""
    sent: list[dict] = []

    def on_socket(ws):
        def on_sent(payload):
            try:
                sent.append(json.loads(payload))
            except (TypeError, ValueError):
                pass

        ws.on("framesent", on_sent)

    page.on("websocket", on_socket)
    seen = watch_server(page)
    a = app(port=pty_device.path, flow=FLOW)
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.wait_for_selector("#task-instructions", state="visible")
    before = len([m for m in sent if m.get("cmd") == "advance"])
    page.keyboard.press("Enter")
    wait_events(folder, "words.stim", 1, part="word")
    starts = [e["block"] for e in events_of(folder) if e["name"] == "block_start"]
    assert starts == ["list1"]
    assert len([m for m in sent if m.get("cmd") == "advance"]) == before + 1
    # The between screen before list 2 (reached by a pause, which ends list 1).
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.click("#fs-return")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    page.wait_for_function("document.getElementById('between-instructions').textContent.includes('Enter')")
    before = len([m for m in sent if m.get("cmd") == "advance"])
    page.keyboard.press("Enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="task"][data-phase="running"]', timeout=15_000)
    wait_until(lambda: [e for e in named(folder, "words.stim", "word") if e["block"] == "list2"], "the first word of list 2", timeout=20)
    starts = [e["block"] for e in events_of(folder) if e["name"] == "block_start"]
    assert starts == ["list1", "list2"]
    assert len([m for m in sent if m.get("cmd") == "advance"]) == before + 1
    assert server_state(a)["block"] == "list2" and server_state(a)["phase"] == "running"
    assert not [m for m in seen if m.get("t") == "error"], [m for m in seen if m.get("t") == "error"]

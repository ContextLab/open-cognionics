"""Starting a list with Enter, the recall clock, and ending recall early ("I can't remember any
more"), in a real Chromium against the real server on a pseudo-terminal device.

The page asks the engine to end the block with ``{"cmd": "block_finished", "reason":
"participant_done"}`` once the participant has ended recall. These tests hold whether or not the
engine knows that command: either the block ends at once, or the engine answers that the command is
unknown and the stage says "Waiting for the time to finish" (no field) until the block's time is up.
``test_task_words_finish_early.py`` checks the engine's side and is run once branch night-engine is
merged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from brain_mapper import flow as flow_module
from test_engine import session_folder_of
from test_shell_browser import SCREEN, advance_until, begin, server_state
from test_task_words import (
    DELAYED_TEXT,
    ENTER_LINE,
    FLOW,
    RECALL_VIEW,
    START_LINE,
    TOLERANCE_MS,
    named,
    recall_field_ready,
    studied,
    type_entry,
    wait_events,
    watch_server,
)

#: "Yes" in the end-recall question takes no press for this long after it appears (words.js).
CONFIRM_GUARD_MS = 300
WAIT_TEXT = "Waiting for the time to finish"


def watch_sent(page) -> list[dict]:
    """Every command the page sends the server, as parsed JSON (filled as they go out)."""
    sent: list[dict] = []

    def on_socket(ws):
        def on_frame(payload):
            try:
                sent.append(json.loads(payload))
            except (TypeError, ValueError):
                pass

        ws.on("framesent", on_frame)

    page.on("websocket", on_socket)
    return sent


def asked(sent: list[dict]) -> int:
    """How many times the page sent block_finished."""
    return len([c for c in sent if c.get("cmd") == "block_finished"])


def wait_sent(page, sent: list[dict], count: int, timeout: float = 10.0) -> None:
    """Waits until block_finished went out `count` times. Playwright hands over socket frames only
    while it runs a call, so the wait is made of short page waits (not a sleep)."""
    deadline = time.monotonic() + timeout
    while asked(sent) < count:
        assert time.monotonic() < deadline, f"block_finished sent {asked(sent)} times, not {count}"
        page.wait_for_timeout(50)


def one_list_flow(tmp_path: Path, list_seconds: float, recall_seconds: float) -> Path:
    raw = json.loads(FLOW.read_text())
    raw[1]["blocks"] = [{"id": "list1", "seconds": list_seconds, "points": 100}]
    raw[2]["blocks"] = [{"id": "recall", "seconds": recall_seconds, "points": 100}]
    path = tmp_path / "words-one-list.json"
    path.write_text(json.dumps(raw))
    return path


def focused(page) -> str:
    return page.evaluate("document.activeElement ? document.activeElement.id : ''")


def finish_early(page, folder: Path, round_: str) -> dict:
    """Tab from the field to "I can't remember any more", Enter, "Keep going" once (back to the
    field), then Tab, Enter and Enter on "Yes". Returns the `recall_end` payload."""
    assert focused(page) == "recall-input"
    page.keyboard.press("Tab")
    assert focused(page) == "recall-done"
    assert page.inner_text("#recall-done") == "I can't remember any more"
    page.keyboard.press("Enter")
    page.wait_for_selector("#recall-confirm", state="visible")
    assert page.inner_text("#recall-confirm-text") == "End recall now?"
    assert page.locator("#recall-done").is_hidden()
    assert focused(page) == "recall-yes"
    # Keep going: back to the field, nothing ended.
    page.keyboard.press("Tab")
    assert focused(page) == "recall-keep"
    page.keyboard.press("Enter")
    page.wait_for_selector("#recall-confirm", state="hidden")
    page.wait_for_function("document.activeElement && document.activeElement.id === 'recall-input'")
    wait_events(folder, f"{round_}.recall_continue", 1, timeout=10)
    assert not named(folder, f"{round_}.recall_end")
    # Now end it.
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    page.wait_for_selector("#recall-confirm", state="visible")
    page.wait_for_timeout(CONFIRM_GUARD_MS + 100)  # pacing past the guard, as a person would
    page.keyboard.press("Enter")
    [end] = wait_events(folder, f"{round_}.recall_end", 1, timeout=10)
    return end


def check_ended_quietly(page) -> None:
    """After the participant ended recall: the between screen (the engine ended the block), or the
    quiet waiting line with no field in it (the engine does not know the command yet)."""
    page.wait_for_function(
        f"""() => {{
          const root = document.getElementById('screen');
          if (root.dataset.screen === 'between') return true;
          const wait = document.getElementById('words-wait');
          return !!wait && wait.textContent === {json.dumps(WAIT_TEXT)}
            && document.getElementById('recall-input') === null;
        }}""",
        timeout=10_000,
    )


def test_enter_starts_each_list_and_finishing_early_asks_once_and_is_logged_in_both_recalls(
    app, pty_device, page, tmp_path
):
    page.set_viewport_size({"width": 1512, "height": 982})
    seen = watch_server(page)
    sent = watch_sent(page)
    a = app(port=pty_device.path, flow=one_list_flow(tmp_path, 80, 40))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.wait_for_selector("#task-instructions", state="visible")
    assert page.inner_text("#task-enter-line") == ENTER_LINE
    # Nothing has started: the block waits for Enter.
    assert server_state(a)["phase"] == "waiting" and not named(folder, "words.stim")
    pressed = page.evaluate("performance.now()")
    page.keyboard.press("Enter")
    [fixation] = wait_events(folder, "words.stim", 1, timeout=10, part="fixation")
    assert fixation["browser_time"] >= pressed
    assert fixation["payload"]["lead_in_ms"] == 2000
    [first] = wait_events(folder, "words.stim", 1, timeout=10, part="word")
    assert abs(first["payload"]["due_ms"] - (fixation["browser_time"] + 2000)) < 1e-6
    assert len(named(folder, "block_start")) == 1  # one Enter, one block

    recall_field_ready(page, timeout=70_000)
    kids = page.evaluate("[...document.getElementById('words-recall').children].map((e) => e.id)")
    assert kids == RECALL_VIEW
    assert page.inner_text("#recall-cue").replace(" ", "") == "*******"
    [start] = named(folder, "words.recall_start")
    [cue] = named(folder, "words.stim", "cue")
    assert start["browser_time"] == cue["browser_time"]  # the clock starts with the cue's frame
    list1 = studied(folder, "list1")

    # Enter twice on the empty field within 2 s: no question, no end, the field keeps the focus.
    t0 = page.evaluate("performance.now()")
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    assert page.evaluate("performance.now()") - t0 < 2000
    assert page.locator("#recall-confirm").is_hidden()
    assert focused(page) == "recall-input"
    type_entry(page, list1[2])
    [resp] = wait_events(folder, "words.resp", 1, timeout=10)
    # Events arrive in order: nothing was logged for the two Enters before this entry.
    assert not named(folder, "words.recall_confirm") and not named(folder, "words.recall_end")
    assert resp["payload"]["text"] == list1[2]
    type_entry(page, "half", enter=False)
    end = finish_early(page, folder, "words")
    p = end["payload"]
    assert p["ended_by"] == "participant" and p["list"] == 1
    assert abs(p["recall_ms"] - (end["browser_time"] - start["browser_time"])) < 1e-6
    assert 0 < p["recall_ms"] < start["payload"]["recall_ms"]
    [unsub] = named(folder, "words.unsubmitted")
    assert unsub["payload"]["text"] == "half" and unsub["payload"]["ended"] == "participant"
    check_ended_quietly(page)
    [trial] = wait_events(folder, "words.trial", 1, timeout=10)
    t = trial["payload"]
    assert t["status"] == "done" and t["scored"] and t["ended_by"] == "participant"
    assert t["typed"] == [list1[2]] and t["unsubmitted"] == "half"
    assert abs(t["recall_ms"] - p["recall_ms"]) < 1e-6
    # The page asked the engine to end the block, after the trial record went out.
    wait_sent(page, sent, 1)
    asks = [i for i, c in enumerate(sent) if c.get("cmd") == "block_finished"]
    assert len(asks) == 1 and sent[asks[0]] == {"cmd": "block_finished", "reason": "participant_done"}
    trial_sent = [i for i, c in enumerate(sent) if c.get("cmd") == "event" and c.get("name") == "words.trial"]
    assert trial_sent and trial_sent[0] < asks[0]
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=90_000)
    page.click("#advance")

    # Delayed recall: Enter starts it; a 2 s lead-in, then the cue and the field.
    page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]', timeout=15_000)
    assert page.inner_text("#task-instructions") == f"{DELAYED_TEXT} {START_LINE}"
    page.keyboard.press("Enter")
    recall_field_ready(page, timeout=15_000)
    [lead] = named(folder, "recall.stim", "fixation")
    [rcue] = named(folder, "recall.stim", "cue")
    assert abs(rcue["payload"]["due_ms"] - (lead["browser_time"] + 2000)) < 1e-6
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    type_entry(page, list1[5].upper())
    wait_events(folder, "recall.resp", 1, timeout=10)
    assert not named(folder, "recall.recall_end")
    rend = finish_early(page, folder, "recall")
    assert rend["payload"]["ended_by"] == "participant"
    check_ended_quietly(page)
    [rec] = wait_events(folder, "recall.trial", 1, timeout=10)
    assert rec["payload"]["ended_by"] == "participant" and rec["payload"]["unsubmitted"] is None
    wait_sent(page, sent, 2)
    assert asked(sent) == 2
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=60_000)

    # Scores: an early end changes nothing in the rule; possible is the words studied.
    scores = server_state(a)["scores"]["tasks"]
    assert (scores["words"]["earned"], scores["words"]["possible"]) == (1, 12)
    assert (scores["recall"]["earned"], scores["recall"]["possible"]) == (1, 24)
    # No event was refused; only the engine's answer to block_finished may be an error.
    errors = [m for m in seen if m.get("t") == "error"]
    assert all("block_finished" in m.get("msg", "") for m in errors), errors


def test_the_recall_clock_runs_90_s_from_the_cue_and_looks_the_same_to_the_end(
    app, pty_device, page, tmp_path
):
    seconds = flow_module.TASK_SECONDS["words"]
    assert seconds == 145.0
    page.set_viewport_size({"width": 1920, "height": 1080})
    a = app(port=pty_device.path, flow=one_list_flow(tmp_path, seconds, 20))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.keyboard.press("Enter")
    recall_field_ready(page, timeout=70_000)
    [fixation] = named(folder, "words.stim", "fixation")
    [cue] = named(folder, "words.stim", "cue")
    [start] = named(folder, "words.recall_start")
    study_ms = cue["browser_time"] - fixation["browser_time"]
    assert 2000 + 36000 + 11 * 800 + 1200 - TOLERANCE_MS <= study_ms <= 52600 + TOLERANCE_MS
    # Its own 90 s, whatever the study took, counted from the cue's frame.
    assert start["payload"]["recall_ms"] == 90000
    remaining = float(page.get_attribute("#recall-time", "data-remaining"))
    assert 88 < remaining <= 90
    assert page.inner_text("#recall-time") in ("Time left 1:30", "Time left 1:29")
    style = (
        "(() => { const e = document.getElementById('recall-time'); const s = getComputedStyle(e);"
        " return [e.className, s.color, s.fontSize, s.fontWeight, s.animationName, s.opacity,"
        " s.visibility]; })()"
    )
    early = page.evaluate(style)
    page.wait_for_function(
        "Number(document.getElementById('recall-time').dataset.remaining) < 8", timeout=100_000
    )
    assert page.evaluate(style) == early  # the last 10 s look like the rest: no flashing
    [end] = wait_events(folder, "words.recall_end", 1, timeout=20)
    assert end["payload"]["ended_by"] == "time"
    assert abs(end["payload"]["recall_ms"] - 90000) <= TOLERANCE_MS
    page.wait_for_function(
        "document.getElementById('words-wait') && document.getElementById('words-wait').textContent"
        " === 'Time is up.' || document.getElementById('screen').dataset.screen === 'between'"
    )
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=20_000)
    [trial] = named(folder, "words.trial")
    assert trial["payload"]["status"] == "done" and trial["payload"]["ended_by"] == "time"

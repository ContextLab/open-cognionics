"""End to end: "I can't remember any more" ends the block at once, in Words and in Delayed recall.

Written against the engine command ``{"cmd": "block_finished", "reason": "participant_done"}``
(branch night-engine; allowed for words blocks and for delayed recall). It passes once that branch
is merged; before then the engine answers that the command is unknown and the block runs out its
time (``test_task_words_finish.py`` covers the page's side either way).
"""

from __future__ import annotations

import time

from test_engine import events_of, session_folder_of
from test_shell_browser import SCREEN, advance_until, begin, server_state
from test_task_words import named, recall_field_ready, studied, type_entry, wait_events, watch_server
from test_task_words_finish import finish_early, one_list_flow


def test_ending_recall_early_ends_the_block_now_and_keeps_its_records_and_scores(
    app, pty_device, page, tmp_path
):
    seen = watch_server(page)
    # Long blocks: if the block ended by its time, the test would wait far past its bounds.
    a = app(port=pty_device.path, flow=one_list_flow(tmp_path, 145, 125))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    page.keyboard.press("Enter")
    recall_field_ready(page, timeout=70_000)
    list1 = studied(folder, "list1")
    type_entry(page, list1[0])
    type_entry(page, list1[1])
    wait_events(folder, "words.resp", 2, timeout=10)
    end = finish_early(page, folder, "words")
    asked = time.monotonic()
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=5_000)
    assert time.monotonic() - asked < 5
    # The block ended when the participant was done, long before its 145 s.
    [block_end] = [e for e in events_of(folder) if e["name"] == "block_end"][-1:]
    [start] = [e for e in events_of(folder) if e["name"] == "block_start"][-1:]
    assert (block_end["server_mono_ns"] - start["server_mono_ns"]) / 1e9 < 120
    [trial] = named(folder, "words.trial")
    t = trial["payload"]
    assert t["status"] == "done" and t["scored"] and t["ended_by"] == "participant"
    assert t["typed"] == list1[:2]
    assert end["payload"]["ended_by"] == "participant"
    page.click("#advance")

    page.wait_for_selector(f'{SCREEN}[data-step="recall"][data-phase="waiting"]', timeout=15_000)
    page.keyboard.press("Enter")
    recall_field_ready(page, timeout=15_000)
    type_entry(page, list1[3])
    wait_events(folder, "recall.resp", 1, timeout=10)
    finish_early(page, folder, "recall")
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=5_000)
    [rec] = named(folder, "recall.trial")
    assert rec["payload"]["status"] == "done" and rec["payload"]["ended_by"] == "participant"
    scores = server_state(a)["scores"]["tasks"]
    assert (scores["words"]["earned"], scores["words"]["possible"]) == (2, 12)
    assert (scores["recall"]["earned"], scores["recall"]["possible"]) == (1, 24)
    assert not [m for m in seen if m.get("t") == "error"], [m for m in seen if m.get("t") == "error"]

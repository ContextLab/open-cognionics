"""End to end: a task page that has no trial left that fits its block says ``block_finished``
(reason ``trials_done``) and the real engine ends the block then, within its grace, as a
finished block (night-tasks sends the command, night-engine
takes it; neither branch had the other).

Real Chromium, the real server and the simulated headset; the perception task (trials of
3100-3500 ms after a 2 s lead-in, so a 12 s block holds 2 or 3 trials and then has time left
in which no further trial fits).
"""

from __future__ import annotations

from brain_mapper.engine import TASK_GRACE_S
from test_engine import events_of, session_folder_of
from test_shell_browser import SCREEN, server_state
from test_task_perception import AREA, BLOCK_OVER, awaiting, flow_with, named, practise, start, to_instructions


def test_a_block_with_no_trial_left_that_fits_ends_by_trials_done_through_the_real_engine(
    app, pty_device, page, tmp_path
):
    a = app(port=pty_device.path, flow=flow_with(tmp_path, 12, 12))
    to_instructions(a, page)
    folder = session_folder_of(a)
    practise(page, folder)
    start(page, "block")
    awaiting(page, 0, False)
    trial = 0
    while True:
        target = f'{AREA}[data-trial="{trial}"][data-practice="false"][data-awaiting="true"], {BLOCK_OVER}'
        page.wait_for_selector(target, timeout=25_000)
        if page.locator(BLOCK_OVER).count():
            break
        page.keyboard.press("f")
        trial += 1
    events = events_of(folder)
    start_ev = next(e for e in events if e["name"] == "block_start" and e["block"] == "block1")
    end_ev = next(e for e in events if e["name"] == "block_end" and e["block"] == "block1")
    # The page said so and the engine ended the block by it.
    assert end_ev["payload"]["ended_by"] == "trials_done", end_ev
    # Every trial the page started ended before the block did; none was cut short.
    scored = [t["payload"] for t in named(folder, "trial") if not t["payload"]["practice"] and t["payload"]["block"] == "block1"]
    assert len(scored) == trial >= 2
    assert all(p["status"] == "done" and p["scored"] for p in scored)
    last_trial = max(t["unix_time"] for t in named(folder, "trial") if t["payload"]["block"] == "block1" and not t["payload"]["practice"])
    assert last_trial <= end_ev["unix_time"]
    # Ended within the block's time and its grace, not by the clock after the grace.
    took = end_ev["unix_time"] - start_ev["unix_time"]
    assert took < 12 + TASK_GRACE_S, took
    # A finished block: it counts as played and the next block follows (not this one again).
    state = server_state(a)
    assert state["blocks_done"] == 1 and state["phase"] == "between"
    start(page, "block")
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]', timeout=10_000)
    starts = [e["block"] for e in events_of(folder) if e["name"] == "block_start"]
    assert starts == ["block1", "block2"]

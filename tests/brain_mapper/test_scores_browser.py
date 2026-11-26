"""Scores in a real session: a scripted participant in a real Chromium, the real server on a
pseudo-terminal headset, and the points shown, saved in ``session.json``, zipped and re-scored.

The flow is Words (one list), Flanker (one block) and Delayed recall. The participant types some
studied words (with a repeat, another case and a word not on the list) and answers the flanker
trials right, wrong or not at all by a fixed plan, practice included. What the screen must show is
worked out from what the script did: the words it typed, and for flanker the plan of each trial that
the runner finished (``status`` done; abandoned trials and the practice trials
do not count). A finished session, a stopped one, one paused by a lost link and then stopped, and
one ended by Ctrl-C while paused by a lost link are played. The summary is checked
to sit on the dark page ground with text contrast of at least 7:1, from the computed styles.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path

from brain_mapper import scores
from test_calibration_browser import watch_socket
from test_engine import events_of, next_state, open_ws, send, session_folder_of
from test_link_pause import Plug, to_running_block, until
from test_package import wait_packaged, zip_contents
from test_shell_browser import SCREEN, advance_until, begin
from test_skip_browser import answer_yes, ask
from test_task_flanker import act, awaiting_selector, practise, start_block
from test_task_words import recall_field_ready, studied, type_entry
from test_whole_session_browser import one_task_flow
from shots import ShotsDir

SHOTS = ShotsDir("shots-feat-scores")
BETWEEN = f'{SCREEN}[data-screen="between"]'
UNSTUDIED = "qzxv"  # not a word of the pool


def flow_file(tmp_path: Path, flanker_seconds: float = 20) -> Path:
    raw = [
        {"id": "participant", "kind": "setup", "title": "Participant", "blocks": []},
        {"id": "words", "kind": "task", "title": "Words", "blocks": [{"id": "list1", "seconds": 64, "points": 100}]},
        {"id": "flanker", "kind": "task", "title": "Flanker", "blocks": [{"id": "block1", "seconds": flanker_seconds, "points": 100}]},
        {"id": "recall", "kind": "recall", "title": "Delayed free recall",
         "blocks": [{"id": "recall", "seconds": 12, "points": 100}]},
        {"id": "summary", "kind": "summary", "title": "Summary", "blocks": []},
    ]
    path = tmp_path / "scores-flow.json"
    path.write_text(json.dumps(raw))
    return path


def plan_for(i: int) -> str:
    return "wrong" if i % 4 == 1 else "miss" if i % 4 == 3 else "correct"


def text(page, selector: str) -> str:
    return page.inner_text(selector).strip()


def wait_text(page, selector: str, value: str, timeout: float = 10_000) -> None:
    page.wait_for_function(
        "([s, v]) => { const e = document.querySelector(s); return !!e && e.textContent.trim() === v; }",
        arg=[selector, value],
        timeout=timeout,
    )


def all_stored(page) -> None:
    page.wait_for_function(
        "async () => (await import('/static/js/tasks/runner.js')).pendingEvents() === 0", timeout=15_000
    )


def play_words(page, folder: Path) -> tuple[int, int]:
    """Studies list 1 and types 3 of its words (one twice, one in capitals) and one unstudied word."""
    advance_until(page, "words", "waiting")
    page.click("#advance")
    recall_field_ready(page)
    words = studied(folder, "list1")
    assert len(words) == 12
    for entry in (words[2].upper(), f" {words[5]} ", words[2], UNSTUDIED, words[9]):
        type_entry(page, entry)
    page.wait_for_selector(BETWEEN, timeout=20_000)
    return 3, 12


def play_flanker(page, stop_after: int | None = None, hang_up=None, hud: list | None = None) -> dict[int, str]:
    """Practice (right, wrong, no answer), then block trials by ``plan_for``; returns trial -> plan.

    ``stop_after``: once that many block trials were answered, full screen is left (the block pauses
    and the trial in progress is abandoned) and Stop here is asked on the pause screen; ``hang_up``
    is called instead, at that point, when given. ``hud`` collects the HUD total before each trial."""
    page.wait_for_selector("#task-instructions", state="visible")
    practise(page, ["correct", "wrong", "miss", "correct", "wrong", "correct"])
    start_block(page)  # Enter on the block's prompt
    acted: dict[int, str] = {}
    i = 0
    while True:
        if stop_after is not None and i == stop_after:
            if hang_up is not None:
                hang_up()
            else:
                page.evaluate("document.exitFullscreen()")
                page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
                ask(page, "stop-here", "fs-")
            return acted
        page.wait_for_selector(f"{awaiting_selector(i, False)}, {BETWEEN}", timeout=15_000)
        if page.locator(BETWEEN).count():
            return acted
        if hud is not None:
            hud.append(text(page, "#hud-points"))
        act(page, plan_for(i))
        acted[i] = plan_for(i)
        i += 1


def flanker_expected(folder: Path, acted: dict[int, str]) -> tuple[int, int]:
    """Earned and possible from the script's plan, for the block trials the runner finished. A trial
    that went on while the stop question was open finished with no answer from the script."""
    finished = [
        e["payload"]["trial"] for e in events_of(folder)
        if e["name"] == "flanker.trial" and not e["payload"]["practice"] and e["payload"]["status"] == "done"
    ]
    assert finished and set(acted) <= set(finished) | {max(acted)}
    return sum(acted.get(t, "miss") == "correct" for t in finished), len(finished)


def play_recall(page, folder: Path) -> tuple[int, int]:
    """Types 2 words of list 1 (one twice) and one unstudied word, of the 24 words of both lists."""
    words = studied(folder, "list1")
    advance_until(page, "recall", "waiting")
    page.click("#advance")
    recall_field_ready(page)
    for entry in (words[0], words[0].capitalize(), UNSTUDIED, words[11]):
        type_entry(page, entry)
    page.wait_for_selector(BETWEEN, timeout=20_000)
    return 2, 24


CONTRAST = """() => {
  const parse = (c) => { const p = c.match(/rgba?\\(([^)]+)\\)/)[1].split(',').map(Number);
                         return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
                       return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ground = (el) => {
    const chain = []; for (let e = el; e; e = e.parentElement) chain.push(e);
    let color = [255, 255, 255];
    for (const e of chain.reverse()) {
      const c = parse(getComputedStyle(e).backgroundColor);
      color = [0, 1, 2].map((i) => c[i] * c[3] + color[i] * (1 - c[3]));
    }
    return color;
  };
  const low = [], panels = [];
  let checked = 0;
  for (const e of document.querySelectorAll('#screen, #screen *, #hud, #hud *')) {
    const cs = getComputedStyle(e);
    const own = [...e.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
    const shown = e.getClientRects().length > 0 && cs.visibility !== 'hidden';
    if (shown && e.closest('#screen') && parse(cs.backgroundColor)[3] > 0 && !e.matches('.btn, .btn *, [role=progressbar], [role=progressbar] *')) panels.push(e.id || e.className || e.tagName);
    if (!own || !shown) continue;
    const bg = ground(e), fg = parse(cs.color);
    const mixed = [0, 1, 2].map((i) => fg[i] * fg[3] + bg[i] * (1 - fg[3]));
    const a = lum(mixed), b = lum(bg);
    const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    checked += 1;
    if (ratio < 7) low.push([e.id || e.className || e.tagName, e.textContent.trim().slice(0, 40), ratio.toFixed(2)]);
  }
  return { low, panels, checked, body: getComputedStyle(document.body).backgroundColor,
           html: getComputedStyle(document.documentElement).backgroundColor };
}"""


def check_on_dark_ground(page, least: int = 15) -> None:
    found = page.evaluate(CONTRAST)
    assert found["body"] == "rgb(18, 24, 38)", found  # the page's dark navy, as every dark screen
    assert found["panels"] == [], found  # no lighter panel or card under the summary
    assert found["checked"] >= least, found
    assert found["low"] == [], found


def check_saved(folder: Path, expected: dict[str, tuple[int, int]], total: tuple[int, int]) -> dict:
    """session.json, the zip (its session.json and behavior/scores.json) and a re-scoring agree."""
    zipped = zip_contents(wait_packaged(folder))
    meta = json.loads((folder / "session.json").read_text())
    saved = meta["scores"]
    assert {t: (v["earned"], v["possible"]) for t, v in saved["tasks"].items()} == expected
    assert (saved["total"]["earned"], saved["total"]["possible"]) == total
    assert json.loads(zipped["behavior/scores.json"]) == saved
    assert json.loads(zipped["session.json"])["scores"] == saved
    assert scores.score_folder(folder) == saved  # re-scored from events.jsonl alone
    rules = {t: v["rule"] for t, v in saved["tasks"].items()}
    assert rules == {t: scores.RULES[t] for t in expected}
    return saved


def row(page, step: str) -> tuple[str, str]:
    item = f'#summary-scores li[data-step="{step}"]'
    why = page.locator(f"{item} .why")
    return text(page, f"{item} .how"), (why.inner_text().strip() if why.count() else "")


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / name))


def test_a_finished_session_shows_earned_of_possible_between_blocks_and_on_the_summary(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 900, "height": 900})
    a = app(port=pty_device.path, flow=flow_file(tmp_path))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    assert text(page, "#hud-points") == "—"  # nothing possible yet

    words = play_words(page, folder)
    wait_text(page, "#between-gained", "3 of 12")
    assert text(page, "#between-lead") == "Words, points so far"
    wait_text(page, "#between-total", "3 of 12 points")
    wait_text(page, "#hud-points", "3 of 12")
    shot(page, "between-words.png")

    page.click("#advance")
    hud: list[str] = []
    acted = play_flanker(page, hud=hud)
    all_stored(page)
    # The server's scores grow with each trial during the block, and the HUD shows them after
    # each trial (before the night of 2026-11-25 it stayed at the total of the block before until
    # the block was over).
    assert len(hud) >= 4 and hud[0] == "3 of 12" and len(set(hud)) == len(hud), hud
    flanker = flanker_expected(folder, acted)
    assert 0 < flanker[0] < flanker[1]  # some right, some not
    total = (words[0] + flanker[0], words[1] + flanker[1])
    wait_text(page, "#between-gained", f"{flanker[0]} of {flanker[1]}")
    wait_text(page, "#between-total", f"{total[0]} of {total[1]} points")
    wait_text(page, "#hud-points", f"{total[0]} of {total[1]}")
    shot(page, "between-flanker.png")

    page.click("#advance")
    recall = play_recall(page, folder)
    total = (total[0] + recall[0], total[1] + recall[1])
    wait_text(page, "#between-gained", "2 of 24")
    wait_text(page, "#between-total", f"{total[0]} of {total[1]} points")

    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"][data-phase="waiting"]')
    check_on_dark_ground(page, least=5)
    page.click("#advance")  # Finish
    page.wait_for_selector("#summary-download", state="visible", timeout=60_000)
    expected = {"words": words, "flanker": flanker, "recall": recall}
    check_saved(folder, expected, total)
    assert row(page, "words") == ("3 of 12", "")
    assert row(page, "flanker") == (f"{flanker[0]} of {flanker[1]}", "")
    assert row(page, "recall") == ("2 of 24", "")
    assert text(page, "#summary-points") == f"Total: {total[0]} of {total[1]} points"
    assert text(page, "#hud-points") == f"{total[0]} of {total[1]}"
    # No score is shown as a percentage. (The results entry's progress line, added with the EEG
    # results, says how far their computation is in %; it is not a score.)
    outside_results = page.evaluate(
        "(() => { const c = document.getElementById('screen').cloneNode(true);"
        " for (const r of c.querySelectorAll('#results-entry')) r.remove(); return c.innerText ?? c.textContent; })()"
    )
    assert "%" not in outside_results
    check_on_dark_ground(page)
    shot(page, "summary-finished.png")


def test_a_stopped_session_shows_what_was_scored_and_a_dash_for_the_round_not_started(app, pty_device, page, tmp_path):
    page.set_viewport_size({"width": 900, "height": 900})
    a = app(port=pty_device.path, flow=flow_file(tmp_path, flanker_seconds=40))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    words = play_words(page, folder)
    page.click("#advance")
    acted = play_flanker(page, stop_after=4)
    answer_yes(page, "fs-")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"][data-phase="done"]', timeout=30_000)
    page.wait_for_selector("#summary-download", state="visible", timeout=60_000)
    flanker = flanker_expected(folder, acted)
    total = (words[0] + flanker[0], words[1] + flanker[1])
    trials = [e["payload"] for e in events_of(folder) if e["name"] == "flanker.trial" and not e["payload"]["practice"]]
    assert trials[-1]["status"] == "abandoned"  # the trial in progress at the stop: not counted
    check_saved(folder, {"words": words, "flanker": flanker}, total)
    assert row(page, "words") == ("3 of 12", "")
    # The pause before the stop ended the flanker block (since 2026-11-25; the reason was "").
    assert row(page, "flanker") == (f"{flanker[0]} of {flanker[1]}", "1 block interrupted by a pause")
    assert row(page, "recall") == ("—", "Stopped before it started")
    assert f"Points so far: {total[0]} of {total[1]}." in text(page, "#summary-text")
    assert text(page, "#summary-points") == f"Total: {total[0]} of {total[1]} points"
    check_on_dark_ground(page)
    shot(page, "summary-stopped.png")


def test_a_lost_link_pauses_the_session_and_the_scores_are_kept(app, pty_device, page, tmp_path):
    """A hang-up during flanker pauses the session (reason ``link_lost``); the scores at that
    moment are those of the finished trials (the abandoned one is not counted); Stop here on the
    paused screen ends the session with them."""
    page.set_viewport_size({"width": 900, "height": 900})
    messages = watch_socket(page)
    a = app(port=pty_device.path, flow=flow_file(tmp_path, flanker_seconds=40))
    begin(a, page)
    advance_until(page, "words", "waiting")
    folder = session_folder_of(a)
    words = play_words(page, folder)
    page.click("#advance")
    acted = play_flanker(page, stop_after=4, hang_up=pty_device.hang_up)
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]', timeout=30_000)
    page.wait_for_selector("#fs-lost", state="visible")
    assert "headset connection was lost" in page.inner_text("#fs-lost-why")
    paused = [e for e in events_of(folder) if e["name"] == "paused"]
    assert [e["payload"]["reason"] for e in paused] == ["link_lost"]
    all_stored(page)
    trials = [e["payload"] for e in events_of(folder) if e["name"] == "flanker.trial" and not e["payload"]["practice"]]
    assert trials[-1]["status"] == "abandoned"  # the trial in progress at the drop: not counted
    flanker = flanker_expected(folder, acted)
    shown = [m for m in messages if m.get("t") == "state"][-1]["scores"]["tasks"]
    assert {t: (v["earned"], v["possible"]) for t, v in shown.items()} == {"words": words, "flanker": flanker}
    ask(page, "stop-here", "fs-")
    answer_yes(page, "fs-")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"][data-phase="done"]', timeout=30_000)
    page.wait_for_selector("#summary-scores", state="visible", timeout=60_000)
    total = (words[0] + flanker[0], words[1] + flanker[1])
    meta = check_saved(folder, {"words": words, "flanker": flanker}, total)
    session = json.loads((folder / "session.json").read_text())
    assert session["ending"] == "stopped" and session["ended_early"] is False
    assert meta["tasks"]["flanker"]["trials"] == flanker[1]
    assert row(page, "words") == ("3 of 12", "")
    # The lost link ended the flanker block (since 2026-11-25; the reason was "").
    assert row(page, "flanker") == (f"{flanker[0]} of {flanker[1]}", "1 block interrupted by a pause")
    assert row(page, "recall") == ("—", "Stopped before it started")
    assert text(page, "#summary-points") == f"Total: {total[0]} of {total[1]} points"
    shot(page, "stopped-after-lost-link.png")


def test_ctrl_c_while_paused_by_a_lost_link_keeps_the_scores(app, tmp_path):
    """Without the page: trials sent over the participant's socket as the runner sends them, a
    hang-up (paused), then Ctrl-C to the app. The session ends early with its scores in
    ``session.json``, in ``behavior/scores.json`` and in the zip."""
    plug = Plug(tmp_path)
    try:
        a = app(port=str(plug.link), flow=one_task_flow(tmp_path, "flanker", 120))
        with open_ws(a, "participant") as ws:
            folder = to_running_block(a, ws)
            right = [True, False, True, True, False, True]
            for i, correct in enumerate(right):
                payload = {"block": "block1", "trial": i, "practice": False, "status": "done",
                           "scored": True, "correct": correct}  # fmt: skip
                send(ws, cmd="event", name="flanker.trial", payload=payload, browser_time=1000.0 + i, time_origin=1.7e12)
            until(lambda: len([e for e in events_of(folder) if e["name"] == "flanker.trial"]) == len(right), "the trials")
            plug.unplug()
            state = next_state(ws, timeout=20, predicate=lambda m: m["phase"] == "paused")
            assert state["reason"] == "link_lost"
            # The trial in progress at the drop, as the page sends it: abandoned, not scored.
            abandoned = {"block": "block1", "trial": len(right), "practice": False, "status": "abandoned", "scored": False}
            send(ws, cmd="event", name="flanker.trial", payload=abandoned, browser_time=2000.0, time_origin=1.7e12)
            until(lambda: len([e for e in events_of(folder) if e["name"] == "flanker.trial"]) == len(right) + 1, "abandoned")
            expected = {"earned": sum(right), "possible": len(right)}
            shown = next_state(ws, timeout=5)["scores"]["tasks"]["flanker"]
            assert {k: shown[k] for k in expected} == expected
        a.process.send_signal(signal.SIGINT)
        assert a.process.wait(timeout=60) == 0
        zipped = zip_contents(wait_packaged(folder, timeout=10))
        meta = json.loads((folder / "session.json").read_text())
        assert meta["ending"] == "ended_early" and meta["ended_early"] is True
        saved = meta["scores"]
        assert {k: saved["tasks"]["flanker"][k] for k in expected} == expected
        assert (saved["total"]["earned"], saved["total"]["possible"]) == (sum(right), len(right))
        assert json.loads((folder / "behavior" / "scores.json").read_text()) == saved
        assert json.loads(zipped["behavior/scores.json"]) == saved
        assert json.loads(zipped["session.json"])["scores"] == saved
        assert scores.score_folder(folder) == saved
    finally:
        plug.close()

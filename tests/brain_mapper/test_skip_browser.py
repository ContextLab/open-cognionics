"""Skip a round, stop early, play one round and continue an earlier session, in a real Chromium.

Each test starts ``brain-mapper`` on a pseudo-terminal device with the short flow (2 s blocks), drives
the page the way a person would (buttons, the label field) and reads the DOM, the server's own state
messages and the files the session wrote. Nothing waits on a fixed sleep: the page and the files say
when something has happened.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pty_device import PtyDevice, config_reply, onhead_stream
from test_engine import SHORT, events_of, markers_of
from test_participants import stop
from test_shell_browser import (
    RECORD_SCREENS,
    SCREEN,
    advance_until,
    begin,
    long_flow,
    press,
    screen_of,
    server_state,
)


@pytest.fixture
def device():
    made: list[PtyDevice] = []

    def new() -> PtyDevice:
        dev = PtyDevice(onhead_stream(), config_reply())
        made.append(dev)
        return dev

    yield new
    for dev in made:
        dev.hang_up()


def folder_of(a) -> Path:
    found = sorted(p for p in (a.data_dir / "sessions").iterdir() if p.is_dir())
    assert found, "no session folder"
    return found[-1]


def ledger_of(folder: Path) -> list[dict]:
    return [json.loads(x) for x in (folder / "points.jsonl").read_text().splitlines()]


#: What the HUD shows: the session's score as earned of possible (see "Scores" in the docs). In these
#: flows no trial is scored (a word list's trial is cut short by its short block, placeholder rounds
#: log none, and a continued session is scored on its own), so it is the dash; the ledger's total
#: is checked from the server's state and points.jsonl.
DASH = "\u2014"


def hud_total(page) -> str:
    return page.inner_text("#hud-points").strip()


def expect_total(page, value: str) -> None:
    """Waits for the HUD to show ``value`` (it follows the server's state)."""
    page.wait_for_function(
        "(v) => document.getElementById('hud-points').textContent.trim() === v", arg=value, timeout=5000
    )


def to_between_after_one_block(page) -> None:
    """From the participant screen: through setup, the first block of words, to the between screen."""
    advance_until(page, "words", "waiting")
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)


def ask(page, button: str, prefix: str = "") -> None:
    """Presses a leave button; nothing has been sent until the question is answered."""
    page.click(f"#{prefix}{button}")
    page.wait_for_selector(f"#{prefix}leave-ask", state="visible")


def answer_yes(page, prefix: str = "") -> None:
    page.click(f"#{prefix}leave-yes")


# -- skip -----------------------------------------------------------------------------------------


def test_skip_this_round_on_the_between_screen_moves_to_the_next_round_with_the_total_unchanged(
    app, device, page
):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    expect_total(page, DASH)
    ask(page, "skip-round")
    # a question first: the server still has the session where it was
    state = server_state(a)
    assert (state["step"], state["phase"], state["points_total"]) == ("words", "between", 100)
    assert "Skip Words?" in page.inner_text("#leave-text")
    page.click("#leave-no")
    page.wait_for_selector("#leave-ask", state="hidden")
    assert page.locator("#skip-round").is_visible()
    ask(page, "skip-round")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"][data-step="perception"][data-phase="waiting"]')
    assert hud_total(page) == DASH
    state = server_state(a)
    assert state["points_total"] == 100 and state["outcomes"]["words"] == "partial"
    folder = folder_of(a)
    assert len(ledger_of(folder)) == 1  # the one block that was finished; the skip added nothing
    skipped = [e for e in events_of(folder) if e["name"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["step"] == "words" and skipped[0]["payload"]["blocks_done"] == 1
    assert any(m["label"].startswith("words/skipped#") for m in markers_of(folder / "eeg"))


def test_skip_this_round_on_the_pause_screen_goes_on_unpaused_with_the_total_unchanged(
    app, device, page, tmp_path
):
    a = app(port=device().path, flow=long_flow(tmp_path, 20))
    begin(a, page)
    advance_until(page, "words", "waiting")
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    page.wait_for_selector("#fs-lost", state="visible")
    assert page.locator("#fs-skip-round").is_visible() and page.locator("#fs-stop-here").is_visible()
    ask(page, "skip-round", "fs-")
    answer_yes(page, "fs-")
    page.wait_for_selector(f'{SCREEN}[data-step="perception"][data-phase="waiting"]')
    state = server_state(a, predicate=lambda m: m["step"] == "perception")
    assert state["phase"] == "waiting" and state["reason"] is None and state["points_total"] == 0
    assert hud_total(page) == DASH
    folder = folder_of(a)
    assert ledger_of(folder) == []
    skipped = [e for e in events_of(folder) if e["name"] == "skipped"]
    # The pause had already ended list1 (since 2026-11-25; the skip used to end it:
    # 'skipped[0]["payload"]["block"] == "list1"'), so the skip ends no block.
    assert skipped[0]["payload"]["block"] is None
    ends = [e for e in events_of(folder) if e["name"] == "block_end" and e["step"] == "words"]
    assert [(e["block"], e["payload"]["ended_by"]) for e in ends] == [("list1", "interrupted")]
    assert ends[0]["payload"]["seconds"] > 0


def test_skip_is_offered_on_a_rounds_instruction_screen_and_on_calibration_but_not_on_participant_or_device(
    app, device, page
):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    assert page.locator("#skip-round").count() == 0 and page.locator("#stop-here").count() == 0
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert page.locator("#skip-round").is_hidden() and page.locator("#stop-here").is_visible()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="calibration"]')
    assert page.locator("#skip-round").is_visible() and page.locator("#stop-here").is_visible()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"][data-step="words"]')
    assert page.locator("#skip-round").is_visible()
    page.click("#advance")  # the block runs: nothing to press by accident
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    assert page.locator("#skip-round").is_hidden() and page.locator("#stop-here").is_hidden()


def test_skip_this_round_and_stop_here_are_plainly_secondary_to_continue(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    sizes = page.evaluate(
        """() => {
          const style = (id) => getComputedStyle(document.getElementById(id));
          const box = (id) => document.getElementById(id).getBoundingClientRect();
          return {
            go: [parseFloat(style('advance').fontSize), style('advance').backgroundColor, box('advance').bottom],
            skip: [parseFloat(style('skip-round').fontSize), style('skip-round').backgroundColor, box('skip-round').top],
            stop: [parseFloat(style('stop-here').fontSize), style('stop-here').backgroundColor, box('stop-here').top],
          };
        }"""
    )
    # Smaller than Continue, and never under 16 px (the readable minimum).
    assert 16 <= sizes["skip"][0] < sizes["go"][0] and 16 <= sizes["stop"][0] < sizes["go"][0]
    assert sizes["skip"][1] == sizes["stop"][1] == "rgba(0, 0, 0, 0)"  # no filled button
    assert sizes["go"][1] != "rgba(0, 0, 0, 0)"
    assert sizes["skip"][2] - sizes["go"][2] >= 24 and sizes["stop"][2] - sizes["go"][2] >= 24  # set apart


# -- stop -----------------------------------------------------------------------------------------


def test_stop_here_shows_the_summary_saying_the_session_was_stopped_not_the_failure_screen(app, device, page):
    a = app(port=device().path, flow=SHORT)
    page.add_init_script(RECORD_SCREENS)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "stop-here")
    assert "ends and the recording is saved" in page.inner_text("#leave-text")
    state = server_state(a)
    assert state["stopped"] is False and state["phase"] == "between"  # not yet
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert "stopped by the participant" in page.inner_text("#summary-text")
    assert page.locator("#ended-reason").count() == 0
    seen = [p[0] for p in page.evaluate("window.__screens")]
    assert "ended" not in seen
    items = page.locator("#stopped-rounds li")
    assert items.count() == 9  # the nine rounds of the route
    assert page.locator('#stopped-rounds li[data-step="words"]').get_attribute("data-outcome") == "partial"
    assert "Words" in items.first.inner_text() and "Partly played" in items.first.inner_text()
    assert "continue from Perception" in page.inner_text("#stopped-next")
    assert page.locator("#fs-lost").is_hidden()
    assert hud_total(page) == DASH  # no animation on the summary screen
    final = server_state(a, predicate=lambda m: m["stopped"])
    assert final["failure"] is None and final["ended_early"] is False and final["phase"] == "done"
    folder = folder_of(a)
    saved = json.loads((folder / "progress.json").read_text())
    assert saved["status"] == "stopped" and saved["points_total"] == 100
    assert [json.loads(x)["total"] for x in (folder / "points.jsonl").read_text().splitlines()] == [100]
    assert [e["name"] for e in events_of(folder)].count("stopped") == 1


def test_stop_here_on_the_pause_screen_ends_the_session_and_a_reload_still_shows_the_stopped_summary(
    app, device, page, tmp_path
):
    a = app(port=device().path, flow=long_flow(tmp_path, 20))
    begin(a, page)
    advance_until(page, "words", "waiting")
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-phase="running"]')
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    ask(page, "stop-here", "fs-")
    answer_yes(page, "fs-")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert page.locator("#fs-lost").is_hidden()  # the pause is over with the session
    page.reload()
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert "stopped by the participant" in page.inner_text("#summary-text")
    assert hud_total(page) == DASH
    assert ledger_of(folder_of(a)) == []  # the block that was running earned nothing


# -- play one round ----------------------------------------------------------------------------------


def test_play_this_round_only_walks_setup_that_round_and_the_summary(app, device, page):
    a = app(port=device().path, flow=SHORT)
    page.add_init_script(RECORD_SCREENS)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    assert page.locator("#pick-panel").is_hidden() and page.locator("#play-only").is_hidden()
    page.click('.route-pick[data-step="spatial"]')
    page.wait_for_selector("#play-only", state="visible")
    assert page.get_attribute('.route-pick[data-step="spatial"]', "aria-pressed") == "true"
    page.click("#play-only")
    assert "Spatial memory" in page.inner_text("#start-title")
    assert "Only Spatial memory will be played" in page.inner_text("#pick-text")
    assert page.locator("#play-all").is_visible()
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    assert "Only Spatial memory" in page.inner_text("#participant-selection")
    while True:
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
        last = screen_of(page)[0] == "summary"
        if screen_of(page)[0] == "placeholder":
            assert page.inner_text("#hud-label").startswith("Round 1 of 1")
        press(page)
        if last:
            break
    page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")
    seen = [tuple(p) for p in page.evaluate("window.__screens") if p[0] != "connecting"]
    assert seen == [
        ("start", "participant"),
        ("participant", "participant"),
        ("device", "device"),
        ("calibration", "calibration"),
        ("placeholder", "spatial"),
        ("between", "spatial"),
        ("placeholder", "spatial"),
        ("between", "spatial"),  # 2 blocks since 2026-11-25 (were 3)
        ("summary", "summary"),
    ]
    state = server_state(a)
    assert state["plan"] == ["participant", "device", "calibration", "spatial", "summary"]
    assert state["points_total"] == 200  # was 300 with 3 blocks
    saved = json.loads((folder_of(a) / "progress.json").read_text())
    assert saved["status"] == "finished" and saved["plan"] == state["plan"]


def test_a_round_that_was_chosen_can_be_unchosen_and_the_whole_route_is_played_again(app, device, page):
    a = app(port=device().path, flow=SHORT)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="nback"]')
    page.click("#play-only")
    page.click("#play-all")
    assert "9 rounds" in page.inner_text("#start-title")
    page.click('.route-pick[data-step="nback"]')
    page.click('.route-pick[data-step="nback"]')  # pressed again: no longer chosen
    assert page.locator("#pick-panel").is_hidden()
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert len(server_state(a)["plan"]) == 13


def choose_round_then_participant_screen(a, page, step: str) -> None:
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click(f'.route-pick[data-step="{step}"]')
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')


def no_session_folder(a) -> bool:
    return not (a.data_dir / "sessions").exists() or not any((a.data_dir / "sessions").iterdir())


# Changed from "types P001, presses Continue, and the refusal 'finished the words' appears":
# the reason is now shown as soon as the label is typed and Continue is off, so nothing is
# sent. The refusal itself is still tested, below, for the case that the page could not ask.
def test_recall_alone_for_a_participant_without_word_lists_is_explained_as_soon_as_the_label_is_typed(
    app, device, page
):
    a = app(port=device().path, flow=SHORT)
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click('.route-pick[data-step="recall"]')
    assert "earlier session of the same participant" in page.inner_text("#pick-note")
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    assert page.locator("#advance").is_enabled()  # no label yet, so nothing is known
    type_label(page, "P001")
    page.wait_for_function(
        "document.getElementById('participant-progress-note').textContent.includes('needs the word lists')"
    )
    assert "Play the whole route instead" in page.inner_text("#participant-progress-note")
    assert page.locator("#advance").is_disabled()
    assert page.locator("#whole-route").is_visible()
    page.press("#participant-label", "Enter")  # a disabled Continue cannot be pressed from the keyboard either
    page.wait_for_timeout(300)
    assert page.locator(f'{SCREEN}[data-screen="participant"]').count() == 1
    assert no_session_folder(a)
    # the way back: the whole route, and Continue works again
    page.click("#whole-route")
    assert page.locator("#participant-chosen").is_hidden()
    assert page.locator("#advance").is_enabled()
    assert page.inner_text("#participant-progress-note") == ""
    assert page.evaluate("document.activeElement.id") == "participant-label"
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert len(server_state(a)["plan"]) == 13


def test_a_refusal_of_recall_alone_says_what_to_do_and_puts_the_focus_on_the_way_back(app, device, page):
    a = app(port=device().path, flow=SHORT)
    # The page cannot ask whether the lists were studied, so only the server's refusal is left.
    page.route("**/api/progress", lambda route: route.abort())
    choose_round_then_participant_screen(a, page, "recall")
    page.fill("#participant-label", "P001")
    page.wait_for_timeout(500)  # the lookup was tried and failed: no reason is shown
    assert page.inner_text("#participant-progress-note") == "" and page.locator("#advance").is_enabled()
    page.click("#advance")
    page.wait_for_function("document.getElementById('participant-problem').textContent.includes('studied')")
    problem = page.inner_text("#participant-problem")
    assert "Play the whole route instead" in problem
    assert page.evaluate("document.activeElement.id") == "whole-route"
    assert page.locator(f'{SCREEN}[data-screen="participant"]').count() == 1
    assert no_session_folder(a)
    page.click("#whole-route")
    assert page.locator("#participant-chosen").is_hidden()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert len(server_state(a)["plan"]) == 13


# -- continue ----------------------------------------------------------------------------------------


def stopped_after_one_block(app, device, browser, flow=SHORT):
    """P001 plays the first block of words (100 points) and stops; returns the data folder."""
    first = app(port=device().path, flow=flow)
    context = browser.new_context()
    try:
        page = context.new_page()
        begin(first, page)
        to_between_after_one_block(page)
        ask(page, "stop-here")
        answer_yes(page)
        page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    finally:
        context.close()
    earlier = folder_of(first)
    data_dir = first.data_dir
    stop(first)
    return data_dir, earlier


def start_screen_to_participant(a, page) -> None:
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')


def type_label(page, label: str) -> None:
    """Types a label and waits for the page's answer from /api/progress."""
    with page.expect_response(lambda r: r.url.endswith("/api/progress")):
        page.fill("#participant-label", label)


def test_after_a_stop_the_same_participant_is_offered_to_continue_and_the_total_is_carried(
    app, device, page, browser
):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    requested: list[str] = []
    page.on("request", lambda r: requested.append(r.url))
    start_screen_to_participant(second, page)
    assert page.locator("#continue-offer").is_hidden() and page.locator("#start-over").is_hidden()
    # another participant is not offered it
    type_label(page, "P002")
    assert page.locator("#continue-offer").is_hidden() and page.locator("#start-over").is_hidden()
    assert page.inner_text("#advance") == "Continue"
    # the same participant is
    type_label(page, "P001")
    page.wait_for_selector("#continue-offer", state="visible")
    assert page.inner_text("#advance") == "Continue from Perception"
    assert page.locator("#start-over").is_visible()
    # The old ledger total is no longer shown on the offer (it is not the earned-of-possible score).
    assert "points" not in page.inner_text("#continue-offer")
    assert "stopped before Perception" in page.inner_text("#continue-offer")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert hud_total(page) == DASH  # scores are per session: the carried ledger total is not shown
    new = next(p for p in (data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    assert json.loads((new / "session.json").read_text())["continues"] == earlier.name
    saved = json.loads((new / "progress.json").read_text())
    assert saved["continues"] == earlier.name and saved["points_total"] == 100
    assert saved["plan"][3] == "perception" and "words" not in saved["plan"] and "summary" == saved["plan"][-1]
    first_line = ledger_of(new)[0]
    assert first_line["reason"] == "carried" and first_line["total"] == 100 and first_line["amount"] == 0
    state = server_state(second)
    assert state["points_total"] == 100 and state["step"] == "device"
    # no label in any address the page asked for, and nothing outside this computer
    assert not any("P001" in url or "P002" in url for url in requested)
    assert all(url.startswith(second.url) or url.startswith("ws://127.0.0.1") for url in requested)
    # the first block played in the continued session adds to the carried total
    advance_until(page, "perception", "waiting")
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    expect_total(page, DASH)
    assert [x["total"] for x in ledger_of(new)] == [100, 200]


def test_start_from_the_beginning_makes_a_session_that_continues_nothing(app, device, page, browser):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    start_screen_to_participant(second, page)
    type_label(page, "P001")
    page.wait_for_selector("#start-over", state="visible")
    page.click("#start-over")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert hud_total(page) == DASH
    new = next(p for p in (data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    assert "continues" not in json.loads((new / "session.json").read_text())
    assert json.loads((new / "progress.json").read_text())["continues"] is None
    assert ledger_of(new) == []
    assert len(server_state(second)["plan"]) == 13
    assert json.loads((earlier / "progress.json").read_text())["status"] == "stopped"


def test_a_label_typed_and_sent_at_once_still_gets_the_offer_instead_of_a_silent_new_start(
    app, device, page, browser
):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    start_screen_to_participant(second, page)
    page.fill("#participant-label", "P001")
    page.press("#participant-label", "Enter")  # before the page has had time to ask
    page.wait_for_selector("#continue-offer", state="visible")
    assert page.locator(f'{SCREEN}[data-screen="participant"]').count() == 1
    assert not any(p.name != earlier.name for p in (data_dir / "sessions").iterdir() if p.is_dir())  # nothing was started


def test_a_damaged_record_is_explained_on_the_participant_screen_and_a_new_start_still_works(
    app, device, page, browser
):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    (earlier / "progress.json").write_text("{this is not json")
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    start_screen_to_participant(second, page)
    type_label(page, "P001")
    page.wait_for_function("document.getElementById('participant-progress-note').textContent.includes('no earlier session')")
    assert "progress.json is not valid JSON" in page.inner_text("#participant-progress-note")
    assert page.locator("#continue-offer").is_hidden() and page.inner_text("#advance") == "Continue"
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert (earlier / "progress.json").read_text() == "{this is not json"


def test_the_summary_after_a_stop_lists_skipped_rounds_as_skipped(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    ask(page, "skip-round")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-step="perception"][data-phase="waiting"]')
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    assert page.locator('#stopped-rounds li[data-step="words"]').get_attribute("data-outcome") == "skipped"
    assert "Skipped" in page.locator('#stopped-rounds li[data-step="words"]').inner_text()
    assert page.locator('#stopped-rounds li[data-step="perception"]').get_attribute("data-outcome") == "not_reached"
    assert "continue from Perception" in page.inner_text("#stopped-next")
    saved = json.loads((folder_of(a) / "progress.json").read_text())
    assert {s["id"]: s["outcome"] for s in saved["steps"]}["words"] == "skipped"


def test_one_round_chosen_with_an_earlier_session_plays_that_round_as_a_new_session_and_offers_no_continue(
    app, device, page, browser
):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    requested_bodies: list[str] = []
    page.on("request", lambda r: requested_bodies.append(r.post_data or "") if r.method == "POST" else None)
    choose_round_then_participant_screen(second, page, "nback")
    type_label(page, "P001")  # P001 has an earlier session that could be continued
    assert page.locator("#continue-offer").is_hidden() and page.locator("#start-over").is_hidden()
    assert page.inner_text("#advance") == "Continue"
    line = page.inner_text("#participant-selection")
    assert "Only 2-back will be played" in line and "as a new session" in line
    assert page.locator("#whole-route").is_visible()
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    new = next(p for p in (data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    assert "continues" not in json.loads((new / "session.json").read_text())
    assert json.loads((new / "progress.json").read_text())["continues"] is None
    assert server_state(second)["plan"] == ["participant", "device", "calibration", "nback", "summary"]
    sent = [json.loads(b) for b in requested_bodies if '"label"' in b]
    assert sent and all("continues" not in b and b.get("only") == "nback" for b in sent)


def test_the_way_back_from_one_round_brings_the_continue_offer_back(app, device, page, browser):
    data_dir, earlier = stopped_after_one_block(app, device, browser)
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    choose_round_then_participant_screen(second, page, "nback")
    type_label(page, "P001")
    assert page.locator("#continue-offer").is_hidden()
    page.click("#whole-route")
    assert page.locator("#participant-chosen").is_hidden()
    page.wait_for_selector("#continue-offer", state="visible")
    assert page.inner_text("#advance") == "Continue from Perception" and page.locator("#start-over").is_visible()
    assert page.evaluate("document.activeElement.id") == "participant-label"
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    new = next(p for p in (data_dir / "sessions").iterdir() if p.is_dir() and p.name != earlier.name)
    assert json.loads((new / "session.json").read_text())["continues"] == earlier.name


def test_recall_alone_is_allowed_for_a_participant_whose_words_round_was_partly_played(
    app, device, page, browser
):
    data_dir, earlier = stopped_after_one_block(app, device, browser)  # one words block was finished
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    choose_round_then_participant_screen(second, page, "recall")
    type_label(page, "P001")
    assert page.inner_text("#participant-progress-note") == "" and page.locator("#advance").is_enabled()
    type_label(page, "P002")  # another participant has studied nothing
    page.wait_for_function(
        "document.getElementById('participant-progress-note').textContent.includes('needs the word lists')"
    )
    assert page.locator("#advance").is_disabled()
    type_label(page, "P001")
    page.wait_for_function("document.getElementById('participant-progress-note').textContent === ''")
    page.click("#advance")
    page.wait_for_selector(f'{SCREEN}[data-screen="device"]')
    assert server_state(second)["plan"] == ["participant", "device", "calibration", "recall", "summary"]


def progress_numbers(page) -> tuple[int, int]:
    """(done, total) from the words under the progress bar, for example "1 of 13 blocks done"."""
    page.wait_for_function("document.getElementById('hud-progress-text').textContent.length > 0")
    words = page.inner_text("#hud-progress-text").split()
    assert words[1] == "of" and words[3:] == ["blocks", "done"], words
    return int(words[0]), int(words[2])


def test_the_progress_bar_counts_a_finished_block_of_a_left_round_in_both_numbers(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    advance_until(page, "words", "waiting")
    assert page.inner_text("#hud-progress-text").startswith("0 of ")
    _, whole = progress_numbers(page)
    press(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="between"]', timeout=15_000)
    expect_total(page, DASH)
    assert progress_numbers(page) == (1, whole)
    ask(page, "skip-round")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-step="perception"][data-phase="waiting"]')
    # words keeps its one finished block; its second block is not going to be played
    assert hud_total(page) == DASH and progress_numbers(page) == (1, whole - 1)
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    done, total = progress_numbers(page)
    assert (done, total) == (1, whole - 1) and hud_total(page) == DASH
    assert page.get_attribute("#hud-progress", "aria-valuenow") == str(round(100 * done / total))


# -- the leave controls: focus and what a screen reader hears --------------------------------------


def active_id(page) -> str:
    return page.evaluate("(document.activeElement && document.activeElement.id) || document.activeElement.tagName")


def test_the_question_is_a_named_group_whose_words_are_a_status(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "skip-round")
    group = page.get_by_role("group", name="Skip Words? The rest of this round is not played; trials already played keep their points.")
    assert group.count() == 1 and group.is_visible()
    assert page.get_attribute("#leave-text", "role") == "status"
    assert active_id(page) == "leave-no"  # the safe answer has the focus
    page.click("#leave-no")
    assert active_id(page) == "skip-round"  # and back on the button that asked


def test_after_yes_to_skip_the_focus_is_on_a_button_never_on_the_page_body(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "skip-round")
    # read in the same turn as the click, before any answer from the server could change the page
    now = page.evaluate(
        """(() => {
            document.getElementById('leave-yes').click();
            return [document.getElementById('leave-sent').textContent,
                    document.getElementById('skip-round').getAttribute('aria-disabled'),
                    document.activeElement.id];
        })()"""
    )
    assert now == ["Skipping this round.", "true", "skip-round"]  # back on the button that was pressed
    page.wait_for_selector(f'{SCREEN}[data-screen="placeholder"][data-step="perception"][data-phase="waiting"]')
    page.wait_for_function("document.activeElement && document.activeElement !== document.body")
    assert page.evaluate("document.activeElement.tagName") == "BUTTON"


def test_after_yes_to_stop_the_focus_is_on_the_summary_heading(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    page.wait_for_function("document.activeElement && document.activeElement.id === 'summary-title'")


def test_an_unanswered_question_closes_after_ten_seconds_and_the_focus_goes_back_to_its_button(app, device, page):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    to_between_after_one_block(page)
    ask(page, "stop-here")
    assert active_id(page) == "leave-no"
    page.wait_for_selector("#leave-ask", state="hidden", timeout=15_000)
    assert active_id(page) == "stop-here"


def test_the_stopped_summary_promises_no_continuation_when_nothing_was_played(app, device, page, browser):
    a = app(port=device().path, flow=SHORT)
    begin(a, page)
    advance_until(page, "calibration", "waiting")
    ask(page, "stop-here")
    answer_yes(page)
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    text = page.inner_text("#stopped-next")
    assert "nothing to continue from" in text and "continue from" not in text.replace("nothing to continue from", "")
    assert page.locator('#stopped-rounds li[data-outcome="not_reached"]').count() == 9
    data_dir = a.data_dir
    stop(a)
    # and the page told the truth: the same participant is not offered that session
    second = app(port=device().path, flow=SHORT, data_dir=data_dir)
    start_screen_to_participant(second, page)
    type_label(page, "P001")
    assert page.locator("#continue-offer").is_hidden() and page.inner_text("#advance") == "Continue"


def test_the_progress_bar_on_the_participant_screen_counts_only_the_round_that_was_chosen(app, device, page):
    a = app(port=device().path, flow=SHORT)
    choose_round_then_participant_screen(a, page, "nback")
    page.wait_for_function("document.getElementById('hud-progress-text').textContent.length > 0")
    assert progress_numbers(page) == (0, 2)  # the two blocks of 2-back (were three), not the whole route
    page.click("#whole-route")
    page.wait_for_function("!document.getElementById('hud-progress-text').textContent.startsWith('0 of 2 ')", timeout=5000)
    assert progress_numbers(page)[1] > 2

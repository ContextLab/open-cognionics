"""Several rounds chosen on the start screen: the flow, the server, the files and the page.

Everything is real: ``brain-mapper`` as a subprocess on a pseudo-terminal headset, the files it
writes in a temporary folder, and a real Chromium driven the way a person would (a mouse with and
without Shift, the keyboard, a touch screen). The labels are made up (``P001``, ``P002``).
"""

from __future__ import annotations

import json

import pytest

from brain_mapper import flow, progress
from test_engine import SHORT, events_of, next_state, open_ws
from test_package import wait_packaged, zip_contents
from test_participants import request, session_folder, stop
from test_progress import device, drive_to, newest_folder, play_one_round_and_stop, post  # noqa: F401
from test_shell_browser import RECORD_SCREENS, SCREEN, press, screen_of, server_state
from test_skip_browser import no_session_folder, type_label

ROUNDS = [s.id for s in flow.load(SHORT) if s.kind not in flow.NO_POINTS]


def ids(steps):
    return [s.id for s in steps]


# -- flow.select ------------------------------------------------------------------------------------


def test_select_a_list_keeps_the_setup_those_rounds_in_route_order_and_the_summary():
    chosen = flow.select(flow.load(SHORT), only=["video", "perception"])
    assert ids(chosen) == ["participant", "device", "calibration", "perception", "video", "summary"]


def test_select_a_list_of_one_is_the_same_as_that_one_id():
    steps = flow.load(SHORT)
    assert ids(flow.select(steps, only=["nback"])) == ids(flow.select(steps, only="nback"))


def test_chosen_gives_the_rounds_in_route_order_or_none():
    steps = flow.load(SHORT)
    assert flow.chosen(steps, None) is None
    assert flow.chosen(steps, "nback") == ["nback"]
    assert flow.chosen(steps, ("recall", "words", "video")) == ["words", "video", "recall"]


@pytest.mark.parametrize(
    "bad, words",
    [
        ([], "at least one round"),
        (["words", "words"], "more than once"),
        (["words", "nope"], "words, perception, nback"),
        (["words", "summary"], "words, perception, nback"),
        (["words", 5], "words, perception, nback"),
        ({"words": 1}, "words, perception, nback"),
    ],
)
def test_select_refuses_a_list_that_is_empty_repeats_a_round_or_names_no_round(bad, words):
    with pytest.raises(ValueError, match=words):
        flow.select(flow.load(SHORT), only=bad)


def test_select_a_list_together_with_start_at_raises():
    with pytest.raises(ValueError, match="not both"):
        flow.select(flow.load(SHORT), only=["words", "video"], start_at="nback")


# -- progress.json of several rounds, and of one (older sessions) ------------------------------------


def record(only) -> dict:
    return {
        "session": "P001_20261125_070000",
        "uuid": "u",
        "continues": None,
        "only": only,
        "seed": 4,
        "session_id": "0123456789abcdef",
        "status": "stopped",
        "plan": ["participant", "words", "video", "summary"],
        "steps": [
            {"id": "participant", "kind": "setup", "title": "Participant", "outcome": "finished"},
            {"id": "words", "kind": "task", "title": "Words", "outcome": "finished"},
            {"id": "video", "kind": "video", "title": "Video", "outcome": "not_reached"},
            {"id": "summary", "kind": "summary", "title": "Summary", "outcome": "not_reached"},
        ],
        "points_total": 200,
    }


@pytest.mark.parametrize("only", ["words", ["words", "video"], None])
def test_progress_reads_one_round_several_rounds_or_none(tmp_path, only):
    progress.write(tmp_path, record(only))
    assert progress.read(tmp_path).only == only


@pytest.mark.parametrize("only", [[], ["words", 3], 7, {"words": 1}])
def test_progress_refuses_an_only_that_is_neither_a_round_id_nor_a_list_of_them(tmp_path, only):
    progress.write(tmp_path, record(only))
    with pytest.raises(progress.ProgressDamaged, match="only"):
        progress.read(tmp_path)


# -- the server ------------------------------------------------------------------------------------


def test_a_label_with_several_rounds_plays_them_in_route_order_and_records_the_choice(app, device):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    status, body = post(a, {"label": "P001", "only": ["video", "perception"]})
    assert status == 200, body
    with open_ws(a) as ws:
        state = next_state(ws)
    assert state["plan"] == ["participant", "device", "calibration", "perception", "video", "summary"]
    assert state["only"] == ["perception", "video"]
    folder = session_folder(a)
    assert json.loads((folder / "session.json").read_text())["only"] == ["perception", "video"]
    saved = json.loads((folder / "progress.json").read_text())
    assert saved["only"] == ["perception", "video"] and saved["plan"] == state["plan"]
    assert progress.read(folder).only == ["perception", "video"]


def test_a_list_of_one_round_is_recorded_as_that_round_as_before(app, device):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "only": ["nback"]})[0] == 200
    folder = session_folder(a)
    assert json.loads((folder / "session.json").read_text())["only"] == "nback"
    assert json.loads((folder / "progress.json").read_text())["only"] == "nback"
    # the same choice sent again, in the other form, is the same choice
    assert post(a, {"label": "P001", "only": "nback"})[0] == 200


def test_the_same_rounds_sent_again_in_another_order_are_the_same_choice(app, device):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P001", "only": ["video", "words"]})[0] == 200
    assert post(a, {"label": "P001", "only": ["words", "video"]})[0] == 200
    status, body = post(a, {"label": "P001", "only": ["words"]})
    assert status == 409 and body["code"] == "already_set"
    assert len(list((a.data_dir / "sessions").iterdir())) == 1


@pytest.mark.parametrize(
    "only, code, words",
    [
        ([], "bad_selection", "Choose at least one round"),
        (["words", "words"], "bad_selection", "more than once"),
        (["words", "nope"], "unknown_step", "'nope'"),
        (["words", "device"], "unknown_step", "'device'"),
        (["words", 5], "bad_body", '"only"'),
        ({"words": True}, "bad_body", '"only"'),
        (["nback", "recall"], "recall_needs_words", "studied the words lists"),
    ],
)
def test_a_list_that_cannot_be_played_is_refused_in_words_and_nothing_is_created(app, device, only, code, words):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    status, body = post(a, {"label": "P001", "only": only})
    assert status in (400, 409) and body["code"] == code, body
    assert words in body["error"]
    assert not (a.data_dir / "sessions").exists() or not any((a.data_dir / "sessions").iterdir())
    assert not (a.data_dir / "participants.json").exists()
    assert post(a, {"label": "P001"})[0] == 200


def test_recall_with_other_rounds_but_without_words_uses_the_lists_studied_before(app, device):  # noqa: F811
    first = app(port=device().path, flow=SHORT)
    studied = play_one_round_and_stop(first, "words", blocks=2)
    seed = json.loads((studied / "progress.json").read_text())["seed"]
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    assert post(second, {"label": "P001", "only": ["nback", "recall"]})[0] == 200
    folder = newest_folder(second)
    saved = json.loads((folder / "progress.json").read_text())
    assert saved["only"] == ["nback", "recall"] and saved["seed"] == seed
    stop(second)
    # another participant has studied no lists: refused, with the reason
    third = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, body = post(third, {"label": "P002", "only": ["nback", "recall"]})
    assert status == 409 and body["code"] == "recall_needs_words"
    assert "Delayed free recall" in body["error"] and "choose the words round" in body["error"]


def test_words_and_recall_chosen_together_need_nothing_from_before(app, device):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    assert post(a, {"label": "P002", "only": ["recall", "words"]})[0] == 200
    with open_ws(a) as ws:
        assert next_state(ws)["plan"][-3:] == ["words", "recall", "summary"]


def test_a_session_of_several_rounds_stands_apart_and_is_never_offered_to_continue(app, device):  # noqa: F811
    first = app(port=device().path, flow=SHORT)
    assert post(first, {"label": "P001", "only": ["words", "video"]})[0] == 200
    with open_ws(first) as ws:
        drive_to(ws, "words")
        ws.send(json.dumps({"cmd": "advance"}))
        next_state(ws, predicate=lambda m: m["phase"] == "between")
        ws.send(json.dumps({"cmd": "stop"}))
        next_state(ws, predicate=lambda m: m["stopped"])
    stop(first)
    second = app(port=device().path, flow=SHORT, data_dir=first.data_dir)
    status, offer = request(second, "POST", "/api/progress", {"participant": "P001"})
    assert status == 200 and offer["session"] is None and offer["next_step"] is None
    assert offer["words_done"] is True  # a partly played words round counts as studied


# -- the start screen --------------------------------------------------------------------------------


def pressed(page) -> list[str]:
    return page.eval_on_selector_all(
        '.route-pick[aria-pressed="true"]', "(all) => all.map((b) => b.dataset.step)"
    )


def pick(page, step: str, **options) -> None:
    page.click(f'.route-pick[data-step="{step}"]', **options)


def start_screen(a, page) -> None:
    page.goto(a.url)
    page.wait_for_selector(f'{SCREEN}[data-screen="start"]')


def test_the_start_screen_says_in_one_line_how_to_choose_several(app, device, page):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    how = page.inner_text("#pick-how")
    # Before 2026-11-26 the line explained Shift + click and Space; it now shows visible check
    # boxes under Choose several instead.
    assert how == "Click a round to play it alone, or press Choose several to check more than one."
    assert page.locator("#pick-several").is_visible()
    assert page.get_attribute("#pick-several", "aria-pressed") == "false"
    group = page.locator('[role="group"][aria-describedby="pick-how"]')
    assert group.count() == 1 and group.locator(".route-pick").count() == len(ROUNDS)


def test_setup_is_listed_apart_and_choose_several_shows_a_check_box_on_each_round(app, device, page):  # noqa: F811
    """Since 2026-11-26 the rounds are numbered 1 to n (Setup is its own row), and
    Choose several draws a visible check box on every round, which the keyboard also checks."""
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    numbers = page.eval_on_selector_all(".route-list .num", "ns => ns.map((n) => n.textContent)")
    assert numbers == [str(i) for i in range(1, len(ROUNDS) + 1)]
    assert page.inner_text("#route-setup").split("\n")[0] == "First: setup"
    boxes = """() => [...document.querySelectorAll('.route-pick')].map((b) => {
        const s = getComputedStyle(b, '::before');
        return s.content !== 'none' && parseFloat(s.width) > 0;
    })"""
    assert not any(page.evaluate(boxes))  # no check boxes until Choose several is on
    page.click("#pick-several")
    assert page.inner_text("#pick-how") == "Check each round to play. Click a checked round again to uncheck it."
    assert all(page.evaluate(boxes)) and len(page.evaluate(boxes)) == len(ROUNDS)
    page.focus(f'.route-pick[data-step="{ROUNDS[0]}"]')
    page.keyboard.press("Space")
    page.focus(f'.route-pick[data-step="{ROUNDS[2]}"]')
    page.keyboard.press("Enter")  # with Choose several on, Enter checks too
    assert pressed(page) == [ROUNDS[0], ROUNDS[2]]


def test_a_plain_click_chooses_one_round_and_shift_click_adds_removes_and_takes_the_range(app, device, page):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    pick(page, "words")
    assert pressed(page) == ["words"]
    pick(page, "nback")  # a plain click: that round alone
    assert pressed(page) == ["nback"]
    pick(page, "flanker", modifiers=["Shift"])  # next to the last clicked: added
    assert pressed(page) == ["nback", "flanker"]
    text = page.inner_text("#pick-text")
    assert "2-back" in text and "Flanker" in text and "2 rounds" in text
    assert "4 blocks, 8 seconds" in text  # 2 + 2 blocks of 2 s (were 3 + 3: "6 blocks, 12 seconds")
    pick(page, "flanker", modifiers=["Shift"])  # again: removed
    assert pressed(page) == ["nback"]
    pick(page, "words", modifiers=["Shift"])  # across a gap (perception): the range from flanker
    assert pressed(page) == ["words", "perception", "nback", "flanker"]
    pick(page, "perception", modifiers=["Shift"])  # next to the last clicked: removed
    assert pressed(page) == ["words", "nback", "flanker"]
    assert page.inner_text("#play-only") == "Play these 3 rounds only"
    pick(page, "video")  # a plain click again: only that one
    assert pressed(page) == ["video"]
    assert page.inner_text("#play-only") == "Play this round only"
    pick(page, "video")  # the only one, clicked again: none
    assert pressed(page) == [] and page.locator("#pick-panel").is_hidden()


def test_the_keyboard_space_adds_or_removes_the_round_in_focus_and_enter_chooses_it_alone(app, device, page):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    page.focus('.route-pick[data-step="words"]')
    page.keyboard.press("Space")
    assert pressed(page) == ["words"]
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.dataset.step") == "perception"
    page.keyboard.press("Space")
    assert pressed(page) == ["words", "perception"]
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.keyboard.press("Space")  # flanker
    assert pressed(page) == ["words", "perception", "flanker"]
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Space")  # perception again: removed
    assert pressed(page) == ["words", "flanker"]
    page.keyboard.press("Enter")  # Enter is a plain click: that round alone
    assert pressed(page) == ["perception"]
    # the round in focus has a ring of its own (not the one of a chosen round)
    page.keyboard.press("Tab")
    focused, plain = page.evaluate(
        "[getComputedStyle(document.activeElement, '::after').outlineStyle,"
        " getComputedStyle(document.querySelector('.route-pick[data-step=\"video\"]'), '::after').outlineStyle]"
    )
    assert focused == "solid" and plain == "none"


def test_choose_several_lets_a_touch_screen_add_and_remove_rounds_by_tapping(app, device, browser):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    context = browser.new_context(has_touch=True, viewport={"width": 900, "height": 900})
    try:
        page = context.new_page()
        start_screen(a, page)
        page.tap('.route-pick[data-step="words"]')
        page.tap('.route-pick[data-step="video"]')  # without Choose several: that round alone
        assert pressed(page) == ["video"]
        page.tap("#pick-several")
        assert page.get_attribute("#pick-several", "aria-pressed") == "true"
        assert page.get_attribute(".route", "data-several") == "true"
        page.tap('.route-pick[data-step="words"]')
        page.tap('.route-pick[data-step="spatial"]')
        assert pressed(page) == ["words", "spatial", "video"]
        page.tap('.route-pick[data-step="spatial"]')
        assert pressed(page) == ["words", "video"]
        page.tap("#play-only")
        assert "Words" in page.inner_text("#pick-text") and "Video" in page.inner_text("#pick-text")
        assert "2 chosen rounds" in page.inner_text("#start-title")
        page.tap("#pick-several")  # off again: a tap chooses one round alone
        page.tap('.route-pick[data-step="nback"]')
        assert pressed(page) == ["nback"]
    finally:
        context.close()


def test_recall_without_words_in_a_selection_is_explained_on_both_screens(app, device, page):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    pick(page, "nback")
    pick(page, "recall", modifiers=["ControlOrMeta"])
    assert pressed(page) == ["nback", "recall"]
    assert "word lists from an earlier session" in page.inner_text("#pick-note")
    pick(page, "words", modifiers=["ControlOrMeta"])  # with the words round the note goes
    assert page.locator("#pick-note").is_hidden()
    pick(page, "words", modifiers=["ControlOrMeta"])
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    line = page.inner_text("#participant-selection")
    assert "Only 2-back and Delayed free recall will be played" in line and "as a new session" in line
    type_label(page, "P001")
    page.wait_for_function(
        "document.getElementById('participant-progress-note').textContent.includes('needs the word lists')"
    )
    assert page.locator("#advance").is_disabled() and page.locator("#whole-route").is_visible()
    assert no_session_folder(a)


def test_two_rounds_apart_play_exactly_those_in_order_from_start_to_zip(app, device, page, browser):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    look_context = browser.new_context()
    look = look_context.new_page()
    seen_by_onlooker: list[str] = []
    try:
        look.goto(a.url + "/onlooker")
        page.add_init_script(RECORD_SCREENS)
        start_screen(a, page)
        pick(page, "video")
        pick(page, "perception", modifiers=["Shift"])  # across a gap: the range; then trimmed
        pick(page, "nback", modifiers=["ControlOrMeta"])
        pick(page, "flanker", modifiers=["ControlOrMeta"])
        pick(page, "spatial", modifiers=["ControlOrMeta"])
        assert pressed(page) == ["perception", "video"]
        page.click("#play-only")
        assert page.inner_text("#start-title") == "Play 2 chosen rounds with the headset on."
        assert "3 timed blocks, 6 seconds" in page.inner_text("#start-lede")
        assert "Only Perception and Video will be played" in page.inner_text("#pick-text")
        page.click("#fs-enter")
        page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
        assert "Only Perception and Video will be played" in page.inner_text("#participant-selection")
        labels: list[str] = []
        while True:
            page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
            screen = screen_of(page)[0]
            if screen == "placeholder":
                labels.append(page.inner_text("#hud-label"))
                text = look.inner_text("#ol-progress-text")
                if text:
                    seen_by_onlooker.append(text)
            if screen == "summary":
                break
            press(page)
        assert "Every round is done" in page.inner_text("#summary-text")
        press(page)  # Finish
        page.wait_for_function("document.getElementById('screen').dataset.phase === 'done'")
        folder = newest_folder(a)
        zipped = wait_packaged(folder)
    finally:
        look_context.close()
    seen: list[tuple] = []
    for pair in page.evaluate("window.__screens"):
        if pair[0] not in ("connecting", "between") and (not seen or seen[-1] != tuple(pair)):
            seen.append(tuple(pair))  # a round's blocks, between screens left out, count once
    assert seen == [
        ("start", "participant"),
        ("participant", "participant"),
        ("device", "device"),
        ("calibration", "calibration"),
        ("placeholder", "perception"),
        ("placeholder", "video"),
        ("summary", "summary"),
    ]
    assert labels[0].startswith("Round 1 of 2 · Perception")
    assert labels[-1].startswith("Round 2 of 2 · Video")
    assert seen_by_onlooker and all(t.endswith("of 3 blocks done") for t in seen_by_onlooker)
    starts = [e["step"] for e in events_of(folder) if e["name"] == "block_start"]
    assert starts == ["perception", "perception", "video"]
    content = zip_contents(zipped)
    meta = json.loads(content["session.json"])
    assert meta["only"] == ["perception", "video"]
    saved = json.loads(content["progress.json"])
    assert saved["status"] == "finished" and saved["only"] == ["perception", "video"]
    assert saved["plan"] == ["participant", "device", "calibration", "perception", "video", "summary"]
    assert saved["points_total"] == 300
    assert server_state(a)["plan"] == saved["plan"]


def test_a_stopped_session_of_chosen_rounds_does_not_promise_a_continuation(app, device, page):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    start_screen(a, page)
    pick(page, "words")
    pick(page, "video", modifiers=["ControlOrMeta"])
    page.click("#play-only")
    page.click("#fs-enter")
    page.wait_for_selector(f'{SCREEN}[data-screen="participant"]')
    while screen_of(page)[0] != "between":
        page.wait_for_selector("#advance:not([disabled])", state="visible", timeout=30_000)
        if screen_of(page)[0] == "between":
            break
        press(page)
    page.click("#stop-here")
    page.click("#leave-yes")
    page.wait_for_selector(f'{SCREEN}[data-screen="summary"]')
    text = page.inner_text("#stopped-next")
    assert "continue from" not in text
    assert "choose them again on the start screen" in text
    items = page.eval_on_selector_all("#stopped-rounds li", "(all) => all.map((li) => li.dataset.step)")
    assert items == ["words", "video"]


def test_the_start_screen_with_choose_several_on_still_fits_900_by_900(app, device, browser):  # noqa: F811
    a = app(port=device().path, flow=SHORT)
    context = browser.new_context(viewport={"width": 900, "height": 900})
    try:
        page = context.new_page()
        start_screen(a, page)
        page.click("#pick-several")
        pick(page, "words")
        pick(page, "recall")
        page.wait_for_function("document.fonts.status === 'loaded'")
        height, inner = page.evaluate("[document.documentElement.scrollHeight, window.innerHeight]")
        assert height <= inner, (height, inner)
        box = page.locator("#play-only").bounding_box()
        assert box and box["y"] + box["height"] <= inner
    finally:
        context.close()


"""Framework problem found in the perception adaptation round: at 1024x768 the task screen
scrolled during practice, because the leave controls stayed below the legend under the panel.
The panel (``--panel``) is what shrinks to make room, not the text."""

import pytest
from test_shell_browser import SCREEN
from test_tasks_browser import EXAMPLE, answer, awaiting, to_instructions


def test_the_example_task_screen_does_not_scroll_during_practice_at_1024x768(app, pty_device, page):
    page.set_viewport_size({"width": 1024, "height": 768})
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    page.click("#practice")
    awaiting(page, 0, True)
    height = page.evaluate("document.documentElement.scrollHeight")
    leave = page.locator("#leave").bounding_box()
    assert height <= 768, (height, leave)


FITS = """
() => {
  const doc = document.documentElement;
  const inside = (id) => {
    const node = document.getElementById(id);
    if (!node || !node.getClientRects().length) return null;
    const r = node.getBoundingClientRect();
    if (!r.height) return null;
    return r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth;
  };
  const panel = document.getElementById('task-panel');
  return {
    scroll: doc.scrollHeight - innerHeight,
    scrollX: doc.scrollWidth - innerWidth,
    panel: inside('task-panel'),
    legend: inside('task-legend'),
    leave: inside('leave'),
    panelSize: panel && panel.getClientRects().length ? panel.getBoundingClientRect().height : null,
    legendFont: getComputedStyle(document.getElementById('task-legend')).fontSize,
  };
}
"""


@pytest.mark.parametrize("size", [(1024, 768), (1920, 1080)])
def test_panel_legend_and_leave_controls_fit_without_scrolling_in_every_phase(app, pty_device, page, size):
    width, height = size
    page.set_viewport_size({"width": width, "height": height})
    a = app(port=pty_device.path, flow=EXAMPLE)
    to_instructions(a, page)
    seen = {}

    def check(phase, panel_shown):
        page.wait_for_timeout(100)  # a frame or two for the layout of the new phase
        fit = page.evaluate(FITS)
        seen[phase] = fit
        assert fit["scroll"] <= 0 and fit["scrollX"] <= 0, (phase, fit)
        assert fit["legendFont"] == "16px", (phase, fit)  # the text keeps its size
        if panel_shown:
            assert fit["panel"] is True and fit["legend"] is True, (phase, fit)
            assert fit["panelSize"] >= 200, (phase, fit)
        if fit["leave"] is not None:
            assert fit["leave"] is True, (phase, fit)

    check("instructions", False)
    page.click("#practice")
    awaiting(page, 0, True)
    check("practice", True)
    answer(page, 0, True)
    answer(page, 1, True)
    page.wait_for_selector("#advance", state="visible")
    page.click("#advance")
    awaiting(page, 0, False)
    check("running", True)
    page.evaluate("document.exitFullscreen()")
    page.wait_for_selector(f'{SCREEN}[data-phase="paused"]')
    check("paused", False)
    # the same panel in practice and in the block: the stimuli keep their size
    assert seen["practice"]["panelSize"] == seen["running"]["panelSize"]
    if size == (1920, 1080):
        assert seen["running"]["panelSize"] == pytest.approx(min(0.6 * 1080, 760), abs=1)  # as before
    print(size, {k: (v["panelSize"], v["scroll"]) for k, v in seen.items()})

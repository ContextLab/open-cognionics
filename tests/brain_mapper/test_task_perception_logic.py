"""The perception task's pure logic and its grating drawing, in a real Chromium.

The modules ``tasks/perception-logic.js`` and ``tasks/perception-draw.js`` are
loaded from the real ``brain-mapper`` server's static files (started on a
pseudo-terminal device) and called through ``page.evaluate``. The generator
passed to the trial functions is a real seeded generator (mulberry32) with the
four methods the task framework's generator has: ``int(n)``, ``float()``,
``pick(array)`` and ``shuffle(array)``.
"""

from __future__ import annotations

import math

import pytest

LOGIC = "/static/js/tasks/perception-logic.js"
DRAW = "/static/js/tasks/perception-draw.js"
SEEDS = 200

#: Loads both modules and a mulberry32 generator onto ``window``.
PRELUDE = """
async ([logic, draw]) => {
  window.L = await import(logic);
  window.D = await import(draw);
  window.gen = (seed) => {
    let a = seed >>> 0;
    const float = () => {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const int = (n) => Math.floor(float() * n);
    const pick = (arr) => arr[int(arr.length)];
    const shuffle = (arr) => {
      const out = arr.slice();
      for (let i = out.length - 1; i > 0; i--) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    };
    return { int, float, pick, shuffle };
  };
  return true;
}
"""


@pytest.fixture
def js(app, pty_device, page):
    """A page of the real server with both modules and ``gen(seed)`` loaded."""
    a = app(port=pty_device.path)
    page.goto(a.url + LOGIC)
    assert page.evaluate(PRELUDE, [LOGIC, DRAW]) is True
    return page


# ---------------------------------------------------------------- trial lists


def blocks(js, seeds, count=None):
    return js.evaluate(
        "([seeds, count]) => seeds.map((s) => count === null"
        " ? L.makeBlockTrials(gen(s)) : L.makeBlockTrials(gen(s), count))",
        [list(seeds), count],
    )


def longest_run(sides):
    best = run = 0
    prev = None
    for s in sides:
        run = run + 1 if s == prev else 1
        prev = s
        best = max(best, run)
    return best


def test_block_trials_meet_every_constraint_over_200_seeds(js):
    lists = blocks(js, range(1, SEEDS + 1))
    per_block = js.evaluate("L.TRIALS_PER_BLOCK")
    # 60 s block; the shortest possible trial is fixation 500 + stimulus 200 + gap 600 = 1300 ms,
    # so at most 60000 / 1300 = 46.2 trials fit; the list must not run out.
    assert per_block >= 47
    assert per_block % 8 == 0
    for trials in lists:
        assert len(trials) == per_block
        sides = [t["side"] for t in trials]
        assert set(sides) <= {"left", "right"}
        # balanced in runs of 8: every consecutive group of 8 holds 4 of each
        for k in range(0, len(sides), 8):
            assert sides[k : k + 8].count("left") == 4, (k, sides)
        # no more than 4 the same in a row, also across the groups of 8
        assert longest_run(sides) <= 4, sides
        for t in trials:
            assert t["fixation_ms"] == 500
            assert isinstance(t["gap_ms"], int) and 600 <= t["gap_ms"] <= 1000
            assert 0 <= t["phase_deg"] < 360
            assert set(t) == {"side", "phase_deg", "fixation_ms", "gap_ms"}


def test_block_trials_spread_the_jitter_and_the_phase(js):
    gaps = [t["gap_ms"] for trials in blocks(js, range(1, 51)) for t in trials]
    phases = [t["phase_deg"] for trials in blocks(js, range(1, 51)) for t in trials]
    # uniform 600..1000: both ends are reached and the mean is near 800
    assert min(gaps) == 600 and max(gaps) == 1000
    assert abs(sum(gaps) / len(gaps) - 800) < 10
    # phase covers all four quadrants
    assert {int(p // 90) for p in phases} == {0, 1, 2, 3}
    # runs of 4 do occur (the rule is a limit, not a ban on runs)
    assert any(longest_run([t["side"] for t in trials]) == 4 for trials in blocks(js, range(1, 51)))


def test_block_trials_are_deterministic_and_seeds_differ(js):
    first = blocks(js, range(1, 21))
    again = blocks(js, range(1, 21))
    assert first == again
    sides = [tuple(t["side"] for t in trials) for trials in first]
    assert len(set(sides)) == len(sides)


def test_block_trials_with_an_explicit_count(js):
    assert blocks(js, [7], 16)[0].__len__() == 16
    assert blocks(js, [7], 0)[0] == []
    with pytest.raises(Exception, match="multiple of 8"):
        blocks(js, [7], 12)


def test_practice_trials_over_200_seeds(js):
    lists = js.evaluate(
        f"Array.from({{length: {SEEDS}}}, (_, i) => L.makePracticeTrials(gen(i + 1)))"
    )
    assert js.evaluate("[L.PRACTICE_TRIALS, L.PRACTICE_TILT_DEG]") == [6, 15]
    for trials in lists:
        assert len(trials) == 6
        sides = [t["side"] for t in trials]
        assert sides.count("left") == 3
        assert longest_run(sides) <= 4
        for t in trials:
            assert t["tilt_deg"] == 15
            assert t["fixation_ms"] == 500
            assert isinstance(t["gap_ms"], int) and 600 <= t["gap_ms"] <= 1000
            assert 0 <= t["phase_deg"] < 360
    again = js.evaluate(
        f"Array.from({{length: {SEEDS}}}, (_, i) => L.makePracticeTrials(gen(i + 1)))"
    )
    assert lists == again
    assert len({tuple(t["side"] for t in trials) for trials in lists}) > 1


def test_timing_constants(js):
    assert js.evaluate(
        "[L.FIXATION_MS, L.STIMULUS_MS, L.RESPONSE_WINDOW_MS, L.GAP_MIN_MS, L.GAP_MAX_MS,"
        " L.FEEDBACK_MS, L.BLOCK_MS]"
    ) == [500, 200, 1500, 600, 1000, 500, 60000]


# ---------------------------------------------------------------- keys and sides


def test_keys_sides_and_signed_tilt(js):
    out = js.evaluate(
        """() => [
          L.responseOf('f'), L.responseOf('F'), L.responseOf('j'), L.responseOf('J'),
          L.responseOf('k'), L.responseOf(null),
          L.isCorrect('left', 'left'), L.isCorrect('left', 'right'),
          L.isCorrect('right', 'right'), L.isCorrect('right', null),
          L.signedTilt('left', 8), L.signedTilt('right', 8),
        ]"""
    )
    assert out == ["left", "left", "right", "right", None, None, True, False, True, False, -8, 8]


# ---------------------------------------------------------------- staircase


def run_staircase(js, outcomes, state=None):
    """Feeds ``outcomes`` (True/False/None) to the staircase; returns per-trial steps and state."""
    return js.evaluate(
        """([outcomes, state]) => {
          let s = state === null ? L.newStaircase() : state;
          const steps = [];
          for (const c of outcomes) {
            const r = L.updateStaircase(s, c);
            steps.push({tilt: r.tilt_deg, trial: r.staircase_trial, reversal: r.reversal,
                        next: r.state.tilt_deg});
            s = r.state;
          }
          return {steps, state: s};
        }""",
        [outcomes, state],
    )


def test_staircase_start_and_constants(js):
    s = js.evaluate("L.newStaircase()")
    assert s["tilt_deg"] == 8.0
    assert js.evaluate("[L.START_TILT_DEG, L.STEP_FACTOR, L.MIN_TILT_DEG, L.MAX_TILT_DEG]") == [
        8.0,
        1.26,
        0.2,
        20,
    ]


def test_staircase_hand_worked_sequence(js):
    # C C W C C W, start 8:
    # t0 C at 8      -> one correct, no step                       next 8
    # t1 C at 8      -> two in a row: down, 8 / 1.26 = 6.3492       next 6.3492 (first step)
    # t2 W at 6.3492 -> up, 6.3492 * 1.26 = 8; direction down->up   reversal at 6.3492
    # t3 C at 8      -> one correct                                 next 8
    # t4 C at 8      -> down to 6.3492; up->down                    reversal at 8
    # t5 None (miss) at 6.3492 -> counts as wrong: up to 8          reversal at 6.3492
    r = run_staircase(js, [True, True, False, True, True, None])
    d = 8 / 1.26
    want = [
        (8, 0, False, 8),
        (8, 1, False, d),
        (d, 2, True, 8),
        (8, 3, False, 8),
        (8, 4, True, d),
        (d, 5, True, 8),
    ]
    for step, (tilt, trial, rev, nxt) in zip(r["steps"], want, strict=True):
        assert step["tilt"] == pytest.approx(tilt, rel=1e-12)
        assert step["trial"] == trial
        assert step["reversal"] is rev
        assert step["next"] == pytest.approx(nxt, rel=1e-12)


def test_staircase_wrong_after_one_correct_resets_the_count(js):
    # C W C C: t0 C (count 1), t1 W -> up 8*1.26 = 10.08 (first step, no reversal),
    # t2 C (count 1, not 2: the wrong reset it), t3 C -> down to 10.08/1.26 = 8, reversal at 10.08
    r = run_staircase(js, [True, False, True, True])
    assert [s["reversal"] for s in r["steps"]] == [False, False, False, True]
    assert r["steps"][2]["tilt"] == pytest.approx(10.08)
    assert r["steps"][3]["tilt"] == pytest.approx(10.08)
    assert r["state"]["tilt_deg"] == pytest.approx(8.0)


def test_staircase_limits(js):
    # 80 correct: 40 steps down from 8 would be 8 / 1.26**40 = 0.0008; held at 0.2
    down = run_staircase(js, [True] * 80)
    assert down["state"]["tilt_deg"] == 0.2
    assert min(s["tilt"] for s in down["steps"]) == 0.2
    assert not any(s["reversal"] for s in down["steps"])
    # 10 wrong: 8 * 1.26**4 = 20.17 > 20, so held at 20 from the fourth on
    up = run_staircase(js, [False] * 10)
    assert up["state"]["tilt_deg"] == 20
    assert max(s["tilt"] for s in up["steps"]) == 20
    assert [s["next"] for s in up["steps"]][:3] == pytest.approx([10.08, 12.7008, 16.003008])


def test_staircase_carries_on_into_the_next_block(js):
    # the state after block 1 is the start of block 2: trial index and tilt carry on
    first = run_staircase(js, [True, True, False])
    second = run_staircase(js, [True, True], first["state"])
    whole = run_staircase(js, [True, True, False, True, True])
    assert [s["trial"] for s in second["steps"]] == [3, 4]
    assert second["steps"] == whole["steps"][3:]
    assert second["state"] == whole["state"]


def test_staircase_update_does_not_change_its_input(js):
    assert js.evaluate(
        """() => {
          const s = L.newStaircase();
          const copy = JSON.stringify(s);
          L.updateStaircase(s, true); L.updateStaircase(s, false);
          return JSON.stringify(s) === copy;
        }"""
    )


# ---------------------------------------------------------------- scoring


def rec(tilt, correct, rt, reversal, i, practice=False):
    return {
        "tilt_deg": tilt,
        "correct": correct,
        "rt_ms": rt,
        "reversal": reversal,
        "staircase_trial": None if practice else i,
        "practice": practice,
        "scored": not practice,
        "response": None if rt is None else "left",
    }


def score(js, records):
    return js.evaluate("(r) => L.scorePerception(r)", records)


def test_score_with_eight_reversals_uses_the_last_six(js):
    # reversal tilts in order: 8, 4, 6, 3, 5, 2.5, 4.5, 2 (8 reversals)
    # last 6: 6, 3, 5, 2.5, 4.5, 2; product 6*3=18, *5=90, *2.5=225, *4.5=1012.5, *2=2025
    # threshold = 2025 ** (1/6) = 45 ** (1/3) = 3.5569
    tilts = [8, 4, 6, 3, 5, 2.5, 4.5, 2]
    records = [rec(t, i % 2 == 0, 400 + 10 * i, True, i) for i, t in enumerate(tilts)]
    # two non-reversal trials: one correct (rt 1000), one miss
    records.append(rec(1.5, True, 1000, False, 8))
    records.append(rec(1.5, False, None, False, 9))
    # practice trials are left out of everything
    records.insert(0, rec(15, False, 300, True, None, practice=True))
    s = score(js, records)
    assert s["threshold_deg"] == pytest.approx(45 ** (1 / 3), rel=1e-12)
    assert s["threshold_reason"] is None
    assert s["n_reversals"] == 8
    assert s["n_trials"] == 10
    # correct: indices 0,2,4,6 and the rt-1000 trial = 5 of 10
    assert s["accuracy"] == pytest.approx(0.5)
    # correct RTs: 400, 420, 440, 460, 1000 -> median 440
    assert s["median_rt_correct_ms"] == 440


def test_score_with_four_reversals_drops_the_first(js):
    # reversals 8, 2, 4, 1 -> all but the first: 2, 4, 1 -> (2*4*1) ** (1/3) = 8 ** (1/3) = 2
    records = [rec(t, True, 500 + 100 * i, True, i) for i, t in enumerate([8, 2, 4, 1])]
    s = score(js, records)
    assert s["threshold_deg"] == pytest.approx(2.0, rel=1e-12)
    assert s["threshold_reason"] is None
    assert s["n_reversals"] == 4
    assert s["accuracy"] == 1
    # correct RTs 500, 600, 700, 800 -> median (600 + 700) / 2 = 650
    assert s["median_rt_correct_ms"] == 650


def test_score_with_two_reversals_keeps_the_second(js):
    # reversals 8, 3 -> all but the first: 3 -> threshold 3
    records = [rec(8, True, 500, True, 0), rec(3, False, 600, True, 1)]
    s = score(js, records)
    assert s["threshold_deg"] == pytest.approx(3.0)
    assert s["n_reversals"] == 2


def test_score_orders_by_staircase_trial(js):
    # the same four as above, given out of order: still 8 first (trial 0), so 2, 4, 1 -> 2
    records = [rec(t, True, 500, True, i) for i, t in enumerate([8, 2, 4, 1])]
    s = score(js, list(reversed(records)))
    assert s["threshold_deg"] == pytest.approx(2.0, rel=1e-12)


def test_score_with_one_reversal_is_null_with_a_reason(js):
    s = score(js, [rec(8, True, 500, False, 0), rec(8, False, 600, True, 1)])
    assert s["threshold_deg"] is None
    assert s["threshold_reason"] == "fewer than 2 reversals (1)"
    assert s["n_reversals"] == 1
    assert s["n_trials"] == 2
    assert s["accuracy"] == pytest.approx(0.5)
    assert s["median_rt_correct_ms"] == 500


def test_score_with_no_responses(js):
    # all misses: accuracy 0, no correct RT, no reversal (all steps go up)
    records = [rec(8 * 1.26**i, False, None, False, i) for i in range(5)]
    s = score(js, records)
    assert s == {
        "threshold_deg": None,
        "threshold_reason": "fewer than 2 reversals (0)",
        "n_reversals": 0,
        "n_trials": 5,
        "accuracy": 0,
        "median_rt_correct_ms": None,
    }


def test_score_with_all_wrong_answers(js):
    records = [rec(20, False, 450, False, i) for i in range(4)]
    s = score(js, records)
    assert s["accuracy"] == 0
    assert s["median_rt_correct_ms"] is None
    assert s["threshold_deg"] is None


def test_score_of_an_empty_list_and_practice_only(js):
    empty = {
        "threshold_deg": None,
        "threshold_reason": "fewer than 2 reversals (0)",
        "n_reversals": 0,
        "n_trials": 0,
        "accuracy": None,
        "median_rt_correct_ms": None,
    }
    assert score(js, []) == empty
    assert score(js, [rec(15, True, 300, False, None, practice=True)]) == empty


def test_score_from_a_simulated_staircase(js):
    # end to end in the logic: a run through updateStaircase gives records whose score is the
    # geometric mean of the reversal tilts computed here by hand from the same steps
    pattern = [True, True, False] * 10 + [True, True, True, False] * 5
    r = run_staircase(js, pattern)
    records = [
        rec(s["tilt"], c, 500, s["reversal"], s["trial"])
        for s, c in zip(r["steps"], pattern, strict=True)
    ]
    rev = [s["tilt"] for s in r["steps"] if s["reversal"]]
    assert len(rev) >= 6
    want = math.exp(sum(math.log(t) for t in rev[-6:]) / 6)
    assert score(js, records)["threshold_deg"] == pytest.approx(want, rel=1e-12)


# ---------------------------------------------------------------- drawing

#: Renders a grating and returns its orientation recovered from the pixels: the dominant
#: gradient direction (structure tensor, central differences) inside 80% of the patch radius,
#: in degrees in screen coordinates (y down), so + is clockwise from vertical.
RECOVER = """
([tilt, contrast, dpr, phase]) => {
  const canvas = document.createElement('canvas');
  const info = D.drawGrating(canvas, {sizeCss: 400, dpr, tiltDeg: tilt, phaseDeg: phase,
                                      contrast});
  const w = canvas.width, h = canvas.height;
  const px = canvas.getContext('2d').getImageData(0, 0, w, h).data;
  const lum = (x, y) => px[4 * (y * w + x)];
  const cx = (w - 1) / 2, cy = (h - 1) / 2, r = 0.8 * info.patch_px * dpr / 2;
  let jxx = 0, jyy = 0, jxy = 0;
  for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
    if ((x - cx) ** 2 + (y - cy) ** 2 > r * r) continue;
    const gx = (lum(x + 1, y) - lum(x - 1, y)) / 2, gy = (lum(x, y + 1) - lum(x, y - 1)) / 2;
    jxx += gx * gx; jyy += gy * gy; jxy += gx * gy;
  }
  const deg = 0.5 * Math.atan2(2 * jxy, jxx - jyy) * 180 / Math.PI;
  return {deg, w, h, patch_px: info.patch_px, size_px: info.size_px};
}
"""


@pytest.mark.parametrize("tilt", [-8, -1, 0, 1, 8])
@pytest.mark.parametrize("dpr", [1, 2])
def test_drawn_orientation_is_recovered_within_half_a_degree(js, tilt, dpr):
    for phase in (0, 77.5, 200):
        got = js.evaluate(RECOVER, [tilt, 1.0, dpr, phase])
        assert abs(got["deg"] - tilt) < 0.5, (tilt, dpr, phase, got)
        # the canvas is drawn at device pixel ratio
        assert got["w"] == got["h"] == got["size_px"] == 400 * dpr
        # the patch is 30% of the stimulus square wide (CSS px)
        assert got["patch_px"] == 120


def test_positive_tilt_is_clockwise(js):
    # clockwise from vertical: the top of each stripe leans right, so the luminance profile of a
    # row above the center is the profile of a row below the center moved to the right.
    shifts = js.evaluate(
        """() => [-8, 8].map((tilt) => {
          const canvas = document.createElement('canvas');
          D.drawGrating(canvas, {sizeCss: 400, dpr: 1, tiltDeg: tilt, phaseDeg: 0, contrast: 1});
          const w = canvas.width, d = 20, c = w / 2;
          const ctx = canvas.getContext('2d');
          const row = (y) => Array.from(ctx.getImageData(0, y, w, 1).data.filter((_, i) => i % 4 === 0));
          const top = row(c - d), bottom = row(c + d);
          let best = 0, bestErr = Infinity;
          for (let s = -15; s <= 15; s++) {
            let err = 0;
            for (let x = c - 30; x < c + 30; x++) err += (top[x] - bottom[x - s]) ** 2;
            if (err < bestErr) { bestErr = err; best = s; }
          }
          return best;
        })"""
    )
    # rows 40 px apart: shift = 40 * tan(8 deg) = 5.6 px
    assert shifts[0] < -4 and shifts[1] > 4, shifts


def test_grating_frequency_contrast_and_background(js):
    out = js.evaluate(
        """() => {
          const canvas = document.createElement('canvas');
          const info = D.drawGrating(canvas, {sizeCss: 400, dpr: 2, tiltDeg: 0, phaseDeg: 0});
          const w = canvas.width;
          const data = canvas.getContext('2d').getImageData(0, 0, w, w).data;
          const at = (x, y) => [data[4 * (y * w + x)], data[4 * (y * w + x) + 1],
                                data[4 * (y * w + x) + 2], data[4 * (y * w + x) + 3]];
          const mid = w / 2;
          const row = [];
          for (let x = 0; x < w; x++) row.push(at(x, mid));
          // phase 180 puts a trough at the center
          const other = document.createElement('canvas');
          D.drawGrating(other, {sizeCss: 400, dpr: 2, tiltDeg: 0, phaseDeg: 180});
          const center = Array.from(other.getContext('2d').getImageData(mid, mid, 1, 1).data);
          // an explicit background
          const third = document.createElement('canvas');
          const info3 = D.drawGrating(third, {sizeCss: 100, dpr: 1, tiltDeg: 0, background: [100, 50, 0],
                                              contrast: 0.2});
          const corner3 = Array.from(third.getContext('2d').getImageData(0, 0, 1, 1).data);
          // an explicit light color darker than the background in one channel
          const info4 = D.drawGrating(document.createElement('canvas'),
                                      {sizeCss: 100, dpr: 1, tiltDeg: 0, background: [100, 50, 0],
                                       light: [80, 200, 0]});
          return {row, center, corner: at(0, 0), edge: at(mid, 2), info, info3, info4, corner3,
                  hex: D.hex([18, 36, 255]), full: D.peakColor([18, 24, 38], [255, 255, 255], 1),
                  half: D.peakColor([18, 24, 38], [255, 255, 255], 0.5),
                  light: D.LIGHT, contrast: D.CONTRAST};
        }"""
    )
    bg, peak = [18, 24, 38], [255, 255, 255]
    # contrast 1 on the page's navy: per channel peak = bg + 1 x (light - bg), the light color
    # (--stim, white) itself
    assert (out["light"], out["contrast"]) == ([255, 255, 255], 1)
    assert out["info"] == {"patch_px": 120, "size_px": 800, "background": bg, "peak": peak}
    for c in range(3):
        assert peak[c] == bg[c] + out["contrast"] * (out["light"][c] - bg[c])
    # the center row is the documented formula, to the nearest pixel value: 3 cycles per 100 CSS
    # px (2 device px each), stripes from the background up to the peak, fading into the ground
    sigma = 120 * 2 / 6
    mid = 400
    for x, px in enumerate(out["row"]):
        dx = x + 0.5 - mid
        env = math.exp(-(dx * dx + 0.25) / (2 * sigma * sigma))
        w = env * (1 + math.cos(2 * math.pi * 0.03 / 2 * dx)) / 2
        for c in range(3):
            assert abs(px[c] - (bg[c] + (peak[c] - bg[c]) * w)) <= 0.5 + 1e-9, (x, px)
        assert px[3] == 255
        assert all(bg[c] <= px[c] <= peak[c] for c in range(3))
    assert out["row"][mid][:3] == [255, 255, 255]  # the peak at the center (phase 0)
    assert out["center"][:3] == bg  # phase 180: a trough (the ground itself) at the center
    # the ground, opaque, at the corner and at the edge of the square: no visible square
    assert out["corner"] == bg + [255]
    assert out["edge"] == bg + [255]
    assert out["corner3"] == [100, 50, 0, 255]
    # bg + 0.2 x (255 - bg): 100 + 31, 50 + 41, 0 + 51
    assert out["info3"]["peak"] == [131, 91, 51]
    # never darker than the background: a light channel below it stays at the background
    assert out["info4"]["peak"] == [100, 200, 0]
    assert out["hex"] == "#1224ff"
    assert out["full"] == [255, 255, 255]
    # halfway: 18 + 118.5, 24 + 115.5, 38 + 108.5, rounded as Math.round does (half up)
    assert out["half"] == [137, 140, 147]


def test_score_uses_only_the_scored_records_of_the_log(js):
    # As the screen logs them: a trial abandoned before its answer (scored false, no reversal
    # field) and a trial cut short after its answer (scored false, with a reversal). Neither
    # enters any measure: only records with scored === true count (scoredRecords).
    records = [
        rec(8, True, 500, False, 0),
        rec(8, False, 600, True, 1),
        {**rec(6.3, False, None, False, 2), "scored": False, "status": "abandoned"},
        {**rec(8, True, 700, True, 2), "scored": True, "status": "done"},
        {**rec(8, True, 800, True, 3), "scored": False, "status": "cut_short"},
    ]
    del records[2]["reversal"]
    s = score(js, records)
    # reversals of scored trials: 8 (t1), 8 (t2) -> all but the first: 8 -> 8
    assert s["n_reversals"] == 2
    assert s["threshold_deg"] == pytest.approx(8.0)
    # t0, t1, t2(done) -> 2 of 3 correct; correct RTs 500, 700 -> median 600
    assert s["n_trials"] == 3
    assert s["accuracy"] == pytest.approx(2 / 3)
    assert s["median_rt_correct_ms"] == 600


def test_score_ignores_records_without_scored_true(js):
    # a record with no `scored` field (or scored false) is not a scored trial
    bare = {k: v for k, v in rec(8, True, 500, True, 0).items() if k != "scored"}
    s = score(js, [bare, {**rec(4, True, 500, True, 1), "scored": False}])
    assert s["n_trials"] == 0 and s["n_reversals"] == 0 and s["accuracy"] is None

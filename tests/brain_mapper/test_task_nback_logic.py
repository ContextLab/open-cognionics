"""The pure logic of the 2-back task (``web/js/tasks/nback-logic.js``) in a real Chromium.

Every test starts the real ``brain-mapper`` on a pty device, opens the module's own URL
from the server's static files, imports it with a dynamic ``import()`` and calls it through
``page.evaluate``. The trial generator gets a real seeded generator defined here
(mulberry32 with ``int``, ``float``, ``pick`` and ``shuffle``). The lists come back to
Python, where every constraint of the task brief is checked again from the letters alone.
"""

from __future__ import annotations

import re
from pathlib import Path
from statistics import NormalDist

import pytest

WEB = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "web"
LOGIC = WEB / "js" / "tasks" / "nback-logic.js"
URL = "/static/js/tasks/nback-logic.js"
LETTERS = set("BCDFGHJKLMNPQRSTVWXZ")
SEEDS = range(1, 251)

#: A real small seeded generator (mulberry32) with the four methods the framework's has.
GEN = """
const makeGen = (seed) => {
  let a = seed >>> 0;
  const float = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const int = (n) => Math.floor(float() * n);
  return {
    int,
    float,
    pick: (xs) => xs[int(xs.length)],
    shuffle(xs) {
      const out = Array.from(xs);
      for (let i = out.length - 1; i > 0; i -= 1) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
};
"""


@pytest.fixture
def run(app, pty_device, page):
    """``run(body, arg)`` evaluates ``body`` with ``m`` (the module), ``makeGen`` and ``arg``."""
    started = app(port=pty_device.path)
    response = page.goto(started.url + URL)
    assert response is not None and response.status == 200

    def evaluate(body: str, arg=None):
        script = (
            "async (arg) => {" + GEN + f"const m = await import({URL!r});" + body + "}"
        )
        return page.evaluate(script, arg)

    return evaluate


def blocks(run, kind: str, seeds) -> list[list[dict]]:
    return run(
        f"return arg.map((seed) => m.{kind}(makeGen(seed)));",
        list(seeds),
    )


def check_list(trials: list[dict], length: int, targets: int, lures: int) -> None:
    """Checks one list against the brief, recomputing every label from the letters."""
    letters = [t["letter"] for t in trials]
    assert len(trials) == length
    assert [t["position"] for t in trials] == list(range(1, length + 1))
    assert set(letters) <= LETTERS
    counts = {"target": 0, "1-back": 0, "3-back": 0}
    run_of_targets = longest_run = 0
    for i, trial in enumerate(trials):
        assert set(trial) == {"letter", "position", "needs_response", "target", "lure", "tail"}
        assert trial["tail"] is False
        # No letter three times in a row.
        if i >= 2:
            assert not (letters[i] == letters[i - 1] == letters[i - 2]), letters
        if i < 2:
            # The first two letters need no answer and are neither targets nor lures;
            # the second differs from the first, so no repeat comes before the answers.
            assert trial["needs_response"] is False
            assert trial["target"] is False and trial["lure"] is None
            if i == 1:
                assert letters[1] != letters[0]
            continue
        assert trial["needs_response"] is True
        target = letters[i] == letters[i - 2]
        assert trial["target"] is target
        one_back = letters[i] == letters[i - 1]
        three_back = i >= 3 and letters[i] == letters[i - 3]
        if target:
            assert trial["lure"] is None
            counts["target"] += 1
            run_of_targets += 1
            longest_run = max(longest_run, run_of_targets)
            continue
        run_of_targets = 0
        # A non-target is a 1-back lure, a 3-back lure (not also a 1-back repeat), or
        # repeats none of the three letters before it.
        if trial["lure"] == "1-back":
            assert one_back and not three_back
        elif trial["lure"] == "3-back":
            assert three_back and not one_back
        else:
            assert trial["lure"] is None
            assert not one_back and not three_back
        if trial["lure"]:
            counts[trial["lure"]] += 1
    assert counts == {"target": targets, "1-back": lures, "3-back": lures}
    # No more than 3 targets in a row.
    assert longest_run <= 3


def test_the_module_has_no_dom_no_clock_and_no_own_randomness():
    text = LOGIC.read_text()
    # Globals as used in code (`window_ms`, a record field, is not the global `window`).
    for pattern in (r"Math\.random", r"\bDate\b", r"\bdocument\.", r"\bwindow\.",
                    r"\bperformance\.", r"\bsetTimeout\b", r"\brequestAnimationFrame\b"):
        assert not re.search(pattern, text), pattern


def test_the_timing_and_keys_follow_the_brief(run):
    got = run(
        "return {timing: m.TIMING, letters: m.LETTERS, block: m.BLOCK_LETTERS,"
        " practice: m.PRACTICE_LETTERS, practiceTargets: m.PRACTICE_TARGETS,"
        " tail: m.TAIL_LETTERS, keys: m.KEYS};"
    )
    assert got["timing"] == {
        "letterMs": 500,
        "leadInMs": 2000,
        # One letter every 3200 ms: the 2400 ms answer window from the onset, 500 ms of
        # feedback after it, 300 ms of the cross alone (see the docs).
        "trialMs": 3200,
        "responseWindowMs": 2400,
        "feedbackMs": 500,
        "gapMs": 300,
        "blockMs": 50000,
    }
    assert got["letters"] == list("BCDFGHJKLMNPQRSTVWXZ")
    assert got["block"] == 14 and got["tail"] == 4
    assert got["practice"] == 10 and got["practiceTargets"] == 3
    assert got["keys"] == {"j": "same", "f": "different"}


def test_every_block_list_meets_the_constraints(run):
    # The first 14 letters (the 50 s block: 2 s lead-in, then 3200 ms a letter): 12 answerable;
    # 30% targets = round(3.6) = 4; 8 non-targets; 15% lures of each kind = round(1.2) = 1.
    # Then a tail of 4 plain non-targets, flagged tail: true.
    for trials in blocks(run, "blockTrials", SEEDS):
        assert len(trials) == 18
        check_list(trials[:14], length=14, targets=4, lures=1)
        letters = [t["letter"] for t in trials]
        for i, trial in enumerate(trials[14:], start=14):
            assert trial == {
                "letter": letters[i], "position": i + 1, "needs_response": True,
                "target": False, "lure": None, "tail": True,
            }
            assert letters[i] in LETTERS
            assert letters[i] not in letters[i - 3:i]  # no repeat of the 3 before it


def test_the_tail_grows_on_demand_from_the_same_stream(run):
    # extendTail(gen, list, n) adds plain non-targets (tail: true) until the list has n
    # letters. Growing in two steps gives the same list as in one, so a reload that rebuilds
    # the list from the block's stream gets the same tail.
    got = run(
        "return arg.map((seed) => { const g1 = makeGen(seed); let a = m.blockTrials(g1);"
        " a = m.extendTail(g1, a, 40); a = m.extendTail(g1, a, 70);"
        " const g2 = makeGen(seed); const b = m.extendTail(g2, m.blockTrials(g2), 70);"
        " const g3 = makeGen(seed); const c = m.extendTail(g3, m.blockTrials(g3), 10);"
        " return {a, b, c}; });",
        list(SEEDS),
    )
    for one in got:
        trials = one["a"]
        assert trials == one["b"] and len(trials) == 70
        assert one["c"] == trials[:18]  # never shortened
        check_list(trials[:14], length=14, targets=4, lures=1)
        letters = [t["letter"] for t in trials]
        for i, trial in enumerate(trials[14:], start=14):
            assert trial == {
                "letter": letters[i], "position": i + 1, "needs_response": True,
                "target": False, "lure": None, "tail": True,
            }
            assert letters[i] not in letters[i - 3:i]


def test_a_run_that_starts_after_a_pause_judges_letters_only_within_the_run(run):
    # runTrial(list, index, runStart): the trial as shown when the run (the letters shown
    # since the block started, or since a resume or a reload) started at runStart.
    starts = [0, 1, 2, 3, 5, 9, 14]
    got = run(
        "return arg.seeds.map((seed) => { const list = m.blockTrials(makeGen(seed));"
        " return {list, runs: arg.starts.map((s) => list.slice(s).map((_, k) =>"
        " m.runTrial(list, s + k, s)))}; });",
        {"seeds": list(SEEDS), "starts": starts},
    )
    for one in got:
        listed = one["list"]
        letters = [t["letter"] for t in listed]
        for start, trials in zip(starts, one["runs"]):
            for k, trial in enumerate(trials):
                i = start + k
                base = listed[i]
                assert {key: trial[key] for key in ("letter", "position", "tail")} == {
                    key: base[key] for key in ("letter", "position", "tail")
                }
                if start == 0:
                    assert trial == {**base, "after_resume": False}
                    continue
                if k < 2:
                    # The first two letters after a resume need no answer and are not judged.
                    assert trial["after_resume"] is True and trial["needs_response"] is False
                    assert trial["target"] is False and trial["lure"] is None
                    continue
                assert trial["after_resume"] is False
                assert trial["needs_response"] is True
                # Judged only against letters of the same run.
                assert trial["target"] is (letters[i] == letters[i - 2]) is base["target"]
                if base["lure"] == "3-back" and i - 3 < start:
                    assert trial["lure"] is None
                else:
                    assert trial["lure"] == base["lure"]


def test_every_practice_list_meets_the_constraints(run):
    # 10 letters: 8 answerable; 3 targets (the brief); 5 non-targets;
    # 15% lures of each kind = round(0.75) = 1.
    for trials in blocks(run, "practiceTrials", SEEDS):
        check_list(trials, length=10, targets=3, lures=1)


def test_the_same_seed_gives_the_same_list_and_different_seeds_differ(run):
    for kind in ("blockTrials", "practiceTrials"):
        first = blocks(run, kind, SEEDS)
        again = blocks(run, kind, SEEDS)
        assert first == again
        texts = {"".join(t["letter"] for t in trials) for trials in first}
        assert len(texts) == len(first)


def test_the_generator_is_only_used_through_its_four_methods(run):
    # A generator object with only int, float, pick and shuffle (no other properties)
    # is enough, and the list given to shuffle is not changed by the module.
    got = run(
        "const g = makeGen(7); const frozen = Object.freeze({int: g.int, float: g.float,"
        " pick: g.pick, shuffle: (xs) => g.shuffle(Object.freeze(Array.from(xs)))});"
        " return m.blockTrials(frozen).length;"
    )
    assert got == 18  # 14 letters and the tail of 4


def test_keys_map_to_responses_and_correctness(run):
    got = run(
        "const t = {needs_response: true, target: true, lure: null};"
        "const n = {needs_response: true, target: false, lure: '1-back'};"
        "const first = {needs_response: false, target: false, lure: null};"
        "return {keys: ['j', 'J', 'f', 'F', ' ', 'k'].map(m.responseForKey),"
        " t: ['same', 'different', null].map((r) => m.isCorrect(t, r)),"
        " n: ['same', 'different', null].map((r) => m.isCorrect(n, r)),"
        " first: ['same', 'different', null].map((r) => m.isCorrect(first, r))};"
    )
    assert got["keys"] == ["same", "same", "different", "different", None, None]
    # A target: "same" is right; "different" and no answer are wrong (a miss).
    assert got["t"] == [True, False, False]
    # A non-target: "different" is right; "same" is a false alarm; no answer is wrong.
    assert got["n"] == [False, True, False]
    # The first two letters are not scored.
    assert got["first"] == [None, None, None]


def test_probit_matches_the_python_inverse_normal(run):
    ps = [1e-6, 0.001, 0.0125, 0.02425, 0.1, 0.125, 0.2142857142857143, 0.3, 0.5, 0.7,
          0.875, 0.9, 0.97575, 0.999, 1 - 1e-6]
    got = run("return arg.map(m.probit);", ps)
    for p, z in zip(ps, got):
        assert z == pytest.approx(NormalDist().inv_cdf(p), abs=1e-8), p


def record(target, response, rt=None, lure=None, needs=True, practice=False):
    return {
        "letter": "B", "position": 3, "needs_response": needs, "target": target,
        "lure": lure, "response": response, "rt_ms": rt, "practice": practice,
        "correct": None, "block": 1, "trial": 1, "tail": False,
        # As the runner logs it: only a done trial that is not practice is scored.
        "status": "done", "scored": not practice,
    }


def z(p: float) -> float:
    return NormalDist().inv_cdf(p)


def test_scoring_uses_only_scored_records_and_leaves_out_resumes_and_short_windows(run):
    # Only `scored: true` records (done, not practice) enter any measure: a letter cut short
    # by the block end, answered or not, is excluded (not a miss), and so is an abandoned one.
    def r(target, response, rt, **extra):
        return {**record(target, response, rt), "window_ms": 2400, "after_resume": False,
                "status": "done", "scored": True, **extra}

    cut = {"status": "cut_short", "scored": False}
    records = [
        # Scored: a hit, a correct rejection and a false alarm.
        r(True, "same", 500),
        r(False, "different", 700),
        r(False, "same", 900),
        # Left out: the first two letters after a resume, even answered ...
        r(True, "same", 450, after_resume=True, needs_response=False),
        # ... a window shorter than the full 2400 ms ...
        r(True, "same", 300, window_ms=2000),
        # ... cut short by the block end, answered inside the window or not answered ...
        r(False, "same", 2399, **cut),
        r(True, None, None, **cut),
        # ... abandoned with an answer ...
        r(True, "same", 400, status="abandoned", scored=False),
        # ... and a record without `scored: true` at all.
        {**r(True, None, None), "scored": None},
    ]
    got = run("return m.score(arg);", records)
    assert (got["targets"], got["non_targets"]) == (1, 2)
    assert (got["hits"], got["misses"]) == (1, 0)
    assert (got["false_alarms"], got["correct_rejections"]) == (1, 1)
    assert got["median_rt_hits_ms"] == 500 and got["no_response"] == 0


def test_scoring_a_hand_worked_mixed_block(run):
    records = [
        # Not scored: the first two letters (even with a key press) and a practice trial.
        record(False, None, needs=False),
        record(False, "same", 300, needs=False),
        record(True, "same", 400, practice=True),
        # Not scored either: a tail letter, and a trial the runner marked scored: false.
        {**record(False, "same", 450), "tail": True},
        {**record(True, None), "scored": False, "status": "cut_short"},
        # 4 targets: 2 hits (600 ms, 800 ms), 1 "different" (miss), 1 no answer (miss).
        record(True, "same", 600),
        record(True, "same", 800),
        record(True, "different", 700),
        record(True, None),
        # 6 non-targets: a false alarm on a 1-back lure (500 ms), a correct "different"
        # on a 1-back lure and on a 3-back lure, 2 plain "different", 1 no answer.
        record(False, "same", 500, lure="1-back"),
        record(False, "different", 650, lure="1-back"),
        record(False, "different", 640, lure="3-back"),
        record(False, "different", 610),
        record(False, "different", 620),
        record(False, None),
    ]
    got = run("return m.score(arg);", records)
    # hit rate = 2/4 = 0.5; false-alarm rate = 1/6.
    # Log-linear: H = (2 + 0.5) / (4 + 1) = 0.5; F = (1 + 0.5) / (6 + 1) = 1.5/7.
    h, f = 2.5 / 5, 1.5 / 7
    assert got == {
        "targets": 4,
        "non_targets": 6,
        "hits": 2,
        "misses": 2,
        "false_alarms": 1,
        # "different" on a non-target: 4 (the unanswered one is neither a false alarm
        # nor a correct rejection; it is counted in no_response).
        "correct_rejections": 4,
        "hit_rate": 0.5,
        "false_alarm_rate": pytest.approx(1 / 6),
        # d' = z(0.5) - z(0.2142857) = 0 - (-0.7916) = 0.7916
        "d_prime": pytest.approx(z(h) - z(f), abs=1e-9),
        # c = -(z(H) + z(F)) / 2 = -(0 - 0.7916) / 2 = 0.3958
        "criterion": pytest.approx(-(z(h) + z(f)) / 2, abs=1e-9),
        # median of 600 and 800
        "median_rt_hits_ms": 700,
        # 3 lures (two 1-back, one 3-back), 1 false alarm among them: 1/3.
        "lure_false_alarm_rate": pytest.approx(1 / 3),
        # one target and one non-target without an answer
        "no_response": 2,
    }
    assert got["d_prime"] == pytest.approx(0.7916386, abs=1e-6)


def test_scoring_a_perfect_block(run):
    records = [
        record(True, "same", 500), record(True, "same", 900), record(True, "same", 700),
        record(False, "different", 400), record(False, "different", 450, lure="3-back"),
        record(False, "different", 480),
    ]
    got = run("return m.score(arg);", records)
    # H = (3 + 0.5) / (3 + 1) = 0.875; F = (0 + 0.5) / (3 + 1) = 0.125.
    # d' = z(0.875) - z(0.125) = 1.150349 - (-1.150349) = 2.300699;
    # c = -(1.150349 - 1.150349) / 2 = 0.
    assert got["hits"] == 3 and got["misses"] == 0
    assert got["false_alarms"] == 0 and got["correct_rejections"] == 3
    assert got["hit_rate"] == 1 and got["false_alarm_rate"] == 0
    assert got["d_prime"] == pytest.approx(2 * z(0.875), abs=1e-9)
    assert got["d_prime"] == pytest.approx(2.3006988, abs=1e-6)
    assert got["criterion"] == pytest.approx(0, abs=1e-9)
    # median of 500, 700, 900
    assert got["median_rt_hits_ms"] == 700
    assert got["lure_false_alarm_rate"] == 0
    assert got["no_response"] == 0


def test_scoring_with_no_responses_at_all(run):
    records = [record(False, None, needs=False), record(False, None, needs=False)]
    records += [record(True, None)] * 3 + [record(False, None)] * 2
    got = run("return m.score(arg);", records)
    # Every target missed; the 2 non-targets are neither false alarms nor correct rejections.
    # H = (0 + 0.5) / (3 + 1) = 0.125; F = (0 + 0.5) / (2 + 1) = 1/6.
    h, f = 0.5 / 4, 0.5 / 3
    assert got == {
        "targets": 3, "non_targets": 2, "hits": 0, "misses": 3, "false_alarms": 0,
        "correct_rejections": 0, "hit_rate": 0, "false_alarm_rate": 0,
        "d_prime": pytest.approx(z(h) - z(f), abs=1e-9),
        "criterion": pytest.approx(-(z(h) + z(f)) / 2, abs=1e-9),
        "median_rt_hits_ms": None, "lure_false_alarm_rate": None, "no_response": 5,
    }
    assert got["d_prime"] < 0


def test_scoring_an_empty_list_and_only_unscored_trials(run):
    empty = {
        "targets": 0, "non_targets": 0, "hits": 0, "misses": 0, "false_alarms": 0,
        "correct_rejections": 0, "hit_rate": None, "false_alarm_rate": None,
        "d_prime": None, "criterion": None, "median_rt_hits_ms": None,
        "lure_false_alarm_rate": None, "no_response": 0,
    }
    assert run("return m.score([]);") == empty
    unscored = [record(False, "same", 300, needs=False), record(True, "same", 400, practice=True)]
    assert run("return m.score(arg);", unscored) == empty
    # Only targets: rates and d' need both kinds of trial, so those without one are null.
    only_targets = run("return m.score(arg);", [record(True, "same", 500)])
    assert only_targets["hit_rate"] == 1 and only_targets["false_alarm_rate"] is None
    assert only_targets["d_prime"] is None and only_targets["criterion"] is None
    assert only_targets["median_rt_hits_ms"] == 500


def test_every_exported_function_has_jsdoc():
    text = LOGIC.read_text()
    exported = re.findall(r"export function (\w+)", text)
    assert exported
    for name in exported:
        assert re.search(r"\*/\s*export function " + name + r"\(", text), name

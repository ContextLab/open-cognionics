"""The flanker task's pure logic (``web/js/tasks/flanker-logic.js``) in a real Chromium.

Each test starts the real ``brain-mapper`` command on a pseudo-terminal device, opens its page,
imports the module from the server's own static files and calls it through ``page.evaluate``.
Trial generation is given a real seeded generator defined here (mulberry32 with the four methods
the framework's generator has: ``int``, ``float``, ``pick``, ``shuffle``). The constraints are
checked in Python on the lists the module returned.
"""

from __future__ import annotations

import itertools
import statistics

import pytest

MODULE = "/static/js/tasks/flanker-logic.js"
SEEDS = list(range(1, 301))

#: mulberry32 (Tommy Ettinger), wrapped with the four methods the framework's generator has.
RNG_JS = """
function makeRng(seed) {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const rng = {
    float: () => next(),
    int: (n) => Math.floor(next() * n),
    pick: (xs) => xs[Math.floor(next() * xs.length)],
    shuffle: (xs) => {
      const out = xs.slice();
      for (let i = out.length - 1; i > 0; i--) {
        const j = Math.floor(next() * (i + 1));
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    },
  };
  return rng;
}
"""


@pytest.fixture
def run(app, pty_device, page):
    """``run(body, arg)`` evaluates ``body`` (JS statements, ``L`` = the module, ``makeRng``
    defined, ``arg`` the argument) in the real page and returns what it returns."""
    a = app(port=pty_device.path)
    page.goto(a.url)

    def call(body: str, arg=None):
        script = (
            "async (arg) => {\n"
            f"  const L = await import('{MODULE}');\n"
            f"{RNG_JS}\n"
            f"{body}\n"
            "}"
        )
        return page.evaluate(script, arg)

    return call


def combo(trial: dict) -> tuple[bool, str]:
    return (trial["congruent"], trial["target"])


def longest_run(trials: list[dict]) -> int:
    return max(
        (len(list(g)) for _, g in itertools.groupby(trials, key=combo)), default=0
    )


ALL_COMBOS = {(True, "left"), (True, "right"), (False, "left"), (False, "right")}
TRIAL_KEYS = {"congruent", "target", "flankers", "fixation_ms", "gap_ms"}


def check_trial(t: dict) -> None:
    assert set(t) == TRIAL_KEYS
    assert isinstance(t["congruent"], bool)
    assert t["target"] in ("left", "right")
    assert t["flankers"] in ("left", "right")
    if t["congruent"]:
        assert t["flankers"] == t["target"]
    else:
        assert t["flankers"] != t["target"]
    assert t["fixation_ms"] == 500
    assert isinstance(t["gap_ms"], int)
    assert 700 <= t["gap_ms"] <= 1100


# --- trial generation -------------------------------------------------------------------------


def test_block_constraints_over_300_seeds(run):
    lists = run("return arg.map((s) => L.blockTrials(makeRng(s)));", SEEDS)
    gaps = []
    for trials in lists:
        assert (
            len(trials) == 40
        )  # the flow's blocks are 50 s: 5 runs of 8, more than fit
        for t in trials:
            check_trial(t)
            gaps.append(t["gap_ms"])
        # Every run of 8 (0-7, 8-15, ..., 32-39): 2 of each of the 4 combinations.
        for start in range(0, 40, 8):
            chunk = [combo(t) for t in trials[start : start + 8]]
            assert {c: chunk.count(c) for c in ALL_COMBOS} == dict.fromkeys(
                ALL_COMBOS, 2
            )
        # Never more than 3 identical trials in a row, across run boundaries too.
        assert longest_run(trials) <= 3
        # 50% congruent and 50% left.
        assert sum(t["congruent"] for t in trials) == 20
        assert sum(t["target"] == "left" for t in trials) == 20
    # The gap is uniform over 700..1100: both ends occur and the mean is near 900
    # (12000 draws, sd about 116, standard error about 1.1 ms).
    assert min(gaps) == 700
    assert max(gaps) == 1100
    assert abs(statistics.fmean(gaps) - 900) < 6
    # Some lists reach a run of 3 across a boundary: the limit is the brief's 3, not lower.
    assert any(longest_run(trials) == 3 for trials in lists)


def test_block_same_seed_same_list_different_seeds_differ(run):
    out = run(
        "return arg.map((s) => [L.blockTrials(makeRng(s)), L.blockTrials(makeRng(s))]);",
        SEEDS,
    )
    for first, second in out:
        assert first == second
    as_text = {repr(first) for first, _ in out}
    assert len(as_text) == len(SEEDS)


def test_practice_constraints_and_determinism(run):
    out = run(
        "return arg.map((s) => [L.practiceTrials(makeRng(s)), L.practiceTrials(makeRng(s))]);",
        SEEDS,
    )
    seen = set()
    for trials, again in out:
        assert trials == again
        assert len(trials) == 6
        for t in trials:
            check_trial(t)
        assert sum(t["congruent"] for t in trials) == 3
        assert sum(t["target"] == "left" for t in trials) == 3
        assert longest_run(trials) <= 3
        seen.add(repr(trials))
    assert len(seen) > 100


def test_timing_constants(run):
    t = run("return L.TIMING;")
    assert t == {
        "block_ms": 50000,
        "fixation_ms": 500,
        "answer_ms": 1500,
        "gap_min_ms": 700,
        "gap_max_ms": 1100,
        "feedback_ms": 500,
        "lead_in_ms": 2000,
        "anticipation_ms": 150,
    }
    assert run("return [L.BLOCK_TRIALS, L.PRACTICE_TRIALS, L.BLOCKS];") == [40, 6, 2]
    # A trial's fixed length: cross, arrows (the window), feedback, gap.
    assert run("return [L.trialMs({fixation_ms: 500, gap_ms: 700}), L.trialMs({fixation_ms: 500, gap_ms: 1100})];") == [3200, 3600]


def test_module_is_pure(run):
    source = run(f"return await (await fetch('{MODULE}')).text();")
    for banned in ("Math.random", "Date", "document", "window", "performance"):
        assert banned not in source


# --- responses and records ----------------------------------------------------------------------


def test_correctness_and_feedback(run):
    out = run(
        """
        const con = {congruent: true, target: 'left', flankers: 'left', fixation_ms: 500, gap_ms: 812};
        const inc = {congruent: false, target: 'right', flankers: 'left', fixation_ms: 500, gap_ms: 1100};
        return {
          correct: [L.isCorrect(con, 'left'), L.isCorrect(con, 'right'), L.isCorrect(con, null),
                    L.isCorrect(inc, 'right'), L.isCorrect(inc, 'left')],
          feedback: [L.feedbackFor(inc, 'right'), L.feedbackFor(inc, 'left'), L.feedbackFor(inc, null)],
        };
        """
    )
    assert out["correct"] == [True, False, False, True, False]
    assert out["feedback"] == ["Correct", "Incorrect", "No response"]


# --- scoring --------------------------------------------------------------------------------------


def rec(congruent, correct, rt, practice=False, response="x", status="done"):
    """A trial record with the fields scoring reads; ``scored`` as the runner sets it (a ``done``
    trial that is not practice)."""
    if rt is None:
        response = None
    return {
        "congruent": congruent,
        "correct": correct,
        "rt_ms": rt,
        "response": response,
        "practice": practice,
        "status": status,
        "scored": status == "done" and not practice,
    }


def score(run, records):
    return run("return L.score(arg);", records)


def test_score_worked_example_even_counts(run):
    records = [
        rec(True, True, 400),
        rec(True, True, 500),
        rec(True, False, 450),
        rec(True, False, None),  # miss
        rec(False, True, 600),
        rec(
            False, True, 100
        ),  # correct but under 150 ms: an anticipation, left out of RT
        rec(False, True, 700),
        rec(False, False, 300),
        rec(True, True, 9999, practice=True),  # practice: not scored
    ]
    s = score(run, records)
    # Congruent: 4 trials, 2 correct -> accuracy 2/4 = 0.5; correct RTs [400, 500] -> median 450.
    assert s["congruent"] == {
        "n": 4,
        "n_correct": 2,
        "accuracy": 0.5,
        "median_rt_ms": 450,
    }
    # Incongruent: 4 trials, 3 correct -> 3/4 = 0.75; correct RTs >= 150 are [600, 700] -> 650.
    assert s["incongruent"] == {
        "n": 4,
        "n_correct": 3,
        "accuracy": 0.75,
        "median_rt_ms": 650,
    }
    # Costs, incongruent minus congruent: RT 650 - 450 = 200 ms; accuracy 0.75 - 0.5 = 0.25.
    assert s["flanker_cost_rt_ms"] == 200
    assert s["flanker_cost_accuracy"] == pytest.approx(0.25)
    # 8 scored trials (the practice one is left out), 1 miss, 1 anticipation (the 100 ms one).
    assert s["n_trials"] == 8
    assert s["n_misses"] == 1
    assert s["n_anticipations"] == 1


def test_score_worked_example_odd_counts_and_boundary(run):
    records = [
        rec(True, True, 300),
        rec(True, True, 900),
        rec(True, True, 150),  # exactly 150 ms is not under 150: kept
        rec(False, True, 350),
        rec(False, True, 1000),
        rec(False, True, 360),
        rec(False, True, 149),  # anticipation
        rec(False, True, 380),
        rec(False, False, 140),  # wrong and an anticipation
    ]
    s = score(run, records)
    # Congruent: 3/3 correct -> 1.0; RTs [150, 300, 900] -> median 300.
    assert s["congruent"] == {
        "n": 3,
        "n_correct": 3,
        "accuracy": 1.0,
        "median_rt_ms": 300,
    }
    # Incongruent: 6 trials, 5 correct -> 5/6; correct RTs >= 150: [350, 360, 380, 1000]
    # -> median (360 + 380) / 2 = 370.
    assert s["incongruent"]["n"] == 6
    assert s["incongruent"]["n_correct"] == 5
    assert s["incongruent"]["accuracy"] == pytest.approx(5 / 6)
    assert s["incongruent"]["median_rt_ms"] == 370
    # Costs: 370 - 300 = 70 ms; 5/6 - 1 = -1/6.
    assert s["flanker_cost_rt_ms"] == 70
    assert s["flanker_cost_accuracy"] == pytest.approx(-1 / 6)
    assert s["n_trials"] == 9
    assert s["n_misses"] == 0
    # 149 ms and 140 ms are anticipations; 150 ms is not.
    assert s["n_anticipations"] == 2


def test_score_empty_list(run):
    s = score(run, [])
    empty = {"n": 0, "n_correct": 0, "accuracy": None, "median_rt_ms": None}
    assert s == {
        "n_trials": 0,
        "n_misses": 0,
        "n_anticipations": 0,
        "congruent": empty,
        "incongruent": empty,
        "flanker_cost_rt_ms": None,
        "flanker_cost_accuracy": None,
    }


def test_score_all_misses(run):
    records = [rec(True, False, None), rec(True, False, None), rec(False, False, None)]
    s = score(run, records)
    # Congruent 0/2 = 0, incongruent 0/1 = 0; no RTs anywhere, so no medians and no RT cost.
    assert s["congruent"] == {
        "n": 2,
        "n_correct": 0,
        "accuracy": 0,
        "median_rt_ms": None,
    }
    assert s["incongruent"] == {
        "n": 1,
        "n_correct": 0,
        "accuracy": 0,
        "median_rt_ms": None,
    }
    assert s["flanker_cost_accuracy"] == 0
    assert s["flanker_cost_rt_ms"] is None
    assert s["n_trials"] == 3
    assert s["n_misses"] == 3
    assert s["n_anticipations"] == 0


def test_score_leaves_out_cut_short_abandoned_and_error_records(run):
    records = [
        rec(True, True, 400),
        rec(False, True, 600),
        rec(False, False, None),  # a scored miss
        rec(True, False, None, status="cut_short"),  # the block ended during it
        rec(
            False, True, 300, status="abandoned"
        ),  # a pause during it (a response came first)
        rec(True, False, None, status="abandoned"),
        rec(True, False, None, status="error"),
        rec(True, True, 120, practice=True),
    ]
    s = score(run, records)
    # Only the first three count: congruent 1/1 -> 1.0, median 400; incongruent 1/2 -> 0.5,
    # median 600. Costs 600 - 400 = 200 ms and 0.5 - 1.0 = -0.5. One miss, no anticipation.
    assert s["n_trials"] == 3
    assert s["n_misses"] == 1
    assert s["n_anticipations"] == 0
    assert s["congruent"] == {
        "n": 1,
        "n_correct": 1,
        "accuracy": 1.0,
        "median_rt_ms": 400,
    }
    assert s["incongruent"] == {
        "n": 2,
        "n_correct": 1,
        "accuracy": 0.5,
        "median_rt_ms": 600,
    }
    assert s["flanker_cost_rt_ms"] == 200
    assert s["flanker_cost_accuracy"] == -0.5
    # A record without `scored: true` (for example one built by hand) is not scored.
    s = score(
        run, [{"congruent": True, "correct": True, "rt_ms": 400, "response": "left"}]
    )
    assert s["n_trials"] == 0


def test_score_only_practice_and_one_condition(run):
    # Only practice records: nothing scored.
    s = score(
        run, [rec(True, True, 400, practice=True), rec(False, True, 500, practice=True)]
    )
    assert s["n_trials"] == 0
    assert s["congruent"]["accuracy"] is None
    # Only congruent trials: incongruent is empty, so both costs are null.
    s = score(run, [rec(True, True, 420), rec(True, True, 480)])
    # Congruent: 2/2 -> 1.0; median of [420, 480] = 450.
    assert s["congruent"] == {
        "n": 2,
        "n_correct": 2,
        "accuracy": 1.0,
        "median_rt_ms": 450,
    }
    assert s["incongruent"]["n"] == 0
    assert s["flanker_cost_rt_ms"] is None
    assert s["flanker_cost_accuracy"] is None

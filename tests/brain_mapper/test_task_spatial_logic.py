"""The pure logic of the spatial memory task (``web/js/tasks/spatial-logic.js``).

The module is loaded in real Chromium from the real brain-mapper server (started on a
pty device, as every browser test does) and called through ``page.evaluate``. The
random numbers come from a real seeded generator defined here (mulberry32 with the
methods ``int``, ``float``, ``pick`` and ``shuffle``), passed to the module as a parameter.
"""

from __future__ import annotations

import itertools
from collections import Counter
from pathlib import Path

import pytest

from brain_mapper import flow

MODULE = "/static/js/tasks/spatial-logic.js"
SOURCE = (
    Path(__file__).resolve().parents[2]
    / "brain_mapper/src/brain_mapper/web/js/tasks/spatial-logic.js"
)
SEEDS = range(1, 251)
TRIAL_KEYS = {"load", "lit", "probe", "probe_lit", "gap_ms"}

# A real seeded generator (mulberry32), with the four methods the task framework's
# generator has. ``make(seed)`` returns a new one.
GENERATOR = """
const make = (seed) => {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const int = (n) => Math.floor(next() * n);
  return {
    int,
    float: next,
    pick: (list) => list[int(list.length)],
    shuffle(list) {
      const out = Array.from(list);
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
def logic(app, pty_device, page):
    """``logic(body, arg)`` runs ``body`` with ``L`` (the module) and ``make`` in scope."""
    a = app(port=pty_device.path)
    page.goto(a.url)

    def run(body: str, arg=None):
        script = (
            f"async (arg) => {{ const L = await import({MODULE!r}); {GENERATOR} {body} }}"
        )
        return page.evaluate(script, arg)

    return run


def blocks(logic, seeds=SEEDS):
    return logic(
        "return arg.map((s) => L.blockTrials(make(s)));", list(seeds)
    )


def check_trial(t):
    assert set(t) == TRIAL_KEYS
    assert t["load"] in (2, 4, 6)
    lit = t["lit"]
    assert len(lit) == t["load"]
    assert len(set(lit)) == len(lit), "lit squares drawn without replacement"
    assert all(isinstance(c, int) and 0 <= c <= 24 for c in lit)
    assert isinstance(t["probe"], int) and 0 <= t["probe"] <= 24
    assert isinstance(t["probe_lit"], bool)
    assert (t["probe"] in lit) == t["probe_lit"]
    assert isinstance(t["gap_ms"], int) and 800 <= t["gap_ms"] <= 1200


def test_the_module_is_pure():
    text = SOURCE.read_text()
    for word in ("Math.random", "Date", "document", "window", "performance", "import "):
        assert word not in text, word


def test_the_timing_constants_match_the_brief(logic):
    got = logic(
        "return {grid: L.GRID, loads: L.LOADS, fix: L.FIXATION_MS, stim: L.STIM_MS,"
        " ret: L.RETENTION_MS, probe: L.PROBE_MS, gmin: L.GAP_MIN_MS, gmax: L.GAP_MAX_MS,"
        " fb: L.FEEDBACK_MS, block: L.BLOCK_MS, n: L.TRIALS_PER_BLOCK, run: L.RUN_LENGTH,"
        " keys: L.KEYS};"
    )
    assert got == {
        "grid": 5, "loads": [2, 4, 6], "fix": 500, "stim": 1000, "ret": 2000,
        "probe": 3000, "gmin": 800, "gmax": 1200, "fb": 500, "block": 60000,
        "n": 24, "run": 12, "keys": {"KeyF": "no", "KeyJ": "yes"},
    }
    # A trial's fixed length: gap and cross, filled squares, empty grid, probe window, feedback.
    assert logic("return [L.trialMs({gap_ms: 800}), L.trialMs({gap_ms: 1200})];") == [7800, 8200]


def test_every_block_meets_the_constraints_over_250_seeds(logic):
    for trials in blocks(logic):
        assert len(trials) == 24
        for t in trials:
            check_trial(t)
        # Runs of 12: each of the 6 (load, probe_lit) combinations exactly twice.
        for start in (0, 12):
            run = Counter((t["load"], t["probe_lit"]) for t in trials[start:start + 12])
            assert run == {c: 2 for c in itertools.product((2, 4, 6), (True, False))}
        # So over the block: loads equally often, probe lit half the time in each load.
        assert Counter(t["load"] for t in trials) == {2: 8, 4: 8, 6: 8}


def test_the_draws_cover_every_square_and_the_whole_gap_range(logic):
    lit, probe_lit, probe_not, gaps, orders = Counter(), Counter(), Counter(), set(), set()
    for trials in blocks(logic):
        orders.add(tuple((t["load"], t["probe_lit"]) for t in trials[:12]))
        for t in trials:
            lit.update(t["lit"])
            gaps.add(t["gap_ms"])
            (probe_lit if t["probe_lit"] else probe_not)[t["probe"]] += 1
    assert set(lit) == set(range(25))
    assert set(probe_lit) == set(range(25))
    assert set(probe_not) == set(range(25))
    # 250 x 24 = 6000 draws over 401 values: both ends and nearly all values appear.
    assert min(gaps) == 800 and max(gaps) == 1200
    assert len(gaps) > 390
    # The run order is shuffled, not fixed.
    assert len(orders) > 200


def test_the_same_seed_gives_the_same_list_and_different_seeds_differ(logic):
    first = blocks(logic)
    again = blocks(logic)
    assert first == again
    as_text = {repr(trials) for trials in first}
    assert len(as_text) == len(SEEDS)


def test_the_generator_is_the_only_source_of_randomness(logic):
    # Two generators from one seed give the same list even after other draws elsewhere.
    same = logic(
        "const a = L.blockTrials(make(7)); make(99).int(5);"
        " const b = L.blockTrials(make(7));"
        " return JSON.stringify(a) === JSON.stringify(b);"
    )
    assert same is True


def test_the_practice_trials(logic):
    lists = logic("return arg.map((s) => L.practiceTrials(make(s)));", list(SEEDS))
    for trials in lists:
        assert [t["load"] for t in trials] == [2, 4, 2]
        assert [t["probe_lit"] for t in trials] == [True, False, True]
        for t in trials:
            check_trial(t)
    assert lists == logic("return arg.map((s) => L.practiceTrials(make(s)));", list(SEEDS))
    assert len({repr(t) for t in lists}) == len(SEEDS)


def test_a_response_is_correct_only_when_it_matches_the_probe(logic):
    got = logic(
        "const lit = {load: 2, lit: [3, 9], probe: 9, probe_lit: true, gap_ms: 900};"
        " const not = {load: 2, lit: [3, 9], probe: 4, probe_lit: false, gap_ms: 900};"
        " return [L.isCorrect(lit, 'yes'), L.isCorrect(lit, 'no'), L.isCorrect(lit, null),"
        " L.isCorrect(not, 'yes'), L.isCorrect(not, 'no'), L.isCorrect(not, null)];"
    )
    assert got == [True, False, False, False, True, False]


def rec(load, probe_lit, response, rt_ms, practice=False):
    correct = response is not None and (response == "yes") == probe_lit
    return {
        "load": load, "lit": [], "probe": 0, "probe_lit": probe_lit,
        "response": response, "correct": correct, "rt_ms": rt_ms, "gap_ms": 1000,
        "practice": practice, "block": 0, "trial": 0,
        # as the runner writes them: a done trial that is not practice is scored
        "status": "done", "scored": not practice,
    }


def score(logic, records):
    return logic("return L.score(arg);", records)


def test_scoring_a_hand_worked_example(logic):
    records = [
        # load 2: three lit probes (yes, yes, no), three not-lit (no, yes, none)
        rec(2, True, "yes", 500),
        rec(2, True, "yes", 700),
        rec(2, True, "no", 900),
        rec(2, False, "no", 600),
        rec(2, False, "yes", 800),
        rec(2, False, None, None),
        # load 4: all four right
        rec(4, True, "yes", 400),
        rec(4, True, "yes", 600),
        rec(4, False, "no", 500),
        rec(4, False, "no", 700),
        # load 6: only lit probes, one yes one no
        rec(6, True, "yes", 1000),
        rec(6, True, "no", 1200),
        # a practice trial: left out (it would give load 6 a not-lit probe and K = 3)
        rec(6, False, "no", 300, practice=True),
    ]
    s = score(logic, records)
    two, four, six = s["by_load"]
    # Load 2: H = 2 yes / 3 lit = 2/3; F = 1 yes / 2 answered not-lit = 1/2 (the
    # unanswered one is left out); K = 2 x (2/3 - 1/2) = 2 x 1/6 = 1/3.
    # Accuracy = 3 correct (500, 700, 600) / 6 trials = 0.5; median of 500, 600, 700 = 600.
    assert two["load"] == 2
    assert two["n_trials"] == 6 and two["no_response"] == 1
    assert two["hits"] == 2 and two["lit_answered"] == 3
    assert two["false_alarms"] == 1 and two["not_lit_answered"] == 2
    assert two["hit_rate"] == pytest.approx(2 / 3)
    assert two["fa_rate"] == pytest.approx(1 / 2)
    assert two["accuracy"] == pytest.approx(0.5)
    assert two["median_rt_ms"] == 600
    assert two["k"] == pytest.approx(1 / 3)
    # Load 4: H = 2/2 = 1, F = 0/2 = 0, K = 4 x (1 - 0) = 4; accuracy 4/4 = 1;
    # median of 400, 500, 600, 700 = (500 + 600) / 2 = 550.
    assert four["n_trials"] == 4 and four["no_response"] == 0
    assert four["hit_rate"] == 1 and four["fa_rate"] == 0
    assert four["accuracy"] == 1
    assert four["median_rt_ms"] == 550
    assert four["k"] == pytest.approx(4)
    # Load 6: H = 1/2, no not-lit probes so F and K are null; accuracy 1/2;
    # median of the one correct RT (1000) = 1000.
    assert six["n_trials"] == 2
    assert six["hit_rate"] == pytest.approx(0.5)
    assert six["fa_rate"] is None
    assert six["k"] is None
    assert six["accuracy"] == pytest.approx(0.5)
    assert six["median_rt_ms"] == 1000
    # Overall: K = max(1/3, 4) = 4; 12 scored trials, 1 with no response.
    assert s["k"] == pytest.approx(4)
    assert s["n_trials"] == 12
    assert s["no_response"] == 1


def test_overall_k_is_the_largest_even_when_all_are_negative(logic):
    records = [
        # load 2: lit -> no, not-lit -> yes: H = 0, F = 1, K = 2 x (0 - 1) = -2
        rec(2, True, "no", 500),
        rec(2, False, "yes", 500),
        # load 4: lit yes, lit no, not-lit yes, not-lit no -> H = 1/2, F = 1/2,
        # K = 4 x (1/2 - 1/2) = 0
        rec(4, True, "yes", 500),
        rec(4, True, "no", 500),
        rec(4, False, "yes", 500),
        rec(4, False, "no", 500),
    ]
    s = score(logic, records)
    assert s["by_load"][0]["k"] == pytest.approx(-2)
    assert s["by_load"][1]["k"] == pytest.approx(0)
    assert s["by_load"][2]["k"] is None and s["by_load"][2]["n_trials"] == 0
    # max(-2, 0) = 0
    assert s["k"] == pytest.approx(0)
    only_negative = score(logic, records[:2])
    assert only_negative["k"] == pytest.approx(-2)


def test_scoring_all_misses(logic):
    # Every answer wrong: load 4 lit -> no twice, not-lit -> yes twice.
    # H = 0/2 = 0, F = 2/2 = 1, K = 4 x (0 - 1) = -4; accuracy 0/4 = 0; no correct RTs.
    records = [rec(4, True, "no", 450), rec(4, True, "no", 550),
               rec(4, False, "yes", 650), rec(4, False, "yes", 750)]
    s = score(logic, records)
    four = s["by_load"][1]
    assert four["hit_rate"] == 0 and four["fa_rate"] == 1
    assert four["k"] == pytest.approx(-4)
    assert four["accuracy"] == 0
    assert four["median_rt_ms"] is None
    assert s["k"] == pytest.approx(-4)


def test_scoring_with_no_responses(logic):
    # Six unanswered trials: all left out of H and F, so H, F and K are null;
    # accuracy 0/2 per load; no correct RTs; reported as 6 with no response.
    records = [rec(load, lit, None, None) for load in (2, 4, 6) for lit in (True, False)]
    s = score(logic, records)
    for row in s["by_load"]:
        assert row["n_trials"] == 2 and row["no_response"] == 2
        assert row["hit_rate"] is None and row["fa_rate"] is None and row["k"] is None
        assert row["accuracy"] == 0
        assert row["median_rt_ms"] is None
    assert s["k"] is None
    assert s["no_response"] == 6 and s["n_trials"] == 6


def test_scoring_an_empty_list(logic):
    s = score(logic, [])
    assert [row["load"] for row in s["by_load"]] == [2, 4, 6]
    for row in s["by_load"]:
        assert row["n_trials"] == 0 and row["no_response"] == 0
        assert row["hit_rate"] is None and row["fa_rate"] is None
        assert row["accuracy"] is None and row["median_rt_ms"] is None and row["k"] is None
    assert s == {"by_load": s["by_load"], "k": None, "n_trials": 0, "no_response": 0}
    # Only practice trials: the same as empty.
    assert score(logic, [rec(2, True, "yes", 500, practice=True)]) == s


def test_scoring_leaves_out_trials_that_were_not_done(logic):
    # The runner's records carry `status` and `scored`; a cut-short or abandoned trial is not
    # scored. Kept: load 2 lit yes 500 and not-lit no 700 -> H = 1/1, F = 0/1, K = 2 x 1 = 2,
    # accuracy 2/2 = 1, median (500 + 700) / 2 = 600. The two left out would add a wrong
    # answer (cut_short, not-lit yes) and a miss (abandoned).
    def runner(load, probe_lit, response, rt_ms, status):
        r = rec(load, probe_lit, response, rt_ms)
        r["status"], r["scored"] = status, status == "done"
        return r

    records = [
        runner(2, True, "yes", 500, "done"),
        runner(2, False, "no", 700, "done"),
        runner(2, False, "yes", 400, "cut_short"),
        runner(2, True, None, None, "abandoned"),
    ]
    s = score(logic, records)
    two = s["by_load"][0]
    assert two["n_trials"] == 2 and two["no_response"] == 0
    assert two["hit_rate"] == 1 and two["fa_rate"] == 0
    assert two["accuracy"] == 1 and two["median_rt_ms"] == 600
    assert two["k"] == pytest.approx(2)
    assert s["k"] == pytest.approx(2) and s["n_trials"] == 2


def test_scoring_a_generated_block_answered_perfectly(logic):
    # Every generated trial answered right in 600 ms: H = 1, F = 0 in each load, so
    # K = load and the overall K = 6; accuracy 1; median RT 600.
    s = logic(
        "const trials = L.blockTrials(make(42));"
        " const records = trials.map((t, i) => { const response = t.probe_lit ? 'yes' : 'no';"
        " return {...t, response, correct: L.isCorrect(t, response), rt_ms: 600,"
        " practice: false, block: 0, trial: i, status: 'done', scored: true}; });"
        " return L.score(records);"
    )
    assert [row["k"] for row in s["by_load"]] == [2, 4, 6]
    assert all(row["accuracy"] == 1 and row["median_rt_ms"] == 600 for row in s["by_load"])
    assert s["k"] == 6 and s["n_trials"] == 24 and s["no_response"] == 0


def test_a_record_without_scored_true_is_left_out(logic):
    # The same rule as the runner's scoredRecords: only `scored === true` counts. A record
    # with no `scored` field (or scored: false although status says done) is not scored.
    r = rec(2, True, "yes", 500)
    del r["scored"]
    assert score(logic, [r])["n_trials"] == 0
    r["scored"] = False
    assert score(logic, [r])["n_trials"] == 0
    r["scored"] = True
    assert score(logic, [r])["n_trials"] == 1


def test_the_two_blocks_of_the_default_flow_each_get_their_own_balanced_list(logic):
    step = next(s for s in flow.default_flow() if s.id == "spatial")
    assert [(b.id, b.seconds) for b in step.blocks] == [
        ("block1", 60.0), ("block2", 60.0),
    ]
    ids = [b.id for b in step.blocks]
    # Built as the runner builds them: one stream per block, rng(seed, "spatial", block, "trials").
    sessions = logic(
        "const { rng } = await import('/static/js/tasks/rng.js');"
        " return arg.seeds.map((s) => arg.ids.map((b) => L.blockTrials(rng(s, 'spatial', b, 'trials'))));",
        {"seeds": list(SEEDS), "ids": ids},
    )
    combos = {c: 2 for c in itertools.product((2, 4, 6), (True, False))}
    for blocks_ in sessions:
        assert len({repr(b) for b in blocks_}) == 2  # each block its own list
        pooled = Counter()
        for trials in blocks_:
            assert len(trials) == 24
            for t in trials:
                check_trial(t)
            for start in (0, 12):
                assert Counter((t["load"], t["probe_lit"]) for t in trials[start:start + 12]) == combos
            pooled.update((t["load"], t["probe_lit"]) for t in trials)
        # Over the two blocks: 48 trials, 16 per load, the probe lit in 8 of each load.
        assert pooled == {c: 8 for c in combos}


def test_practice_starts_from_its_first_trial_again_after_a_restart(logic):
    # The runner's trial index goes on after practice is stopped (full screen closed) and
    # started again; the practice list must start again from its first trial each time.
    # Calls are (index, number of done trials in history) as the runner makes them.
    got = logic(
        "const next = L.practicePicker(['a', 'b', 'c']);"
        " const h = (n) => Array.from({ length: n }, () => ({}));"
        " const out = [];"
        " out.push(next({ index: 0, history: h(0) }));"  # a
        " out.push(next({ index: 1, history: h(1) }));"  # b (0 was done) ... then abandoned
        " out.push(next({ index: 2, history: h(1) }));"  # restart: a
        " out.push(next({ index: 3, history: h(2) }));"  # b
        " out.push(next({ index: 4, history: h(3) }));"  # c
        " out.push(next({ index: 5, history: h(4) }));"  # past the list: null
        " const fresh = L.practicePicker(['a', 'b', 'c']);"  # a reload: a new picker
        " out.push(fresh({ index: 7, history: h(4) }));"  # a
        " return out;"
    )
    assert got == ["a", "b", "a", "b", "c", None, "a"]

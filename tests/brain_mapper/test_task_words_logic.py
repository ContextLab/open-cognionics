"""The pure logic of the Words and Delayed recall rounds, run in a real Chromium.

``web/js/tasks/words-logic.js`` is loaded from the real server's static files
(the server runs on a pseudo-terminal device) and called through
``page.evaluate``. The seeded generator handed to it is mulberry32 with the four
methods the task framework's generator has (``int``, ``float``, ``pick``,
``shuffle``): a real generator, defined below in the page. The word pool file
``web/assets/wordpool.txt`` is checked as a file and as the server serves it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "web"
POOL = WEB / "assets" / "wordpool.txt"
#: Words in the pool: every one of the 478 data lines of the noun file.
POOL_SIZE = 478
SEEDS = range(1, 301)

#: mulberry32 with the generator methods the logic takes, then the module, as ``window.L``.
SETUP = """async () => {
  window.makeRng = (seed) => {
    let a = seed >>> 0;
    const float = () => {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const int = (n) => Math.floor(float() * n);
    const pick = (array) => array[int(array.length)];
    const shuffle = (array) => {
      const out = array.slice();
      for (let i = out.length - 1; i > 0; i--) {
        const j = int(i + 1);
        [out[i], out[j]] = [out[j], out[i]];
      }
      return out;
    };
    return { int, float, pick, shuffle };
  };
  window.L = await import('/static/js/tasks/words-logic.js');
  window.POOL = window.L.parsePool(await (await fetch('/static/assets/wordpool.txt')).text());
  return true;
}"""

LIST1 = ["anchor", "apple", "bottle", "candle", "doctor", "engine",
         "forest", "garden", "hammer", "island", "jacket", "kettle"]
LIST2 = ["basket", "circle", "dragon", "feather", "guitar", "harbor",
         "ladder", "marble", "needle", "pepper", "rabbit", "saddle"]


@pytest.fixture
def logic(app, pty_device, page):
    """A page on the real server with the module loaded."""
    a = app(port=pty_device.path)
    page.goto(f"{a.url}/static/assets/wordpool.txt")
    assert page.evaluate(SETUP) is True
    return page


# -- the pool file -------------------------------------------------------------------


def test_the_pool_file_holds_distinct_lower_case_words_one_per_line():
    raw = POOL.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    words = raw.decode("ascii").split("\n")[:-1]
    assert len(words) == POOL_SIZE
    assert len(set(words)) == POOL_SIZE
    assert all(re.fullmatch(r"[a-z]+", w) for w in words), [
        w for w in words if not re.fullmatch(r"[a-z]+", w)
    ]


def test_parse_pool_reads_the_served_file_as_written(logic):
    served = logic.evaluate("window.POOL")
    assert served == POOL.read_text().split("\n")[:-1]
    # Blank lines, CR and surrounding spaces are dropped; case is lowered.
    assert logic.evaluate("L.parsePool('Apple\\r\\n\\n  kettle \\nZEBRA\\n')") == [
        "apple", "kettle", "zebra",
    ]
    assert logic.evaluate("L.parsePool('')") == []


# -- list construction ---------------------------------------------------------------


def test_lists_meet_every_constraint_over_300_seeds(logic):
    result = logic.evaluate(
        """(seeds) => {
          const before = POOL.slice();
          const out = seeds.map((s) => L.buildLists(POOL, makeRng(s)));
          return { out, unchanged: JSON.stringify(before) === JSON.stringify(POOL) };
        }""",
        list(SEEDS),
    )
    assert result["unchanged"], "buildLists changed the pool it was given"
    pool = set(POOL.read_text().split("\n")[:-1])
    for seed, lists in zip(SEEDS, result["out"]):
        assert len(lists) == 2, seed
        assert [len(x) for x in lists] == [12, 12], seed
        both = lists[0] + lists[1]
        assert len(set(both)) == 24, f"seed {seed}: a word twice: {both}"
        assert set(both) <= pool, seed
        assert all(w == w.lower() for w in both), seed
        for one in lists:
            prefixes = [w[:3] for w in one]
            assert len(set(prefixes)) == 12, f"seed {seed}: shared first three letters {one}"


def test_the_same_seed_gives_the_same_lists_and_different_seeds_differ(logic):
    result = logic.evaluate(
        """(seeds) => seeds.map((s) => [
             JSON.stringify(L.buildLists(POOL, makeRng(s))),
             JSON.stringify(L.buildLists(POOL, makeRng(s))),
           ])""",
        list(SEEDS),
    )
    for seed, (first, again) in zip(SEEDS, result):
        assert first == again, f"seed {seed} rebuilt differently"
    assert len({first for first, _ in result}) == len(SEEDS)


def test_build_lists_refuses_a_pool_that_cannot_fill_two_lists(logic):
    errors = logic.evaluate(
        """() => {
          const tryIt = (pool) => {
            try { L.buildLists(pool, makeRng(1)); return null; } catch (e) { return String(e.message); }
          };
          const short = POOL.slice(0, 23);
          // 44 words with only 11 different first three letters: no list can hold 12 words.
          const elevenPrefixes = [];
          for (let p = 0; p < 11; p++) for (let k = 0; k < 4; k++)
            elevenPrefixes.push('a' + String.fromCharCode(97 + p) + 'x' + String.fromCharCode(97 + k));
          return { empty: tryIt([]), short: tryIt(short), prefixes: tryIt(elevenPrefixes),
                   dupes: tryIt(POOL.slice(0, 12).concat(POOL.slice(0, 12))) };
        }"""
    )
    assert errors["empty"] and "24" in errors["empty"]
    assert errors["short"] and "24" in errors["short"]
    assert errors["prefixes"] and "12" in errors["prefixes"]
    # 24 entries but only 12 distinct words.
    assert errors["dupes"] and "24" in errors["dupes"]


# -- study timeline ------------------------------------------------------------------


def test_study_timing_constants_follow_the_cited_sources(logic):
    # docs/brain-mapper.md, Words, "Timing and its sources": PEERS Experiment 1 (3000 ms on,
    # 800-1200 ms jittered blank, 1200-1400 ms to the recall cue), Murdock (1962) 1.5 min recall.
    constants = logic.evaluate(
        "({f: L.FIXATION_MS, w: L.WORD_MS, lo: L.ISI_MIN_MS, hi: L.ISI_MAX_MS, clo: L.CUE_DELAY_MIN_MS,"
        " chi: L.CUE_DELAY_MAX_MS, r: L.RECALL_MS, d: L.DELAYED_RECALL_MS, max: L.STUDY_MAX_MS})"
    )
    assert constants == {"f": 2000, "w": 3000, "lo": 800, "hi": 1200, "clo": 1200, "chi": 1400,
                         "r": 90000, "d": 120000, "max": 2000 + 12 * 3000 + 11 * 1200 + 1400}


def jitter_key(jitter: dict) -> str:
    return ",".join(map(str, jitter["blanks_ms"])) + f"|{jitter['cue_delay_ms']}"


def test_jitter_is_drawn_from_the_generator_in_range_and_is_the_same_for_the_same_seed(logic):
    result = logic.evaluate(
        """(seeds) => seeds.map((s) => [L.drawJitter(makeRng(s)), L.drawJitter(makeRng(s))])""",
        list(SEEDS),
    )
    blanks_seen = set()
    for seed, (first, again) in zip(SEEDS, result):
        assert first == again, seed
        assert len(first["blanks_ms"]) == 11, seed
        assert all(isinstance(x, int) and 800 <= x <= 1200 for x in first["blanks_ms"]), seed
        assert isinstance(first["cue_delay_ms"], int) and 1200 <= first["cue_delay_ms"] <= 1400
        blanks_seen.update(first["blanks_ms"])
    # Uniform over whole ms: over 3300 draws both ends of the range turn up and most values do.
    assert min(blanks_seen) == 800 and max(blanks_seen) == 1200
    assert len(blanks_seen) > 350
    assert len({jitter_key(r[0]) for r in result}) == len(SEEDS)


def test_study_schedule_times_each_word_from_the_jitter(logic):
    jitter = {"blanks_ms": [800, 1200, 900, 1000, 1100, 850, 950, 1050, 1150, 801, 1199],
              "cue_delay_ms": 1300}
    schedule = logic.evaluate("([w, j]) => L.studySchedule(w, j)", [LIST1, jitter])
    onset = 2000
    expected = []
    for i, w in enumerate(LIST1):
        blank = jitter["blanks_ms"][i] if i < 11 else jitter["cue_delay_ms"]
        expected.append({"word": w, "position": i + 1, "onset_ms": onset, "offset_ms": onset + 3000,
                         "blank_ms": blank})
        onset += 3000 + blank
    assert schedule == expected
    # The cue: after the last word's offset and the cue delay.
    assert logic.evaluate("(j) => L.cueMs(j)", jitter) == schedule[-1]["offset_ms"] + 1300 == onset
    assert logic.evaluate("(j) => L.studySchedule([], j)", jitter) == []


def test_the_recall_clock_does_not_depend_on_how_long_study_took(logic):
    # The same 90 s from the cue, whether the cue came at 46.4 s or 52.6 s after the fixation, as
    # long as the block's end allows it; only the block's end can cut it (and says so).
    for start in (46400.0, 49000.0, 52600.0):
        assert logic.evaluate("(s) => L.recallEnd(s, 90000, 2000 + 145000, 250)", start) == {
            "end": start + 90000, "capped": False}
    assert logic.evaluate("L.recallEnd(56000, 90000, 145000, 250)") == {"end": 144750, "capped": True}
    assert logic.evaluate("L.recallEnd(1000, 90000, 500, 250)") == {"end": 1000, "capped": True}


# -- scoring -------------------------------------------------------------------------


def test_normalize_trims_and_lowers(logic):
    assert logic.evaluate("L.normalize('  ApPle \\t')") == "apple"
    assert logic.evaluate("L.normalize('')") == ""


def test_immediate_recall_hand_worked(logic):
    typed = ["Apple", "  kettle ", "ZEBRA", "apple", "Forest", "", "anchor"]
    s = logic.evaluate("([w, t]) => L.scoreRecall([{ list: 1, words: w }], t)", [LIST1, typed])
    # Apple -> apple (list 1, position 2); "  kettle " -> kettle (12); ZEBRA is not studied:
    # intrusion; apple again: repeat; Forest -> forest (7); "" is blank: no category;
    # anchor -> 1.
    assert s["typed"] == typed
    assert s["matched"] == [
        {"text": "Apple", "word": "apple", "list": 1, "position": 2},
        {"text": "  kettle ", "word": "kettle", "list": 1, "position": 12},
        {"text": "Forest", "word": "forest", "list": 1, "position": 7},
        {"text": "anchor", "word": "anchor", "list": 1, "position": 1},
    ]
    assert s["intrusions"] == ["ZEBRA"]
    assert s["repeats"] == ["apple"]
    # 4 of 12 recalled: 4 / 12 = 0.3333...
    [one] = s["lists"]
    assert one["list"] == 1 and one["studied"] == 12 and one["recalled"] == 4
    assert one["proportion"] == pytest.approx(4 / 12)
    # Positions 1, 2, 7 and 12 recalled.
    assert one["by_position"] == [1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    assert one["output_order"] == [2, 12, 7, 1]
    assert s["overall"] == {"studied": 12, "recalled": 4, "proportion": pytest.approx(4 / 12)}


def test_delayed_recall_over_both_lists_hand_worked(logic):
    typed = ["Dragon", "apple", "dragon ", "xylophone", "saddle", "APPLE", "pepper",
             "kettle", "Bottle"]
    s = logic.evaluate(
        "([a, b, t]) => L.scoreRecall([{ list: 1, words: a }, { list: 2, words: b }], t)",
        [LIST1, LIST2, typed],
    )
    # Dragon -> list 2 position 3; apple -> 1/2; "dragon " repeat; xylophone intrusion;
    # saddle -> 2/12; APPLE repeat; pepper -> 2/10; kettle -> 1/12; Bottle -> 1/3.
    assert [(m["word"], m["list"], m["position"]) for m in s["matched"]] == [
        ("dragon", 2, 3), ("apple", 1, 2), ("saddle", 2, 12), ("pepper", 2, 10),
        ("kettle", 1, 12), ("bottle", 1, 3),
    ]
    assert [m["text"] for m in s["matched"]] == ["Dragon", "apple", "saddle", "pepper",
                                                 "kettle", "Bottle"]
    assert s["intrusions"] == ["xylophone"]
    assert s["repeats"] == ["dragon ", "APPLE"]
    first, second = s["lists"]
    # List 1: apple, kettle, bottle -> 3 of 12 = 0.25; in output order positions 2, 12, 3.
    assert (first["list"], first["recalled"], first["output_order"]) == (1, 3, [2, 12, 3])
    assert first["proportion"] == pytest.approx(0.25)
    assert first["by_position"] == [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1]
    # List 2: dragon, saddle, pepper -> 3 of 12 = 0.25; positions 3, 12, 10.
    assert (second["list"], second["recalled"], second["output_order"]) == (2, 3, [3, 12, 10])
    assert second["proportion"] == pytest.approx(0.25)
    assert second["by_position"] == [0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1]
    # Overall: 6 of 24 = 0.25.
    assert s["overall"] == {"studied": 24, "recalled": 6, "proportion": pytest.approx(0.25)}


def test_a_list_two_word_in_list_one_recall_is_an_intrusion(logic):
    # Immediate recall scores against the list just studied only.
    s = logic.evaluate(
        "([w, t]) => L.scoreRecall([{ list: 2, words: w }], t)", [LIST2, ["apple", "basket"]]
    )
    assert s["intrusions"] == ["apple"]
    assert [(m["word"], m["list"], m["position"]) for m in s["matched"]] == [("basket", 2, 1)]


def test_scoring_edge_cases(logic):
    # No responses: nothing recalled, 0 / 12 = 0.
    none = logic.evaluate("(w) => L.scoreRecall([{ list: 1, words: w }], [])", LIST1)
    assert none["matched"] == [] and none["intrusions"] == [] and none["repeats"] == []
    assert none["lists"][0]["recalled"] == 0 and none["lists"][0]["proportion"] == 0
    assert none["lists"][0]["by_position"] == [0] * 12
    assert none["lists"][0]["output_order"] == []
    assert none["overall"] == {"studied": 12, "recalled": 0, "proportion": 0}
    # All misses: every entry an intrusion, an intrusion typed twice is an intrusion twice.
    miss = logic.evaluate(
        "([w, t]) => L.scoreRecall([{ list: 1, words: w }], t)",
        [LIST1, ["zebra", "Zebra", "  ", "tiger"]],
    )
    assert miss["intrusions"] == ["zebra", "Zebra", "tiger"]
    assert miss["repeats"] == [] and miss["matched"] == []
    assert miss["overall"]["recalled"] == 0
    # Empty studied list: everything typed is an intrusion, proportion 0 (not a division by 0).
    empty = logic.evaluate("() => L.scoreRecall([{ list: 1, words: [] }], ['apple'])")
    assert empty["intrusions"] == ["apple"]
    assert empty["lists"][0] == {"list": 1, "studied": 0, "recalled": 0, "proportion": 0,
                                 "by_position": [], "output_order": []}
    assert empty["overall"] == {"studied": 0, "recalled": 0, "proportion": 0}
    # No lists at all.
    nolists = logic.evaluate("() => L.scoreRecall([], ['apple'])")
    assert nolists["lists"] == [] and nolists["intrusions"] == ["apple"]
    assert nolists["overall"] == {"studied": 0, "recalled": 0, "proportion": 0}


def test_an_intrusion_from_the_other_studied_list_is_tagged(logic):
    # List 2 recall, list 1 studied before it: "Apple" is list 1 position 2, "zebra" is no word.
    s = logic.evaluate(
        "([a, b, t]) => L.scoreRecall([{ list: 2, words: b }], t, [{ list: 1, words: a }])",
        [LIST1, LIST2, ["basket", "Apple", "zebra", "apple"]],
    )
    # The existing fields are as without the third argument.
    assert s["intrusions"] == ["Apple", "zebra", "apple"]
    assert [(m["word"], m["list"], m["position"]) for m in s["matched"]] == [("basket", 2, 1)]
    assert s["intrusions_tagged"] == [
        {"text": "Apple", "other_list": True, "word": "apple", "list": 1, "position": 2},
        {"text": "zebra", "other_list": False},
        {"text": "apple", "other_list": True, "word": "apple", "list": 1, "position": 2},
    ]
    # Without other lists every intrusion is untagged.
    plain = logic.evaluate(
        "([b, t]) => L.scoreRecall([{ list: 2, words: b }], t)", [LIST2, ["apple"]]
    )
    assert plain["intrusions_tagged"] == [{"text": "apple", "other_list": False}]

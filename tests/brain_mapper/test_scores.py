"""The scores of a session (``brain_mapper.scores``): earned of possible per task, from the trial events.

Every log here is worked by hand: the comment beside each trial says whether it counts and whether
it earns. The same records are then given to each task's own scoring function in
``web/js/tasks/<id>-logic.js``, loaded from the real server in a real Chromium, and the numbers that
function gives must be the ones the server's scores give (the JS definition of "correct" and of the
trials left out is the one the server follows). Real files are written and read for the folder,
``behavior/scores.json`` and the re-scoring of a folder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain_mapper import package, scores

LIST1 = ["anchor", "apple", "bottle", "candle", "doctor", "engine",
         "forest", "garden", "hammer", "island", "jacket", "kettle"]
LIST2 = ["basket", "circle", "dragon", "feather", "guitar", "harbor",
         "ladder", "marble", "needle", "pepper", "rabbit", "saddle"]


def trial(task: str, step: str | None = None, **payload) -> dict:
    """One ``<task>.trial`` event line as the engine writes it (the fields scoring reads, and more)."""
    base = {"block": "block1", "trial": 0, "practice": False, "status": "done", "scored": True}
    base.update(payload)
    return {"seq": 0, "step": step or task, "block": base["block"], "name": f"{task}.trial", "payload": base}


def done(**kw) -> dict:
    return {"status": "done", "scored": True, "practice": False, **kw}


PRACTICE = {"status": "done", "scored": False, "practice": True}
CUT = {"status": "cut_short", "scored": False}
ABANDONED = {"status": "abandoned", "scored": False, "reason": "paused"}

#: perception: 2 earned of 4 presented.
PERCEPTION = [
    {**PRACTICE, "correct": True, "response": "left", "rt_ms": 400},  # practice: not counted
    done(correct=True, response="left", rt_ms=500, tilt_deg=4, reversal=False, staircase_trial=1),  # earns
    done(correct=False, response="right", rt_ms=610, tilt_deg=4, reversal=True, staircase_trial=2),  # wrong
    done(correct=False, response=None, rt_ms=None, tilt_deg=6, reversal=False, staircase_trial=3),  # no response
    {**CUT, "correct": True, "response": "left", "rt_ms": 450},  # cut short: not counted
    {**ABANDONED, "correct": None, "response": None, "rt_ms": None},  # abandoned: not counted
    done(correct=True, response="right", rt_ms=530, tilt_deg=6, reversal=False, staircase_trial=4),  # earns
]

#: flanker: 1 earned of 3 presented.
FLANKER = [
    {**PRACTICE, "correct": True, "congruent": True, "response": "left", "rt_ms": 420},  # practice
    done(correct=True, congruent=True, target="left", response="left", rt_ms=480),  # earns
    done(correct=False, congruent=False, target="right", response="left", rt_ms=520),  # wrong
    done(correct=False, congruent=True, target="left", response=None, rt_ms=None),  # no response
    {**CUT, "correct": True, "congruent": False, "response": "right", "rt_ms": 300},  # cut short
    {"status": "error", "scored": False, "practice": False, "correct": None},  # run threw: not counted
]

#: spatial: 2 earned of 4 presented.
SPATIAL = [
    {**PRACTICE, "correct": True, "load": 2, "probe_lit": True, "response": "yes", "rt_ms": 700},  # practice
    done(correct=True, load=2, probe_lit=True, response="yes", rt_ms=650),  # earns
    done(correct=True, load=4, probe_lit=False, response="no", rt_ms=800),  # earns
    done(correct=False, load=6, probe_lit=True, response="no", rt_ms=900),  # wrong
    done(correct=False, load=4, probe_lit=False, response=None, rt_ms=None),  # no response
    {**ABANDONED, "correct": None, "load": 2, "probe_lit": True, "response": None, "rt_ms": None},  # abandoned
]

#: 2-back: 2 earned of 5 presented (the answerable letters of the JS score).
NBACK = [
    {**PRACTICE, "correct": True, "needs_response": True, "target": True, "lure": None,
     "response": "same", "rt_ms": 500, "tail": False, "after_resume": False, "window_ms": 1700},  # practice
    done(correct=None, needs_response=False, target=False, lure=None, response=None, rt_ms=None,
         tail=False, after_resume=False, window_ms=2400),  # first letter: needs no answer
    done(correct=True, needs_response=True, target=True, lure=None, response="same", rt_ms=700,
         tail=False, after_resume=False, window_ms=2400),  # hit: earns
    done(correct=False, needs_response=True, target=True, lure=None, response=None, rt_ms=None,
         tail=False, after_resume=False, window_ms=2400),  # miss (no response)
    done(correct=True, needs_response=True, target=False, lure=None, response="different", rt_ms=640,
         tail=False, after_resume=False, window_ms=2400),  # correct rejection: earns
    done(correct=False, needs_response=True, target=False, lure="1-back", response=None, rt_ms=None,
         tail=False, after_resume=False, window_ms=2400),  # non-target, no response
    done(correct=False, needs_response=True, target=False, lure="3-back", response="same", rt_ms=590,
         tail=False, after_resume=False, window_ms=2400),  # false alarm
    done(correct=True, needs_response=True, target=False, lure=None, response="different", rt_ms=600,
         tail=True, after_resume=False, window_ms=2400),  # tail letter: not counted
    done(correct=None, needs_response=False, target=False, lure=None, response=None, rt_ms=None,
         tail=False, after_resume=True, window_ms=2400),  # after a resume: not counted
    done(correct=True, needs_response=True, target=True, lure=None, response="same", rt_ms=500,
         tail=False, after_resume=False, window_ms=1800),  # a short window: not counted
    {**CUT, "correct": True, "needs_response": True, "target": True, "lure": None, "response": "same",
     "rt_ms": 400, "tail": False, "after_resume": False, "window_ms": 2400},  # cut short
]

#: words: list 1 gives 3 of 12, list 2 gives 2 of 12; an abandoned segment of list 2 counts nothing.
WORDS = [
    done(block="list1", list=1, words=LIST1,
         typed=["Anchor", " apple ", "apple", "APPLE", "zebra", "basket", " forest　", "anchors"]),
    {**ABANDONED, "block": "list2", "list": 2, "correct": None},  # paused: the run had no result
    done(block="list2", list=2, words=LIST2, typed=["basket", "kettle", "Rabbit", "rabbit ", "hammer"]),
]

#: delayed recall: 3 of 24.
RECALL = [
    done(block="recall", lists=[LIST1, LIST2], typed=["kettle", "Saddle", "saddle", "unicorn", "  needle", "﻿zebra"]),
]

EXPECTED = {
    "perception": {"earned": 2, "possible": 4, "trials": 4, "rule": "scored_trials_correct"},
    "flanker": {"earned": 1, "possible": 3, "trials": 3, "rule": "scored_trials_correct"},
    "spatial": {"earned": 2, "possible": 4, "trials": 4, "rule": "scored_trials_correct"},
    "nback": {"earned": 2, "possible": 5, "trials": 5, "rule": "nback_answerable_correct"},
    "words": {"earned": 5, "possible": 24, "trials": 2, "rule": "words_list_recalled"},
    "recall": {"earned": 3, "possible": 24, "trials": 1, "rule": "recall_both_lists_recalled"},
}


def session_events() -> list[dict]:
    """The whole hand-worked log, in an order a session gives, with other events between."""
    out: list[dict] = [{"seq": 0, "step": "words", "block": None, "name": "step_start", "payload": {}}]
    for task, payloads in (("words", WORDS), ("perception", PERCEPTION), ("nback", NBACK),
                           ("flanker", FLANKER), ("spatial", SPATIAL), ("recall", RECALL)):
        for i, p in enumerate(payloads):
            out.append({"seq": 0, "step": task, "block": p.get("block", "block1"), "name": f"{task}.stim",
                        "payload": {"part": "stim", "trial": i}})
            out.append(trial(task, trial=i, **p))
            if task == "words":
                out.append({"seq": 0, "step": task, "block": "list1", "name": "words.resp",
                            "payload": {"text": "doctor", "list": 1}})  # an entry event: not a score
    out.append({"seq": 0, "step": "video", "block": "video", "name": "example.trial",
                "payload": done(correct=True)})  # a task without a rule: no score
    out.append({"seq": 0, "step": "nback", "block": "block1", "name": "nback.trial", "payload": "not an object"})
    for seq, event in enumerate(out, start=1):
        event["seq"] = seq
    return out


def test_each_task_of_a_hand_worked_log_scores_as_worked_out():
    result = scores.score_events(session_events())
    assert result["tasks"] == EXPECTED
    assert result["total"] == {
        "earned": sum(t["earned"] for t in EXPECTED.values()),
        "possible": sum(t["possible"] for t in EXPECTED.values()),
    } == {"earned": 15, "possible": 64}


@pytest.mark.parametrize("task", sorted(EXPECTED))
def test_a_task_alone_and_the_scorer_fed_one_event_at_a_time_agree(task):
    events = [e for e in session_events() if e["name"].startswith(f"{task}.")]
    alone = scores.score_events(events)
    assert alone["tasks"] == {task: EXPECTED[task]}
    assert alone["total"] == {"earned": EXPECTED[task]["earned"], "possible": EXPECTED[task]["possible"]}
    scorer = scores.Scorer()
    seen = []
    for event in events:
        scorer.add(event)
        seen.append(scorer.result()["tasks"].get(task, {}).get("possible", 0))
    assert scorer.result() == alone
    assert seen == sorted(seen)  # the possible points only ever grow as the log grows


def test_a_task_with_only_practice_cut_short_or_abandoned_trials_has_nothing_possible():
    events = [trial("flanker", **{**PRACTICE, "correct": True}), trial("flanker", **{**CUT, "correct": True}),
              trial("flanker", **{**ABANDONED, "correct": None})]
    result = scores.score_events(events)
    assert result["tasks"] == {"flanker": {"earned": 0, "possible": 0, "trials": 0, "rule": "scored_trials_correct"}}
    assert result["total"] == {"earned": 0, "possible": 0}
    assert scores.score_events([]) == {"tasks": {}, "total": {"earned": 0, "possible": 0}}


def test_a_words_trial_without_its_lists_is_not_counted():
    events = [trial("words", block="list1", list=1, typed=["anchor"]),  # no `words`
              trial("words", block="list2", list=2, words="anchor", typed=["anchor"]),  # not a list
              trial("recall", block="recall", lists=[LIST1, LIST2])]  # no `typed`
    result = scores.score_events(events)
    assert result["tasks"]["words"] == {"earned": 0, "possible": 0, "trials": 0, "rule": "words_list_recalled"}
    assert result["tasks"]["recall"]["possible"] == 0


def test_the_rules_are_written_in_the_docs():
    docs = (Path(__file__).resolve().parents[2] / "docs" / "brain-mapper.md").read_text()
    for rule in set(scores.RULES.values()):
        assert f"`{rule}`" in docs, rule


# -- files ---------------------------------------------------------------------------------------


def write_folder(folder: Path, events: list[dict], bad_line: bool = True) -> None:
    folder.mkdir(parents=True)
    lines = [json.dumps(e) for e in events]
    if bad_line:
        lines.insert(3, '{"seq": 999, "name": "flanker.trial", "payl')  # a line cut off by a crash
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n")


def test_a_folder_is_scored_from_its_event_log_alone_and_written_to_behavior_scores_json(tmp_path):
    folder = tmp_path / "P001_20261125_190000"
    write_folder(folder, session_events())
    from_folder = scores.score_folder(folder)
    assert from_folder == scores.score_events(session_events())
    path = scores.write(folder)
    assert path == folder / "behavior" / "scores.json"
    assert json.loads(path.read_text()) == from_folder
    # Scoring changes nothing else in the folder.
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "behavior/scores.json", "events.jsonl"]


def test_a_folder_without_an_event_log_scores_nothing(tmp_path):
    (tmp_path / "s").mkdir()
    assert scores.score_folder(tmp_path / "s") == {"tasks": {}, "total": {"earned": 0, "possible": 0}}


def test_the_hook_runs_before_every_zip_after_the_behaviour_files():
    assert scores.write_before_zip in package.BEFORE_ZIP
    assert package.BEFORE_ZIP.index(scores.write_before_zip) > package.BEFORE_ZIP.index(package.behavior.before_zip)


# -- the same records through each task's own JS scoring function --------------------------------

JS = """async (data) => {
  const P = await import('/static/js/tasks/perception-logic.js');
  const F = await import('/static/js/tasks/flanker-logic.js');
  const S = await import('/static/js/tasks/spatial-logic.js');
  const N = await import('/static/js/tasks/nback-logic.js');
  const W = await import('/static/js/tasks/words-logic.js');
  const out = {};
  const p = P.scorePerception(data.perception);
  out.perception = { possible: p.n_trials, earned: p.n_trials ? Math.round(p.accuracy * p.n_trials) : 0 };
  const f = F.score(data.flanker);
  out.flanker = { possible: f.n_trials, earned: f.congruent.n_correct + f.incongruent.n_correct };
  const s = S.score(data.spatial);
  out.spatial = { possible: s.n_trials,
                  earned: s.by_load.reduce((n, r) => n + (r.n_trials ? Math.round(r.accuracy * r.n_trials) : 0), 0) };
  const n = N.score(data.nback);
  out.nback = { possible: n.targets + n.non_targets, earned: n.hits + n.correct_rejections };
  // Words: each scored list, as words.js scores it (the list just studied).
  let earned = 0, possible = 0;
  for (const r of data.words.filter((r) => r.scored === true)) {
    const w = W.scoreRecall([{ list: r.list, words: r.words }], r.typed);
    earned += w.overall.recalled; possible += w.overall.studied;
  }
  out.words = { earned, possible };
  earned = 0; possible = 0;
  for (const r of data.recall.filter((r) => r.scored === true)) {
    const w = W.scoreRecall(r.lists.map((words, i) => ({ list: i + 1, words })), r.typed);
    earned += w.overall.recalled; possible += w.overall.studied;
  }
  out.recall = { earned, possible };
  return out;
}"""


def test_each_tasks_own_js_scoring_function_gives_the_same_numbers(app, pty_device, page):
    a = app(port=pty_device.path)
    page.goto(f"{a.url}/static/assets/wordpool.txt")
    data = {"perception": PERCEPTION, "flanker": FLANKER, "spatial": SPATIAL, "nback": NBACK,
            "words": WORDS, "recall": RECALL}
    from_js = page.evaluate(JS, data)
    ours = scores.score_events(session_events())["tasks"]
    assert from_js == {task: {"earned": ours[task]["earned"], "possible": ours[task]["possible"]} for task in from_js}
    assert set(from_js) == set(EXPECTED)


@pytest.mark.parametrize("text", ["﻿apple", "\u0085apple", "\u001capple", " apple ", "\tAPPLE　",
                                  "​apple", "᠎apple"])
def test_an_entry_is_trimmed_and_lowered_as_the_js_normalize_does(app, pty_device, page, text):
    a = app(port=pty_device.path)
    page.goto(f"{a.url}/static/assets/wordpool.txt")
    js = page.evaluate(
        "async (t) => (await import('/static/js/tasks/words-logic.js')).normalize(t)", text
    )
    assert scores.normalize(text) == js

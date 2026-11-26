// Flanker task: pure logic (trial lists, correctness, feedback, scoring).
//
// No DOM, no globals, no clock and no randomness of its own: every random draw comes from the
// generator passed in, an object with int(n) (0..n-1), pick(array) and shuffle(array) (a new
// array). The same generator state gives the same list. Scoring keeps only the runner's scored
// records (`scoredRecords`).

import { scoredRecords } from "./runner.js";

/** Timing of one block and one trial, in milliseconds. */
export const TIMING = Object.freeze({
  block_ms: 50000, // each block lasts 50 s (the flow: 2 x 50 s)
  fixation_ms: 500, // fixation cross before the arrows
  answer_ms: 1500, // the arrows stay this long, the fixed time to answer (the last press counts)
  gap_min_ms: 700, // blank gap after the feedback, uniform over gap_min_ms..gap_max_ms
  gap_max_ms: 1100,
  feedback_ms: 500, // "Correct" / "Incorrect" / "No response" after every trial (FEEDBACK_MS)
  lead_in_ms: 2000, // the fixation cross after Enter, before the first trial (LEAD_IN_MS)
  anticipation_ms: 150, // RTs under this are anticipations, left out of RT measures
});

/** The fixed length of a trial: cross, arrows (the time to answer), feedback, gap: 3200-3600 ms. */
export function trialMs(trial) {
  return trial.fixation_ms + TIMING.answer_ms + TIMING.feedback_ms + trial.gap_ms;
}

/** Number of blocks. */
export const BLOCKS = 2;
/** Trials generated per block: 5 balanced runs of 8, more than fit in 50 s (after the 2 s lead-in,
 *  13 or 14 trials of 3200-3600 ms fit); a trial that would not end in time is not started. */
export const BLOCK_TRIALS = 40;
/** Practice trials before the first block (3 congruent, 3 incongruent). */
export const PRACTICE_TRIALS = 6;
/** Length of a balanced run: 2 of each of the 4 (congruent, target) combinations. */
export const RUN_LENGTH = 8;
/** Longest allowed series of identical trials (same congruent and same target). */
export const MAX_IDENTICAL = 3;

const LEFT = "left";
const RIGHT = "right";
const MAX_ATTEMPTS = 1000;

const opposite = (dir) => (dir === LEFT ? RIGHT : LEFT);
const same = (a, b) => a.congruent === b.congruent && a.target === b.target;

/** True when no more than MAX_IDENTICAL identical trials follow each other in `list`. */
function runsOk(list) {
  let n = 0;
  for (let i = 0; i < list.length; i++) {
    n = i > 0 && same(list[i], list[i - 1]) ? n + 1 : 1;
    if (n > MAX_IDENTICAL) return false;
  }
  return true;
}

/**
 * Shuffles `items` and appends them to `before` (a new array is returned), drawing shuffles
 * until no more than MAX_IDENTICAL identical trials follow each other, across the joint too.
 * @param {{shuffle: function(Array): Array}} rng the seeded generator
 * @param {Array<{congruent: boolean, target: string}>} before trials already placed
 * @param {Array<{congruent: boolean, target: string}>} items trials to place, in any order
 * @returns {Array<{congruent: boolean, target: string}>}
 */
function appendShuffled(rng, before, items) {
  const tail = before.slice(-MAX_IDENTICAL);
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const order = rng.shuffle(items);
    if (runsOk(tail.concat(order))) return before.concat(order);
  }
  throw new Error("flanker: no trial order meets the run limit (generator not random?)");
}

/**
 * Builds one trial from its condition, drawing the gap from the generator.
 * @param {{int: function(number): number}} rng the seeded generator
 * @param {boolean} congruent true when the four outer arrows point the same way as the middle one
 * @param {"left"|"right"} target direction of the middle arrow (the right answer)
 * @returns {{congruent: boolean, target: "left"|"right", flankers: "left"|"right",
 *   fixation_ms: number, gap_ms: number}}
 */
export function makeTrial(rng, congruent, target) {
  return {
    congruent,
    target,
    flankers: congruent ? target : opposite(target),
    fixation_ms: TIMING.fixation_ms,
    gap_ms: TIMING.gap_min_ms + rng.int(TIMING.gap_max_ms - TIMING.gap_min_ms + 1),
  };
}

/**
 * The trial list of one scored block: BLOCK_TRIALS (40) trials. Trials 0-7, 8-15, ... are each a
 * shuffled run of 8 holding 2 of each (congruent, target) combination (a list that is not a
 * multiple of 8 would end with part of a run). Never more than 3 identical trials in a row,
 * across runs too. Each gap is a whole number of ms drawn uniformly from 700..1100. Draw order:
 * all run shuffles first, then one gap per trial in list order.
 * @param {{int: function(number): number, shuffle: function(Array): Array}} rng the seeded
 *   generator for this block (stream "flanker", block index)
 * @returns {Array<{congruent: boolean, target: "left"|"right", flankers: "left"|"right",
 *   fixation_ms: number, gap_ms: number}>}
 */
export function blockTrials(rng) {
  const run = [];
  for (const congruent of [true, false]) {
    for (const target of [LEFT, RIGHT]) {
      for (let k = 0; k < RUN_LENGTH / 4; k++) run.push({ congruent, target });
    }
  }
  let order = [];
  while (order.length < BLOCK_TRIALS) order = appendShuffled(rng, order, run);
  return order.slice(0, BLOCK_TRIALS).map((c) => makeTrial(rng, c.congruent, c.target));
}

/**
 * The practice list: PRACTICE_TRIALS (6) trials, 3 congruent and 3 incongruent, 3 with the middle
 * arrow left and 3 right (each condition has one left, one right and one drawn; the drawn one is
 * opposite between the two conditions), shuffled, never more than 3 identical in a row. Draw order:
 * the pick, the shuffle(s), then one gap per trial.
 * @param {{int: function(number): number, pick: function(Array): *,
 *   shuffle: function(Array): Array}} rng the seeded generator for practice
 * @returns {Array<{congruent: boolean, target: "left"|"right", flankers: "left"|"right",
 *   fixation_ms: number, gap_ms: number}>}
 */
export function practiceTrials(rng) {
  const extra = rng.pick([LEFT, RIGHT]);
  const items = [
    { congruent: true, target: LEFT },
    { congruent: true, target: RIGHT },
    { congruent: true, target: extra },
    { congruent: false, target: LEFT },
    { congruent: false, target: RIGHT },
    { congruent: false, target: opposite(extra) },
  ];
  return appendShuffled(rng, [], items).map((c) => makeTrial(rng, c.congruent, c.target));
}

/**
 * Whether a response is right: it must name the direction of the middle arrow. No response
 * (null) is a miss and not correct.
 * @param {{target: "left"|"right"}} trial
 * @param {"left"|"right"|null} response
 * @returns {boolean}
 */
export function isCorrect(trial, response) {
  return response !== null && response === trial.target;
}

/**
 * Practice feedback text: "Correct", "Incorrect", or "No response" when there was no response (the
 * words of every task: `FEEDBACK` in runner.js).
 * @param {{target: "left"|"right"}} trial
 * @param {"left"|"right"|null} response
 * @returns {"Correct"|"Incorrect"|"No response"}
 */
export function feedbackFor(trial, response) {
  if (response === null) return "No response";
  return isCorrect(trial, response) ? "Correct" : "Incorrect";
}

/**
 * Median of a list of numbers (mean of the middle two for an even count); null when empty.
 * @param {number[]} values
 * @returns {number|null}
 */
export function median(values) {
  if (values.length === 0) return null;
  const s = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** Whether a record's RT is an anticipation (a response under 150 ms). */
function isAnticipation(r) {
  return r.rt_ms !== null && r.rt_ms !== undefined && r.rt_ms < TIMING.anticipation_ms;
}

/** Accuracy and median correct RT of one condition's records. */
function condition(records) {
  const nCorrect = records.filter((r) => r.correct === true).length;
  const rts = records
    .filter((r) => r.correct === true && r.rt_ms !== null && !isAnticipation(r))
    .map((r) => r.rt_ms);
  return {
    n: records.length,
    n_correct: nCorrect,
    accuracy: records.length ? nCorrect / records.length : null,
    median_rt_ms: median(rts),
  };
}

/**
 * Scores a list of flanker trial records (the `flanker.trial` payloads). Only the records
 * `scoredRecords` keeps are counted (scored === true: a `done` trial that is not practice), so
 * practice, cut-short, abandoned and failed trials are left out.
 * Per condition (congruent, incongruent): n, n_correct, accuracy = n_correct / n (misses count as
 * not correct; null when n = 0), median_rt_ms = median RT of correct trials with RT >= 150 ms
 * (null when there are none). Costs are incongruent minus congruent: flanker_cost_rt_ms (ms) and
 * flanker_cost_accuracy (proportion), null when either side is null. n_trials = scored records,
 * n_misses = records with no response, n_anticipations = records with an RT under 150 ms
 * (right or wrong).
 * @param {Array<{congruent: boolean, correct: boolean, rt_ms: (number|null),
 *   response: (string|null), scored: boolean, practice?: boolean}>} records
 * @returns {{n_trials: number, n_misses: number, n_anticipations: number,
 *   congruent: {n: number, n_correct: number, accuracy: (number|null), median_rt_ms: (number|null)},
 *   incongruent: {n: number, n_correct: number, accuracy: (number|null),
 *     median_rt_ms: (number|null)},
 *   flanker_cost_rt_ms: (number|null), flanker_cost_accuracy: (number|null)}}
 */
export function score(records) {
  const scored = scoredRecords(records);
  const con = condition(scored.filter((r) => r.congruent === true));
  const inc = condition(scored.filter((r) => r.congruent === false));
  const diff = (a, b) => (a === null || b === null ? null : a - b);
  return {
    n_trials: scored.length,
    n_misses: scored.filter((r) => r.response === null || r.response === undefined).length,
    n_anticipations: scored.filter(isAnticipation).length,
    congruent: con,
    incongruent: inc,
    flanker_cost_rt_ms: diff(inc.median_rt_ms, con.median_rt_ms),
    flanker_cost_accuracy: diff(inc.accuracy, con.accuracy),
  };
}

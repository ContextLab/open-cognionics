// Perception (orientation discrimination with a staircase): pure functions.
//
// No DOM, no globals, no Math.random, no Date. Every random draw comes from the generator
// passed in (an object with int(n), float(), pick(array) and shuffle(array)).
//
// Sign convention: a tilt to the "right" is clockwise from vertical as seen on screen and has a
// positive signed tilt; "left" is counter-clockwise and negative. Key F answers "left", J "right".
import { scoredRecords } from './runner.js';

/** Fixation cross before each grating, ms. */
export const FIXATION_MS = 500;
/** Grating on screen, ms. */
export const STIMULUS_MS = 200;
/** The answer is taken until this long after grating onset, ms. */
export const RESPONSE_WINDOW_MS = 1500;
/** Shortest gap after a trial, ms (inclusive). */
export const GAP_MIN_MS = 600;
/** Longest gap after a trial, ms (inclusive). */
export const GAP_MAX_MS = 1000;
/** Right/wrong feedback in practice, ms. */
export const FEEDBACK_MS = 500;
/** Length of one block, ms. */
export const BLOCK_MS = 60000;
/**
 * Trials generated per block. A trial lasts at least 500 + 200 + 600 = 1300 ms, so at most
 * 47 can start in 60 s; the list is longer so it never runs out. Multiple of 8.
 */
export const TRIALS_PER_BLOCK = 56;
/** Practice trials before the first block. */
export const PRACTICE_TRIALS = 6;
/** Tilt of every practice trial, degrees. */
export const PRACTICE_TILT_DEG = 15;
/** Staircase start, degrees. */
export const START_TILT_DEG = 8.0;
/** Staircase step: the tilt is divided (harder) or multiplied (easier) by this. */
export const STEP_FACTOR = 1.26;
/** Smallest tilt, degrees. */
export const MIN_TILT_DEG = 0.2;
/** Largest tilt, degrees. */
export const MAX_TILT_DEG = 20;
/** Most trials with the same side in a row. */
export const MAX_RUN = 4;

const SIDES_OF_8 = ['left', 'left', 'left', 'left', 'right', 'right', 'right', 'right'];

function trailingRun(list) {
  let n = 0;
  for (let i = list.length - 1; i >= 0 && list[i] === list[list.length - 1]; i--) n++;
  return n;
}

function leadingRun(list) {
  let n = 0;
  while (n < list.length && list[n] === list[0]) n++;
  return n;
}

/**
 * Sides for `count` trials: each consecutive group of 8 holds 4 "left" and 4 "right" in shuffled
 * order, and no more than MAX_RUN of one side follow each other, also across groups (a group
 * that would make a longer run is shuffled again).
 * @param {{shuffle: function(Array): Array}} rng seeded generator
 * @param {number} count multiple of 8
 * @returns {Array<'left'|'right'>}
 */
export function makeSides(rng, count) {
  if (!Number.isInteger(count) || count < 0 || count % 8 !== 0) {
    throw new Error(`count must be a multiple of 8, got ${count}`);
  }
  const sides = [];
  while (sides.length < count) {
    let group = rng.shuffle(SIDES_OF_8);
    while (
      sides.length > 0 &&
      group[0] === sides[sides.length - 1] &&
      trailingRun(sides) + leadingRun(group) > MAX_RUN
    ) {
      group = rng.shuffle(SIDES_OF_8);
    }
    sides.push(...group);
  }
  return sides;
}

/** A gap drawn uniformly from the whole milliseconds GAP_MIN_MS..GAP_MAX_MS. */
function drawGap(rng) {
  return GAP_MIN_MS + rng.int(GAP_MAX_MS - GAP_MIN_MS + 1);
}

/** A grating phase drawn uniformly from [0, 360) degrees. */
function drawPhase(rng) {
  return rng.float() * 360;
}

/**
 * The trial list of one scored block. The tilt is not in the list: it comes from the staircase
 * when the trial is shown.
 * @param {{int: function(number): number, float: function(): number,
 *          shuffle: function(Array): Array}} rng seeded generator for this block
 * @param {number} [count=TRIALS_PER_BLOCK] number of trials, a multiple of 8
 * @returns {Array<{side: 'left'|'right', phase_deg: number, fixation_ms: number,
 *                  gap_ms: number}>}
 */
export function makeBlockTrials(rng, count = TRIALS_PER_BLOCK) {
  const sides = makeSides(rng, count);
  return sides.map((side) => ({
    side,
    phase_deg: drawPhase(rng),
    fixation_ms: FIXATION_MS,
    gap_ms: drawGap(rng),
  }));
}

/**
 * The practice trials: PRACTICE_TRIALS trials at PRACTICE_TILT_DEG, 3 left and 3 right in
 * shuffled order. Practice does not use or move the staircase.
 * @param {{int: function(number): number, float: function(): number,
 *          shuffle: function(Array): Array}} rng seeded generator for the practice
 * @returns {Array<{side: 'left'|'right', tilt_deg: number, phase_deg: number,
 *                  fixation_ms: number, gap_ms: number}>}
 */
export function makePracticeTrials(rng) {
  const half = PRACTICE_TRIALS / 2;
  const sides = rng.shuffle([...Array(half).fill('left'), ...Array(half).fill('right')]);
  return sides.map((side) => ({
    side,
    tilt_deg: PRACTICE_TILT_DEG,
    phase_deg: drawPhase(rng),
    fixation_ms: FIXATION_MS,
    gap_ms: drawGap(rng),
  }));
}

/**
 * The answer a key gives: "f"/"F" -> "left", "j"/"J" -> "right", anything else -> null.
 * @param {string|null} key KeyboardEvent.key
 * @returns {'left'|'right'|null}
 */
export function responseOf(key) {
  if (typeof key !== 'string') return null;
  const k = key.toLowerCase();
  if (k === 'f') return 'left';
  if (k === 'j') return 'right';
  return null;
}

/**
 * Whether an answer is correct. No answer (null) is a miss and not correct.
 * @param {'left'|'right'} side side of the tilt
 * @param {'left'|'right'|null} response
 * @returns {boolean}
 */
export function isCorrect(side, response) {
  return response !== null && response === side;
}

/**
 * The drawing angle of a trial: + is clockwise ("right"), - counter-clockwise ("left").
 * @param {'left'|'right'} side
 * @param {number} tiltDeg tilt size in degrees (> 0)
 * @returns {number}
 */
export function signedTilt(side, tiltDeg) {
  return side === 'left' ? -tiltDeg : tiltDeg;
}

/**
 * A new staircase, at START_TILT_DEG. It is carried on from block 1 into block 2.
 * @returns {{tilt_deg: number, correct_run: number, direction: ('up'|'down'|null),
 *            trials: number, reversals: number}}
 */
export function newStaircase() {
  return { tilt_deg: START_TILT_DEG, correct_run: 0, direction: null, trials: 0, reversals: 0 };
}

/**
 * One scored trial through the 2-down-1-up staircase. The trial was shown at `state.tilt_deg`.
 * Two correct in a row: the tilt is divided by STEP_FACTOR (step "down"). A wrong answer or a
 * miss: multiplied by STEP_FACTOR (step "up"). The tilt is held within MIN_TILT_DEG..MAX_TILT_DEG.
 * A trial is a reversal when it causes a step whose direction differs from the step before it
 * (the first step is never a reversal); the reversal's tilt is the tilt that trial was shown at.
 * The input state is not changed.
 * @param {{tilt_deg: number, correct_run: number, direction: ('up'|'down'|null),
 *          trials: number, reversals: number}} state
 * @param {boolean|null} correct true, false, or null for a miss (counts as wrong)
 * @returns {{tilt_deg: number, staircase_trial: number, reversal: boolean, state: object}}
 *   tilt_deg and staircase_trial (0-based, across blocks) of the trial just answered, whether it
 *   was a reversal, and the state for the next trial
 */
export function updateStaircase(state, correct) {
  let tilt = state.tilt_deg;
  let run = state.correct_run;
  let step = null;
  if (correct === true) {
    run += 1;
    if (run >= 2) {
      step = 'down';
      run = 0;
      tilt = Math.max(MIN_TILT_DEG, tilt / STEP_FACTOR);
    }
  } else {
    step = 'up';
    run = 0;
    tilt = Math.min(MAX_TILT_DEG, tilt * STEP_FACTOR);
  }
  const reversal = step !== null && state.direction !== null && step !== state.direction;
  return {
    tilt_deg: state.tilt_deg,
    staircase_trial: state.trials,
    reversal,
    state: {
      tilt_deg: tilt,
      correct_run: run,
      direction: step === null ? state.direction : step,
      trials: state.trials + 1,
      reversals: state.reversals + (reversal ? 1 : 0),
    },
  };
}

function median(values) {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 === 1 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function geometricMean(values) {
  return Math.exp(values.reduce((acc, v) => acc + Math.log(v), 0) / values.length);
}

/**
 * Scores the trial records of the task. Only the runner's scored records count
 * (`scoredRecords`: `scored === true`, a done trial that is not practice); practice, cut-short,
 * abandoned and failed trials enter no measure.
 * - Reversals: those of the scored records, in order of staircase_trial.
 * - threshold_deg: geometric mean, exp(mean(ln tilt)), of tilt_deg at the last 6 reversals; with
 *   2 to 5 reversals, of all but the first; with fewer than 2, null and threshold_reason says why.
 * - n_reversals: reversals among the scored records; n_trials: number of scored records.
 * - accuracy: correct / n_trials (a miss is not correct); null when n_trials is 0.
 * - median_rt_correct_ms: median rt_ms of correct scored trials; null when there are none.
 * @param {Array<{tilt_deg: number, correct: boolean, rt_ms: (number|null), reversal: boolean,
 *                staircase_trial: (number|null), practice: boolean, scored: boolean}>} records
 * @returns {{threshold_deg: (number|null), threshold_reason: (string|null), n_reversals: number,
 *            n_trials: number, accuracy: (number|null), median_rt_correct_ms: (number|null)}}
 */
export function scorePerception(records) {
  const scored = scoredRecords(records);
  const staircase = scored
    .filter((r) => typeof r.reversal === 'boolean')
    .sort((a, b) => a.staircase_trial - b.staircase_trial);
  const reversalTilts = staircase.filter((r) => r.reversal === true).map((r) => r.tilt_deg);
  let threshold = null;
  let reason = null;
  if (reversalTilts.length >= 6) {
    threshold = geometricMean(reversalTilts.slice(-6));
  } else if (reversalTilts.length >= 2) {
    threshold = geometricMean(reversalTilts.slice(1));
  } else {
    reason = `fewer than 2 reversals (${reversalTilts.length})`;
  }
  const correct = scored.filter((r) => r.correct === true);
  return {
    threshold_deg: threshold,
    threshold_reason: reason,
    n_reversals: reversalTilts.length,
    n_trials: scored.length,
    accuracy: scored.length === 0 ? null : correct.length / scored.length,
    median_rt_correct_ms: median(correct.filter((r) => r.rt_ms !== null).map((r) => r.rt_ms)),
  };
}

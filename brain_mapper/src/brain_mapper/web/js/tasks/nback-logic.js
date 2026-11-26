// Pure logic of the 2-back task (step id `nback`): letter lists, answers and scoring.
//
// No screen code, no clock, no randomness of its own: list generation takes the seeded
// generator as a parameter (an object with `int(n)`, `float()`, `pick(list)` and
// `shuffle(list)`; only `int` and `pick` are used here).
//
// A trial from the generator: {letter, position, needs_response, target, lure, tail}.
//   position        1, 2, ... within the list
//   needs_response  false for the first two letters, true from the third on
//   target          the letter is the same as the one two back
//   lure            null, "1-back" (same as the previous letter, not a target) or
//                   "3-back" (same as three back, not a target and not a 1-back repeat)
//   tail            true for the extra letters after the BLOCK_LETTERS of a block: they are
//                   shown only if the block runs longer than its letters (after a pause, for
//                   example), are plain non-targets, and are left out of the scores; more
//                   are made on demand (extendTail) from the same stream
// As shown (runTrial), a trial also has `after_resume`: true for the first two letters of a
// run that does not start at the block's first letter (after a pause or a reload). Those need
// no answer, and later letters are judged only against letters of their own run.
// Every non-target that is not a lure differs from each of the three letters before it,
// and the second letter differs from the first.

/** Letters shown, in upper case (consonants only). */
export const LETTERS = Object.freeze("BCDFGHJKLMNPQRSTVWXZ".split(""));

/**
 * Durations in milliseconds. One letter every 3200 ms: the letter for 500 ms, the answer window
 * of 2400 ms from its onset (the last F/J press in it counts), the feedback ("Correct",
 * "Incorrect", "No response") for 500 ms from the window's end, then 300 ms of the cross alone
 * before the next letter. The task starts on Enter, with a 2000 ms fixation cross before the
 * first letter.
 */
export const TIMING = Object.freeze({
  letterMs: 500,
  leadInMs: 2000,
  trialMs: 3200,
  responseWindowMs: 2400,
  feedbackMs: 500,
  gapMs: 300,
  blockMs: 50000,
});

/** Letters of a scored block: in the flow's 50 s blocks, after the 2 s lead-in, 14 letters of
 *  3200 ms fit (2000 + 14 x 3200 = 46 800 ms; a 15th would end at 50 000 ms, on the block's end,
 *  and is not started). */
export const BLOCK_LETTERS = 14;
/** Extra non-target letters after a block's letters, a guard against timing slack. */
export const TAIL_LETTERS = 4;
/** Letters in the practice list. */
export const PRACTICE_LETTERS = 10;
/** Targets in the practice list. */
export const PRACTICE_TARGETS = 3;
/** Share of answerable positions that are targets in a scored block. */
export const TARGET_SHARE = 0.3;
/** Share of the non-targets that are lures, for each of the two kinds. */
export const LURE_SHARE = 0.15;
/** Key (lower case) to answer: `J` same as two back, `F` different. */
export const KEYS = Object.freeze({ j: "same", f: "different" });

const MAX_ATTEMPTS = 5000;

/**
 * The label a position gets from the letters alone.
 * @param {string[]} letters the list so far
 * @param {number} i index (0-based) of the position
 * @returns {"target"|"1-back"|"3-back"|"none"}
 */
function labelAt(letters, i) {
  const x = letters[i];
  if (x === letters[i - 2]) return "target";
  const oneBack = x === letters[i - 1];
  const threeBack = i >= 3 && x === letters[i - 3];
  if (oneBack && !threeBack) return "1-back";
  if (threeBack && !oneBack) return "3-back";
  if (!oneBack && !threeBack) return "none";
  return "both";
}

/**
 * Whether letters can be made for this order of kinds (kinds[k] is the kind at index k + 2).
 * Each rule stops a letter from getting a second label or a third repeat:
 * a target or 1-back lure right after a 1-back lure (three in a row); a 1-back or 3-back lure
 * right after a target (it would be both lures); a 3-back lure two after a 1-back lure (it
 * would be a target); a 3-back lure at the third letter (nothing three back); more than 3
 * targets in a row.
 */
function allowedAt(kinds, kind) {
  const k = kinds.length;
  const prev = k >= 1 ? kinds[k - 1] : "none";
  const prev2 = k >= 2 ? kinds[k - 2] : "none";
  if (kind === "3-back" && (k === 0 || prev === "target" || prev2 === "1-back")) return false;
  if ((kind === "target" || kind === "1-back") && prev === "1-back") return false;
  if (kind === "1-back" && prev === "target") return false;
  if (kind === "target" && k >= 3 && kinds.slice(k - 3).every((x) => x === "target")) {
    return false;
  }
  return true;
}

/**
 * One try at an order of kinds: each next kind is drawn among those the rules allow, with
 * chances in proportion to how many of each are left. Null at a dead end.
 */
function drawOrder(gen, counts) {
  const left = { ...counts };
  const total = Object.values(left).reduce((a, b) => a + b, 0);
  const kinds = [];
  for (let k = 0; k < total; k += 1) {
    const options = KINDS.filter((kind) => left[kind] > 0 && allowedAt(kinds, kind));
    const weight = options.reduce((sum, kind) => sum + left[kind], 0);
    if (weight === 0) return null;
    let draw = gen.int(weight);
    let chosen = options[options.length - 1];
    for (const kind of options) {
      if (draw < left[kind]) {
        chosen = kind;
        break;
      }
      draw -= left[kind];
    }
    kinds.push(chosen);
    left[chosen] -= 1;
  }
  return kinds;
}

const KINDS = ["target", "1-back", "3-back", "none"];

/** Letters for an order of kinds; null if they do not come out as asked. */
function tryLetters(gen, kinds) {
  const first = gen.pick(LETTERS);
  const letters = [first, gen.pick(LETTERS.filter((x) => x !== first))];
  let targetRun = 0;
  for (let k = 0; k < kinds.length; k += 1) {
    const i = k + 2;
    const kind = kinds[k];
    if (kind === "target") letters.push(letters[i - 2]);
    else if (kind === "1-back") letters.push(letters[i - 1]);
    else if (kind === "3-back") {
      if (i < 3) return null;
      letters.push(letters[i - 3]);
    } else {
      const recent = letters.slice(Math.max(0, i - 3), i);
      letters.push(gen.pick(LETTERS.filter((x) => !recent.includes(x))));
    }
    if (labelAt(letters, i) !== kind) return null;
    if (letters[i] === letters[i - 1] && letters[i] === letters[i - 2]) return null;
    targetRun = kind === "target" ? targetRun + 1 : 0;
    if (targetRun > 3) return null;
  }
  return letters;
}

/**
 * A list of 2-back trials with exact counts of targets and lures.
 *
 * The kinds of the answerable positions (targets, 1-back lures, 3-back lures, others) are
 * drawn in order, each among the kinds the rules allow next, in proportion to how many are
 * left; letters are then made to fit (others drawn among letters unlike the three before).
 * A try that reaches a dead end, or whose letters break a rule (a label other than the one
 * asked, a letter three times in a row, more than 3 targets in a row), is drawn again from
 * the same generator, so the same generator state always gives the same list.
 *
 * @param {{int: function(number): number, pick: function(Array): *}} gen seeded generator
 * @param {{length: number, targets: number, lures: number}} counts list length, targets
 *   among the answerable positions, and lures of EACH kind (1-back and 3-back)
 * @returns {{letter: string, position: number, needs_response: boolean, target: boolean,
 *   lure: (null|"1-back"|"3-back"), tail: boolean}[]} (`tail` is false on all of them)
 * @throws {Error} if the counts cannot be met
 */
export function makeTrials(gen, { length, targets, lures }) {
  const answerable = length - 2;
  const others = answerable - targets - 2 * lures;
  if (answerable < 1 || targets < 0 || lures < 0 || others < 0) {
    throw new Error(`nback: cannot make ${length} letters with ${targets} targets`);
  }
  const counts = { target: targets, "1-back": lures, "3-back": lures, none: others };
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
    const order = drawOrder(gen, counts);
    if (order === null) continue;
    const letters = tryLetters(gen, order);
    if (letters === null) continue;
    return letters.map((letter, i) => ({
      letter,
      position: i + 1,
      needs_response: i >= 2,
      target: i >= 2 && order[i - 2] === "target",
      lure: i >= 2 && (order[i - 2] === "1-back" || order[i - 2] === "3-back") ? order[i - 2] : null,
      tail: false,
    }));
  }
  throw new Error("nback: no letter list met the rules");
}

/**
 * The list of one scored block: BLOCK_LETTERS (14) letters in which 30% of the 12
 * answerable positions are targets (round(3.6) = 4) and 15% of the 8 non-targets are lures
 * of each kind (round(1.2) = 1), then TAIL_LETTERS (4) plain non-targets flagged
 * `tail: true` (each unlike the three letters before it).
 * @param {{int: function(number): number, pick: function(Array): *}} gen the generator of
 *   this block's stream
 * @returns {object[]} trials as {@link makeTrials} gives them, then the tail
 */
export function blockTrials(gen) {
  const answerable = BLOCK_LETTERS - 2;
  const targets = Math.round(TARGET_SHARE * answerable);
  const lures = Math.round(LURE_SHARE * (answerable - targets));
  const trials = makeTrials(gen, { length: BLOCK_LETTERS, targets, lures });
  return extendTail(gen, trials, BLOCK_LETTERS + TAIL_LETTERS);
}

/**
 * A block list grown to `length` letters with tail letters: plain non-targets, each unlike
 * the three letters before it, flagged `tail: true`, drawn from `gen` in order. Growing in
 * several steps gives the same letters as growing at once, so the list rebuilt from the
 * block's stream after a reload is the same.
 * @param {{pick: function(Array): *}} gen the block's generator, after {@link blockTrials}
 * @param {object[]} trials the list so far (not changed)
 * @param {number} length the length wanted; a longer list is returned as it is (a copy)
 * @returns {object[]} the longer list
 */
export function extendTail(gen, trials, length) {
  const out = trials.slice();
  while (out.length < length) {
    const recent = out.slice(-3).map((t) => t.letter);
    const letter = gen.pick(LETTERS.filter((x) => !recent.includes(x)));
    out.push({
      letter,
      position: out.length + 1,
      needs_response: true,
      target: false,
      lure: null,
      tail: true,
    });
  }
  return out;
}

/**
 * Trial `index` of a list as shown in a run that started at `runStart` (0 for a block's
 * first run; the first index after a pause or a reload otherwise). The first two letters of a
 * run that starts later are `after_resume: true` and need no answer (not judged: `target`
 * false, `lure` null). A later letter is judged only against letters of its own run: its
 * target (two back) always is; a 3-back lure whose letter three back was before the run
 * becomes `lure: null`.
 * @param {object[]} trials the list
 * @param {number} index the trial's index in the list
 * @param {number} runStart the index of the run's first letter
 * @returns {object} the trial with `after_resume`
 */
export function runTrial(trials, index, runStart) {
  const trial = { ...trials[index], after_resume: false };
  if (runStart > 0 && index - runStart < 2) {
    return { ...trial, after_resume: true, needs_response: false, target: false, lure: null };
  }
  if (runStart > 0 && trial.lure === "3-back" && index - 3 < runStart) trial.lure = null;
  return trial;
}

/**
 * The 10-letter practice list: 3 targets among the 8 answerable positions and 15% of the
 * 5 non-targets as lures of each kind (round(0.75) = 1).
 * @param {{int: function(number): number, pick: function(Array): *}} gen the generator of
 *   the practice stream
 * @returns {object[]} trials as {@link makeTrials} gives them
 */
export function practiceTrials(gen) {
  const answerable = PRACTICE_LETTERS - 2;
  const lures = Math.round(LURE_SHARE * (answerable - PRACTICE_TARGETS));
  return makeTrials(gen, { length: PRACTICE_LETTERS, targets: PRACTICE_TARGETS, lures });
}

/**
 * The answer a key gives.
 * @param {string} key a KeyboardEvent `key` ("j"/"J" or "f"/"F")
 * @returns {("same"|"different"|null)} null for any other key
 */
export function responseForKey(key) {
  return KEYS[String(key).toLowerCase()] ?? null;
}

/**
 * Whether an answer is right.
 * @param {{needs_response: boolean, target: boolean}} trial
 * @param {("same"|"different"|null)} response null when no key was pressed in time
 * @returns {(boolean|null)} null for the first two letters (not scored); false for no
 *   answer on an answerable letter
 */
export function isCorrect(trial, response) {
  if (!trial.needs_response) return null;
  if (response === null || response === undefined) return false;
  return trial.target ? response === "same" : response === "different";
}

const A = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
  1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
const B = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
  6.680131188771972e1, -1.328068155288572e1];
const C = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
  -2.549732539343734, 4.374664141464968, 2.938163982698783];
const D = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996,
  3.754408661907416];

function tail(q) {
  return (((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5])
    / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1);
}

/**
 * The inverse of the standard normal distribution function (Acklam's approximation,
 * relative error below 1.2e-9).
 * @param {number} p a probability, 0 < p < 1
 * @returns {number} z such that P(Z <= z) = p
 */
export function probit(p) {
  if (!(p > 0 && p < 1)) throw new Error("probit needs 0 < p < 1");
  const low = 0.02425;
  if (p < low) return tail(Math.sqrt(-2 * Math.log(p)));
  if (p > 1 - low) return -tail(Math.sqrt(-2 * Math.log(1 - p)));
  const q = p - 0.5;
  const r = q * q;
  return ((((((A[0] * r + A[1]) * r + A[2]) * r + A[3]) * r + A[4]) * r + A[5]) * q)
    / (((((B[0] * r + B[1]) * r + B[2]) * r + B[3]) * r + B[4]) * r + 1);
}

function median(values) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((x, y) => x - y);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/**
 * Scores the answerable, non-practice trials of a list of `nback.trial` records.
 *
 * Only records with `scored: true` (the runner's flag: a done trial that is not practice; the
 * rule of `scoredRecords` in runner.js) are used, so a trial cut short by the block end,
 * answered or not, and an abandoned one never enter a measure (a cut-short letter is not a
 * miss). Of those, left out: `needs_response: false` (the first two letters of a block, and of
 * a run after a resume: `after_resume`), `tail: true`, and a `window_ms` shorter than the full
 * TIMING.responseWindowMs. `response` is the answer ("same"/"different"/null). On a target,
 * "same" is a hit and "different" or no answer a miss. On a non-target, "same" is a false
 * alarm, "different" a correct rejection, and no answer neither (no false alarm).
 *   hit_rate = hits / targets; false_alarm_rate = false_alarms / non_targets
 *   H = (hits + 0.5) / (targets + 1); F = (false_alarms + 0.5) / (non_targets + 1)
 *   d_prime = z(H) - z(F); criterion = -(z(H) + z(F)) / 2
 *   median_rt_hits_ms = median rt_ms of the hits
 *   lure_false_alarm_rate = false alarms on lures (both kinds) / lures
 *   no_response = answerable trials without an answer (targets and non-targets)
 * A rate is null when its denominator is 0; d_prime and criterion are null unless there
 * is at least one target and one non-target.
 *
 * @param {{needs_response: boolean, target: boolean, lure: (null|string),
 *   response: (null|string), rt_ms: (null|number), scored: boolean, tail: boolean,
 *   after_resume: boolean, window_ms: number}[]} records (the last three may be left out)
 * @returns {{targets: number, non_targets: number, hits: number, misses: number,
 *   false_alarms: number, correct_rejections: number, hit_rate: (number|null),
 *   false_alarm_rate: (number|null), d_prime: (number|null), criterion: (number|null),
 *   median_rt_hits_ms: (number|null), lure_false_alarm_rate: (number|null),
 *   no_response: number}}
 */
export function score(records) {
  const scored = (records || [])
    .filter((r) => r && r.scored === true)
    .filter((r) => r.needs_response && !r.tail && !r.after_resume)
    .filter((r) => r.window_ms === undefined || r.window_ms >= TIMING.responseWindowMs);
  const targets = scored.filter((r) => r.target);
  const nonTargets = scored.filter((r) => !r.target);
  const hitRecords = targets.filter((r) => r.response === "same");
  const hits = hitRecords.length;
  const falseAlarms = nonTargets.filter((r) => r.response === "same").length;
  const lures = nonTargets.filter((r) => r.lure);
  const rate = (n, d) => (d === 0 ? null : n / d);
  let dPrime = null;
  let criterion = null;
  if (targets.length > 0 && nonTargets.length > 0) {
    const zH = probit((hits + 0.5) / (targets.length + 1));
    const zF = probit((falseAlarms + 0.5) / (nonTargets.length + 1));
    dPrime = zH - zF;
    criterion = -(zH + zF) / 2;
  }
  return {
    targets: targets.length,
    non_targets: nonTargets.length,
    hits,
    misses: targets.length - hits,
    false_alarms: falseAlarms,
    correct_rejections: nonTargets.filter((r) => r.response === "different").length,
    hit_rate: rate(hits, targets.length),
    false_alarm_rate: rate(falseAlarms, nonTargets.length),
    d_prime: dPrime,
    criterion,
    median_rt_hits_ms: median(hitRecords.map((r) => r.rt_ms)),
    lure_false_alarm_rate: rate(lures.filter((r) => r.response === "same").length, lures.length),
    no_response: scored.filter((r) => r.response === null || r.response === undefined).length,
  };
}

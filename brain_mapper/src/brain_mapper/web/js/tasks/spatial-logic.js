// Spatial memory: pure functions (trial lists, answers, scoring). No page access and no
// randomness of its own: every random draw comes from the generator passed in, an object
// with int(n) (a whole number 0..n-1), pick(list) and shuffle(list) (a new list).

/** Squares per side of the grid; cells are numbered 0-24 row by row. */
export const GRID = 5;
/** The number of squares filled on a trial ("lit" in the data). */
export const LOADS = [2, 4, 6];
/** Fixation cross on the empty grid before the filled squares, ms. */
export const FIXATION_MS = 500;
/** The filled squares are shown for this long, ms. */
export const STIM_MS = 1000;
/** Empty grid between the filled squares and the probe, ms. */
export const RETENTION_MS = 2000;
/** The probe stays this long, the fixed time to answer (the last press counts), ms. */
export const PROBE_MS = 3000;
/** Shortest gap after a trial, ms. */
export const GAP_MIN_MS = 800;
/** Longest gap after a trial, ms. */
export const GAP_MAX_MS = 1200;
/** "Correct" / "Incorrect" / "No response" after every trial, from the end of the time to answer, ms
 *  (the runner's FEEDBACK_MS). */
export const FEEDBACK_MS = 500;
/** Length of one scored block, ms. */
export const BLOCK_MS = 60000;
/** Trials generated per block, more than fit: after the 2 s lead-in, 7 trials of 7800-8200 ms
 *  fit in a 60 s block; a trial that would not end in time is not started. */
export const TRIALS_PER_BLOCK = 24;
/** Every run of this many trials holds each (load, probe lit) combination twice. */
export const RUN_LENGTH = 12;
/** The answer of each key (KeyboardEvent.code): J (right hand) "yes, this square was filled",
 *  F (left hand) "no, it was not". */
export const KEYS = { KeyF: "no", KeyJ: "yes" };

/** The fixed length of a trial: gap and cross, filled squares, empty grid, probe (the response
 *  time to answer), feedback: 7800-8200 ms. */
export function trialMs(trial) {
  return trial.gap_ms + FIXATION_MS + STIM_MS + RETENTION_MS + PROBE_MS + FEEDBACK_MS;
}

const CELLS = Array.from({ length: GRID * GRID }, (_, i) => i);

/**
 * One trial of a given load and probe kind.
 * @param {{int: function(number): number, pick: function(Array): *, shuffle: function(Array): Array}} gen
 * @param {number} load How many squares light up (2, 4 or 6).
 * @param {boolean} probeLit Whether the probe is one of the filled squares.
 * @returns {{load: number, lit: number[], probe: number, probe_lit: boolean, gap_ms: number}}
 *   `lit` in ascending order, drawn without replacement from the 25 cells; `probe` drawn
 *   from `lit` when `probe_lit`, otherwise from the cells not lit; `gap_ms` a whole number
 *   drawn uniformly from 800-1200 inclusive.
 */
export function makeTrial(gen, load, probeLit) {
  const lit = gen.shuffle(CELLS).slice(0, load).sort((a, b) => a - b);
  const pool = probeLit ? lit : CELLS.filter((c) => !lit.includes(c));
  const probe = gen.pick(pool);
  const gap_ms = GAP_MIN_MS + gen.int(GAP_MAX_MS - GAP_MIN_MS + 1);
  return { load, lit, probe, probe_lit: probeLit, gap_ms };
}

/**
 * The trial list of one scored block: 24 trials in two runs of 12; each run holds each of
 * the 6 (load, probe lit) combinations twice, in shuffled order. The block's time limit
 * cuts the list wherever it falls.
 * @param {{int: function(number): number, pick: function(Array): *, shuffle: function(Array): Array}} gen
 *   The block's seeded generator.
 * @returns {Array<{load: number, lit: number[], probe: number, probe_lit: boolean, gap_ms: number}>}
 */
export function blockTrials(gen) {
  const combos = [];
  for (const load of LOADS) {
    for (const probeLit of [true, false]) {
      for (let k = 0; k < RUN_LENGTH / (LOADS.length * 2); k += 1) combos.push([load, probeLit]);
    }
  }
  const trials = [];
  while (trials.length < TRIALS_PER_BLOCK) {
    for (const [load, probeLit] of gen.shuffle(combos)) {
      trials.push(makeTrial(gen, load, probeLit));
    }
  }
  return trials.slice(0, TRIALS_PER_BLOCK);
}

/**
 * The 3 practice trials: loads 2, 4, 2 with the probe lit, not lit, lit (so both answers
 * are practiced).
 * @param {{int: function(number): number, pick: function(Array): *, shuffle: function(Array): Array}} gen
 *   The practice's seeded generator.
 * @returns {Array<{load: number, lit: number[], probe: number, probe_lit: boolean, gap_ms: number}>}
 */
export function practiceTrials(gen) {
  return [
    makeTrial(gen, 2, true),
    makeTrial(gen, 4, false),
    makeTrial(gen, 2, true),
  ];
}

/**
 * The practice trial for each call of the runner's `next`. The runner's trial index goes on after
 * practice is stopped and started again (full screen closed during practice), so a restart is
 * told by the calls: a call that does not follow a done trial of the call before it starts the
 * list from its first trial again.
 * @param {Array<object>} list The practice trials (`practiceTrials`).
 * @returns {function({index: number, history: Array}): (object|null)} `next` for the runner:
 *   `index` the runner's trial number, `history` the done trials of this practice so far; null
 *   once the list is used up.
 */
export function practicePicker(list) {
  let start = null;
  let last = null;
  return ({ index, history }) => {
    const done = history.length;
    const goingOn = last !== null && index === last.index + 1 && done === last.done + 1;
    if (!goingOn) start = index;
    last = { index, done };
    const k = index - start;
    return k < list.length ? list[k] : null;
  };
}

/**
 * Whether an answer is right.
 * @param {{probe_lit: boolean}} trial
 * @param {"yes"|"no"|null} response null for no answer, which is never right.
 * @returns {boolean}
 */
export function isCorrect(trial, response) {
  if (response === "yes") return trial.probe_lit === true;
  if (response === "no") return trial.probe_lit === false;
  return false;
}

function median(values) {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/**
 * Scores trial records. Only records with `scored === true` count (the rule of the runner's
 * `scoredRecords`: done trials that are not practice); practice, cut-short and abandoned trials
 * are left out.
 * Per load (2, 4, 6):
 * - `n_trials` scored trials; `no_response` of them with no answer;
 * - `hits` "yes" answers on lit probes, of `lit_answered` answered lit-probe trials;
 *   `hit_rate` H = hits / lit_answered (null if 0);
 * - `false_alarms` "yes" answers on not-lit probes, of `not_lit_answered` answered
 *   not-lit-probe trials; `fa_rate` F = false_alarms / not_lit_answered (null if 0);
 *   trials with no answer are left out of H and F;
 * - `accuracy` = right answers / n_trials (no answer counts as wrong; null if no trials);
 * - `median_rt_ms` median rt_ms of the right answers (null if none);
 * - `k` Cowan's K = load x (H - F), null when H or F is null.
 * Overall: `k` the largest per-load K (null if none), `n_trials`, `no_response`.
 * @param {Array<{load: number, probe_lit: boolean, response: ("yes"|"no"|null), rt_ms: (number|null), scored: boolean}>} records
 * @returns {{by_load: Array<{load: number, n_trials: number, no_response: number, hits: number, lit_answered: number, false_alarms: number, not_lit_answered: number, hit_rate: (number|null), fa_rate: (number|null), accuracy: (number|null), median_rt_ms: (number|null), k: (number|null)}>, k: (number|null), n_trials: number, no_response: number}}
 */
export function score(records) {
  // The runner's rule (scoredRecords in runner.js): only records with scored === true.
  const scored = records.filter((r) => r && r.scored === true);
  const by_load = LOADS.map((load) => {
    const rows = scored.filter((r) => r.load === load);
    const answered = rows.filter((r) => r.response === "yes" || r.response === "no");
    const lit = answered.filter((r) => r.probe_lit === true);
    const notLit = answered.filter((r) => r.probe_lit === false);
    const hits = lit.filter((r) => r.response === "yes").length;
    const false_alarms = notLit.filter((r) => r.response === "yes").length;
    const hit_rate = lit.length ? hits / lit.length : null;
    const fa_rate = notLit.length ? false_alarms / notLit.length : null;
    const right = rows.filter((r) => isCorrect(r, r.response));
    return {
      load,
      n_trials: rows.length,
      no_response: rows.length - answered.length,
      hits,
      lit_answered: lit.length,
      false_alarms,
      not_lit_answered: notLit.length,
      hit_rate,
      fa_rate,
      accuracy: rows.length ? right.length / rows.length : null,
      median_rt_ms: median(right.map((r) => r.rt_ms).filter((v) => typeof v === "number")),
      k: hit_rate === null || fa_rate === null ? null : load * (hit_rate - fa_rate),
    };
  });
  const ks = by_load.map((row) => row.k).filter((k) => k !== null);
  return {
    by_load,
    k: ks.length ? Math.max(...ks) : null,
    n_trials: by_load.reduce((n, row) => n + row.n_trials, 0),
    no_response: by_load.reduce((n, row) => n + row.no_response, 0),
  };
}

// Pure logic of the Words round (`words`) and the Delayed recall round (`recall`):
// the word pool, the two study lists, the study timeline and the scoring of typed
// recall. No DOM, no globals, no Math.random, no Date. Randomness comes only from
// the generator passed in (methods int, pick, shuffle).

/** Words in one study list. */
export const LIST_LENGTH = 12;
/** Study lists in a session. */
export const LIST_COUNT = 2;
/** No two words in one list start with the same this many letters. */
export const PREFIX_LENGTH = 3;
/** Fixation cross (the lead-in) before the first word, ms. */
export const FIXATION_MS = 2000;
/** Time each word is on screen, ms (PEERS Experiment 1: 3000 ms; see docs, "Timing and its sources"). */
export const WORD_MS = 3000;
/** Shortest and longest blank after a word, ms, drawn uniformly in whole ms (PEERS: 800-1200 ms). */
export const ISI_MIN_MS = 800;
export const ISI_MAX_MS = 1200;
/** Shortest and longest delay from the last word's offset to the recall cue, ms (PEERS: 1200-1400 ms). */
export const CUE_DELAY_MIN_MS = 1200;
export const CUE_DELAY_MAX_MS = 1400;
/** Recall time after each list, ms, counted from the recall cue (Murdock 1962: 1.5 min). */
export const RECALL_MS = 90000;
/** Recall time of delayed recall, ms, counted from the recall cue. */
export const DELAYED_RECALL_MS = 120000;
/** The longest study phase, from the fixation onset to the recall cue, ms. */
export const STUDY_MAX_MS =
  FIXATION_MS + LIST_LENGTH * WORD_MS + (LIST_LENGTH - 1) * ISI_MAX_MS + CUE_DELAY_MAX_MS;
/** The recall cue, a row of asterisks (PEERS: "a tone sounded, a row of asterisks appeared"). */
export const CUE_TEXT = "* * * * * * *";
/** The line that tells the participant how to start a list. */
export const ENTER_LINE = "Press Enter to start.";
/** The line that tells the participant how to start delayed recall. */
export const START_LINE = "Press Enter to start.";
/** Prompt of immediate recall (the Words round). */
export const IMMEDIATE_PROMPT =
  'Type the words you remember, in any order. Press Enter after each word.';
/** Prompt of delayed recall. */
export const DELAYED_PROMPT =
  'Remember those lists of words you learned earlier? Type any words you remembered from either ' +
  'list, in any order.';
/** The control that ends recall early, and its confirmation. */
export const DONE_LABEL = "I can't remember any more";
export const CONFIRM_TEXT = "End recall now?";

/**
 * The jitter of one list: the blank after each of the first 11 words (`blanks_ms`, each
 * ISI_MIN_MS to ISI_MAX_MS) and the delay from the last word's offset to the recall cue
 * (`cue_delay_ms`, CUE_DELAY_MIN_MS to CUE_DELAY_MAX_MS), whole ms drawn uniformly with
 * `rng.int` in that order. The same generator state gives the same jitter.
 * @param {{int: function(number): number}} rng the seeded generator
 * @returns {{blanks_ms: number[], cue_delay_ms: number}}
 */
export function drawJitter(rng) {
  const blanks = [];
  for (let i = 0; i < LIST_LENGTH - 1; i += 1) blanks.push(ISI_MIN_MS + rng.int(ISI_MAX_MS - ISI_MIN_MS + 1));
  const cue = CUE_DELAY_MIN_MS + rng.int(CUE_DELAY_MAX_MS - CUE_DELAY_MIN_MS + 1);
  return { blanks_ms: blanks, cue_delay_ms: cue };
}

/**
 * The form a typed or studied word is compared in: trimmed and lower case.
 * @param {string} text
 * @returns {string}
 */
export function normalize(text) {
  return String(text).trim().toLowerCase();
}

/**
 * Reads the word pool file (`assets/wordpool.txt`): one word per line. Lines are
 * trimmed and lower-cased, CR is dropped, blank lines are left out.
 * @param {string} text the file's text
 * @returns {string[]} the words in file order
 */
export function parsePool(text) {
  return String(text)
    .split('\n')
    .map(normalize)
    .filter((w) => w.length > 0);
}

/**
 * Draws the two study lists: the pool (distinct words, after `normalize`) is
 * shuffled with `rng.shuffle`, then walked in that order; each word goes to list 1
 * if list 1 is not full and holds no word with the same first three letters, else
 * to list 2 on the same terms, else it is passed over, until both lists hold 12.
 * So the 24 words are distinct and no two words in a list share their first three
 * letters. The pool is not changed. The same pool and a generator in the same
 * state give the same lists, which is how delayed recall rebuilds them from the
 * session seed (stream "words").
 * @param {string[]} pool the word pool
 * @param {{shuffle: function(Array): Array}} rng the seeded generator
 * @returns {string[][]} two lists of 12 words, in study order
 * @throws {Error} if the pool cannot fill both lists
 */
export function buildLists(pool, rng) {
  const needed = LIST_COUNT * LIST_LENGTH;
  const distinct = [...new Set(pool.map(normalize).filter((w) => w.length > 0))];
  if (distinct.length < needed) {
    throw new Error(`the word pool has ${distinct.length} distinct words; ${needed} are needed`);
  }
  const lists = Array.from({ length: LIST_COUNT }, () => []);
  const prefixes = Array.from({ length: LIST_COUNT }, () => new Set());
  for (const word of rng.shuffle(distinct)) {
    const prefix = word.slice(0, PREFIX_LENGTH);
    const k = lists.findIndex((l, i) => l.length < LIST_LENGTH && !prefixes[i].has(prefix));
    if (k < 0) continue;
    lists[k].push(word);
    prefixes[k].add(prefix);
    if (lists.every((l) => l.length === LIST_LENGTH)) return lists;
  }
  throw new Error(
    `the word pool cannot fill ${LIST_COUNT} lists of ${LIST_LENGTH} words ` +
      `with different first ${PREFIX_LENGTH} letters`,
  );
}

/**
 * The study timeline of one list, in ms from the fixation onset: the cross from 0 to
 * FIXATION_MS, then word p (1-based) from FIXATION_MS + (p - 1) * WORD_MS + the blanks
 * before it, on for WORD_MS; `blank_ms` is the blank that follows it (for the last word,
 * the delay to the recall cue).
 * @param {string[]} words the list in study order
 * @param {{blanks_ms: number[], cue_delay_ms: number}} jitter from `drawJitter`
 * @returns {{word: string, position: number, onset_ms: number, offset_ms: number, blank_ms: number}[]}
 */
export function studySchedule(words, jitter) {
  let onset = FIXATION_MS;
  return words.map((word, i) => {
    const blank = i < words.length - 1 ? jitter.blanks_ms[i] : jitter.cue_delay_ms;
    const item = { word, position: i + 1, onset_ms: onset, offset_ms: onset + WORD_MS, blank_ms: blank };
    onset += WORD_MS + blank;
    return item;
  });
}

/**
 * When the recall cue is due, in ms from the fixation onset: the last word's offset plus
 * the cue delay.
 * @param {{blanks_ms: number[], cue_delay_ms: number}} jitter
 * @returns {number}
 */
export function cueMs(jitter) {
  const blanks = jitter.blanks_ms.slice(0, LIST_LENGTH - 1).reduce((a, b) => a + b, 0);
  return FIXATION_MS + LIST_LENGTH * WORD_MS + blanks + jitter.cue_delay_ms;
}

/**
 * The end of a recall period: `recallMs` after its start, its own clock, so a longer
 * study does not shorten it; only the block's end (less `marginMs`) can cut it, and then
 * `capped` is true.
 * @param {number} startMs the recall start (the cue's frame)
 * @param {number} recallMs the recall time left
 * @param {number} blockEndMs when the page expects the block to end
 * @param {number} marginMs how long before the block's end recall must end
 * @returns {{end: number, capped: boolean}}
 */
export function recallEnd(startMs, recallMs, blockEndMs, marginMs) {
  const own = startMs + recallMs;
  const limit = blockEndMs - marginMs;
  return own <= limit ? { end: own, capped: false } : { end: Math.max(startMs, limit), capped: true };
}

/**
 * Scores typed recall against studied lists. An entry matches a studied word if
 * they are equal after `normalize`; a later entry of an already matched word is a
 * repeat; any other entry that is not blank is an intrusion (an intrusion typed
 * twice is two intrusions). Entries that are blank after trimming are in no
 * category. Raw texts are kept as typed. Immediate recall passes only the list
 * just studied, so a word of the other list is an intrusion there; `others` (the lists studied
 * before it) only tags such an intrusion in `intrusions_tagged`.
 * @param {{list: number, words: string[]}[]} studied the lists, each with its number
 * @param {string[]} typed the entries in the order they were made
 * @param {{list: number, words: string[]}[]} [others] other studied lists, for the tags only
 * @returns {{
 *   typed: string[],
 *   matched: {text: string, word: string, list: number, position: number}[],
 *   intrusions: string[],
 *   intrusions_tagged: ({text: string, other_list: false} |
 *     {text: string, other_list: true, word: string, list: number, position: number})[],
 *   repeats: string[],
 *   lists: {list: number, studied: number, recalled: number, proportion: number,
 *           by_position: number[], output_order: number[]}[],
 *   overall: {studied: number, recalled: number, proportion: number},
 * }} `matched` in output order (position is 1-based); per list, `by_position[i]`
 *   is 1 if the word at position i + 1 was recalled, `output_order` the positions
 *   in the order they were recalled, `proportion` = recalled / studied (0 for an
 *   empty list); `overall` the same over all lists.
 */
export function scoreRecall(studied, typed, others = []) {
  const where = new Map();
  for (const { list, words } of studied) {
    words.forEach((w, i) => where.set(normalize(w), { list, position: i + 1 }));
  }
  const elsewhere = new Map();
  for (const { list, words } of others) {
    words.forEach((w, i) => elsewhere.set(normalize(w), { list, position: i + 1 }));
  }
  const matched = [];
  const intrusions = [];
  const intrusionsTagged = [];
  const repeats = [];
  const seen = new Set();
  for (const text of typed) {
    const word = normalize(text);
    if (word.length === 0) continue;
    const at = where.get(word);
    if (!at) {
      intrusions.push(text);
      const other = elsewhere.get(word);
      intrusionsTagged.push(
        other ? { text, other_list: true, word, list: other.list, position: other.position } : { text, other_list: false },
      );
    }
    else if (seen.has(word)) repeats.push(text);
    else {
      seen.add(word);
      matched.push({ text, word, list: at.list, position: at.position });
    }
  }
  const lists = studied.map(({ list, words }) => {
    const mine = matched.filter((m) => m.list === list);
    const recalled = new Set(mine.map((m) => m.position));
    return {
      list,
      studied: words.length,
      recalled: mine.length,
      proportion: words.length ? mine.length / words.length : 0,
      by_position: words.map((_, i) => (recalled.has(i + 1) ? 1 : 0)),
      output_order: mine.map((m) => m.position),
    };
  });
  const total = lists.reduce((n, l) => n + l.studied, 0);
  return {
    typed: typed.slice(),
    matched,
    intrusions,
    intrusions_tagged: intrusionsTagged,
    repeats,
    lists,
    overall: {
      studied: total,
      recalled: matched.length,
      proportion: total ? matched.length / total : 0,
    },
  };
}

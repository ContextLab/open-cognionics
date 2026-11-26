// The 2-back task (step id `nback`). One letter at a time in the shared stimulus area: the letter
// for 500 ms, then a fixation cross until the next letter, one letter every 3200 ms. From the
// third letter on, J = the same letter as two before, F = a different one. The letter lists,
// answers and scores are in `nback-logic.js`.
//
// The task starts on Enter (the runner's prompt, then a 2 s fixation cross). Timing (the
// runner's frame timing): the first letter of a run (the block, the practice, or the letters
// after a reload) is shown in the next frame; letter k of the run is asked for at exactly first
// onset + k x 3200 ms with `io.showAt`, so the schedule does not drift. The letter goes in the
// frame nearest onset + 500 ms (`io.clearAt`, whatever keys are pressed). The fixation cross is
// the empty panel's own content (`css/tasks/nback.css`): it comes on in the frame that takes the
// letter off, and goes when the next letter is drawn. The answer window is
// TIMING.responseWindowMs (2400 ms) from the onset, in practice and in the blocks; a press does not
// end it: every F/J press is logged (`nback.resp`) and the last one is the answer. At its end the
// feedback ("Correct", "Incorrect", "No response") is shown for 500 ms, for every letter that
// needs an answer (the first two letters need none and get none: the cross stays). A trial that
// would not end before the block's end is not started (the runner's `trialMs`). A key press after
// the window is logged as `nback.stray` (the runner logs the first one, this file the ones after
// it, with `extra: true`). After a reload the first two letters of the new run need no answer
// (`after_resume`, see runTrial in nback-logic.js).
import { el } from "../format.js";
import { lateFrames } from "./frames.js";
import {
  TIMING,
  blockTrials,
  extendTail,
  isCorrect,
  practiceTrials,
  runTrial,
  score,
  PRACTICE_LETTERS,
} from "./nback-logic.js";
import { feedbackText, scoredRecords, taskScreen } from "./runner.js";

const KEYS = { KeyF: "different", KeyJ: "same" };
const CODES = Object.keys(KEYS);

function isTyping(target) {
  return !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
}

function letter(text) {
  return el("div", { class: "nback-letter" }, text);
}

/** Watches the keys of one trial so that stray presses after the first one are logged too: a
 *  press of F or J in the answer window is an answer (the runner logs it), any other key, and
 *  any key outside the window, is stray. */
function strayWatcher(io) {
  const w = { open: false, count: 0 };
  w.onKey = (event) => {
    if (event.repeat || isTyping(event.target)) return;
    if (w.open && CODES.includes(event.code)) return;
    w.count += 1;
    // The runner logs the first stray key of a trial; the later ones are logged here.
    if (w.count > 1) io.event("stray", { key: event.code, awaiting: w.open, extra: true }, event.timeStamp);
  };
  document.addEventListener("keydown", w.onKey);
  return w;
}

/** The trials of the current run in `history` (done trials of the block, oldest first): the
 *  run is the trials numbered without a gap up to `index - 1`; a gap (a trial abandoned by a
 *  pause, or lost to a reload) starts a new run. */
function currentRun(history, index) {
  let k = history.length;
  while (k > 0 && history[k - 1].trial === index - (history.length - k) - 1) k -= 1;
  return history.slice(k);
}

/** Two-back, explained on screen before practice: six letters and what to press for each. From the
 *  third letter on, a bracket joins each letter to the one two before it (css/tasks/nback.css). */
const EXAMPLE = [
  ["B", "no answer"],
  ["K", "no answer"],
  ["B", "J: same as 2 back"],
  ["F", "F: K was 2 back"],
  ["K", "F: B was 2 back"],
  ["F", "J: same as 2 back"],
];

export const { mount, update, tick, unmount, instructions, startsOnEnter } = taskScreen({
  name: "nback",
  keys: KEYS,
  sides: { left: ["F", "different"], right: ["J", "same"], keys: ["KeyF", "KeyJ"] },
  enter: true,
  trialMs: () => TIMING.trialMs,
  panel: true,
  block: (index) => ({
    instructions:
      index === 0
        ? // Short lines (the instructions keep their line breaks, css/shell.css).
          "Compare each letter with the letter two positions earlier.\n" +
          "Same: J    Different: F\n" +
          "Do not answer the first two letters.\n" +
          "You can change your answer; your last press counts."
        : "Same as before: J if the letter is the same as the one two before it, F if different. " +
          "The first two letters need no answer.",
    example: index === 0 ? EXAMPLE : null,
    practice: index === 0 ? PRACTICE_LETTERS : 0,
  }),
  trials({ blockRng, practice }) {
    const gen = { int: blockRng.int, pick: blockRng.pick, shuffle: blockRng.shuffle, float: blockRng.next };
    let list = practice ? practiceTrials(gen) : blockTrials(gen);
    return ({ index, history }) => {
      if (index >= list.length) {
        if (practice) return null;
        list = extendTail(gen, list, index + 1); // more tail letters, from the same stream
      }
      const run = currentRun(history, index);
      const first = run.length ? run[0].letter_browser_ms : null;
      return {
        ...runTrial(list, index, index - run.length),
        scheduled_onset_ms: first === null ? null : first + run.length * TIMING.trialMs,
      };
    };
  },
  async run(trial, io) {
    const keys = strayWatcher(io);
    try {
      const due = trial.scheduled_onset_ms;
      const draw = (area) => area.replaceChildren(letter(trial.letter));
      const extra = { part: "letter", letter: trial.letter, position: trial.position };
      const onset = due === null ? await io.show(draw, extra) : await io.showAt(due, draw, extra);
      const frameMs = io.frameMs;
      const windowMs = TIMING.responseWindowMs;
      keys.open = true;
      const answered = io.window({ ms: windowMs, from: onset }).then((a) => {
        keys.open = false;
        return a;
      });
      answered.catch(() => {}); // an abandoned trial rejects it; the awaits below see that
      // Every change has an explicit due time, so a key press never moves the letter's offset:
      // the letter is up TIMING.letterMs from its onset.
      await io.clearAt(onset + TIMING.letterMs);
      const answer = await answered;
      const correct = isCorrect(trial, answer.answer);
      let text = null;
      if (trial.needs_response) {
        text = feedbackText(answer.key !== null, correct);
        await io.feedback(text, onset + windowMs);
      }
      await io.wait(TIMING.trialMs, onset); // the trial lasts until the next letter is due
      return {
        correct,
        feedback: text,
        window_ms: windowMs,
        letter_late_frames: due === null ? 0 : lateFrames(onset, due, frameMs),
        frame_ms: frameMs,
        stray_presses: keys.count,
      };
    } finally {
      document.removeEventListener("keydown", keys.onKey);
    }
  },
});

/** The scores of a list of `nback.trial` records: `score` over the runner's scored records. */
export function scores(records) {
  return score(scoredRecords(records));
}

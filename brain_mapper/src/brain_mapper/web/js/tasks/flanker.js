// The flanker task: a row of five arrows; press F if the middle arrow points left, J if right.
//
// The task starts on Enter (the runner's prompt, then a 2 s fixation cross). One trial, drawn in
// the shared stimulus area, always the same length (`trialMs`): a fixation cross for 500 ms, the
// arrows for the whole 1500 ms response window (a press does not end it: every F/J press is
// logged, the last one is the answer), "Correct" / "Incorrect" / "No response" for 500 ms from the
// end of the window, then a blank gap of 700-1100 ms (drawn per trial from the seeded generator).
// The parts are `fixation`, `arrows` and `feedback` (`flanker.stim`); the blank is the clear that
// ends the feedback (`flanker.stim_off`). All timing is the runner's: each change is put on the
// frame nearest to when it is due, counted from the frame that showed the part before it
// (`io.wait`), from the arrows' onset for the window's end (`io.clearAt` / `io.feedback` at onset
// + 1500 ms), and the next trial's cross from the end of this trial's gap (`io.showAt`), so the
// schedule does not drift. Trial lists, responses and scoring live in `flanker-logic.js`.

import { el } from "../format.js";
import { PRACTICE_TRIALS, TIMING, blockTrials, feedbackFor, isCorrect, practiceTrials, trialMs } from "./flanker-logic.js";
import { taskScreen } from "./runner.js";

const SVG = "http://www.w3.org/2000/svg";
/** The drawing is a 500 x 500 square filling the panel; the arrows are 80 wide, 92 apart, on the
 *  middle line, the middle one on the fixation point. */
const SIZE = 500;
const STEP = 92;
/** A right-pointing arrow around (0, 0): a shaft and a triangular head. */
const ARROW = "M -40 -9 L 5 -9 L 5 -26 L 40 0 L 5 26 L 5 9 L -40 9 Z";

/** When the cross of the trial after the last one that ended its gap is due, on this page:
 *  `{block, practice, index, due}`. Nothing kept across a pause or a reload: a trial that does
 *  not follow it at once shows its cross in the next frame. */
let following = null;

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/** The square field every part is drawn in; it fills the panel. */
function field(part, trial, index, content) {
  const f = el("div", { class: "flanker-field", id: "flanker-field", "data-phase": part, "data-trial": String(index) });
  if (part === "arrows") {
    f.dataset.target = trial.target;
    f.dataset.flankers = trial.flankers;
    f.dataset.congruent = String(trial.congruent);
  }
  f.append(content);
  return f;
}

function canvas() {
  return svg("svg", { viewBox: `0 0 ${SIZE} ${SIZE}`, class: "flanker-svg", "aria-hidden": "true" });
}

function fixation() {
  const s = canvas();
  const c = SIZE / 2;
  s.append(
    svg("rect", { x: c - 20, y: c - 3, width: 40, height: 6, class: "flanker-ink flanker-fix" }),
    svg("rect", { x: c - 3, y: c - 20, width: 6, height: 40, class: "flanker-ink flanker-fix" }),
  );
  return s;
}

function arrows(trial) {
  const s = canvas();
  const c = SIZE / 2;
  for (let i = 0; i < 5; i += 1) {
    const dir = i === 2 ? trial.target : trial.flankers;
    const x = c + (i - 2) * STEP;
    const flip = dir === "left" ? " scale(-1 1)" : "";
    s.append(
      svg("path", {
        d: ARROW,
        class: "flanker-arrow flanker-ink",
        "data-dir": dir,
        "data-index": i,
        transform: `translate(${x} ${c})${flip}`,
      }),
    );
  }
  return s;
}

const INSTRUCTIONS =
  "Answer the direction of the middle arrow. Ignore the other arrows.\n" +
  "Keep your eyes on the cross in the middle.\n" +
  "If you press the wrong key, press the right one: your last press counts.";

/** One example arrow, drawn like the trials' arrows (white), for the instruction screen. */
function exampleArrow(dir) {
  return () => {
    const s = svg("svg", { viewBox: "-44 -30 88 60", class: "flanker-example-arrow", "aria-hidden": "true" });
    s.append(svg("path", { d: ARROW, class: "flanker-ink", transform: dir === "left" ? "scale(-1 1)" : "" }));
    return s;
  };
}

/** The example row on the instruction screen only: the middle arrow points the other way, and is
 *  marked as the one to answer. */
const EXAMPLE = [
  [exampleArrow("left"), ""],
  [exampleArrow("left"), ""],
  [exampleArrow("right"), "middle: J"],
  [exampleArrow("left"), ""],
  [exampleArrow("left"), ""],
];

export const { mount, update, tick, unmount, instructions, startsOnEnter } = taskScreen({
  name: "flanker",
  keys: { KeyF: "left", KeyJ: "right" },
  sides: { left: ["F", "left"], right: ["J", "right"], keys: ["KeyF", "KeyJ"] },
  enter: true,
  trialMs,
  panel: true,
  block: (index) => ({
    instructions: index === 0 ? INSTRUCTIONS : "Same as before: F if the middle arrow points left, J if it points right.",
    example: index === 0 ? EXAMPLE : null,
    practice: index === 0 ? PRACTICE_TRIALS : 0,
  }),
  trials: ({ blockRng, practice }) => {
    const list = practice ? practiceTrials(blockRng) : blockTrials(blockRng);
    return ({ index }) => (index < list.length ? list[index] : null);
  },
  async run(trial, io) {
    const draw = (part, content) => (area) => area.replaceChildren(field(part, trial, io.index, content));
    const after = following;
    following = null;
    const anchored =
      after !== null &&
      after.block === io.block &&
      after.practice === io.practice &&
      after.index === io.index - 1 &&
      after.due >= performance.now() - io.frameMs;
    const cross = draw("fixation", fixation());
    if (anchored) await io.showAt(after.due, cross, { part: "fixation" });
    else await io.show(cross, { part: "fixation" });
    await io.wait(trial.fixation_ms);
    const onset = await io.show(draw("arrows", arrows(trial)), {
      part: "arrows",
      congruent: trial.congruent,
      target: trial.target,
      flankers: trial.flankers,
    });
    // The arrows stay for the whole window; the window ends in the frame nearest onset + 1500 ms,
    // and the arrows' offset and the feedback's onset are made in that same frame.
    const end = onset + TIMING.answer_ms;
    const answer = await io.window({ ms: TIMING.answer_ms, from: onset });
    await io.clearAt(end);
    const text = feedbackFor(trial, answer.answer);
    await io.feedback(text, end);
    const due = await io.wait(trial.gap_ms);
    following = { block: io.block, practice: io.practice, index: io.index, due };
    return { correct: isCorrect(trial, answer.answer), feedback: text };
  },
});

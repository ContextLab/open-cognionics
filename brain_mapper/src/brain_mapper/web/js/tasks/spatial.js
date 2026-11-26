// Spatial memory: a 5 x 5 grid of white outlines in the shared stimulus area; 2, 4 or 6 squares
// are filled white together, then one square is marked with a thicker, glowing outline and the
// participant answers J (it was filled) or F (it was not). Trial lists and scoring are in
// ./spatial-logic.js. Event parts and fields keep their names: "lit" is a filled square.
//
// One trial, every change on the display frame nearest to its due time (io.showAt / io.clearAt),
// each due time anchored to the trial's first frame or to the onset of the filled squares, so
// nothing drifts:
//   gap_ms (800-1200, jittered) + 500 ms   fixation cross on the empty grid (from the trial's first frame)
//   1000 ms                                the filled squares       (spatial.stim, part "lit")
//   2000 ms                                the empty grid           (spatial.stim_off, part "lit")
//   3000 ms, the response window           the probe outline        (spatial.stim, part "probe")
//                                          then spatial.stim_off, part "probe"
//   500 ms                                 Correct / Incorrect / No response (part "feedback")
// A press does not end the window: every F/J press in it is logged (`spatial.resp`) and the
// last one is the answer, so a trial always lasts the same (`trialMs`). The task starts on
// Enter (the runner's prompt, then a 2 s fixation cross before the first trial).
// The jittered gap after a trial is run at the start of the next trial, together with that
// trial's fixation, so a trial that was answered is not cut short by the gap that follows it.
// `gap_ms` in a trial record is therefore the gap before that trial's 500 ms fixation.
//
// The grid and the fixation cross are in the runner's background layer (`io.background`), drawn
// once and never removed between trials (only a pause, the block's end or leaving the screen
// takes it away). The stimuli are drawn above it in the stimulus area, on a second 5 x 5 grid of
// the same size: the filled squares, the probe, and a plain center square that hides the cross
// while the squares, the empty grid and the probe are shown. `io.clearAt` after the filled squares
// empties
// the stimulus area; the center square is put back by the code right after the awaited clear,
// which runs in the same animation frame before it is painted, so the cross does not show then.
// Between trials the stimulus area is empty and the cross shows.
import { el } from "../format.js";
import { feedbackText, taskScreen } from "./runner.js";
import * as L from "./spatial-logic.js";

const INSTRUCTIONS =
  "Keep your eyes on the cross. A few squares of the grid are filled for one second: remember " +
  "them. Two seconds later one square gets a thick outline. Press J if that square was filled, " +
  "F if it was not. You have three seconds. If you press the wrong key, press the right one: " +
  "your last press counts.";

const CENTRE = Math.floor((L.GRID * L.GRID) / 2);

/** The grid with its fixation cross, drawn once in the background layer. */
function drawGrid(layer) {
  const grid = el("div", { class: "spatial-grid", id: "spatial-grid", "data-part": "fixation" });
  for (let i = 0; i < L.GRID * L.GRID; i += 1) {
    grid.append(el("div", { class: "spatial-cell", "data-cell": String(i) }));
  }
  grid.append(el("div", { class: "spatial-fixation", id: "spatial-fixation", "aria-hidden": "true" }, "+"));
  layer.append(grid);
}

/** A stimulus over the grid: the filled squares (`lit`), the `probe` outline, and the plain
 *  center square that hides the cross. */
function drawOver(area, { lit = [], probe = null } = {}) {
  const over = el("div", { class: "spatial-grid spatial-over", "aria-hidden": "true" });
  for (let i = 0; i < L.GRID * L.GRID; i += 1) {
    const shown = i === CENTRE || lit.includes(i) || i === probe;
    const cell = el("div", { class: shown ? "spatial-cell" : "spatial-slot" });
    if (lit.includes(i)) cell.dataset.lit = "true";
    if (i === probe) cell.dataset.probe = "true";
    over.append(cell);
  }
  const layer = el("div", { class: "spatial-layer" });
  layer.append(over);
  area.append(layer);
}

/** The grid in the background layer, drawn if it is not there (first trial, after a pause). */
function grid(io) {
  return io.area.querySelector(".task-background #spatial-grid");
}

function setPart(io, part) {
  const g = grid(io);
  if (g) g.dataset.part = part;
}

export const { mount, update, tick, unmount, instructions, startsOnEnter } = taskScreen({
  name: "spatial",
  keys: L.KEYS,
  sides: { left: ["F", "not filled"], right: ["J", "filled"], keys: ["KeyF", "KeyJ"] },
  enter: true,
  trialMs: L.trialMs,
  panel: true,
  block: (index) => ({
    instructions: index === 0 ? INSTRUCTIONS : "Same as before: J if the outlined square was filled, F if it was not.",
    practice: index === 0 ? 3 : 0,
  }),
  trials: ({ blockRng, practice }) => {
    if (practice) return L.practicePicker(L.practiceTrials(blockRng));
    const list = L.blockTrials(blockRng);
    return ({ index }) => (index < list.length ? list[index] : null);
  },
  async run(trial, io) {
    // The trial's first frame (the stimulus area is empty: the cross on the grid shows).
    const start = grid(io) ? await io.clear() : await io.background(drawGrid);
    const litOnset = await io.showAt(
      start + trial.gap_ms + L.FIXATION_MS,
      (area) => {
        drawOver(area, { lit: trial.lit });
        setPart(io, "lit");
      },
      { part: "lit", load: trial.load, lit: trial.lit },
    );
    await io.clearAt(litOnset + L.STIM_MS);
    drawOver(io.area);
    setPart(io, "retention");
    const probeOnset = await io.showAt(
      litOnset + L.STIM_MS + L.RETENTION_MS,
      (area) => {
        area.querySelector(".spatial-layer")?.remove();
        drawOver(area, { probe: trial.probe });
        setPart(io, "probe");
      },
      { part: "probe", probe: trial.probe, probe_lit: trial.probe_lit },
    );
    // rt is counted from the probe onset; the probe stays for the whole window and goes in the
    // frame it ends, the feedback coming on in that same frame.
    const answer = await io.window({ ms: L.PROBE_MS });
    const correct = L.isCorrect(trial, answer.answer);
    await io.clearAt(probeOnset + L.PROBE_MS);
    setPart(io, "feedback");
    const text = feedbackText(answer.answer !== null, correct);
    await io.feedback(text, probeOnset + L.PROBE_MS);
    setPart(io, "fixation");
    // fixation_browser_ms: the trial's first frame (the cross on the empty grid), browser clock.
    return { correct, feedback: text, fixation_browser_ms: start };
  },
});

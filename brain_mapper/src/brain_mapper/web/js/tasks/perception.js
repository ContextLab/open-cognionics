// Perception: orientation discrimination with a staircase. The screen; the trial lists, the
// staircase and the scoring are in ./perception-logic.js, the grating in ./perception-draw.js.
// Frame timing is the runner's (`io.show`, `io.wait`: whole frames from the last paint).
//
// One trial, in the shared stimulus area, which a canvas of the page's own navy fills (so it shows
// no edge; the grating is light stripes, rising to --stim, that fade into that ground):
//   fixation  a cross for gap_ms + 500 ms (gap_ms 600..1000 from the seeded generator), shown in
//             the first frame after the previous trial; the grating is drawn offscreen meanwhile
//   grating   the Gabor patch, 200 ms
//   blank     the plain ground until 1500 ms after the grating's onset: the fixed response
//             window (a press does not end it; every F/J press is logged, the last one counts)
//   feedback  "Correct", "Incorrect" or "No response" for 500 ms, after every trial
// A trial therefore always lasts gap_ms + 2500 ms (`trialMs`). The task starts on Enter (the
// runner's prompt, then a 2 s fixation cross before the first trial).
//
// The staircase moves when a scored trial reaches the end of its response window
// before the block's end; an unscored trial (practice, abandoned, cut short) changes nothing. It is
// kept with the runner's task store (per session and step), so it carries on from block 1
// into block 2 and through a pause or a reload, and a later session starts it afresh.

import { el } from "../format.js";
import { framesFor, lateFrames } from "./frames.js";
import { drawGrating, hex, BACKGROUND, CONTRAST, CYCLES_PER_100_CSS_PX, LIGHT } from "./perception-draw.js";
import {
  FEEDBACK_MS,
  FIXATION_MS,
  PRACTICE_TRIALS,
  RESPONSE_WINDOW_MS,
  STIMULUS_MS,
  isCorrect,
  makeBlockTrials,
  makePracticeTrials,
  newStaircase,
  signedTilt,
  updateStaircase,
} from "./perception-logic.js";
import { feedbackNode, feedbackText, taskScreen, taskStore } from "./runner.js";

const KEYS = { KeyF: "left", KeyJ: "right" };

/** [r, g, b] of a computed CSS color ("rgb(r, g, b)"), or null. */
function rgbOf(text) {
  const m = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(text || "");
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

/** [r, g, b] of an opaque CSS color in any notation, as a canvas reads it ("#rrggbb"), or null
 *  (not a color, or not opaque). */
function rgbOfCss(text) {
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.fillStyle = "#000001";
  ctx.fillStyle = text || "";
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/.exec(ctx.fillStyle);
  if (!m || (ctx.fillStyle === "#000001" && text !== "#000001")) return null;
  return [1, 2, 3].map((i) => parseInt(m[i], 16));
}

/** The colors the canvas is drawn with, read from the page: the ground is the body's background
 *  (the navy the stimulus area shows), the cross and the light of the grating's stripes are
 *  --stim (white). */
function colors() {
  const ground = rgbOf(getComputedStyle(document.body).backgroundColor) || [...BACKGROUND];
  const stim = getComputedStyle(document.documentElement).getPropertyValue("--stim").trim();
  return { ground, groundCss: `rgb(${ground.join(", ")})`, stim, light: rgbOfCss(stim) || [...LIGHT] };
}

/** The last state this screen saw (the task store is keyed on its session id and step). */
let lastState = null;
/** When the running block ends (performance.now clock), as the runner reckons it from the state:
 *  a trial that finishes then is cut short, and an unscored trial does not move the staircase. */
let endsAt = Infinity;

function staircaseStore(io) {
  return io ? io.store("staircase") : taskStore(lastState, "staircase");
}

// -- a key pressed before the grating's frame -----------------------------------------------------

/** The trial whose grating is on: a key of the task with a time before its onset is stray. */
let early = null;

function onEarlyKey(event) {
  const e = early;
  if (!e || !(event.code in KEYS) || event.repeat) return;
  if (event.timeStamp >= e.onset) return;
  event.stopImmediatePropagation();
  event.preventDefault();
  if (!e.logged) {
    e.logged = true;
    e.io.event("stray", { key: event.code, awaiting: true, before_onset: true }, event.timeStamp);
  }
}

// -- drawing ------------------------------------------------------------------------------------

/** The runner's stream with the method names the logic module uses. */
function generator(stream) {
  return { int: stream.int, float: stream.next, pick: stream.pick, shuffle: stream.shuffle };
}

/** The canvas (it fills the stimulus area), the offscreen canvas of the grating and the practice
 *  feedback; one of each for the page, reused. */
let view = null;

function viewOf() {
  if (view) return view;
  const canvas = el("canvas", { id: "perception-canvas", "aria-hidden": "true" });
  Object.assign(canvas.style, { display: "block", width: "100%", height: "100%" });
  const feedback = feedbackNode("", { id: "perception-feedback" });
  feedback.hidden = true;
  const holder = el("div", { class: "perception-square" });
  Object.assign(holder.style, { position: "absolute", inset: "0" });
  holder.append(canvas, feedback);
  view = { holder, canvas, feedback, offscreen: document.createElement("canvas"), dpr: 0, colors: colors() };
  return view;
}

/** Puts the canvas in the background layer of the stimulus area (`io.background`: it stays
 *  between trials) and sizes its pixels to the area. */
function place(v, layer, sizeCss, dpr) {
  if (v.holder.parentNode !== layer) layer.append(v.holder);
  const px = Math.round(sizeCss * dpr);
  if (v.canvas.width !== px) v.canvas.width = px;
  if (v.canvas.height !== px) v.canvas.height = px;
  v.dpr = dpr;
  v.colors = colors();
}

function paintBlank(v) {
  const ctx = v.canvas.getContext("2d");
  ctx.fillStyle = v.colors.groundCss;
  ctx.fillRect(0, 0, v.canvas.width, v.canvas.height);
}

function paintFixation(v) {
  paintBlank(v);
  const ctx = v.canvas.getContext("2d");
  const c = v.canvas.width / 2;
  const arm = 12 * v.dpr;
  const thick = 3 * v.dpr;
  ctx.fillStyle = v.colors.stim;
  ctx.fillRect(c - arm, c - thick / 2, 2 * arm, thick);
  ctx.fillRect(c - thick / 2, c - arm, thick, 2 * arm);
}

const COMMON = {
  stimulus_ms: STIMULUS_MS,
  response_window_ms: RESPONSE_WINDOW_MS,
  contrast: CONTRAST,
  cycles_per_100_css_px: CYCLES_PER_100_CSS_PX,
  gamma_corrected: false,
};

// -- the task -----------------------------------------------------------------------------------

async function runTrial(trial, io) {
  const v = viewOf();
  const dpr = window.devicePixelRatio || 1;
  const sizeCss = io.area.getBoundingClientRect().width;
  if (!io.area.contains(v.holder)) {
    // the first trial, or the first after a pause or a skip took the background away: the plain
    // ground first
    // The cross at once (it takes over from the lead-in cross in the same frame).
    await io.background((layer) => {
      place(v, layer, sizeCss, dpr);
      paintFixation(v);
    });
  }
  const crossOn = await io.show(
    () => {
      place(v, v.holder.parentNode, sizeCss, dpr);
      v.feedback.hidden = true;
      paintFixation(v);
    },
    { part: "fixation" },
  );
  // The grating is drawn now, offscreen (one canvas and one ImageData, reused), and copied in at
  // its onset.
  const info = drawGrating(v.offscreen, {
    sizeCss,
    dpr,
    tiltDeg: trial.signed_tilt_deg,
    phaseDeg: trial.phase_deg,
    background: v.colors.ground,
    light: v.colors.light,
  });
  const drawn = {
    dpr,
    size_css: sizeCss,
    canvas_px: info.size_px,
    patch_px: info.patch_px,
    background_color: hex(info.background),
    peak_color: hex(info.peak),
  };
  const crossMs = trial.gap_ms + FIXATION_MS;
  // The frame period the runner uses for each wait is read just before it (it is measured
  // continuously, so it can move by a few microseconds within a trial).
  const crossFrameMs = io.frameMs;
  const crossFrames = framesFor(crossMs, crossFrameMs);
  const gratingDue = await io.wait(crossMs);
  const onset = await io.show(() => v.canvas.getContext("2d").drawImage(v.offscreen, 0, 0), {
    part: "grating",
    side: trial.side,
    tilt_deg: trial.tilt_deg,
    signed_tilt_deg: trial.signed_tilt_deg,
    phase_deg: trial.phase_deg,
    ...drawn,
  });
  early = { io, onset, logged: false };
  // The fixed window: every F/J press in it is logged, the last one is the answer.
  const answered = io.window({ ms: RESPONSE_WINDOW_MS, from: onset });
  answered.catch(() => {}); // an abandoned trial rejects it; the await below sees that too
  const stimFrameMs = io.frameMs;
  const stimFrames = framesFor(STIMULUS_MS, stimFrameMs);
  // The blank is due stimFrames after the grating's onset. It is shown with `showAt` at that due
  // time: a key pressed while the grating is on ends the runner's pending wait, and a plain
  // `show` would then put the blank in the next frame and cut the grating short.
  const blankDue = await io.wait(STIMULUS_MS);
  const offset = await io.showAt(blankDue, () => paintBlank(v), { part: "blank", frames: stimFrames });
  const answer = await answered;
  early = null;
  const frameMs = io.frameMs;
  const gratingLate = lateFrames(onset, gratingDue, frameMs);
  const blankLate = lateFrames(offset, blankDue, frameMs);
  const correct = isCorrect(trial.side, answer.answer);
  const result = {
    correct,
    ...drawn,
    frame_ms: frameMs,
    fixation_frame_ms: crossFrameMs,
    fixation_frames: crossFrames,
    fixation_ms_shown: onset - crossOn,
    stimulus_frame_ms: stimFrameMs,
    stimulus_frames: stimFrames,
    stimulus_ms_shown: offset - onset,
    grating_due_ms: gratingDue,
    grating_late_frames: gratingLate,
    blank_due_ms: blankDue,
    blank_late_frames: blankLate,
    late_frames: gratingLate + blankLate,
  };
  // The feedback in the frame the window ends, for FEEDBACK_MS, in practice and in the blocks.
  const text = feedbackText(answer.answer !== null, correct);
  await io.feedback(text, onset + RESPONSE_WINDOW_MS);
  result.feedback = text;
  if (io.practice) return result;
  if (performance.now() >= endsAt) return result; // cut short by the block's end: no move
  const store = staircaseStore(io);
  const step = updateStaircase(store.get(newStaircase()), correct);
  store.set(step.state);
  return {
    ...result,
    tilt_deg: step.tilt_deg,
    staircase_trial: step.staircase_trial,
    reversal: step.reversal,
    next_tilt_deg: step.state.tilt_deg,
  };
}

const screen = taskScreen({
  name: "perception",
  keys: KEYS,
  sides: { left: ["F", "tilted left"], right: ["J", "tilted right"], keys: ["KeyF", "KeyJ"] },
  enter: true,
  // The fixed length of a trial: the cross (gap + 500 ms), the response window from the grating's
  // onset (1500 ms) and the feedback (500 ms): 3100-3500 ms.
  trialMs: (t) => t.gap_ms + FIXATION_MS + RESPONSE_WINDOW_MS + FEEDBACK_MS,
  panel: true,
  block: (index) => ({
    instructions:
      index === 0
        ? "Keep your eyes on the cross. A striped patch appears for a moment, its stripes tilted " +
          "a little to the left or to the right. Press F for left, J for right. If you are not sure, " +
          "guess. If you press the wrong key, press the right one: your last press counts."
        : "Same as before: F if the stripes are tilted to the left, J if to the right.",
    practice: index === 0 ? PRACTICE_TRIALS : 0,
  }),
  trials({ blockRng, practice }) {
    const g = generator(blockRng);
    if (practice) {
      const list = makePracticeTrials(g);
      return ({ index }) => {
        if (index >= list.length) return null;
        const t = list[index];
        return { ...t, signed_tilt_deg: signedTilt(t.side, t.tilt_deg), staircase_trial: null, ...COMMON };
      };
    }
    const list = makeBlockTrials(g);
    return ({ index }) => {
      if (index >= list.length) return null;
      const t = list[index];
      const s = staircaseStore(null).get(newStaircase());
      return {
        ...t,
        tilt_deg: s.tilt_deg,
        signed_tilt_deg: signedTilt(t.side, s.tilt_deg),
        staircase_trial: s.trials,
        ...COMMON,
      };
    };
  },
  async run(trial, io) {
    try {
      return await runTrial(trial, io);
    } finally {
      if (early && early.io === io) early = null;
    }
  },
});

export const { tick, instructions, startsOnEnter } = screen;

export function mount(root, api) {
  endsAt = Infinity;
  screen.mount(root, api);
  window.addEventListener("keydown", onEarlyKey, true);
}

export function unmount(api) {
  window.removeEventListener("keydown", onEarlyKey, true);
  early = null;
  screen.unmount(api);
}

/** The runner's update, after noting the state (the task store's session id and step). */
export function update(state, api) {
  if (state) lastState = state;
  // the runner's own reckoning of the block's end (runner.js update)
  if (state && state.phase === "running" && api && api.ctx) endsAt = api.ctx.stateAt + state.remaining * 1000;
  screen.update(state, api);
}

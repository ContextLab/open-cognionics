// Rest: an instruction screen with Start, then a tone, the timed period (the fixation cross with
// eyes open; a nearly blank dark screen with eyes closed), a tone at its end, and Continue.
// The points total, the progress bar and the time left are hidden here.
//
// The tones are sines made with the Web Audio API (tone.js, as the calibration's tone): 440 Hz at
// the start and 660 Hz at the end, 300 ms each, faded in and out over 20 ms. Start is a press, so
// the audio may play. Each tone is logged as an event (`rest.tone_start`, `rest.tone_end`; the
// server puts an EEG marker for each) with the audio context's scheduled time of the tone.
// The start tone is played when the server says the period has begun (`running` after `waiting`),
// the end tone when it says the period is over (`between` after `running`).
import { el } from "../format.js";
import { leaveControls } from "./leave.js";
import { ensureAudio, playTone } from "../tone.js";

const SVG = "http://www.w3.org/2000/svg";
/** The two tones of a rest period. */
export const TONES = {
  start: { hz: 440, ms: 300, level: 0.3, rampMs: 20 },
  end: { hz: 660, ms: 300, level: 0.3, rampMs: 20 },
};
/** What the instruction screen says. */
export const WORDS = {
  closed: "Close your eyes when you hear the tone. Keep them closed and sit still. Open them when you hear the tone again.",
  open: "Look at the cross and sit still. A tone marks the start and the end.",
};

let body = null;
let actions = null;
let shown = "";
let leave = null;
let prevPhase = null;
let wrap = null;

function cross() {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("id", "fixation-cross");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Fixation cross");
  svg.setAttribute("viewBox", "0 0 360 360");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", "M180 120 V240 M120 180 H240");
  path.setAttribute("stroke", "#ffffff");
  path.setAttribute("stroke-width", "10");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("fill", "none");
  svg.append(path);
  return svg;
}

function minutes(seconds) {
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} minute${seconds === 60 ? "" : "s"}`;
  return `${Math.round(seconds)} seconds`;
}

function tone(api, which, state) {
  const spec = TONES[which];
  const at = performance.now();
  const played = playTone(spec);
  api.send({
    cmd: "event",
    name: `rest.tone_${which}`,
    payload: {
      hz: spec.hz,
      ms: spec.ms,
      level: spec.level,
      ramp_ms: spec.rampMs,
      block: state.block,
      played: played !== null,
      audio_time: played ? played.audioTime : null,
      output_latency: played ? played.outputLatency : null,
      base_latency: played ? played.baseLatency : null,
      context_time: played ? played.contextTime : null,
      context_performance_time: played ? played.performanceTime : null,
    },
    browser_time: at,
    time_origin: performance.timeOrigin,
  });
}

export function mount(root, api) {
  wrap = el("div", { class: "rest" });
  body = el("div", { class: "rest-body" });
  actions = el("div", { class: "rest-actions" });
  leave = leaveControls(api);
  wrap.append(body, actions, leave.root);
  root.replaceChildren(wrap);
  shown = "";
  prevPhase = api.ctx.state ? api.ctx.state.phase : null;
}

export function unmount() {
  prevPhase = null;
}

export function update(state, api) {
  leave.update(state);
  const open = state.step.includes("open");
  // The tones follow the server's phase: the period began, or it is over.
  if (state.phase === "running" && prevPhase === "waiting") tone(api, "start", state);
  if (state.phase === "between" && prevPhase === "running") tone(api, "end", state);
  prevPhase = state.phase;
  const key = `${state.phase}/${open}`;
  if (key === shown) return;
  shown = key;
  wrap.dataset.phase = state.phase;
  wrap.dataset.eyes = open ? "open" : "closed";
  // Nothing but the cross (or the dark) while the period runs.
  leave.root.hidden = state.phase === "running";
  if (state.phase === "between" || state.phase === "done") {
    body.replaceChildren(
      el("h1", { class: "rest-title" }, "The rest is over"),
      el("p", { class: "instruction" }, "Press Continue when you are ready."),
    );
    actions.replaceChildren(api.advanceButton("Continue", "btn btn--light"));
    return;
  }
  if (state.phase === "running" || state.phase === "paused") {
    body.replaceChildren(...(open ? [cross()] : []));
    actions.replaceChildren();
    return;
  }
  const step = api.ctx.flow && api.ctx.flow.find((s) => s.id === state.step);
  const seconds = step && step.blocks[0] ? step.blocks[0].seconds : null;
  const start = api.advanceButton("Start", "btn btn--light");
  // The press that starts the rest is the one that allows the tones.
  start.addEventListener("click", () => ensureAudio(), true);
  body.replaceChildren(
    el("h1", { class: "rest-title" }, api.stepTitle(state.step)),
    el("p", { class: "instruction", id: "rest-instruction" }, open ? WORDS.open : WORDS.closed),
    ...(seconds ? [el("p", { class: "rest-length" }, `This rest lasts ${minutes(seconds)}.`)] : []),
  );
  actions.replaceChildren(start);
}

export function tick() {}

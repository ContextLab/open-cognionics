// Device setup: three illustrated steps, the state of the link, live traces and a tip.
//
// What it draws from: the last `link` message (state and `problem`), the last `status` message
// (rate, lost samples, battery), the `hello` (labels, and the file name of a replay) and the
// `trace` messages, which are asked for with `{cmd:"display"}` while this screen is shown.
// Continue is enabled while the link is live; the server refuses it otherwise.
import { el } from "../format.js";
import { chooseTip } from "../tips.js";
import { TraceView } from "../traces.js";
import { leaveControls } from "./leave.js";

const STEPS = [
  {
    id: "power",
    title: "Switch the headset on",
    text: "Press the power button on the box on top of the headset.",
    image: "/static/assets/device/step-power.svg",
    alt: "A headset box with its power button pressed",
  },
  {
    id: "dongle",
    title: "Plug in the dongle",
    text: "Push the USB dongle into a USB port of this computer.",
    image: "/static/assets/device/step-dongle.svg",
    alt: "A USB dongle being pushed into a computer",
  },
  {
    id: "seat",
    title: "Put the headset on",
    text: "Seat the band on the head, the forehead pads on the forehead, and clip the ear clips to the earlobes.",
    image: "/static/assets/device/step-seat.svg",
    alt: "A head seen from the side with the headset band, forehead pads and an ear clip",
  },
];

const LINK_WORDS = {
  live: "Live",
  connecting: "Connecting",
  ended: "Ended",
};

let nodes = null;
let leave = null;
let view = null;
let viewLabels = "";

function stat(id, caption) {
  const value = el("div", { class: "stat-value", id });
  const box = el("div", { class: "stat" });
  box.append(value, el("div", { class: "stat-caption" }, caption));
  return { box, value };
}

export function mount(root, api) {
  const wrap = el("div", { class: "device" });
  const head = el("div", { class: "device-head" });
  head.append(el("h1", { id: "device-title" }, "Device setup"));

  const steps = el("ol", { class: "device-steps", id: "device-steps" });
  for (const [i, step] of STEPS.entries()) {
    const li = el("li", { "data-step": step.id });
    const picture = el("img", { src: step.image, alt: step.alt, width: "240", height: "160" });
    const words = el("div", { class: "step-words" });
    words.append(el("div", { class: "step-num" }, String(i + 1)), el("h2", {}, step.title), el("p", {}, step.text));
    li.append(picture, words);
    steps.append(li);
  }

  const replay = el("p", { class: "device-replay", id: "device-replay" });
  replay.hidden = true;

  const link = el("div", { class: "device-link", id: "device-link", role: "status" });
  const linkWord = el("span", { class: "link-word", id: "device-link-word" });
  const linkNote = el("span", { class: "link-note", id: "device-link-note" });
  link.append(el("span", { class: "link-dot", "aria-hidden": "true" }), linkWord, linkNote);

  const rate = stat("device-rate", "samples a second");
  const lost = stat("device-lost", "samples lost");
  const battery = stat("device-battery", "battery reading");
  const stats = el("div", { class: "device-stats" });
  stats.append(rate.box, lost.box, battery.box);

  const tip = el("p", { class: "device-tip", id: "device-tip", role: "status" });
  tip.hidden = true;

  const canvas = el("canvas", {
    class: "device-traces",
    id: "device-traces",
    "aria-label": "Live traces of the EEG channels, one row for each",
  });
  const scale = el("p", { class: "device-scale", id: "device-scale" });
  // Where the traces will be, until they come: an outlined box that says what is awaited.
  const waiting = el("div", { class: "device-waiting", id: "device-waiting", role: "status" });

  leave = leaveControls(api);
  // Continue says where it leads when the next step is the calibration (the contact check first).
  const plan = api.planFlow();
  const next = plan[plan.findIndex((s) => s.id === "device") + 1];
  const go = api.advanceButton(next && next.id === "calibration" ? "Continue to contact check" : "Continue");
  const actions = el("div", { class: "row device-actions" });
  actions.append(go, leave.root);

  const status = el("div", { class: "device-status" });
  status.append(link, stats, tip);
  const live = el("div", { class: "device-live" });
  live.append(waiting, canvas, scale);
  wrap.append(head, steps, replay, status, live, actions);
  root.replaceChildren(wrap);
  nodes = { steps, replay, link, linkWord, linkNote, rate, lost, battery, tip, canvas, scale, waiting, go, pressedAt: -1e9 };
  go.addEventListener("click", () => (nodes.pressedAt = performance.now()));
  view = null;
  viewLabels = "";
  api.setTraceSink((rows) => view && view.push(rows));
  api.setDisplay(true);
}

export function unmount(api) {
  api.setDisplay(false);
  api.setTraceSink(null);
  if (view) view.stop();
  view = null;
  viewLabels = "";
  nodes = null;
}

/** The text of the battery reading, as read: no judgement of it. */
export function batteryText(volts) {
  return typeof volts === "number" ? `${volts.toFixed(2)} V` : "not read";
}

export function update(state, api) {
  if (!nodes) return;
  const ctx = api.ctx;
  const hello = ctx.hello;
  const replay = !!hello && hello.kind === "replay";
  const link = ctx.link;
  const linkState = link ? link.state : "connecting";
  const live = linkState === "live";
  const stats = live ? ctx.stats : null;

  nodes.steps.hidden = replay;
  nodes.replay.hidden = !replay;
  if (replay) {
    nodes.replay.textContent =
      `This session runs from a recording${hello.name ? `: ${hello.name}` : ""}. ` +
      "There is no headset to set up; the traces below are the recording being played.";
  }

  nodes.link.dataset.state = linkState;
  nodes.link.dataset.live = String(live);
  nodes.link.dataset.problem = link && link.problem ? link.problem.code : "";
  nodes.link.dataset.attempt = link && link.problem ? String(link.problem.attempt) : "";
  nodes.linkWord.textContent = replay && live ? "Replay" : link && link.problem && link.problem.dropped ? "Dropped" : LINK_WORDS[linkState] || linkState;
  const attempt = link && link.problem && link.problem.attempt ? ` (attempt ${link.problem.attempt})` : "";
  nodes.linkNote.textContent = live ? (replay ? "playing the recording" : "Headset connected") : link && link.problem ? `trying again${attempt}` : "waiting for the headset";

  const rate = stats && stats.rate !== null && stats.rate !== undefined ? stats.rate : null;
  nodes.rate.value.textContent = rate === null ? "–" : String(Math.round(rate));
  nodes.rate.value.dataset.hz = rate === null ? "" : String(rate);
  // Which status message the figures are from: it advances with each new reading, even when the
  // rate is the same as the last one.
  nodes.rate.value.dataset.reading = stats ? String(ctx.statusCount || 0) : "";
  nodes.lost.value.textContent = stats ? String(stats.lost) : "–";
  nodes.lost.value.dataset.lost = stats ? String(stats.lost) : "";
  nodes.battery.value.textContent = stats ? batteryText(stats.bat) : "–";
  nodes.battery.value.dataset.volts = stats && typeof stats.bat === "number" ? String(stats.bat) : "";

  const tip = replay ? null : chooseTip(link, stats);
  nodes.tip.hidden = !tip;
  nodes.tip.textContent = tip ? tip.text : "";
  nodes.tip.dataset.tip = tip ? tip.code : "";

  // Traces: made once the channel names are known (after the first `hello`).
  const labels = hello ? hello.labels : null;
  if (labels && labels.join(",") !== viewLabels) {
    if (view) view.stop();
    viewLabels = labels.join(",");
    view = new TraceView(nodes.canvas, labels);
    view.start();
  }
  nodes.canvas.dataset.live = String(live);
  // Until the first rows have come there is nothing to draw, and the room is left to the rest.
  const have = !!view && view.total > 0;
  nodes.canvas.hidden = !have;
  nodes.waiting.hidden = have;
  const waitText = live ? "Waiting for signal…" : "Waiting for the headset…";
  if (nodes.waiting.textContent !== waitText) nodes.waiting.textContent = waitText;
  nodes.scale.textContent = !have
    ? "The traces appear in this box once the headset sends data."
    : live
      ? "last 8 seconds, one row for each channel"
      : "the link is down; no new data";

  // The server refuses Continue until the link is live and the recording has started.
  // (the button re-enables itself a few seconds after a press, see `advanceButton`).
  if (!live) nodes.go.disabled = true;
  else if (performance.now() - nodes.pressedAt > 3000) nodes.go.disabled = false;
  nodes.go.dataset.ready = String(live);
  leave.update(state);
}

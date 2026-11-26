// Calibration: the contact check on the 3-D fit view, then eyes open, eyes closed and blinks.
//
// The server owns the sub-step (`state.substep`) and its clock; this screen draws them. During
// `contact` the `contact` messages give the state of every electrode over the last second: the
// fit view colours the contacts, the panel counts the good ones and lists the others as pills
// (a pill brings its contact forward). "Check again" asks for the check again; Continue (the
// `advance` command, enabled once the check that is on has been measured) keeps the states of
// that moment, leaves the bad electrodes out and goes on. `settling` ("Getting ready") lasts until
// the data show the check off; `not_off` says it did not go off (Check again or Stop).
// The tone that ends eyes closed is made here with the Web Audio API and logged as the event
// `tone`; each blink cue is logged as the event `blink_cue` with the page's clock, its place in
// the schedule (`due_s`, by the server's clock) and how late it was drawn (`late_ms`). A cue is
// never left out: one drawn late is logged as late.
import { el, formatTime } from "../format.js";
import { WORDS, contactPanel } from "./contact-panel.js";
import { leaveControls } from "./leave.js";
import * as setup from "./setup.js";
import { audioReady, ensureAudio, playTone as play } from "../tone.js";
import { continueLabel, createSignalPanel, toneNote } from "./signal-check.js";

/** The sub-steps that show the fit view: the contact check and the signal check after it. */
const VIEW_SUBS = ["contact", "signal", "signal_done"];
/** The sub-steps that keep it (hidden while settling), so that one WebGL context serves both. */
const KEEP_VIEW = [...VIEW_SUBS, "settling"];

export { WORDS };
/** The tone at the end of eyes closed: a sine, faded in and out over 20 ms. */
export const TONE = { hz: 880, ms: 500, level: 0.3 };
const CUE_MS = 400;

function playTone() {
  return play(TONE) !== null;
}

const TEST = new URLSearchParams(location.search).has("test");

let nodes = null;
let plain = false; // a calibration step without sub-steps (a flow file may have one): the setup screen
let leave = null;
let check = null; // the contact panel (contact-panel.js)
let screenRoot = null;
let prevSub = null;
let cues = null; // { sent: [bool], frame, offAt } while the blinks run
let gesture = null;

function logEvent(api, name, payload, at = performance.now()) {
  api.send({ cmd: "event", name, payload, browser_time: at, time_origin: performance.timeOrigin });
}

function fixationCross() {
  const SVG = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "calib-cross");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Fixation cross");
  svg.setAttribute("viewBox", "0 0 120 120");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", "M60 30 V90 M30 60 H90");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "6");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

export function mount(root, api) {
  screenRoot = root;
  plain = !(api.ctx.state && api.ctx.state.substep);
  if (plain) {
    setup.mount(root, api);
    return;
  }
  const wrap = el("div", { class: "calib" });

  // contact: the 3-D view and the panel (the same view as the headset check of the paused screen)
  check = contactPanel({
    ids: { root: "calib-contact", stage: "calib-view", count: "contact-count", pills: "contact-pills", excluded: "contact-excluded", off: "contact-off", noTone: "contact-no-tone" },
    title: "Contact check",
    offText: "The contact check is off, because the page was reloaded or closed. Press Check again to measure.",
    excludedText: (bad) => `If you continue now, these are left out: ${bad.join(", ")}.`,
  });
  const contact = check.root;
  const signal = createSignalPanel();
  contact.append(signal.root);
  const again = el("button", { class: "btn btn--quiet", id: "check-again", type: "button" }, "Check again");
  again.addEventListener("click", () => api.send({ cmd: "check_again" }));

  // the timed parts and the end
  const timed = el("div", { class: "calib-timed", id: "calib-timed" });
  const title = el("h1", { id: "calib-title" });
  const text = el("p", { id: "calib-text" });
  const sound = el("p", { class: "calib-off", id: "calib-no-sound" });
  sound.hidden = true;
  const cross = fixationCross();
  const cue = el("div", { class: "blink-cue", id: "blink-cue", "data-on": "false", "aria-hidden": "true" });
  const cueCount = el("p", { class: "calib-note", id: "blink-count" });
  const left = el("p", { class: "calib-left", id: "calib-left" });
  timed.append(title, text, sound, cross, cue, cueCount, left);
  const notOff = el("div", { class: "calib-done", id: "calib-not-off", role: "alert" });
  notOff.append(
    el("h1", {}, "The contact check did not go off"),
    el("p", {}, "The headset still reports its contact check on, so eyes open was not started. Press Check again to go back to the contact check, or Stop."),
  );

  const done = el("div", { class: "calib-done", id: "calib-done" });

  leave = leaveControls(api);
  const go = api.advanceButton("Continue");
  go.addEventListener("click", () => (pressedAt = performance.now()));
  pressedAt = -1e9;
  const actions = el("div", { class: "row calib-actions" });
  actions.append(go, again, leave.root);
  wrap.append(contact, timed, notOff, done, actions);
  root.replaceChildren(wrap);
  nodes = { contact, panel: check.panel, signal, again, timed, notOff, title, text, sound, cross, cue, cueCount, left, done, go };
  prevSub = null;
  cues = null;
  // Any press on this screen may start the audio (browsers allow sound only after one).
  gesture = () => ensureAudio();
  document.addEventListener("pointerdown", gesture, true);
  document.addEventListener("keydown", gesture, true);
  if (TEST) window.__calibration = { get view() { return check && check.view; }, get loading() { return !!check && check.loading; }, audioReady };
}

function disposeView() {
  if (check) check.dispose();
}

export function unmount() {
  plain = false;
  disposeView();
  check = null;
  stopCues(null);
  if (gesture) {
    document.removeEventListener("pointerdown", gesture, true);
    document.removeEventListener("keydown", gesture, true);
  }
  gesture = null;
  if (screenRoot) {
    delete screenRoot.dataset.substep;
    delete screenRoot.dataset.check;
  }
  nodes = null;
}

let pressedAt = -1e9;
function drawContact(state, api) {
  const ready = check.draw(state, api, screenRoot);
  // Continue waits for a measure of the check that is on now (the server refuses it before).
  nodes.go.disabled = !ready || performance.now() - pressedAt < 3000;
  nodes.go.dataset.ready = String(ready);
}

function drawDone(state) {
  const result = state.calibration;
  const parts = [el("h1", {}, "Calibration is done")];
  if (!result || !Object.keys(result.states).length) {
    parts.push(el("p", {}, "The contacts were not measured."));
  } else {
    const names = Object.keys(result.states);
    const good = names.filter((n) => result.states[n] === "good").length;
    parts.push(el("p", {}, `${good} of ${names.length} contacts were good when you continued.`));
    parts.push(el("p", {}, result.excluded.length ? `Left out: ${result.excluded.join(", ")}.` : "No contact is left out."));
  }
  const key = JSON.stringify(result);
  if (nodes.done.dataset.key !== key) {
    nodes.done.dataset.key = key;
    nodes.done.replaceChildren(...parts);
  }
}

function timing(api, state) {
  const step = api.ctx.flow && api.ctx.flow.find((s) => s.id === state.step);
  return step && step.calibration;
}

function drawTimed(state, api) {
  const sub = state.substep;
  const t = timing(api, state) || { eyes_closed: 20, blink_cues: 5 };
  const noSound = !audioReady();
  nodes.cross.style.display = sub === "eyes_open" ? "" : "none";
  nodes.cue.style.display = sub === "blinks" ? "" : "none";
  nodes.cueCount.hidden = sub !== "blinks";
  if (sub === "settling" && state.link_down) {
    nodes.title.textContent = "Waiting for the headset";
    nodes.text.textContent =
      "The headset connection was lost. Eyes open starts by itself once the connection is back.";
  } else if (sub === "settling") {
    nodes.title.textContent = "Getting ready";
    nodes.text.textContent = "The contact check is going off. Eyes open starts in a moment.";
  } else if (sub === "eyes_open") {
    nodes.title.textContent = "Eyes open";
    nodes.text.textContent = `Look at the cross and keep your eyes open. Next you close your eyes for ${Math.round(t.eyes_closed)} seconds; a tone says when to open them.`;
  } else if (sub === "eyes_closed") {
    nodes.title.textContent = "Close your eyes";
    nodes.text.textContent = "Close your eyes now and keep them closed until you hear a tone.";
  } else {
    nodes.title.textContent = "Open your eyes";
    nodes.text.textContent = `Blink once each time the circle lights up. There are ${t.blink_cues}.`;
  }
  // Said before eyes closed begins (and during it) when no sound can be played.
  nodes.sound.hidden = !(noSound && (sub === "eyes_open" || sub === "eyes_closed"));
  nodes.sound.textContent = `No sound can be played in this window, so there will be no tone. Count to about ${Math.round(t.eyes_closed)}, then open your eyes: this screen will say "Open your eyes".`;
}

function stopCues(api, ended = false) {
  if (cues && cues.frame) cancelAnimationFrame(cues.frame);
  // The blinks ended (the step is done, not skipped) before a cue could be drawn (a page held
  // up): it is still logged, as not shown, so that every cue of the schedule has its line.
  if (cues && api && ended && cues.t) {
    const now = performance.now();
    for (let k = 0; k < cues.t.blink_cues; k++) {
      if (cues.sent[k]) continue;
      cues.sent[k] = true;
      const due = cues.t.blink_lead + k * cues.t.blink_gap;
      logEvent(api, "blink_cue", { n: k + 1, of: cues.t.blink_cues, due_s: due, late_ms: null, shown: false }, now);
    }
  }
  cues = null;
}

function runCues(api) {
  if (cues) return;
  cues = { sent: [], frame: 0, offAt: 0, t: null };
  // Cues the server has already logged in these blinks (this page was reloaded) are not sent again.
  for (const n of (api.ctx.state && api.ctx.state.cues_logged) || []) cues.sent[n - 1] = true;
  const loop = (now) => {
    if (!cues || !nodes) return;
    const state = api.ctx.state;
    const t = state && timing(api, state);
    if (!state || state.substep !== "blinks" || !t) {
      stopCues(api, !!state && state.substep === "done");
      return;
    }
    cues.t = t;
    const left = api.remaining();
    if (left !== null && state.phase === "running") {
      // Where the blinks are by the server's clock; each cue is due at its place in the schedule.
      const elapsed = t.blinks - left;
      for (let k = 0; k < t.blink_cues; k++) {
        if (cues.sent[k]) continue;
        const due = t.blink_lead + k * t.blink_gap;
        if (elapsed < due) break;
        cues.sent[k] = true;
        nodes.cue.dataset.on = "true";
        nodes.cue.dataset.n = String(k + 1);
        nodes.cueCount.textContent = `${k + 1} of ${t.blink_cues}`;
        cues.offAt = now + CUE_MS;
        const lateMs = Math.max(0, Math.round((elapsed - due) * 1000));
        logEvent(api, "blink_cue", { n: k + 1, of: t.blink_cues, due_s: due, late_ms: lateMs, shown: true }, now);
      }
    }
    if (cues.offAt && now >= cues.offAt) {
      nodes.cue.dataset.on = "false";
      cues.offAt = 0;
    }
    cues.frame = requestAnimationFrame(loop);
  };
  cues.frame = requestAnimationFrame(loop);
}

export function update(state, api) {
  if (plain) {
    setup.update(state, api);
    return;
  }
  if (!nodes) return;
  const sub = state.substep || null;
  screenRoot.dataset.substep = sub || "";
  if (sub !== "contact") delete screenRoot.dataset.check;
  nodes.contact.hidden = !VIEW_SUBS.includes(sub);
  nodes.panel.hidden = sub !== "contact";
  nodes.signal.root.hidden = !(sub === "signal" || sub === "signal_done");
  nodes.timed.hidden = !["settling", "eyes_open", "eyes_closed", "blinks"].includes(sub);
  nodes.notOff.hidden = sub !== "not_off";
  nodes.done.hidden = sub !== "done";
  nodes.again.hidden = !(sub === "contact" || sub === "not_off" || sub === "signal_done");
  nodes.go.hidden = !(sub === "contact" || sub === "done" || sub === null || sub === "signal_done");
  // After the signal check Continue says what it goes on with ("Continue with noisy channels").
  const goText = sub === "signal_done" ? continueLabel(state.calibration) : "Continue";
  if (nodes.go.textContent !== goText) nodes.go.textContent = goText;

  if (VIEW_SUBS.includes(sub)) check.load();
  else if (!KEEP_VIEW.includes(sub)) disposeView();
  if (sub === "contact") drawContact(state, api);
  if (sub === "signal" || sub === "signal_done") {
    // Continue is always allowed once the signal check has a result (the operator decides).
    if (sub === "signal_done") nodes.go.disabled = performance.now() - pressedAt < 3000;
    const view = check.view;
    const key = JSON.stringify([sub, state.calibration, !!view]);
    nodes.signal.update(state, api, key !== lastSignal ? view : null);
    lastSignal = key;
    check.repaint(); // the view now shows the signal colors: the contact check paints again
  } else if (sub === "contact") {
    lastSignal = "";
  }
  if (!nodes.timed.hidden) drawTimed(state, api);
  if (sub === "done") drawDone(state);

  // The tone: requested when eyes closed has ended (the server has moved on to the blinks).
  if (prevSub === "eyes_closed" && sub === "blinks") {
    const played = playTone();
    logEvent(api, "tone", { hz: TONE.hz, ms: TONE.ms, level: TONE.level, audio: played ? "played" : "unavailable" });
  }
  // A pause ends this run of the cues: on Resume they start again from the server's list of
  // cues logged in these blinks (empty when a lost link restarted the part, so all are shown
  // again; the ones already logged after a plain pause, so none is shown twice).
  if (sub === "blinks" && state.phase === "paused") stopCues(api);
  else if (sub === "blinks") runCues(api);
  else stopCues(api, sub === "done" && prevSub === "blinks");
  prevSub = sub;
  tick(api);
  leave.update(state);
}

let lastSignal = "";

export function tick(api) {
  if (!nodes) return;
  nodes.signal.tick(api);
  const state = api.ctx.state;
  const left = api.remaining();
  const timed = state && ["eyes_open", "eyes_closed", "blinks"].includes(state.substep);
  nodes.left.textContent = timed && left !== null ? `${formatTime(left)} left` : "";
}

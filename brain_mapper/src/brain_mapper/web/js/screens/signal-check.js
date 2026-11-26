// The signal check, shown on the calibration screen after the contact check (see quality.py).
//
// `signal` (the server's sub-step) collects about 6 s of data with the tone off: a countdown.
// `signal_done` shows the result: the fit view colors each electrode by the worse of its contact
// state and its signal state, the headline says the exception first in plain words ("Fp1 is noisy.
// All 20 contacts are good."), one plain line says what to do when a channel is not fine, and the
// list behind "All channels" gives both states of every channel in words. Continue then says what
// it goes on with ("Continue with noisy channels"). Continue (eyes open) is always allowed; Check
// again starts the contact check again, and then this check. The words are also used by the
// onlooker (`headline`, `SIGNAL_WORDS`).
import { el } from "../format.js";
import { STATE_COLORS } from "../scene3d/headset.js";

/** The words on screen for the four signal states. */
export const SIGNAL_WORDS = { fine: "Fine", noisy: "Noisy", clipped: "Clipped", flat: "Flat" };
/** The contact words (as on the contact check). */
const CONTACT_WORDS = { good: "Good", marginal: "Weak", bad: "No contact" };
/** The fit view's colors for a signal state (the three of the contact check). */
const AS_CONTACT = { fine: "good", noisy: "marginal", clipped: "bad", flat: "bad" };
const RANK = { good: 0, marginal: 1, bad: 2 };

let styled = false;
function ensureStyle() {
  if (styled) return;
  styled = true;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/css/signal-check.css";
  document.head.append(link);
}

/** "17 of 20 contacts good; 3 channels noisy: Fp1, Fp2, Pz" from a calibration result. */
export function headline(calibration) {
  const contactStates = (calibration && calibration.states) || {};
  const signal = calibration && calibration.signal;
  const names = Object.keys(contactStates);
  const good = names.filter((n) => contactStates[n] === "good").length;
  const parts = [names.length ? `${good} of ${names.length} contacts good` : "Contacts not measured"];
  if (!signal || !signal.states || !Object.keys(signal.states).length) {
    parts.push(signal && signal.error ? `signal not measured (${signal.error})` : "signal not measured");
    return parts.join("; ");
  }
  const states = signal.states;
  const total = Object.keys(states).length;
  const bad = { noisy: [], clipped: [], flat: [] };
  for (const [name, state] of Object.entries(states)) if (bad[state]) bad[state].push(name);
  const notFine = bad.noisy.length + bad.clipped.length + bad.flat.length;
  if (!notFine) {
    parts.push(`all ${total} channels fine`);
  } else {
    for (const state of ["noisy", "clipped", "flat"]) {
      const list = bad[state];
      if (list.length) parts.push(`${list.length} ${list.length === 1 ? "channel" : "channels"} ${state}: ${list.join(", ")}`);
    }
  }
  return parts.join("; ");
}

/** Names in words: "Fp1", "Fp1 and F7", "Fp1, F7 and O1". */
function listed(names) {
  return names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The channels whose signal is not fine, by state, in the order of the result. */
function notFineByState(calibration) {
  const states = (calibration && calibration.signal && calibration.signal.states) || {};
  const bad = { noisy: [], clipped: [], flat: [] };
  for (const [name, state] of Object.entries(states)) if (bad[state]) bad[state].push(name);
  return bad;
}

/** The result in plain words, the exception first: "Fp1 is noisy. All 20 contacts are good." or
 *  "All 20 channels look fine. 18 of 20 contacts are good." */
export function resultSentence(calibration) {
  const contactStates = (calibration && calibration.states) || {};
  const signal = calibration && calibration.signal;
  const names = Object.keys(contactStates);
  const good = names.filter((n) => contactStates[n] === "good").length;
  const contacts = !names.length
    ? "The contacts were not measured."
    : good === names.length
      ? `All ${names.length} contacts are good.`
      : `${good} of ${names.length} contacts are good.`;
  if (!signal || !signal.states || !Object.keys(signal.states).length) {
    return `${signal && signal.error ? `The signal could not be checked (${signal.error}).` : "The signal was not checked."} ${contacts}`;
  }
  const bad = notFineByState(calibration);
  const parts = [];
  for (const state of ["noisy", "clipped", "flat"]) {
    const list = bad[state];
    if (list.length) parts.push(`${listed(list)} ${list.length === 1 ? "is" : "are"} ${state}.`);
  }
  if (!parts.length) parts.push(`All ${Object.keys(signal.states).length} channels look fine.`);
  return `${parts.join(" ")} ${contacts}`;
}

/** What Continue says after the signal check: "Continue", or what it goes on with when a channel
 *  is not fine ("Continue with noisy channels", "Continue with a flat channel", ...). */
export function continueLabel(calibration) {
  const bad = notFineByState(calibration);
  const states = ["noisy", "clipped", "flat"].filter((k) => bad[k].length);
  if (!states.length) return "Continue";
  const count = states.reduce((n, k) => n + bad[k].length, 0);
  if (states.length > 1) return "Continue with channels that are not fine";
  return count === 1 ? `Continue with a ${states[0]} channel` : `Continue with ${states[0]} channels`;
}

/** The line shown on the contact check when the tone is not there at all (else ""). */
export function toneNote(message) {
  return message && message.tone_seen === false
    ? "The contact-check tone is not showing on any electrode, so these contact states may be wrong. The signal check after Continue looks at the signal itself."
    : "";
}

/** The color state of an electrode: the worse of its contact and its signal. */
export function combined(contactState, signalState) {
  const a = contactState in RANK ? contactState : "good";
  const b = AS_CONTACT[signalState] || "good";
  return RANK[a] >= RANK[b] ? a : b;
}

function swatch(state) {
  const dot = el("span", { class: "swatch", "aria-hidden": "true" });
  dot.style.background = STATE_COLORS[state];
  return dot;
}

export function createSignalPanel() {
  ensureStyle();
  const root = el("div", { class: "signal-panel", id: "signal-panel" });
  const title = el("h1", { id: "signal-title" }, "Signal check");
  const count = el("p", { class: "contact-count signal-headline", id: "signal-headline", role: "status" });
  const left = el("p", { class: "signal-left", id: "signal-left" });
  const warn = el("p", { class: "calib-off signal-warn", id: "signal-warn" });
  warn.hidden = true;
  const list = el("ul", { class: "signal-list", id: "signal-list", "aria-label": "Contact and signal of each electrode" });
  // The full list, contact and signal of every channel, behind a disclosure.
  const all = el("details", { class: "signal-all", id: "signal-all" });
  all.append(el("summary", {}, "All channels"), list);
  all.hidden = true;
  root.append(title, count, left, warn, all);
  let listKey = "";

  function update(state, api, view) {
    const sub = state.substep;
    const result = state.calibration || null;
    if (sub === "signal") {
      title.textContent = "Signal check";
      const paused = state.phase === "paused";
      count.textContent = paused
        ? "Paused: the signal check starts again on Resume."
        : "Looking at the signal with the contact check off. Sit still and look at the screen.";
      delete count.dataset.done;
      warn.hidden = true;
      all.hidden = true;
      list.replaceChildren();
      listKey = "";
      tick(api);
      return;
    }
    left.textContent = "";
    const signal = result && result.signal;
    count.dataset.done = "true";
    count.textContent = resultSentence(result);
    all.hidden = false;
    const notFine = (signal && signal.not_fine) || [];
    warn.hidden = !(notFine.length || (signal && signal.error));
    warn.textContent = signal && signal.error
      ? "The signal could not be checked. Press Check again, or Continue to record anyway."
      : "Some channels are not fine. Adjust those electrodes and press Check again, or Continue to record with them as they are.";
    const contactStates = (result && result.states) || {};
    const signalStates = (signal && signal.states) || {};
    const names = Object.keys(signalStates).length ? Object.keys(signalStates) : Object.keys(contactStates);
    const colors = Object.fromEntries(names.map((n) => [n, combined(contactStates[n], signalStates[n])]));
    if (view && !view.unavailable && names.length) {
      const known = view.headset ? view.headset.contacts.map((x) => x.name) : names;
      view.setStates(Object.fromEntries(Object.entries(colors).filter(([n]) => known.includes(n))));
    }
    const key = JSON.stringify([contactStates, signalStates]);
    if (key === listKey) return;
    listKey = key;
    const items = names.map((name) => {
      const c = contactStates[name];
      const s = signalStates[name];
      const item = el("li", { "data-contact": name, "data-signal": s || "", "data-state": c || "" });
      item.append(
        swatch(colors[name]),
        el("span", { class: "signal-name" }, name),
        el("span", { class: "signal-words" }, `${CONTACT_WORDS[c] || "Not measured"} · ${SIGNAL_WORDS[s] || "Not measured"}`),
      );
      if (s && s !== "fine") item.classList.add("is-off");
      return item;
    });
    list.replaceChildren(...items);
  }

  function tick(api) {
    const state = api.ctx.state;
    if (!state || state.substep !== "signal") return;
    const s = api.remaining();
    left.textContent = s === null || state.phase === "paused" ? "" : `${Math.max(0, Math.ceil(s))} s left`;
  }

  return { root, update, tick };
}

// The onlooker window: the live display for someone watching, next to the participant's page.
//
// It never controls the session. Its websocket (role=onlooker) sends one command only, `display`
// (ask for `trace` messages), which the server allows every client; the server ignores every
// session command from an onlooker as well. It is not full screen and has no buttons.
//
// What it shows, and where it comes from:
//   - the network view, fed by assets/sample-network.json, with "Sample data" beside it at all
//     times; the score place reads "not measured yet" (nothing is computed in this stage);
//   - the headset with contact states: live ones from a `contact` message ({states, good}) and
//     the ones saved at calibration from a `calibration` field of the `state` message; without
//     either it says the contact check is not done yet;
//   - live traces (`trace` messages), the step and block, the score total and the progress,
//     which follow the server's state and change only between blocks, as on the participant's page.
// It shows no stimulus content of any task, and not the participant's label (no message to an
// onlooker carries it).
//
// Memory: traces live in a fixed ring buffer (traces.js); every state replaces the last one; the
// DOM is built once. After the server restarts, the socket is opened again every second.

import { formatTime } from "./format.js";
import { totalText } from "./scores.js";
import { TraceView } from "./traces.js";
import { showMessage } from "./scene3d/stage.js";
import { showSignal } from "./onlooker-signal.js";

const params = new URLSearchParams(location.search);
const testing = params.get("test") === "1";
const RETRY_MS = 1000;
/** A calibration field that did not change is ignored while a contact message came this recently. */
const LIVE_MS = 3000;
const GOOD_WORDS = { good: true, marginal: true, bad: true };
const STOPPED = "The 3-D view stopped. Trying to bring it back.";
const GAVE_UP = "The 3-D view could not be restarted. Reload this window.";
/** How long a lost WebGL context is given to come back before the view is built again. */
const RESTORE_MS = testing && Number(params.get("restore_ms")) > 0 ? Number(params.get("restore_ms")) : 3000;
/** How many times a view is built again after a lost context before the page gives up on it. */
const MAX_REBUILDS = 3;

const hook = { ready: false, status: "loading", errors: [], lastState: null };
if (testing) window.__onlooker = hook;

const $ = (id) => document.getElementById(id);
const root = $("ol-root");
const ctx = { state: null, stateAt: 0, flow: null, hello: null, link: null, firstState: true };
const shown = { points: null };
let socket = null;
let displayOn = true;
let traces = null;
let traceLabels = "";
const slots = {};
let contact = { source: "none", states: {}, good: null, at: 0, savedJson: "" };

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function send(text) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(text);
  return true;
}

function askForTraces() {
  send(JSON.stringify({ cmd: "display", on: displayOn }));
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/ws?role=onlooker`);
  socket.addEventListener("open", () => {
    root.dataset.conn = "open";
    // The server may be a new one that counts revisions from 0 again.
    ctx.resync = true;
    $("ol-lost").hidden = true;
    askForTraces();
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    onMessage(message);
  });
  socket.addEventListener("close", () => {
    root.dataset.conn = "lost";
    $("ol-lost").hidden = false;
    setTimeout(connect, RETRY_MS);
  });
}

/** `{name: state}` with only the entries the headset view can draw. */
function cleanStates(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [name, state] of Object.entries(raw)) {
    if (typeof state === "string" && GOOD_WORDS[state]) out[name] = state;
  }
  return out;
}

/** The contact states of a `calibration` field: `{states: {...}}`, or the map itself. */
function calibrationStates(field) {
  if (!field || typeof field !== "object") return null;
  const raw = field.states && typeof field.states === "object" ? field.states : field;
  const states = cleanStates(raw);
  return Object.keys(states).length ? states : null;
}

/** The headset view, or null while it is stopped, given up or not built (never a disposed one). */
function fitView() {
  const slot = slots.fit;
  return slot && slot.alive && slot.view && !slot.view.unavailable ? slot.view : null;
}

function paintContacts() {
  const view = fitView();
  if (!view || ["none", "settling", "error"].includes(contact.source)) return;
  const names = view.headset.contacts.map((c) => c.name);
  view.setStates(Object.fromEntries(Object.entries(contact.states).filter(([name]) => names.includes(name))));
}

function drawContactText() {
  if (contact.source === "none") return;
  if (contact.source === "settling" || contact.source === "error") {
    setText($("ol-contact"), contact.source === "settling" ? SETTLING_TEXT : `Contact cannot be measured: ${contact.note}`);
    $("ol-contact").dataset.source = contact.source;
    return;
  }
  const view = fitView();
  const names = view ? view.headset.contacts.map((c) => c.name) : [];
  const known = names.length ? Object.entries(contact.states).filter(([name]) => names.includes(name)) : Object.entries(contact.states);
  const count = typeof contact.good === "number" ? contact.good : known.filter(([, s]) => s === "good").length;
  const total = names.length || 20;
  let what = "Saved at calibration";
  let source = "saved";
  if (contact.source === "live") {
    const age = performance.now() - contact.at;
    if (age <= LIVE_MS) {
      what = "Live contact check";
      source = "live";
    } else {
      what = `Contact states from the last check, ${formatAge(age / 1000)} ago`;
      source = "last-check";
    }
  }
  setText($("ol-contact"), `${what}: ${count} of ${total} contacts good`);
  $("ol-contact").dataset.source = source;
}

function formatAge(seconds) {
  const s = Math.max(1, Math.round(seconds));
  return s < 90 ? `${s} seconds` : `${Math.round(s / 60)} minutes`;
}

function showContact() {
  paintContacts();
  drawContactText();
}

// Words for a contact check that is on and has no measure to show: the first second after 0x11
// is left out (`settling`), or the headset's sample rate does not allow the measure (`error`).
const SETTLING_TEXT = "Contact check on: the first measure comes in a second";

function onContact(message) {
  if (typeof message.error === "string" && message.error) {
    contact = { ...contact, source: "error", note: message.error, at: performance.now() };
    drawContactText();
    return;
  }
  const states = cleanStates(message.states);
  if (!Object.keys(states).length) {
    // A check switched on before anything was measured: say so. (After a measure, the states
    // shown stay until they are called the last check.)
    if (message.settling === true && ["none", "settling", "error"].includes(contact.source)) {
      contact = { ...contact, source: "settling", at: performance.now() };
      drawContactText();
    }
    return;
  }
  contact = { ...contact, source: "live", states, good: typeof message.good === "number" ? message.good : null, at: performance.now() };
  showContact();
}

function onCalibration(field) {
  const states = calibrationStates(field);
  if (!states) return;
  const json = JSON.stringify(states);
  const changed = json !== contact.savedJson;
  const stale = performance.now() - contact.at > LIVE_MS;
  contact.savedJson = json;
  if (!changed && !stale) return;
  if (!changed && contact.source === "saved") return;
  contact = { ...contact, source: "saved", states, good: null };
  showContact();
}

function stepTitle(id) {
  const step = ctx.flow ? ctx.flow.find((s) => s.id === id) : undefined;
  return step ? step.title : id;
}

/** Blocks done of the blocks to play (the same counting as the participant's bar). */
function progress(s) {
  const plan = ctx.flow && s.plan ? s.plan.map((id) => ctx.flow.find((x) => x.id === id)).filter(Boolean) : [];
  const finished = s.blocks_by_step || {};
  let done = 0;
  let total = 0;
  for (const step of plan) {
    const outcome = s.outcomes ? s.outcomes[step.id] : undefined;
    if (outcome === "skipped") continue;
    if (outcome === "partial") {
      done += finished[step.id] || 0;
      total += finished[step.id] || 0;
      continue;
    }
    total += step.blocks.length;
    if (outcome === "finished") done += step.blocks.length;
    else if (step.id === s.step) done += s.blocks_done;
  }
  return [done, total];
}

const PHASES = {
  waiting: "Not started.",
  running: "Running.",
  paused: "Paused.",
  between: "Block finished.",
  done: "Finished.",
};

function remaining() {
  const s = ctx.state;
  if (!s || s.remaining === null || s.remaining === undefined) return null;
  if (s.phase === "running") return Math.max(0, s.remaining - (performance.now() - ctx.stateAt) / 1000);
  return s.remaining;
}

function drawTime() {
  const s = ctx.state;
  const left = remaining();
  const timed = s && ["waiting", "running", "paused"].includes(s.phase) && s.blocks_total > 0;
  setText($("ol-time"), timed && left !== null ? `${formatTime(left)} left in this block` : "");
}

// How the session ended, from the state alone (an onlooker is never sent the session record).
const ENDINGS = {
  finished: "The session is over: the participant finished it.",
  stopped: "The participant stopped the session.",
  early: "The session ended before its last step.",
};

function ending(s) {
  if (s.failure || s.ended_early) return "early";
  if (s.stopped) return "stopped";
  if (s.phase === "done") return "finished";
  return "";
}

function render() {
  const s = ctx.state;
  if (s) {
    const ended = ending(s);
    root.dataset.ended = ended;
    setText($("ol-ended"), ended ? ENDINGS[ended] : "");
    $("ol-ended").hidden = !ended;
    root.dataset.step = s.step;
    root.dataset.phase = s.phase;
    root.dataset.kind = s.kind;
    setText($("ol-step"), stepTitle(s.step));
    const blocks = s.blocks_total > 0;
    // Between blocks the block named is the one that just finished.
    const n = s.phase === "between" || s.phase === "done" ? s.blocks_done : Math.min(s.blocks_done + 1, s.blocks_total);
    setText($("ol-block"), blocks ? `Block ${n} of ${s.blocks_total}.` : s.kind === "setup" ? "Setup." : "");
    setText($("ol-phase"), blocks ? PHASES[s.phase] || "" : "");
    // The session score (earned of possible, scores.js) follows the server's, never during a
    // running block (and the first state sets it).
    if (s.phase !== "running" || shown.points === null) {
      const total = totalText(s.scores);
      if (shown.points !== total) {
        shown.points = total;
        setText($("ol-points"), total);
      }
    }
    if (ctx.flow) {
      const [done, total] = progress(s);
      const percent = total > 0 ? Math.round((100 * done) / total) : 0;
      $("ol-progress").firstElementChild.style.width = `${percent}%`;
      $("ol-progress").setAttribute("aria-valuenow", String(percent));
      setText($("ol-progress-text"), total > 0 ? `${done} of ${total} blocks done` : "");
    }
    drawTime();
  }
  const link = ctx.link ? ctx.link.state : "connecting";
  const replay = ctx.hello && ctx.hello.kind === "replay";
  $("ol-link").dataset.state = link;
  setText(
    $("ol-link-text"),
    link === "live" ? (replay ? "Playing a recording" : "Headset linked") : link === "ended" ? "Link ended" : "Waiting for the headset",
  );
  const labels = ctx.hello ? ctx.hello.labels : null;
  if (labels && labels.join(",") !== traceLabels) {
    if (traces) traces.stop();
    traceLabels = labels.join(",");
    traces = new TraceView($("ol-traces"), labels);
    traces.start();
  }
}

function onMessage(message) {
  switch (message.t) {
    case "hello":
      ctx.hello = message;
      break;
    case "link":
      ctx.link = message;
      break;
    case "trace":
      if (traces) traces.push(message.x);
      return;
    case "contact":
      onContact(message);
      return;
    case "error":
      if (testing) {
        hook.errors.push({ kind: message.kind, msg: message.msg });
        if (hook.errors.length > 50) hook.errors.shift();
      }
      return;
    case "state":
      if (ctx.state && !ctx.resync && message.rev < ctx.state.rev) return;
      ctx.resync = false;
      ctx.state = message;
      ctx.stateAt = performance.now();
      hook.lastState = message;
      if (message.calibration) onCalibration(message.calibration);
      showSignal(message.calibration);
      break;
    default:
      return;
  }
  render();
}

async function loadFlow() {
  try {
    const response = await fetch("/api/flow");
    if (!response.ok) throw new Error(`status ${response.status}`);
    ctx.flow = await response.json();
    render();
  } catch (error) {
    setTimeout(loadFlow, RETRY_MS);
  }
}

async function getJson(name) {
  try {
    const response = await fetch(`/static/assets/${name}`);
    if (!response.ok) throw new Error(`the server answered ${response.status}`);
    return await response.json();
  } catch (error) {
    throw new Error(`${name} could not be loaded (${error.message})`);
  }
}

function say(slot, text) {
  if (!slot.note) slot.note = showMessage(slot.container, text);
  else slot.note.textContent = text;
}

function clearNote(slot) {
  if (slot.note) slot.note.remove();
  slot.note = null;
}

function dropView(slot) {
  slot.alive = false;
  slot.gen += 1;
  clearTimeout(slot.timer);
  if (slot.view && !slot.view.unavailable) {
    try {
      slot.view.dispose();
    } catch (error) {
      // the old scene is gone either way
    }
  }
  slot.view = null;
}

/** Builds the view of `slot` and watches its WebGL context: when it is lost the view says so and
 *  waits for the browser to restore it (three.js carries on by itself); if that does not happen in
 *  RESTORE_MS the scene is built again, at most MAX_REBUILDS times, then a last message is shown. */
function buildView(slot) {
  const gen = ++slot.gen;
  slot.view = slot.make();
  slot.alive = true;
  const view = slot.view;
  if (!view || view.unavailable) return;
  const canvas = view.stage.renderer.domElement;
  canvas.addEventListener("webglcontextlost", (event) => {
    if (gen !== slot.gen) return;
    event.preventDefault(); // lets the browser restore it
    slot.alive = false;
    say(slot, STOPPED);
    clearTimeout(slot.timer);
    slot.timer = setTimeout(() => rebuild(slot, gen), RESTORE_MS);
  });
  canvas.addEventListener("webglcontextrestored", () => {
    if (gen !== slot.gen) return;
    clearTimeout(slot.timer);
    clearNote(slot);
    slot.alive = true;
    paintContacts();
  });
}

function rebuild(slot, gen) {
  if (gen !== slot.gen) return;
  dropView(slot);
  if (slot.rebuilds >= MAX_REBUILDS) {
    say(slot, GAVE_UP);
    return;
  }
  slot.rebuilds += 1;
  clearNote(slot);
  try {
    buildView(slot);
    paintContacts();
  } catch (error) {
    say(slot, GAVE_UP);
  }
}

function manage(name, container, make) {
  const slot = { container, make, view: null, alive: false, gen: 0, rebuilds: 0, timer: 0, note: null };
  slots[name] = slot;
  buildView(slot);
  return slot;
}

async function startViews() {
  const network = $("ol-network");
  const headset = $("ol-fit");
  try {
    let modules;
    try {
      modules = await Promise.all([import("./scene3d/fit-view.js"), import("./scene3d/network-view.js")]);
    } catch (error) {
      throw new Error(`the 3-D code could not be loaded (${error.message})`);
    }
    const [{ createFitView }, { createNetworkView }] = modules;
    const [meshes, sample] = await Promise.all([getJson("meshes.json"), getJson("sample-network.json")]);
    manage("network", network, () => {
      const net = createNetworkView(network, meshes, {});
      if (!net.unavailable) {
        net.setSample(true);
        net.setNetwork(sample);
      }
      return net;
    });
    manage("fit", headset, () => createFitView(headset, meshes));
    showContact(); // contact states that came before the view was ready
    const unsupported = Object.values(slots).some((s) => !s.view || s.view.unavailable);
    hook.status = unsupported ? "unsupported" : "ready";
  } catch (error) {
    hook.status = "failed";
    showMessage(network, `Could not load the 3-D view: ${error.message}`);
    showMessage(headset, "Could not load the 3-D view.");
  }
}

Object.defineProperty(hook, "fit", { get: () => (slots.fit ? slots.fit.view : null) });
Object.defineProperty(hook, "network", { get: () => (slots.network ? slots.network.view : null) });
hook.onMessage = onMessage;
hook.sendRaw = send;
hook.ready = true;
setInterval(() => {
  if (ctx.state) drawTime();
  drawContactText();
}, 250);
loadFlow();
connect();
startViews();

// The participant page: one page, a screen chosen from the session state.
//
// The server owns the step, the block timers and the scores. This file turns
// each `state` message into a screen (`#screen[data-screen]`), keeps the HUD,
// and handles full screen: leaving it during a running block sends `pause`,
// the overlay `#fs-lost` shows how to return, and returning sends `resume`.

import { openSocket } from "./socket.js";
import { Fullscreen, needsFullscreen } from "./fullscreen.js";
import { Hud } from "./hud.js";
import { el, formatTime, rounds } from "./format.js";
import { totalText } from "./scores.js";
import * as start from "./screens/start.js";
import * as setup from "./screens/setup.js";
import * as calibration from "./screens/calibration.js";
import * as device from "./screens/device.js";
import * as participant from "./screens/participant.js";
import * as placeholder from "./screens/placeholder.js";
import * as video from "./screens/video.js";
import * as between from "./screens/between.js";
import * as rest from "./screens/rest.js";
import * as summary from "./screens/summary.js";
import * as ended from "./screens/ended.js";
import * as refused from "./screens/refused.js";
import * as connecting from "./screens/connecting.js";
import { leaveControls } from "./screens/leave.js";
import { contactPanel } from "./screens/contact-panel.js";
import { headline as signalHeadline } from "./screens/signal-check.js";
import { chooseTip } from "./tips.js";
import * as tasks from "./tasks/index.js";
import { acknowledge, connectionChanged, flushEvents, taskStore } from "./tasks/runner.js";

const MODULES = {
  connecting,
  start,
  participant,
  device,
  calibration,
  setup,
  placeholder,
  video,
  between,
  rest,
  summary,
  ended,
  refused,
};
// Every screen is on the dark ground, as the task screens are (2026-11-25 night: one look for
// everything a participant sees during a session).
const GROUND = {};
/** Screens that show the HUD (rest and the status screens do not). */
const HUD_SCREENS = new Set(["participant", "device", "calibration", "setup", "placeholder", "task", "video", "between", "summary"]);
const SETUP_IDS = ["participant", "device", "calibration"];
const ADVANCE_RETRY_MS = 3000;
const NOTICE_MS = 6000;
/** A pause that was sent and has not made the state paused is sent again after this long. */
const PAUSE_RETRY_MS = 1500;
/** How long a new page waits for another live page with the same id to answer. */
const DUPLICATE_WAIT_MS = 200;

function newPageId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** One id per tab: the same after a reload, different in another tab or window. */
function pageId() {
  try {
    let id = sessionStorage.getItem("brain-mapper-page");
    if (!id) {
      id = newPageId();
      sessionStorage.setItem("brain-mapper-page", id);
    }
    return id;
  } catch (error) {
    return newPageId();
  }
}

/**
 * The id this load sends to the server. A duplicated tab copies sessionStorage and so
 * would share the id of the first tab; it asks the other pages of this browser, over a
 * BroadcastChannel, whether the id is in use. A reload gets no answer (its old page is
 * gone), so it keeps the id and takes its place back; a duplicate gets a fresh id, and
 * the server then refuses it as a second window.
 */
async function resolvePageId() {
  const id = pageId();
  if (typeof BroadcastChannel === "undefined") return id;
  const channel = new BroadcastChannel("brain-mapper-page");
  const taken = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), DUPLICATE_WAIT_MS);
    channel.addEventListener("message", (event) => {
      if (event.data && event.data.type === "here" && event.data.id === id) {
        clearTimeout(timer);
        resolve(true);
      }
    });
    channel.postMessage({ type: "who", id });
  });
  if (taken) return newPageId();
  channel.addEventListener("message", (event) => {
    if (event.data && event.data.type === "who" && event.data.id === id) {
      channel.postMessage({ type: "here", id });
    }
  });
  return id;
}

const root = document.getElementById("screen");
const overlay = document.getElementById("fs-lost");
const hud = new Hud();

const ctx = {
  state: null,
  stateAt: 0,
  flow: null,
  hello: null,
  link: null,
  // The last `status` message of the link (rate, lost samples, battery), or null.
  stats: null,
  // The start-up checks, from the `checks` message.
  checks: [],
  // The last `contact` message (states of the electrodes while the contact check is on), or null.
  contact: null,
  sourceError: null,
  fatal: null,
  conn: "connecting",
  started: false,
  // The rounds chosen on the start screen ({ only: [ids] }, in route order), held here until the label is sent.
  selection: null,
  reloadPending: false, // the first state found a block running: this page was just loaded
  pauseSentAt: null,
  screen: "",
  key: "",
  module: null, // the module of the screen shown
};

let socket = null;
let noticeTimer = null;
// Whether the screen shown draws traces, and where the `trace` messages go.
let displayWanted = false;
let traceSink = null;

const fullscreen = new Fullscreen(() => render());
const overlayLeave = leaveControls({ ctx, send: (c) => send(c), stepTitle: (id) => stepOf(id)?.title ?? id, skip: () => api.skip(), stop: () => api.stop() }, "fs-");

function send(command) {
  return socket ? socket.send(command) : false;
}

/** Whether `text` only repeats what the screen says already: the session is over and the summary
 *  or ended screen shows its record (a toast there would cover part of it). */
function saidAlready(text) {
  return text === "the session is over" && !!ctx.state && (ctx.state.phase === "done" || !!ctx.state.stopped);
}

/** Start was pressed while the contact check is still going off after a calibration (the
 *  documented settling time, see engine `_refuse_while_check_goes_off`): instead of a refusal,
 *  the button shows that the round is about to start and the page asks again until it does or
 *  the screen moves on. */
function startWhenSettled() {
  const s = ctx.state;
  if (!s) return;
  const key = `${s.step}/${s.block}/${s.phase}`;
  const button = document.getElementById("advance");
  if (button) {
    if (!button.dataset.waiting) button.dataset.label = button.textContent;
    button.dataset.waiting = "true";
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    button.textContent = "Starting in a moment";
  }
  clearTimeout(ctx.settleTimer);
  ctx.settleTimer = setTimeout(() => {
    const now = ctx.state;
    if (!now || `${now.step}/${now.block}/${now.phase}` !== key) return; // moved on
    send({ cmd: "advance" });
  }, SETTLE_RETRY_MS);
}

function notice(text) {
  if (saidAlready(text)) return;
  const box = document.getElementById("notice");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => (box.hidden = true), NOTICE_MS);
}

function stepOf(id) {
  return ctx.flow ? ctx.flow.find((s) => s.id === id) : undefined;
}

function remaining() {
  const s = ctx.state;
  if (!s || s.remaining === null) return null;
  if (s.phase === "running") return Math.max(0, s.remaining - (performance.now() - ctx.stateAt) / 1000);
  return s.remaining;
}

function atBeginning(state) {
  return ctx.flow && state.step === ctx.flow[0].id && state.phase === "waiting";
}

function endedReason() {
  const state = ctx.state;
  if (state && state.failure) return state.failure;
  if (!state && ctx.sourceError) return `${ctx.sourceError.kind}: ${ctx.sourceError.msg}`;
  const what = ctx.hello && ctx.hello.kind === "replay" ? "The replay stopped" : "The headset link stopped";
  const reason = ctx.link && ctx.link.reason;
  return reason ? `${what} (${reason}).` : `${what}.`;
}

const api = {
  ctx,
  hud,
  fullscreen,
  screen: "",
  remaining,
  endedReason,
  record: () => ctx.record || null,
  stepTitle: (id) => (stepOf(id) ? stepOf(id).title : id),
  /** The task module that draws a step (see `taskOf`), or null. */
  taskOf: (id) => taskOf(id),
  /** sessionStorage for a screen's own values, namespaced by the session id and the current step:
   *  `{get(fallback), set(value), remove()}` (see `taskStore` in tasks/runner.js). */
  store: (key) => taskStore(ctx.state, key),
  start() {
    ctx.started = true;
    render();
  },
  /** The steps of this session, in order: the route, or the part of it the session plays. */
  planFlow() {
    const plan = ctx.state && ctx.state.plan;
    if (!plan || !ctx.flow) return ctx.flow || [];
    const steps = plan.map((id) => stepOf(id)).filter(Boolean);
    // On the participant screen no session exists yet and the server's plan is the whole route;
    // the rounds chosen on the first screen are the only ones that will be played.
    const only = ctx.selection && ctx.selection.only;
    if (only && ctx.state.step === "participant") {
      return steps.filter((s) => s.kind === "setup" || s.kind === "summary" || only.includes(s.id));
    }
    return steps;
  },
  /** Asks the server to skip the round shown (it names the round, so a second press cannot skip another). */
  skip() {
    return send({ cmd: "skip", step: ctx.state ? ctx.state.step : null });
  },
  /** Asks the server to end the session here. */
  stop() {
    return send({ cmd: "stop" });
  },
  /** Sends a command to the server; false if the socket could not take it. */
  send(command) {
    return send(command);
  },
  /** Asks the server for `trace` messages (or no more of them); asked again after a reconnect. */
  setDisplay(on) {
    displayWanted = on;
    send({ cmd: "display", on });
  },
  /** Where `trace` messages go: a function of the rows, or null. */
  setTraceSink(sink) {
    traceSink = sink;
  },
  /** An "Advance" button: pressed once, it waits for the next state before it can be pressed again. */
  advanceButton(label, cls = "btn") {
    const button = el("button", { class: cls, id: "advance", type: "button" }, label);
    button.addEventListener("click", () => {
      button.disabled = true;
      if (!send({ cmd: "advance" })) button.disabled = false;
      setTimeout(() => (button.disabled = false), ADVANCE_RETRY_MS);
    });
    return button;
  },
};

function chooseScreen() {
  const s = ctx.state;
  if (ctx.fatal) return "refused";
  if (!s) return ctx.sourceError || (ctx.link && ctx.link.state === "ended") ? "ended" : "connecting";
  if (s.failure || s.ended_early) return "ended";
  if (!ctx.flow) return "connecting";
  if (!ctx.started && !atBeginning(s)) ctx.started = true; // a reloaded page returns to its step
  if (s.stopped) return "summary";
  if (!ctx.started) return "start";
  if (s.kind === "setup") return SETUP_IDS.includes(s.step) ? s.step : "setup";
  if (s.kind === "summary") return "summary";
  if (s.kind === "rest") return "rest";
  // A task that starts on Enter shows its own prompt between two of its blocks (tasks/runner.js).
  if (s.phase === "between" && !(s.blocks_done < s.blocks_total && taskOf(s.step)?.startsOnEnter)) return "between";
  if (isVideo(s.step)) return "video";
  return taskOf(s.step) ? "task" : "placeholder";
}

/** Step kinds a task module can draw (rest steps keep their own screens). */
const MODULE_KINDS = ["task", "video", "recall"];

/**
 * The module registered in tasks/index.js for a step of the flow, or null. The step's `module`
 * names it (the server sends the step id when the flow file leaves it out); `null` keeps the
 * placeholder.
 */
function taskOf(stepId) {
  const step = stepOf(stepId);
  if (!step || !MODULE_KINDS.includes(step.kind)) return null;
  const name = step.module === undefined ? step.id : step.module;
  if (typeof name !== "string") return null;
  return Object.prototype.hasOwnProperty.call(tasks, name) ? tasks[name] : null;
}

/** Whether a step is drawn by the video screen (screens/video.js): a step of kind `video` whose
 *  `module` is `video` (the step id `video` when the flow leaves it out); `null` keeps the placeholder. */
function isVideo(stepId) {
  const step = stepOf(stepId);
  if (!step || step.kind !== "video") return false;
  return (step.module === undefined ? step.id : step.module) === "video";
}

/** The module that draws screen `name`: a task's own module on the `task` screen. */
function moduleOf(name) {
  return name === "task" ? taskOf(ctx.state.step) : MODULES[name];
}

/** What the HUD calls each setup step. */
const SETUP_NAMES = { participant: "Participant", device: "Headset", calibration: "Calibration" };

/** `setup` on the setup screens (the HUD shows no score there), else ``. */
function hudMode(s) {
  return s.kind === "setup" ? "setup" : "";
}

function label(s) {
  const step = stepOf(s.step);
  const title = step ? step.title : s.step;
  if (s.stopped || s.kind === "summary") return "Summary";
  if (s.kind === "setup") {
    // "Setup 2 of 3: Headset": the setup steps counted apart from the rounds.
    const setups = api.planFlow().filter((x) => x.kind === "setup");
    const i = setups.findIndex((x) => x.id === s.step) + 1;
    const name = SETUP_NAMES[s.step] || title;
    return i > 0 ? `Setup ${i} of ${setups.length}: ${name}` : `Setup: ${name}`;
  }
  const rs = rounds(api.planFlow());
  const n = rs.findIndex((r) => r.id === s.step) + 1;
  const tail = s.phase === "between" ? `, block ${s.blocks_done} of ${s.blocks_total} done` : "";
  return `Round ${n} of ${rs.length} · ${title}${tail}`;
}

/** Blocks done of the blocks to play. A finished block always counts in both numbers; a
 *  round left without finishing keeps only the blocks it finished; a skipped one counts none. */
function progress(s) {
  let done = 0;
  let total = 0;
  const finished = s.blocks_by_step || {};
  for (const step of api.planFlow()) {
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

/** How long the page waits before it asks again to start a round during the settling time. */
const SETTLE_RETRY_MS = 300;

const REASONS = {
  connection_lost: "The connection to brain-mapper was lost.",
  fullscreen_lost: "Full screen was closed.",
  page_reloaded: "This page was reloaded.",
  link_lost: "The headset connection was lost. Wait for it to come back, then press Resume.",
};

/** The headset check of the paused screen: the calibration's contact view (made when first opened). */
let headsetCheck = null;

function drawHeadsetCheck(s, open) {
  const panel = document.getElementById("fs-check-panel");
  const card = overlay.querySelector(".card:not(.hc)");
  panel.hidden = !open;
  card.hidden = open;
  overlay.dataset.check = open ? "open" : "closed";
  if (!open) {
    if (headsetCheck) headsetCheck.dispose();
    return;
  }
  if (!headsetCheck) {
    headsetCheck = contactPanel({
      ids: { root: "hc-contact", stage: "hc-view", count: "hc-count", pills: "hc-pills", excluded: "hc-excluded", off: "hc-off", noTone: "hc-no-tone" },
      title: "Check the headset",
      note: "Adjust the headset until the contacts are good, then press Done.",
      offText: "The contact check is off, because the page was reloaded. Press Done, then Check the headset again.",
      excludedText: (bad) => `No contact: ${bad.join(", ")}.`,
    });
    document.getElementById("hc-slot").append(headsetCheck.root);
  }
  headsetCheck.load();
  headsetCheck.draw(s, api, overlay);
}

/** What the paused screen says about the block the pause stopped. */
function pausedBlockText(s) {
  if (s.interrupted) {
    const last = s.blocks_done >= s.blocks_total;
    return last
      ? "This block has ended and is not repeated. Press Resume to go on to the next round."
      : "This block has ended and is not repeated. Press Resume to go on to the next block.";
  }
  if (s.kind === "rest") return "Resume starts this rest again from the beginning.";
  if (["eyes_open", "eyes_closed", "blinks"].includes(s.substep)) return "Resume starts this part again from the beginning.";
  const left = remaining();
  return left !== null ? `The clock is stopped. Time left in this block: ${formatTime(left)}.` : "The clock is stopped.";
}

function drawOverlay(s, name) {
  const needs = ctx.started && needsFullscreen(s) && !["ended", "refused", "start", "connecting"].includes(name);
  const paused = s.phase === "paused";
  const lost = needs && (!fullscreen.active || paused);
  overlay.hidden = !lost;
  if (!lost) {
    overlayLeave.update(null);
    drawHeadsetCheck(s, false);
    return;
  }
  document.getElementById("fs-lost-title").textContent = paused ? "Paused" : "Full screen is off";
  const reason = REASONS[s.reason] || (s.reason ? `Paused: ${s.reason}.` : "The block is paused.");
  const why = paused ? reason : "This part of the session runs in full screen.";
  document.getElementById("fs-lost-why").textContent = why;
  document.getElementById("fs-lost-left").textContent = paused ? pausedBlockText(s) : "";
  overlayLeave.update(paused ? s : null);
  const check = paused ? s.headset_check : null;
  drawHeadsetCheck(s, !!check && check.phase === "contact");
  // While the headset link is lost, the device tip says what to check (for the operator).
  const tipNode = document.getElementById("fs-lost-tip");
  const linkTip = paused && s.reason === "link_lost" && !(ctx.link && ctx.link.state === "live")
    ? chooseTip(ctx.link, null, false)
    : null;
  tipNode.hidden = !linkTip;
  tipNode.textContent = linkTip ? linkTip.text : "";
  tipNode.dataset.tip = linkTip ? linkTip.code : "";
  // While no samples flow into an open recording (between `link_lost` and `link_back`) the
  // block cannot go on: Resume is grayed out, with a line saying why. Nothing resumes by
  // itself; once the link is back, Resume waits for a press.
  const waiting = paused && !!s.link_down;
  // After a headset check, Resume also waits until the data show the contact check off (settling)
  // and the signal check after it has its result (the same check as after calibration).
  const settling = !!check && (check.phase === "settling" || check.phase === "signal");
  const result = paused ? s.headset_result : null;
  const signalNode = document.getElementById("fs-signal");
  const signalWarn = document.getElementById("fs-signal-warn");
  signalNode.hidden = !result;
  signalNode.textContent = result ? `Signal check: ${signalHeadline(result)}` : "";
  const notFine = result && result.signal ? result.signal.not_fine || [] : [];
  signalWarn.hidden = !(result && (notFine.length || result.signal.error));
  signalWarn.textContent = result && result.signal.error
    ? "The signal could not be checked. Press Check the headset to try again, or Resume to record anyway."
    : "Some channels are not fine. Adjust those electrodes and press Check the headset again, or Resume to record with them as they are.";
  const button = document.getElementById("fs-return");
  button.disabled = waiting || settling;
  button.setAttribute("aria-disabled", String(waiting || settling));
  const wait = document.getElementById("fs-wait");
  wait.hidden = !(waiting || settling);
  wait.textContent = waiting
    ? "Waiting for the headset"
    : check && check.phase === "signal"
      ? `Checking the signal with the contact check off. Sit still. Resume is possible in a moment${check.signal_left !== null && check.signal_left !== undefined ? ` (${Math.max(0, Math.ceil(check.signal_left))} s)` : ""}.`
      : "The contact check is going off. Resume is possible in a moment.";
  // The headset check needs data from the headset: grayed out while the link is down.
  const checkButton = document.getElementById("fs-check");
  checkButton.hidden = !paused;
  checkButton.disabled = waiting || settling;
  checkButton.setAttribute("aria-disabled", String(waiting || settling));
  button.textContent = !paused
    ? "Return to full screen"
    : fullscreen.active
      ? "Resume"
      : "Return to full screen and resume";
}

function maybePause(s, name) {
  if (s.phase !== "running") {
    ctx.pauseSentAt = null;
    return;
  }
  const needs = ctx.started && needsFullscreen(s) && name !== "ended";
  if (!needs || fullscreen.active) return;
  const now = performance.now();
  if (ctx.pauseSentAt !== null && now - ctx.pauseSentAt < PAUSE_RETRY_MS) return;
  // Only a pause that went out counts as sent; one that the socket could not take, or that
  // the server refused or did not act on, is sent again while the block still runs.
  const reason = ctx.reloadPending ? "page_reloaded" : "fullscreen_lost";
  if (send({ cmd: "pause", reason })) {
    ctx.pauseSentAt = now;
    ctx.reloadPending = false;
  }
}

function render() {
  const name = chooseScreen();
  const s = ctx.state;
  const key = `${name}/${s ? s.step : ""}`;
  document.body.dataset.ground = GROUND[name] || "dark";
  root.dataset.screen = name;
  root.dataset.step = s ? s.step : "";
  root.dataset.phase = s ? s.phase : "";
  // Events the socket could not take go out first, in order, once it is open again.
  if (ctx.conn === "open") flushEvents(api);
  if (key !== ctx.key) {
    ctx.key = key;
    // A screen whose module changes is unmounted (a setup screen's module kept for the next
    // step stays as it is); a task screen that is left always ends its trial (cut short or
    // abandoned) before the next screen.
    const previous = ctx.module;
    if (previous && previous.unmount && (previous !== moduleOf(name) || ctx.screen === "task")) {
      previous.unmount(api);
    }
    ctx.screen = name;
    api.screen = name;
    ctx.module = moduleOf(name);
    ctx.module.mount(root, api);
  }
  ctx.module.update(s, api);
  // The score follows the server's, which counts each trial as it is logged (after the screen, so
  // the between-blocks sentence already shows the same total when the HUD changes).
  if (s) hud.setScore(totalText(s.scores));

  const hudOn = !!s && !!ctx.flow && HUD_SCREENS.has(name);
  hud.show(hudOn);
  if (hudOn) {
    hud.setLabel(label(s));
    hud.setMode(hudMode(s));
    const [done, total] = progress(s);
    hud.setProgress(done, total);
  }
  if (s && ctx.flow) {
    drawOverlay(s, name);
    maybePause(s, name);
  } else {
    overlay.hidden = true;
  }
  document.getElementById("conn-lost").hidden = ctx.conn !== "lost" || !!ctx.fatal;
}

document.getElementById("fs-leave-slot").append(overlayLeave.root);

async function loadFlow() {
  try {
    const response = await fetch("/api/flow");
    if (!response.ok) throw new Error(`status ${response.status}`);
    ctx.flow = await response.json();
    render();
  } catch (error) {
    setTimeout(loadFlow, 1000);
  }
}

function onMessage(message) {
  switch (message.t) {
    case "hello":
      ctx.hello = message;
      break;
    case "link":
      ctx.link = message;
      if (message.state !== "live") ctx.stats = null;
      break;
    case "package":
      ctx.record = message; // the session zip: being made, saved or failed (see record.js)
      break;
    case "status":
      ctx.stats = message;
      // Counts the status messages received, so a page (or a test) can tell a new reading
      // from the same figures sent again.
      ctx.statusCount = (ctx.statusCount || 0) + 1;
      break;
    case "checks":
      ctx.checks = message.items;
      break;
    case "contact":
      ctx.contact = message;
      break;
    case "trace":
      if (traceSink) traceSink(message.x);
      return; // drawn on animation frames; nothing else changes
    case "error":
      if (message.cid) {
        // A page event the server will never store (a bad key or part): it is not sent again.
        acknowledge(message);
        console.error(`event not saved: ${message.msg}`);
        notice(message.msg);
        return;
      }
      if (message.kind === "second_window") ctx.fatal = message;
      else if (!ctx.state) ctx.sourceError = message;
      else if (message.kind === "refused" && /^(pause|resume|video_ended|block_finished)/.test(message.msg)) {
        // The state moved on before the command arrived; the next state says where it is.
      } else if (message.kind === "bad_command" && /block_finished/.test(message.msg)) {
        // A server that does not know `block_finished` ends the block at its time.
      } else if (message.kind === "refused" && message.msg.includes("the round can start in a moment")) {
        startWhenSettled();
      } else {
        notice(message.msg);
        const button = document.getElementById("advance");
        if (button) button.disabled = false;
      }
      break;
    case "ack": {
      // A page event was written (or refused for good, e.g. the session is over): not sent again.
      const refused = acknowledge(message);
      if (refused) notice(refused);
      return;
    }
    case "state": {
      // After a reconnect the server may be a new one that counts revisions from 0 again.
      if (ctx.state && !ctx.resync && message.rev < ctx.state.rev) break;
      ctx.resync = false;
      const first = !ctx.state;
      ctx.state = message;
      ctx.stateAt = performance.now();
      if (first) {
        // The score on screen starts from the server's (also when a block is running).
        hud.setScore(totalText(message.scores));
        ctx.reloadPending = message.phase === "running";
        loadFlow();
      }
      break;
    }
    default:
      break;
  }
  render();
}

resolvePageId().then((id) => {
  socket = openSocket({
    pageId: id,
    onMessage,
    onStatus(status) {
      ctx.conn = status;
      if (status === "open") ctx.resync = true;
      if (status === "open" && displayWanted) send({ cmd: "display", on: true });
      connectionChanged(); // events not acknowledged go out again on the new socket
      render();
    },
  });
});

document.getElementById("fs-return").addEventListener("click", async (event) => {
  if (event.currentTarget.disabled) return; // grayed out while the headset link is down
  try {
    if (!fullscreen.active) await fullscreen.enter();
  } catch (error) {
    notice(`Full screen could not be started (${error.message}).`);
    return;
  }
  if (ctx.state && ctx.state.phase === "paused") send({ cmd: "resume" });
  render();
});

document.getElementById("fs-check").addEventListener("click", (event) => {
  if (event.currentTarget.disabled) return; // grayed out while the headset link is down
  if (ctx.state && ctx.state.phase === "paused") send({ cmd: "headset_check" });
});

document.getElementById("hc-done").addEventListener("click", () => {
  send({ cmd: "headset_check_done" });
});

// The time left is the server's value run down since it arrived.
setInterval(() => {
  const module = ctx.module;
  if (module && module.tick && ctx.state) module.tick(api);
}, 100);

render();

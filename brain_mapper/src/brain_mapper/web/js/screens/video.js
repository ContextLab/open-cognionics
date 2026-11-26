// The video round (spec 6.3: Vimeo 19009646, play, pause and end times logged).
//
// The one place where the page shows something from another host: the Vimeo player, in an
// iframe that exists only while this screen is shown (made on mount, removed on unmount). The
// page talks to it with postMessage (Vimeo's documented JSON messages; Vimeo's own player.js is
// not loaded) and takes a message only when its origin is the player's and its source is this
// iframe's window. The address carries Vimeo's own display parameters and nothing of the session,
// and the iframe sends no referrer.
//
// The engine times the block as for any round. The player's events go out as page events
// (`video.ready`, `video.play`, `video.pause`, `video.ended`, `video.buffer_start`,
// `video.buffer_end`, `video.progress` at most once a second, `video.error`), so each gets an EEG
// marker. The end of the video ends the block (`video_ended`); a block whose time is up first
// ends as any other. A pause of the block (full screen closed, a lost connection) pauses the
// video, and the resume plays it again.
//
// Once Start is pressed the player fills the whole window (the page's own full screen; the
// player's full screen is never used, so the app's full-screen element stays the root and no
// pause is tripped): nothing of the page is drawn over it. "Skip this round" and "Stop here"
// come up in a bar at the bottom when the pointer moves or a key is pressed and go again after
// 3 s without input; they stay up while paused and after a player error. While the video plays
// the iframe takes no pointer events (a cross-origin frame would take every pointer move, and
// the page could not tell that the pointer moved); it takes them again when the hint says to
// click it.
//
// Only a part of the video is played: the flow's `start_s` to `end_s` (flow.py, `VIDEO`), without
// the opening title and the closing credit and dedication. A black cover hides the player from
// the start until the player, playing, reports a position at or after `start_s` (so not one frame
// before it is seen); Start first seeks there (`setCurrentTime`). At `end_s` the page pauses the
// player, covers it, logs `video.range_end` and ends the round as the end of the video does. The
// player's own controls and keys are off (`controls=0`, `keyboard=0`); should a seek take it out
// of the part all the same, it is covered and sent back to the last position inside
// (`video.seek`).
import { el } from "../format.js";
import { sendEvent } from "../tasks/runner.js";
import { leaveControls } from "./leave.js";

export const PLAYER_ORIGIN = "https://player.vimeo.com";
/** Vimeo's player parameters (help.vimeo.com, "Player parameters overview"): no session data or
 *  analytics collected (dnt), no title, author or portrait over the video, no full-screen button
 *  (the page is already in full screen). */
export function playerUrl(id) {
  return `${PLAYER_ORIGIN}/video/${id}?dnt=1&title=0&byline=0&portrait=0&fullscreen=0&controls=0&keyboard=0`;
}
/** The player's events the page asks for (names as in Vimeo's player.js documentation). */
const PLAYER_EVENTS = ["play", "pause", "ended", "timeupdate", "seeked", "bufferstart", "bufferend", "error"];
/** A position this close before `start_s` counts as reached (a seek can land a frame short). */
const START_SLACK = 0.05;
/** The part played when the flow gives none (flow.py gives every video step one). */
const WHOLE = { id: 19009646, start_s: 0, end_s: Infinity };
/** How long the player has to say it is ready before the page says it could not be loaded. */
export const READY_MS = 15000;
/** `video.progress` is sent at most this often. */
const PROGRESS_MS = 1000;
/** After this long in a running block without a `play` from the player, a hint says to click it. */
const HINT_MS = 4000;
/** The player's largest width in CSS px (before Start; after it the player fills the window). */
const MAX_WIDTH = 1280;
/** The leave controls over the playing video go after this long without pointer or key input. */
export const CONTROLS_MS = 3000;

const INSTRUCTIONS = "A short video will play. Watch it as you normally would.";
const LOADING = "Loading the video.";
const PROBLEM = "The video could not be loaded. Check the internet connection.";

let nodes = null;
let leave = null;
let player = null; // { frame, ready, failed, timer, playing, lastProgress, asked, askedAt }
let current = null; // the last state
let api_ = null;
let sizer = null; // the ResizeObserver of the stage
let wantPlaying = false;
let endSent = false;
let lastInput = -Infinity; // performance.now() of the last pointer move or key press
let inputTimer = null;
let range = WHOLE; // the flow's video: { id, start_s, end_s }

function log(name, payload, time = performance.now()) {
  if (api_) sendEvent(api_, name, payload, time);
}

function post(message) {
  if (player && player.frame.contentWindow) player.frame.contentWindow.postMessage(message, PLAYER_ORIGIN);
}

/** The iframe's size in CSS px and the device pixel ratio, for `video.ready` and `video.play`. */
function frameSize() {
  const box = player.frame.getBoundingClientRect();
  return {
    frame_css_width: Math.round(box.width),
    frame_css_height: Math.round(box.height),
    device_pixel_ratio: window.devicePixelRatio,
  };
}

function onInput() {
  lastInput = performance.now();
  clearTimeout(inputTimer);
  inputTimer = setTimeout(draw, CONTROLS_MS + 50);
  draw();
}

function position(data) {
  const out = {};
  if (data && Number.isFinite(data.seconds)) out.seconds = data.seconds;
  if (data && Number.isFinite(data.duration)) out.duration = data.duration;
  return out;
}

function onMessage(event) {
  // Only the player in this screen's iframe: its origin, and the iframe's own window.
  if (!player || event.origin !== PLAYER_ORIGIN || event.source !== player.frame.contentWindow) return;
  let data = event.data;
  if (typeof data === "string") {
    try {
      data = JSON.parse(data);
    } catch (error) {
      return;
    }
  }
  if (!data || typeof data !== "object" || typeof data.event !== "string") return; // a method's answer
  const time = event.timeStamp;
  switch (data.event) {
    case "ready":
      if (player.ready || player.failed) return;
      player.ready = true;
      clearTimeout(player.timer);
      for (const name of PLAYER_EVENTS) post({ method: "addEventListener", value: name });
      log("video.ready", { start_s: range.start_s, end_s: range.end_s, ...frameSize() }, time);
      break;
    case "play":
      player.playing = true;
      player.ended = false;
      log("video.play", { ...position(data.data), ...frameSize() }, time);
      break;
    case "pause":
      player.playing = false;
      log("video.pause", position(data.data), time);
      break;
    case "ended":
      player.playing = false;
      log("video.ended", position(data.data), time);
      if (!player.ended) cover();
      player.ended = true;
      break;
    case "timeupdate":
    case "seeked": {
      const at = data.data && data.data.seconds;
      if (!Number.isFinite(at) || !inRange(at, time, data.event === "seeked")) break;
      if (data.event === "timeupdate" && !player.covered &&
          (player.lastProgress === null || time - player.lastProgress >= PROGRESS_MS)) {
        player.lastProgress = time;
        log("video.progress", position(data.data), time);
      }
      break;
    }
    case "bufferstart":
      log("video.buffer_start", {}, time);
      break;
    case "bufferend":
      log("video.buffer_end", {}, time);
      break;
    case "error": {
      const info = data.data || {};
      player.errored = true;
      log("video.error", { reason: "player", name: String(info.name || ""), message: String(info.message || "") }, time);
      break;
    }
    default:
      return;
  }
  draw();
}

function cover() {
  if (!player) return;
  player.covered = true;
  nodes.cover.hidden = false;
}

function seek(to, reason, time = performance.now()) {
  post({ method: "setCurrentTime", value: to });
  log("video.seek", { to, reason }, time);
}

/** The end of the part played: the player paused and covered, the round ended (`draw`). */
function rangeEnd(at, time = performance.now()) {
  if (!player || player.ended) return;
  clearTimeout(player.endTimer);
  cover();
  post({ method: "pause" });
  player.ended = true;
  log("video.range_end", { seconds: at, end_s: range.end_s }, time);
  draw();
}

/** Checks a reported position against the part played. Inside it, it is remembered, and the
 *  player is uncovered once it plays at or after `start_s`. Played on to `end_s`, the part is
 *  over. Otherwise outside it (before `start_s`, or a jump past `end_s`), the player is covered
 *  and sent back to the last position inside. True when inside. */
function inRange(at, time, seeked) {
  if (player.ended) return false;
  // Played on to the end: the last position inside was just before it (a jump there is a seek).
  const playedOn = !seeked && player.lastGood !== null && range.end_s - player.lastGood < 1.5;
  if (at >= range.end_s && playedOn) {
    if (player.started) rangeEnd(at, time);
    else cover();
    return false;
  }
  if (at < range.start_s - START_SLACK || at >= range.end_s) {
    cover();
    if (player.started) seek(player.lastGood ?? range.start_s, "out_of_range", time);
    return false;
  }
  player.lastGood = at;
  if (player.covered && player.started && player.playing) {
    player.covered = false;
    nodes.cover.hidden = true;
    log("video.uncover", { seconds: at }, time);
  }
  // The end comes between two reports (about 4 a second): a timer covers it on time.
  clearTimeout(player.endTimer);
  if (player.playing && range.end_s - at < 1) {
    player.endTimer = setTimeout(() => rangeEnd(range.end_s), (range.end_s - at) * 1000);
  }
  return true;
}

function makePlayer() {
  const frame = el("iframe", {
    id: "video-player",
    title: "Video",
    src: playerUrl(range.id),
    allow: "autoplay",
    referrerpolicy: "no-referrer",
  });
  player = { frame, ready: false, failed: false, timer: null, playing: false, lastProgress: null, askedAt: null, ended: false, errored: false,
    covered: true, started: false, lastGood: null, endTimer: null };
  wantPlaying = false;
  const made = player;
  player.timer = setTimeout(() => {
    if (player !== made || made.ready) return;
    made.failed = true;
    log("video.error", { reason: "not_ready", waited_ms: READY_MS });
    draw();
  }, READY_MS);
  nodes.cover.hidden = false;
  nodes.stage.replaceChildren(frame, nodes.cover);
  fit();
}

/** Sizes the player to the largest 16:9 box that fits in the stage. */
function fit() {
  if (!player || !nodes) return;
  const sized = [player.frame, nodes.cover];
  if (nodes.root.dataset.full === "true") {
    for (const node of sized) node.style.width = node.style.height = "";
    return;
  }
  const box = nodes.stage.getBoundingClientRect();
  const width = Math.max(0, Math.floor(Math.min(box.width, (box.height * 16) / 9, MAX_WIDTH)));
  for (const node of sized) {
    node.style.width = `${width}px`;
    node.style.height = `${Math.floor((width * 9) / 16)}px`;
  }
}

function dropPlayer() {
  if (!player) return;
  clearTimeout(player.timer);
  clearTimeout(player.endTimer);
  player.frame.remove();
  player = null;
}

/** Plays the video while the block runs, the connection is open and the page is in full screen;
 *  pauses it otherwise. Only a change is sent, so a pause made in the player itself stays. */
function follow() {
  if (!player || !player.ready || !current || player.ended) return;
  const want =
    current.phase === "running" && api_.ctx.conn === "open" && api_.fullscreen.active;
  if (want === wantPlaying) return;
  wantPlaying = want;
  if (want && !player.started) {
    // The first play: from the start of the part played (messages are handled in order).
    player.started = true;
    player.askedAt = performance.now();
    seek(range.start_s, "range_start");
  }
  post({ method: want ? "play" : "pause" });
}

function draw() {
  if (!nodes || !current) return;
  const phase = current.phase;
  const waiting = phase === "waiting";
  const failed = !!(player && player.failed);
  const ready = !!(player && player.ready);
  nodes.root.dataset.player = failed ? "failed" : ready ? "ready" : "loading";
  nodes.instructions.hidden = !waiting;
  nodes.status.textContent = waiting && !ready && !failed ? LOADING : "";
  nodes.problem.hidden = !failed;
  const hint =
    phase === "running" && ready && !player.playing && player.askedAt !== null &&
    performance.now() - player.askedAt > HINT_MS;
  nodes.hint.hidden = !hint;
  // From Start on, the player fills the window; the controls come and go over it.
  const full = ready && !failed && (phase === "running" || phase === "paused");
  if ((nodes.root.dataset.full === "true") !== full) {
    nodes.root.dataset.full = String(full);
    document.documentElement.classList.toggle("video-full", full);
    fit();
  }
  const recent = performance.now() - lastInput < CONTROLS_MS;
  const controls = !full || phase !== "running" || player.errored || hint || recent;
  nodes.root.dataset.controls = controls ? "shown" : "hidden";
  nodes.root.dataset.pointer = full && !hint ? "page" : "player";
  const wanted = waiting && ready ? "start" : failed ? "retry" : "";
  if (nodes.shown !== wanted) {
    nodes.shown = wanted;
    nodes.actions.replaceChildren(
      ...(wanted === "start" ? [api_.advanceButton("Start", "btn btn--light")] : []),
      ...(wanted === "retry" ? [nodes.retry] : []),
    );
  }
  // The end of the video ends the block; sent again while the socket could not take it.
  if (player && player.ended && phase === "running" && !endSent) {
    endSent = api_.send({ cmd: "video_ended", step: current.step });
  }
  follow();
}

export function mount(root, api) {
  api_ = api;
  range = rangeOf(api, api.ctx.state ? api.ctx.state.step : "");
  endSent = false;
  const wrap = el("div", { class: "video-round", id: "video-round" });
  nodes = {
    root: wrap,
    title: el("h1", { id: "video-title" }),
    instructions: el("p", { class: "video-instructions", id: "video-instructions" }, INSTRUCTIONS),
    status: el("p", { class: "video-status", id: "video-status", role: "status" }),
    problem: el("p", { class: "video-problem", id: "video-problem", role: "alert" }, PROBLEM),
    hint: el("p", { class: "video-hint", id: "video-hint" }, "If the video does not start, click it."),
    stage: el("div", { class: "video-stage", id: "video-stage" }),
    cover: el("div", { class: "video-cover", id: "video-cover" }),
    bar: el("div", { class: "video-bar", id: "video-bar" }),
    actions: el("div", { class: "video-actions" }),
    retry: el("button", { class: "btn btn--light", id: "video-retry", type: "button" }, "Try again"),
    shown: null,
  };
  nodes.problem.hidden = true;
  nodes.hint.hidden = true;
  nodes.retry.addEventListener("click", () => {
    dropPlayer();
    makePlayer();
    draw();
  });
  leave = leaveControls(api, "", { whileRunning: true });
  nodes.bar.append(nodes.hint, leave.root);
  wrap.append(nodes.title, nodes.instructions, nodes.stage, nodes.status, nodes.problem, nodes.actions, nodes.bar);
  root.replaceChildren(wrap);
  lastInput = -Infinity;
  window.addEventListener("message", onMessage);
  document.addEventListener("pointermove", onInput);
  document.addEventListener("pointerdown", onInput);
  document.addEventListener("keydown", onInput);
  makePlayer();
  sizer = new ResizeObserver(fit);
  sizer.observe(nodes.stage);
}

/** The part of the video the flow plays in step `stepId`. */
function rangeOf(api, stepId) {
  const step = (api.ctx.flow || []).find((s) => s.id === stepId);
  return (step && step.video) || WHOLE;
}

export function update(state, api) {
  api_ = api;
  current = state;
  const given = rangeOf(api, state.step);
  if (given.id !== range.id || given.start_s !== range.start_s || given.end_s !== range.end_s) {
    range = given;
    if (player && player.frame.src !== playerUrl(range.id)) {
      dropPlayer();
      makePlayer();
    }
  }
  nodes.title.textContent = api.stepTitle(state.step);
  leave.update(state);
  draw();
}

export function tick() {
  draw(); // the hint, after a while without play
}

export function unmount() {
  window.removeEventListener("message", onMessage);
  document.removeEventListener("pointermove", onInput);
  document.removeEventListener("pointerdown", onInput);
  document.removeEventListener("keydown", onInput);
  clearTimeout(inputTimer);
  document.documentElement.classList.remove("video-full");
  if (sizer) sizer.disconnect();
  sizer = null;
  dropPlayer();
  nodes = null;
  current = null;
  wantPlaying = false;
}

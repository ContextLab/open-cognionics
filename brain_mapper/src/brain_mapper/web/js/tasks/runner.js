// The trial runner every task uses: `taskScreen(spec)` turns a task description into a screen
// module (`mount`, `update`, `tick`, `unmount`, and `instructions(blockIndex)` for the
// between-blocks screen). See docs/brain-mapper.md, "Tasks", for the whole contract.
//
// The engine times the block; the runner runs trials until it ends. Before the first block
// (phase `waiting`) the screen shows the block's instructions, then the practice trials, then
// Start (the existing `advance`). Stimulus onsets and offsets are logged at the animation frame
// that paints them (the `requestAnimationFrame` timestamp), responses at the `keydown` event's
// `timeStamp`; all go to the server as `event` commands, which stamps each with the EEG sample
// of its arrival. The frame period is measured while a task screen is mounted (`io.frameMs`, kept
// for the page), and
// `io.showAt`, `io.clearAt` and `io.wait` put each change on the frame nearest to when it is due.
//
// Events, `<task>` being `spec.name`:
//   <task>.stim      one per `io.show`/`io.showAt`  payload: the `extra` given, part, frame_ms,
//                    due_ms and late_frames (when it had a due time), block, trial, practice
//   <task>.stim_off  one per `io.clear`/`io.clearAt` after a show, and when a trial ends with
//                    something on the screen  payload: part, frame_ms, due_ms, late_frames, frames
//                    (when timed by `io.wait`), block, trial, practice
//   <task>.resp      one per accepted key      payload: key (a `KeyboardEvent.code`), response (the
//                    answer the task's key map gives, else the code), rt_ms, block, trial, practice
//   <task>.stray     at most one per trial     payload: key, awaiting, block, trial, practice
//   <task>.trial     one per trial             payload: see `summary` below
//   <task>.<name>    from `io.event(name, ...)`
// `status` is `done`, `cut_short` (the block ended first), `abandoned` (a pause, a skip, a stop,
// a lost connection, the page closed; `reason` says which) or `error` (`run` threw). Only a
// `done` trial that is not practice has `scored: true`.
//
// Delivery: every event carries a client id `cid` and the session id; the server answers
// `{"t":"ack","cid"}` once the event is written (or `{"t":"ack","cid","refused"}` when it cannot
// be, e.g. the session is over; or `{"t":"error","cid",...}` for an event it will never store)
// and writes a `cid` only once. Events not yet acknowledged are kept, in order, in sessionStorage
// and sent again after a reconnect or a reload, with `queued: true` in their payload (their
// `browser_time` keeps the true time). A lost connection during a block abandons the trial in
// progress and pauses the block (reason `connection_lost`) as soon as the pause can be sent.

import { el } from "../format.js";
import { leaveControls } from "../screens/leave.js";
import { framePeriod, framesFor, isDueFrame, lateFrames, missedFrames } from "./frames.js";
import { rng } from "./rng.js";

/** A pause that was sent and did not make the block paused is sent again after this long. */
const PAUSE_RETRY_MS = 1500;
const STORE = "brain-mapper-task";
/** The events not yet acknowledged, one sessionStorage key per session id: `<OUTBOX><session id>`. */
const OUTBOX = `${STORE}/outbox/`;
/** Where round 2 kept them (one key for all); still read, so that nothing kept there is lost. */
const LEGACY_PENDING = `${STORE}/pending`;
/** The counts of events the server could not save, until they are reported (kept across a reload). */
const DROPPED = `${STORE}/dropped`;
/** Fields of `<task>.trial` the runner owns; a trial parameter of the same name is kept as `param_<name>`. */
const FIXED = [
  "response", "key", "rt_ms", "correct", "block", "trial", "practice", "status", "scored", "reason", "queued", "cid",
  "rt_first_ms", "rt_last_ms", "responses", "changed",
];
/** A part name. */
const PART = /^[a-z][a-z0-9_]{0,31}$/;
/** The form of a payload key used as it is for a column of the behavior files; any other is stored
 *  as sent and given a neutral column name there. The runner warns once per such key. */
const KEY_RULE = /^[a-z][a-z0-9_]{0,47}$/;
/** A task that starts on Enter shows a fixation cross this long after Enter, before its first trial. */
export const LEAD_IN_MS = 2000;
/** Feedback after every trial ("Correct", "Incorrect", "No response") stays this long. */
export const FEEDBACK_MS = 500;
/** A key pressed in a response window lights its key label this long (the same for either key). */
export const PRESS_SIGN_MS = 150;
/** What the task area says before practice and before every block of a task that starts on Enter. */
export const ENTER_PROMPT = ["Put your fingers on F and J.", "Press Enter to start."];
/** After Enter sent `advance`, another Enter is ignored this long (a refusal re-enables it then). */
const ENTER_RETRY_MS = 1500;
/** The frame period assumed until 30 frame intervals have been measured on this page. */
const DEFAULT_FRAME_MS = 1000 / 60;
/** Frame timestamps kept for the measured period (the last 120 intervals). */
const FRAME_WINDOW = 121;

/** The frame period measured on this page (kept across task screens; replaced only by a full new
 *  measurement), or null before the first one. */
let measuredFrameMs = null;
/** The api of the screen mounted last: where a failure to store is reported. */
let reporter = null;
/** False from a connection change until the server's first state on it: the state the page has
 *  until then may be that of another server or of a session that is over. */
let stateSinceConnect = false;
let staleState = null;
/** `{error, reported}` once a write to sessionStorage failed. */
let storageFailure = null;
const warnedKeys = new Set();

function warnKeys(name, payload) {
  const names = Object.keys(payload || {});
  if (payload && typeof payload.part === "string") names.push(`part ${payload.part}`);
  for (const key of names) {
    const bare = key.startsWith("part ") ? key.slice(5) : key;
    if (KEY_RULE.test(bare) || warnedKeys.has(key)) continue;
    warnedKeys.add(key);
    console.warn(
      `brain-mapper: event ${name} has the ${key.startsWith("part ") ? "part" : "payload key"} "${bare}"; ` +
        "it is saved as sent, but its column in the behavior file gets a neutral name " +
        "(use 1 to 48 of a-z 0-9 _ starting with a letter)",
    );
  }
}

/** Thrown into a trial's `run` when the trial is over before `run` is (an abandoned trial). */
export class Abandoned extends Error {
  constructor() {
    super("the trial is over");
    this.name = "Abandoned";
  }
}

/** The words of practice feedback, the same in every task. */
export const FEEDBACK = Object.freeze({ correct: "Correct", wrong: "Incorrect", none: "No response" });

/** The feedback for an answer: "No response" when there was none, else "Correct" or
 *  "Incorrect". */
export function feedbackText(answered, correct) {
  if (!answered) return FEEDBACK.none;
  return correct ? FEEDBACK.correct : FEEDBACK.wrong;
}

/** The practice feedback as drawn in every task: `.task-feedback` (css/shell.css), centered in
 *  the stimulus area, white on the page's navy ground, 0.06 x --panel. `attrs` adds an id or
 *  the task's own class for its tests (`class` is added to `task-feedback`). */
export function feedbackNode(text, attrs = {}) {
  const { class: extra, ...rest } = attrs;
  return el("p", { ...rest, class: extra ? `task-feedback ${extra}` : "task-feedback", role: "status" }, text);
}

/** The records a score may use: those with `scored === true` (done, not practice). */
export function scoredRecords(records) {
  return (records || []).filter((r) => r && r.scored === true);
}

// -- delivery -----------------------------------------------------------------------------------

const LOAD = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, "0")).join("");
let counter = 0;
/** Events not acknowledged yet, oldest first: `{command, sent}` (`sent`: on the current socket). */
const pending = loadPending();
/** Events of earlier sessions that the server could not save: old session id -> `{count, oldest,
 *  newest}` (browser times), reported once as `events_dropped` in the current session. */
const dropped = loadDropped();

function loadDropped() {
  try {
    const kept = JSON.parse(sessionStorage.getItem(DROPPED) || "[]");
    return new Map(Array.isArray(kept) ? kept : []);
  } catch (error) {
    return new Map();
  }
}

function saveDropped() {
  try {
    if (dropped.size) sessionStorage.setItem(DROPPED, JSON.stringify(Array.from(dropped)));
    else sessionStorage.removeItem(DROPPED);
  } catch (error) {
    noteStorageFailure(error);
  }
}

function outboxKeys() {
  const keys = [];
  for (let i = 0; i < sessionStorage.length; i += 1) {
    const key = sessionStorage.key(i);
    if (key === LEGACY_PENDING || (key && key.startsWith(OUTBOX))) keys.push(key);
  }
  return keys.sort();
}

function loadPending() {
  const found = [];
  try {
    for (const key of outboxKeys()) {
      const kept = JSON.parse(sessionStorage.getItem(key) || "[]");
      if (Array.isArray(kept)) for (const command of kept) found.push({ command, sent: false });
    }
  } catch (error) {
    // no storage: nothing was kept
  }
  return found;
}

function savePending() {
  const groups = new Map();
  for (const item of pending) {
    const id = item.command.session_id || "none";
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(item.command);
  }
  try {
    for (const key of outboxKeys()) {
      if (key === LEGACY_PENDING || !groups.has(key.slice(OUTBOX.length))) sessionStorage.removeItem(key);
    }
    for (const [id, commands] of groups) sessionStorage.setItem(`${OUTBOX}${id}`, JSON.stringify(commands));
  } catch (error) {
    noteStorageFailure(error); // the events still wait in memory; only a reload would lose them
  }
}

function sessionOf(api) {
  const state = api && api.ctx && api.ctx.state;
  return state && state.session_id ? state.session_id : null;
}

/** Sends one `event` command; false if it could not go out now (it is kept and sent later). */
export function sendEvent(api, name, payload, time) {
  warnKeys(name, payload);
  counter += 1;
  const command = {
    cmd: "event",
    name,
    payload,
    browser_time: time,
    time_origin: performance.timeOrigin,
    cid: `${LOAD}-${counter}`,
    session_id: sessionOf(api),
  };
  const item = { command, sent: false };
  const behind = pending.some((p) => !p.sent);
  pending.push(item);
  savePending();
  if (behind) return false; // earlier events wait for the connection: the order is kept
  if (!api.send(command)) return false;
  item.sent = true;
  return true;
}

/** Sends the events that are waiting, in order, with `queued: true`; true when all went out.
 *  app.js calls this on every render while the socket is open. Events kept for an earlier
 *  session go to the server too, which appends them to that session if it can; for those it
 *  cannot save, one `events_dropped` report goes to the current session. */
export function flushEvents(api) {
  for (const item of pending) {
    if (item.sent) continue;
    const command = { ...item.command, payload: { ...item.command.payload, queued: true } };
    if (!api.send(command)) return false;
    item.sent = true;
  }
  // Reported only in a session the server has confirmed on this connection.
  const current = stateSinceConnect ? sessionOf(api) : null;
  if (current) {
    for (const [id, report] of Array.from(dropped)) {
      if (id === current) continue;
      if (pending.some((p) => (p.command.session_id || "none") === id)) continue; // not all answered yet
      dropped.delete(id);
      saveDropped();
      sendEvent(
        api,
        "events_dropped",
        {
          count: report.count,
          session_id: id === "none" ? null : id,
          oldest_browser_ms: report.oldest,
          newest_browser_ms: report.newest,
        },
        performance.now(),
      );
    }
  }
  return true;
}

/** app.js calls this when the socket opens or closes: what was not acknowledged is sent again. */
export function connectionChanged() {
  for (const item of pending) item.sent = false;
  stateSinceConnect = false;
  staleState = reporter && reporter.ctx ? reporter.ctx.state : null;
}

/** app.js calls this with each `ack` message (and each `error` that names a `cid`): the event is
 *  not sent again. Returns the refusal reason, if any. */
export function acknowledge(message) {
  const index = pending.findIndex((p) => p.command.cid === message.cid);
  if (index < 0) return message.refused || null;
  const [item] = pending.splice(index, 1);
  savePending();
  if (message.dropped) {
    const id = item.command.session_id || "none";
    const report = dropped.get(id) || { count: 0, oldest: Infinity, newest: -Infinity };
    report.count += 1;
    report.oldest = Math.min(report.oldest, item.command.browser_time);
    report.newest = Math.max(report.newest, item.command.browser_time);
    dropped.set(id, report);
    saveDropped();
  }
  return message.refused || null;
}

/** How many events wait for an acknowledgement (the tests read it). */
export function pendingEvents() {
  return pending.length;
}

// -- storage -------------------------------------------------------------------------------------

/** Values kept by the runner and by tasks. Memory is the source of truth within a page; each
 *  value is also written to sessionStorage so that a reload goes on where it was. Every key
 *  starts with the session id. */
const memory = new Map();
/** The session id whose storage keys were kept when the others were cleared. */
let cleanedFor = null;

function stored(key, fallback) {
  if (memory.has(key)) return memory.get(key);
  try {
    const value = sessionStorage.getItem(`${STORE}/${key}`);
    if (value !== null) {
      const parsed = JSON.parse(value);
      memory.set(key, parsed);
      return parsed;
    }
  } catch (error) {
    // no storage: nothing was kept by an earlier page
  }
  return fallback;
}

function store(key, value) {
  memory.set(key, value);
  try {
    sessionStorage.setItem(`${STORE}/${key}`, JSON.stringify(value));
  } catch (error) {
    noteStorageFailure(error); // a reload would start from what was stored before; this page goes on
  }
}

/** A write to sessionStorage failed (it is full, or switched off): logged once as the event
 *  `storage_failed`, and the run goes on from memory. */
function noteStorageFailure(error) {
  if (storageFailure) return;
  storageFailure = { error: (error && error.name) || String(error), reported: false };
  reportStorageFailure();
}

function reportStorageFailure() {
  if (!storageFailure || storageFailure.reported || !reporter || !sessionOf(reporter)) return;
  storageFailure.reported = true;
  sendEvent(reporter, "storage_failed", { error: storageFailure.error }, performance.now());
}

/** Removes the runner's stored values of other sessions (not the outboxes, which are sent). */
function clearOtherSessions(sessionId) {
  if (!sessionId || cleanedFor === sessionId) return;
  cleanedFor = sessionId;
  for (const key of Array.from(memory.keys())) if (!key.startsWith(`${sessionId}/`)) memory.delete(key);
  try {
    const stale = [];
    for (let i = 0; i < sessionStorage.length; i += 1) {
      const key = sessionStorage.key(i);
      if (!key || !key.startsWith(`${STORE}/`) || key.startsWith(OUTBOX) || key === LEGACY_PENDING || key === DROPPED) continue;
      if (!key.startsWith(`${STORE}/${sessionId}/`)) stale.push(key);
    }
    for (const key of stale) sessionStorage.removeItem(key);
  } catch (error) {
    // no storage: nothing to clear
  }
}

/** sessionStorage for task code, namespaced by the session id and the step of `state`:
 *  `{get(fallback), set(value), remove()}` for values that JSON can hold. A later session in the
 *  same tab (even one with the same seed) never sees it. `io.store` and `api.store` call this. */
export function taskStore(state, key) {
  const name = `${state && state.session_id}/${state && state.step}/task/${key}`;
  return {
    get: (fallback = null) => stored(name, fallback),
    set: (value) => store(name, value),
    remove: () => {
      memory.delete(name);
      try {
        sessionStorage.removeItem(`${STORE}/${name}`);
      } catch (error) {
        // no storage: the copy in memory is gone
      }
    },
  };
}

function isTyping(target) {
  return !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
}

/** A key on a button (a leave question, the pause overlay) presses the button and is no response. */
function onControl(target) {
  return target instanceof Element && !!target.closest("button, a, #fs-lost, .leave");
}

/**
 * A task screen. `spec`:
 *   name     the event prefix (`<name>.stim` ...), usually the step id
 *   keys     the task's own keys, as `KeyboardEvent.code` values (["Space"], ["KeyF", "KeyJ"]), or
 *            a map from code to answer ({KeyF: "left", KeyJ: "right"}): `response` is then the answer
 *            and `key` the code
 *   legend   [[key text, what it means], ...], shown on screen
 *   panel    true: the stimulus area is the shared square (`.task-panel`), drawn on the page's ground
 *   block(index)  -> { instructions: text, practice: number of practice trials (0 if left out) }
 *   trials({ blockRng, block, blockIndex, practice }) -> next({ index, rng, history }) -> params | null
 *            called once per block (and once for its practice) on a page; the runner keeps `next`
 *            and `history` across pauses. After a reload it is called again: `blockRng` is the same
 *            stream, so the same list results; `index` goes on with the next trial number and
 *            `history` (the `<task>.trial` payloads of the trials that were `done`) is rebuilt from
 *            sessionStorage. `null` ends the trials of the block.
 *   run(params, io) -> Promise<object>   runs one trial; what it returns goes into `<task>.trial`
 *   sides    {left: [key text, meaning], right: [key text, meaning], keys: [left code, right code]}:
 *            large key labels left and right of the stimulus area (in place of the legend); a key
 *            pressed in a response window lights its label for PRESS_SIGN_MS
 *   enter    true: the task starts on Enter. Before practice and before every block the task area
 *            says ENTER_PROMPT; Enter starts the practice, or sends `advance` (which starts the
 *            block's clock); a fixation cross of LEAD_IN_MS comes before the first trial
 *   trialMs(params) -> the fixed length of a trial, feedback and gap included: a trial that would
 *            not end before the block's nominal end is not started; when none fits the page sends
 *            `block_finished` (reason `trials_done`)
 *   example  (with `block(index)`): block(index).example, a list of [top, bottom] pairs shown in a
 *            row under the instructions (a worked example)
 */
export function taskScreen(spec) {
  const name = spec.name;
  const keyMap = spec.keys && !Array.isArray(spec.keys) ? spec.keys : null;
  const keyList = keyMap ? Object.keys(keyMap) : spec.keys || [];
  const ownKeys = new Set(keyList);
  const onEnter = !!spec.enter;
  let m = null; // the mounted screen

  function blockSpec(index) {
    const b = (spec.block && spec.block(index)) || {};
    return { instructions: b.instructions || "", practice: b.practice || 0, example: b.example || null, enterLine: b.enterLine || "" };
  }

  function emit(screen, event, payload, time) {
    if (!sendEvent(screen.api, event, payload, time) && m === screen) lost();
  }

  function where(t) {
    return { block: t.block, trial: t.index, practice: t.practice };
  }

  function frameMs(screen = m) {
    return measuredFrameMs || DEFAULT_FRAME_MS;
  }

  /** `time` moved to the frame grid of the trial (whole frames from its last frame painted). */
  function onGrid(t, time) {
    const f = frameMs();
    return t.lastPaint === null ? time : t.lastPaint + Math.round((time - t.lastPaint) / f) * f;
  }

  const isBackground = (node) => node.nodeType === 1 && node.classList.contains("task-background");

  function hasStimulus(surface) {
    return Array.from(surface.childNodes).some((node) => !isBackground(node));
  }

  /** Removes what the trials drew; the background layer (`io.background`) too when `all`. */
  function clearSurface(surface, all) {
    for (const node of Array.from(surface.childNodes)) if (all || !isBackground(node)) node.remove();
  }

  /** The runner's keys in sessionStorage: session id, step, block. */
  function blockKey(s) {
    return `${s.session_id}/${s.step}/${s.block}`;
  }

  /** The connection is gone: the trial in progress is abandoned and a running block is paused. */
  function lost() {
    if (!m) return;
    if (m.state && m.state.phase === "running") m.pauseWanted = true;
    if (m.trial) abort("abandoned", "connection_lost");
    if (m.mode === "practice") m.loopId += 1; // practice starts again from the instructions
  }

  function requestPause() {
    const now = performance.now();
    if (m.pauseSentAt !== null && now - m.pauseSentAt < PAUSE_RETRY_MS) return;
    if (m.api.send({ cmd: "pause", reason: "connection_lost" })) m.pauseSentAt = now;
  }

  /** The `<task>.trial` payload. Measured and runner fields win; a trial parameter with one of their
   *  names is kept as `param_<name>`, a returned field as `result_<name>` (unless it has the same
   *  value). `run` may return `correct`, and `response`, `key` and `rt_ms` when no key response was
   *  measured (typed answers). */
  function summary(t, final, result, reason) {
    const origin = performance.timeOrigin;
    const r = t.response;
    const timing = {};
    for (const [part, ts] of Object.entries(t.parts)) {
      timing[`${part}_browser_ms`] = ts;
      timing[`${part}_unix_ms`] = origin + ts;
    }
    if (r) {
      timing.resp_browser_ms = r.time;
      timing.resp_unix_ms = origin + r.time;
    }
    const reserved = new Set([...FIXED, ...Object.keys(timing)]);
    const payload = {};
    for (const [key, value] of Object.entries(t.params)) payload[reserved.has(key) ? `param_${key}` : key] = value;
    payload.response = r ? r.answer : null;
    payload.key = r ? r.key : null;
    payload.rt_ms = r ? r.rt_ms : null;
    if (t.presses) {
      // A response window: every press was logged; the last one is the answer.
      const first = t.presses[0];
      const last = t.presses[t.presses.length - 1];
      payload.rt_first_ms = first ? first.rt_ms : null;
      payload.rt_last_ms = last ? last.rt_ms : null;
      payload.responses = t.presses.length;
      payload.changed = !!first && first.answer !== last.answer;
    }
    payload.correct = null;
    for (const [key, value] of Object.entries(result || {})) {
      const mine =
        key === "correct" ||
        (!r && (key === "response" || key === "key" || key === "rt_ms")) ||
        (reserved.has(key) && key in payload && payload[key] === value);
      payload[mine || !reserved.has(key) ? key : `result_${key}`] = value;
    }
    Object.assign(payload, timing, where(t), { status: final, scored: final === "done" && !t.practice });
    if (reason) payload.reason = reason;
    return payload;
  }

  function finish(t, status, result, reason) {
    if (t.closed) return;
    t.closed = true;
    for (const id of t.frames) cancelAnimationFrame(id);
    for (const id of t.timers) clearTimeout(id);
    t.awaiting = null;
    const screen = t.screen;
    if (m === screen && m.trial === t) {
      m.trial = null;
      m.area.dataset.awaiting = "false";
    }
    let final = status;
    if (final === "done" && !t.practice && performance.now() >= screen.endsAt) final = "cut_short";
    const payload = summary(t, final, result, reason);
    emit(screen, `${name}.trial`, payload, performance.now());
    const visible = t.visible;
    if (final !== "done") {
      // Ended from outside (a pause, a skip, a stop, the block's end): nothing stays on the stage
      // under the overlay or the next screen, the background layer included, so it goes now, not
      // in a later frame.
      const had = hasStimulus(screen.surface);
      clearSurface(screen.surface, true);
      if (had && visible) {
        emit(screen, `${name}.stim_off`, { part: visible, frame_ms: frameMs(screen), ...where(t) }, performance.now());
      }
    } else if (hasStimulus(screen.surface)) {
      // What a finished trial left on the screen goes in the next frame; its offset is logged then.
      // The background layer stays.
      requestAnimationFrame((ts) => {
        clearSurface(screen.surface, false);
        if (visible) emit(screen, `${name}.stim_off`, { part: visible, frame_ms: frameMs(screen), ...where(t) }, ts);
      });
    }
    t.resolve(final === "done" ? payload : null);
  }

  /** Ends the trial in progress now; `run` gets `Abandoned` from whatever it awaits. */
  function abort(status, reason) {
    const t = m && m.trial;
    if (!t) return;
    const waiting = Array.from(t.pending);
    finish(t, status, null, reason);
    for (const reject of waiting) reject(new Abandoned());
  }

  function makeIo(t) {
    const screen = t.screen;
    const surface = screen.surface;
    const guard = (executor) =>
      new Promise((resolve, reject) => {
        if (t.closed) {
          reject(new Abandoned());
          return;
        }
        t.pending.add(reject);
        executor((value) => {
          t.pending.delete(reject);
          resolve(value);
        });
      });
    /** Runs `work(ts, frameMs, due)` in the frame nearest to `due` (the next frame when `due` is
     *  null): each frame is checked until one is due. A stimulus change sets the frame grid that
     *  `io.wait` counts from (`grid`); the background layer does not. */
    const paint = (due, change, grid = true) =>
      guard((resolve) => {
        // The lead-in cross stays until the first change of the first trial, in that frame.
        const work = (ts, f, before) => {
          endLeadIn(t, ts, f);
          return change(ts, f, before);
        };
        // Called while the frame in which a response window ended is being prepared (see
        // `response`): a change due in that frame is made in it, not one frame later.
        if (due !== null && t.frameNow !== null && isDueFrame(t.frameNow, due, frameMs(screen))) {
          const ts = t.frameNow;
          if (grid) {
            t.lastPaint = ts;
            t.anchor = ts;
          }
          resolve(work(ts, frameMs(screen), null));
          return;
        }
        // The last frame checked that was not yet due: a change made in the frame after it is
        // late only by the frames that were dropped between the two (see `missedFrames`).
        let before = null;
        const step = (ts) => {
          t.frames.delete(id);
          if (t.closed) return;
          const f = frameMs(screen);
          if (due !== null && !isDueFrame(ts, due, f)) {
            before = ts;
            id = requestAnimationFrame(step);
            t.frames.add(id);
            return;
          }
          if (grid) {
            t.lastPaint = ts;
            t.anchor = ts;
          }
          resolve(work(ts, f, before));
        };
        let id = requestAnimationFrame(step);
        t.frames.add(id);
      });
    /** The due time left by `io.wait` for the next show or clear, used once. */
    const takeDue = () => {
      const next = t.nextDue;
      t.nextDue = null;
      return next;
    };
    const timing = (f, ts, due, frames, before = null) => {
      const out = { frame_ms: f };
      if (due !== null && due !== undefined) {
        out.due_ms = due;
        out.late_frames = before === null ? lateFrames(ts, due, f) : missedFrames(before, ts, due, f);
      }
      if (frames !== null && frames !== undefined) out.frames = frames;
      return out;
    };
    const doClear = (due, frames) => {
      const done = paint(due, (ts, f, before) => {
        clearSurface(surface, false);
        if (t.visible) emit(screen, `${name}.stim_off`, { part: t.visible, ...timing(f, ts, due, frames, before), ...where(t) }, ts);
        t.visible = null;
        return ts;
      });
      done.catch(() => {}); // a clear that is not awaited needs no handler when the trial ends
      return done;
    };
    const doShow = (due, frames, draw, extra) => {
      const part = extra.part === undefined ? "stim" : extra.part;
      if (!PART.test(part)) throw new Error(`a part is 1 to 32 of a-z 0-9 _ starting with a letter, not ${part}`);
      return paint(due, (ts, f, before) => {
        draw(surface);
        t.onset = ts;
        t.visible = part;
        if (!(part in t.parts)) t.parts[part] = ts;
        emit(screen, `${name}.stim`, { ...extra, part, ...timing(f, ts, due, frames, before), ...where(t) }, ts);
        return ts;
      });
    };
    return {
      area: surface,
      index: t.index,
      practice: t.practice,
      block: t.block,
      reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
      /** The measured frame period in ms (the median of the last frame intervals; 1000/60 until
       *  30 have been seen). */
      get frameMs() {
        return frameMs(screen);
      },
      /** Empties the area and, if a stimulus was shown, logs `<task>.stim_off` with that frame's
       *  timestamp; resolves with it. After `io.wait` it is the frame nearest to the end of the wait,
       *  otherwise the next frame. */
      clear() {
        const next = takeDue();
        return doClear(next ? next.due : null, next ? next.frames : null);
      },
      /** `clear` in the frame nearest to `dueMs` (performance.now clock). */
      clearAt(dueMs) {
        takeDue();
        return doClear(dueMs, null);
      },
      /** Calls `draw(area)` and logs `<task>.stim` with that frame's timestamp (the onset); resolves
       *  with the onset. After `io.wait` it is the frame nearest to the end of the wait, otherwise
       *  the next frame. `extra.part` names the part of the stimulus (default `stim`; e.g. `probe`);
       *  the trial record has `<part>_browser_ms`. */
      show(draw, extra = {}) {
        const next = takeDue();
        return doShow(next ? next.due : null, next ? next.frames : null, draw, extra);
      },
      /** `show` in the frame nearest to `dueMs` (performance.now clock); `opts` as `extra`. */
      showAt(dueMs, draw, opts = {}) {
        takeDue();
        return doShow(dueMs, null, draw, opts);
      },
      /** Waits `ms`, as the nearest whole number of frames counted from `from` when given, else
       *  from the end of the wait before it (waits in a row add up), else from the last frame
       *  painted or the end of the last response (a key or a timeout, moved to the frame grid),
       *  whichever came last, else from now. The next `show` or `clear` is painted in the frame
       *  nearest to the end of the wait and logs `due_ms`, `late_frames` and `frames`. A response
       *  ends a wait that was in force before it. Resolves with the due time, up to one frame
       *  before it so that the change can make its frame. */
      wait(ms, from) {
        const f = frameMs(screen);
        let anchor = null;
        if (from !== undefined && from !== null) anchor = from;
        else if (t.nextDue) anchor = t.nextDue.due; // waits in a row add up
        else if (t.anchor !== null) anchor = t.anchor;
        const frames = anchor === null ? null : framesFor(ms, f);
        const due = anchor === null ? performance.now() + ms : anchor + frames * f;
        t.nextDue = { due, frames };
        return guard((resolve) => {
          const id = setTimeout(() => {
            t.timers.delete(id);
            resolve(due);
          }, Math.max(0, due - f - performance.now()));
          t.timers.add(id);
        });
      },
      /** Waits for one of `keys` (default: the task's keys). Resolves `{key, answer, time, rt_ms}`,
       *  `answer` being the task's answer for the key (the code when the task maps none), with
       *  `rt_ms` from `from` (default: the last onset); `{key: null, ...}` when `timeout` ms have
       *  passed. The window ends on the frame grid: in the frame nearest to `from + timeout` (a key
       *  up to then counts), and a `showAt`, `clearAt` or `wait` + `show`/`clear` due in that frame
       *  that follows at once is made in that same frame. */
      response({ keys = keyList, timeout = null, from = t.onset, collect = false } = {}) {
        return guard((resolve) => {
          const awaiting = { keys: new Set(keys), from, resolve, timer: null, frame: null, collect };
          if (collect) t.presses = [];
          if (timeout !== null) {
            const start = from ?? performance.now();
            const due = start + timeout;
            const expire = (ts) => {
              if (t.awaiting !== awaiting || t.closed) return;
              clearTimeout(awaiting.timer);
              t.timers.delete(awaiting.timer);
              if (awaiting.frame !== null) cancelAnimationFrame(awaiting.frame);
              t.awaiting = null;
              screen.area.dataset.awaiting = "false";
              t.nextDue = null; // a wait after the response counts from its end
              t.anchor = ts === null ? onGrid(t, due) : ts;
              if (ts !== null) {
                // Until this frame is painted, a change due in it is made in it (see `paint`).
                t.frameNow = ts;
                setTimeout(() => {
                  if (t.frameNow === ts) t.frameNow = null;
                }, 0);
              }
              if (collect) {
                const presses = t.presses.slice();
                const last = presses[presses.length - 1] || null;
                resolve({
                  key: last ? last.key : null,
                  answer: last ? last.answer : null,
                  time: last ? last.time : null,
                  rt_ms: last ? last.rt_ms : null,
                  presses,
                  end: ts === null ? onGrid(t, due) : ts,
                });
              } else resolve({ key: null, answer: null, time: null, rt_ms: null });
            };
            const watch = (ts) => {
              t.frames.delete(awaiting.frame);
              awaiting.frame = null;
              if (t.awaiting !== awaiting || t.closed) return;
              if (isDueFrame(ts, due, frameMs(screen))) {
                expire(ts);
                return;
              }
              awaiting.frame = requestAnimationFrame(watch);
              t.frames.add(awaiting.frame);
            };
            const f = frameMs(screen);
            // Frames are watched from two frames before the end; a page without frames (hidden)
            // ends the window on the timer, a few frames after the end.
            awaiting.timer = setTimeout(() => {
              t.timers.delete(awaiting.timer);
              if (t.awaiting !== awaiting) return;
              awaiting.frame = requestAnimationFrame(watch);
              t.frames.add(awaiting.frame);
              awaiting.timer = setTimeout(() => expire(null), Math.max(0, due + 4 * f - performance.now()));
              t.timers.add(awaiting.timer);
            }, Math.max(0, due - 2 * f - performance.now()));
            t.timers.add(awaiting.timer);
          }
          t.awaiting = awaiting;
          screen.area.dataset.awaiting = "true";
        });
      },
      /** A fixed response window of `ms` from `from` (default: the last onset), ending on the frame
       *  grid like `response`'s timeout. It does not end on a key: every press of one of `keys` in
       *  it is logged as `<task>.resp` (with `index`, 0 for the first) and lights its key label;
       *  the last one is the answer. Resolves at the end of the window with `{key, answer, time,
       *  rt_ms}` of the last press (all null without one), `presses` (all of them) and `end` (the
       *  window's last frame). The trial record gets `rt_first_ms`, `rt_last_ms`, `responses` and
       *  `changed`; `response`, `key`, `rt_ms` are the last press's. */
      window({ ms, from = t.onset, keys = keyList } = {}) {
        return this.response({ keys, timeout: ms, from, collect: true });
      },
      /** "Correct", "Incorrect" or "No response" (`text`) in the frame nearest `at` for
       *  FEEDBACK_MS, drawn over what is in the area (`.task-feedback`, `#task-feedback`), logged
       *  as `<task>.stim` with part `feedback` and `feedback` (the text); resolves with its onset
       *  when it is taken off again. */
      async feedback(text, at) {
        const node = feedbackNode(text, { id: "task-feedback", "data-trial": String(t.index), "data-practice": String(t.practice) });
        const draw = (area) => {
          for (const old of area.querySelectorAll(":scope > .task-feedback")) old.remove();
          area.append(node);
        };
        const onset = at === undefined || at === null ? await this.show(draw, { part: "feedback", feedback: text }) : await this.showAt(at, draw, { part: "feedback", feedback: text });
        await paint(onset + framesFor(FEEDBACK_MS, frameMs(screen)) * frameMs(screen), (ts, f, before) => {
          node.remove();
          if (t.visible === "feedback") {
            emit(screen, `${name}.stim_off`, { part: "feedback", ...timing(f, ts, onset + framesFor(FEEDBACK_MS, f) * f, null, before), ...where(t) }, ts);
            t.visible = null;
          }
          return ts;
        });
        return onset;
      },
      /** Logs `<task>.<what>` with this trial's block, trial and practice. */
      event(what, payload = {}, time = performance.now()) {
        if (!t.closed) emit(screen, `${name}.${what}`, { ...payload, ...where(t) }, time);
      },
      /** sessionStorage of this session and step for the task's own values (see `taskStore`). */
      store(key) {
        return taskStore(screen.state, key);
      },
      /** Draws `draw(layer)` in the background layer of the stimulus area in the next frame (what
       *  was in the layer is replaced): it stays across trials, behind the stimuli, and is taken
       *  away only when a trial is ended from outside (a pause, a skip, a stop, the block's end)
       *  or the screen is left. Logs `<task>.background`; resolves with the frame's timestamp. */
      background(draw) {
        return paint(
          null,
          (ts, f) => {
            let layer = Array.from(surface.children).find(isBackground);
            if (!layer) {
              layer = el("div", { class: "task-background" });
              surface.prepend(layer);
            }
            layer.replaceChildren();
            draw(layer);
            emit(screen, `${name}.background`, { frame_ms: f, ...where(t) }, ts);
            return ts;
          },
          false,
        );
      },
    };
  }

  function runTrial(params, index, practice, block) {
    return new Promise((resolve) => {
      const t = {
        screen: m,
        index,
        practice,
        block,
        params: params || {},
        pending: new Set(),
        frames: new Set(),
        timers: new Set(),
        awaiting: null,
        response: null,
        onset: null,
        parts: {},
        visible: null,
        lastPaint: null,
        anchor: null,
        frameNow: null,
        nextDue: null,
        stray: false,
        closed: false,
        resolve,
      };
      m.trial = t;
      m.area.dataset.trial = String(index);
      m.area.dataset.practice = String(practice);
      m.area.dataset.awaiting = "false";
      const io = makeIo(t);
      Promise.resolve()
        .then(() => spec.run(t.params, io))
        .then(
          (result) => finish(t, "done", result),
          (error) => {
            if (error instanceof Abandoned || t.closed) return;
            console.error(error);
            finish(t, "error", { error: String(error) }, "error");
          },
        );
    });
  }

  /** The trial list of this block (practice or trials) and its history, made once per page. */
  function generator(label) {
    const s = m.state;
    const key = `${blockKey(s)}/${label}`;
    if (!m.generators[label] || m.generators[label].key !== key) {
      m.generators[label] = {
        key,
        next: spec.trials({
          blockRng: rng(s.seed, name, s.block, label),
          block: s.block,
          blockIndex: s.blocks_done,
          practice: label === "practice",
        }),
        history: stored(`${key}/history`, []),
      };
    }
    return m.generators[label];
  }

  /** The lead-in cross ends in the frame of the first change of the first trial after it. */
  function endLeadIn(t, ts, f) {
    const screen = t.screen;
    const lead = screen.leadIn;
    if (!lead) return;
    screen.leadIn = null;
    lead.node.remove();
    emit(screen, `${name}.lead_in_end`, { due_ms: lead.due, late_frames: lateFrames(ts, lead.due, f), frame_ms: f, ...where(t) }, ts);
  }

  /** The fixation cross of LEAD_IN_MS after Enter: shown in the next frame (`<task>.lead_in`);
   *  resolves with its due end in the frame before that end, so that the first trial's first
   *  change (which takes the cross off, `<task>.lead_in_end`) is made in the frame of the end.
   *  Resolves null if the loop is no longer live. */
  function leadIn(me, live, practice) {
    return new Promise((resolve) => {
      const node = el("div", { class: "task-leadin", id: "task-leadin", "aria-hidden": "true" });
      let due = null;
      const step = (ts) => {
        if (!live()) {
          node.remove();
          if (me.leadIn && me.leadIn.node === node) me.leadIn = null;
          resolve(null);
          return;
        }
        const f = frameMs(me);
        if (due === null) {
          clearSurface(me.surface, true);
          me.surface.append(node);
          due = ts + framesFor(LEAD_IN_MS, f) * f;
          me.leadIn = { node, due };
          emit(
            me,
            `${name}.lead_in`,
            { duration_ms: LEAD_IN_MS, due_ms: due, frames: framesFor(LEAD_IN_MS, f), frame_ms: f, block: me.state.block, practice },
            ts,
          );
        } else if (isDueFrame(ts + f, due, f) || isDueFrame(ts, due, f)) {
          resolve(due);
          return;
        }
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }

  /** No more trials fit in the block: the page says so once (`block_finished`); until the block
   *  ends the stimulus area stays blank. A server without the command answers with an error that
   *  app.js does not show. */
  function blockFinished(me) {
    const key = `${blockKey(me.state)}/finished-sent`;
    if (stored(key, false)) return;
    store(key, true);
    me.api.send({ cmd: "block_finished", reason: "trials_done" });
  }

  async function loop(kind) {
    const me = m;
    const id = ++me.loopId;
    const practice = kind === "practice";
    const label = practice ? "practice" : "trials";
    const s = me.state;
    const count = practice ? me.blockSpec.practice : Infinity;
    const live = () => m === me && me.loopId === id;
    setMode(practice ? "practice" : "trials");
    me.area.focus({ preventScroll: true });
    let ended = "over";
    try {
      const gen = generator(label);
      let startAt = null;
      if (onEnter) {
        startAt = await leadIn(me, live, practice);
        if (startAt === null) return;
      }
      for (let run = 0; run < count && live(); run += 1) {
        if (!practice && performance.now() >= me.endsAt) return; // the block is over; the state says so next
        const index = stored(gen.key, 0);
        const params = gen.next({ index, rng: rng(s.seed, name, s.block, label, index), history: gen.history });
        if (params === null || params === undefined) break;
        if (!practice && spec.trialMs) {
          // No trial is cut short by the clock: one that would not end before the block's
          // nominal end is not started.
          const begin = Math.max(performance.now(), startAt ?? 0);
          if (begin + spec.trialMs(params) + 2 * frameMs(me) > me.endsAt) break;
        }
        store(gen.key, index + 1);
        startAt = null;
        const done = await runTrial(params, index, practice, s.block);
        if (!done) {
          ended = "stopped";
          break;
        }
        gen.history.push(done);
        store(`${gen.key}/history`, gen.history);
      }
    } catch (error) {
      console.error(error);
      ended = "stopped";
    }
    if (!live()) return;
    if (practice) {
      if (ended === "over") store(`${blockKey(s)}/practice-done`, true);
      setMode("");
      draw();
    } else if (ended === "over") {
      setMode("over");
      if (onEnter) {
        // A blank stimulus area until the block ends (at once, with a server that takes
        // `block_finished`).
        blockFinished(me);
      } else {
        me.message.textContent = "There are no more trials in this block. It ends by itself.";
      }
    }
  }

  function setMode(mode) {
    m.mode = mode;
    m.area.dataset.mode = mode || "instructions";
    const trials = mode !== "";
    m.intro.hidden = trials;
    // A task that starts on Enter keeps its stage (the key labels and the prompt) on the
    // instruction screen too; the stage stays (blank) when no more trials fit.
    m.stage.hidden = onEnter ? false : !trials || mode === "over";
    if (!trials || mode !== "over") m.message.textContent = "";
    if (m.prompt) m.prompt.hidden = trials;
    if (onEnter) m.where.hidden = trials;
    if (trials) m.area.dataset.ready = "";
    if (mode === "over" && onEnter) clearSurface(m.surface, true);
  }

  function practiceDone() {
    const s = m.state;
    return m.blockSpec.practice === 0 || stored(`${blockKey(s)}/practice-done`, false);
  }

  /** What Enter does now on the screen of a task that starts on Enter: `practice`, `block` (send
   *  `advance`), or null. */
  function enterAction() {
    const s = m && m.state;
    if (!s || m.mode !== "" || m.trial) return null;
    if (s.phase === "waiting") return practiceDone() ? "block" : "practice";
    if (s.phase === "between" && s.blocks_done < s.blocks_total) return "block";
    return null;
  }

  /** The screen before practice and before every block of a task that starts on Enter: the title,
   *  the block, the instructions (and a worked example) the first time, and the prompt in the
   *  stimulus area between the key labels. */
  function drawEnter() {
    const s = m.state;
    const next = s.phase === "between" ? s.blocks_done : 0;
    const spec_ = blockSpec(next);
    const action = enterAction();
    const afterPractice = s.phase === "waiting" && action === "block" && spec_.practice > 0;
    m.title.textContent = m.api.stepTitle(s.step);
    m.where.textContent =
      action === "practice"
        ? `Practice: ${spec_.practice} ${spec_.practice === 1 ? "trial" : "trials"}`
        : `Block ${Math.min(next + 1, s.blocks_total)} of ${s.blocks_total}`;
    m.instructions.textContent = afterPractice ? "" : spec_.instructions;
    m.instructions.hidden = afterPractice;
    // After a block a pause ended, this prompt is the screen the participant goes on from (app.js
    // shows it instead of the between-blocks screen), so it says what that screen would.
    const cut = s.phase === "between" && action === "block" && !!(s.award && s.award.interrupted);
    m.ready.textContent = afterPractice
      ? "Practice is over. The block works the same way."
      : cut
        ? "The last block was ended by the pause; its finished trials still count."
        : "";
    m.ready.hidden = !afterPractice && !cut;
    const example = afterPractice ? null : spec_.example;
    const exampleKey = example ? JSON.stringify(example) : "";
    if (m.example.dataset.shown !== exampleKey) {
      m.example.dataset.shown = exampleKey;
      m.example.replaceChildren();
      for (const [top, bottom] of example || []) {
        const cell = el("li");
        // A cell's top is text, or a function that draws it (the flanker's arrows).
        const topNode = el("span", { class: "task-example-top" });
        if (typeof top === "function") topNode.append(top());
        else topNode.textContent = top;
        cell.append(topNode, el("span", { class: "task-example-bottom" }, bottom));
        m.example.append(cell);
      }
    }
    m.example.hidden = !example;
    m.actions.replaceChildren();
    m.area.dataset.ready = action || "";
    m.prompt.hidden = !action;
    m.where.hidden = !action;
    m.shown = action || "";
  }

  /** Enter on the prompt: practice starts, or the block is asked to start (`advance`). */
  function enter() {
    const action = enterAction();
    if (action === "practice") {
      loop("practice");
    } else if (action === "block") {
      const now = performance.now();
      if (m.enterSentAt !== null && now - m.enterSentAt < ENTER_RETRY_MS) return;
      if (m.api.send({ cmd: "advance" })) m.enterSentAt = now;
    }
  }

  /** The instruction screen: what to do, the keys, and Practice or Start. */
  function draw() {
    if (onEnter) {
      drawEnter();
      return;
    }
    const s = m.state;
    const wanted = practiceDone() ? "start" : "practice";
    m.title.textContent = m.api.stepTitle(s.step);
    m.where.textContent = `Block ${Math.min(s.blocks_done + 1, s.blocks_total)} of ${s.blocks_total}`;
    m.instructions.textContent = m.blockSpec.instructions;
    // The line that says how the block starts (Words: "Press Enter to start."), just above Start.
    m.enterLine.textContent = m.blockSpec.enterLine;
    m.enterLine.hidden = !m.blockSpec.enterLine;
    if (m.shown === wanted) return;
    m.shown = wanted;
    if (wanted === "practice") {
      const n = m.blockSpec.practice;
      const button = el("button", { class: "btn", id: "practice", type: "button" }, `Practice (${n} ${n === 1 ? "trial" : "trials"})`);
      button.addEventListener("click", () => {
        if (m && m.state.phase === "waiting" && m.mode === "") loop("practice");
      });
      m.actions.replaceChildren(button);
      m.ready.textContent = "";
    } else {
      m.actions.replaceChildren(m.api.advanceButton("Start"));
      m.ready.textContent = m.blockSpec.practice ? "Practice is over. Press Start to begin the block." : "";
    }
  }

  /** Practice stops like a paused block: the trial is abandoned and the instructions come back. */
  function stopPractice(reason) {
    abort("abandoned", reason);
    clearSurface(m.surface, true);
    m.loopId += 1;
    setMode("");
    draw();
  }

  /** The neutral sign that a press was taken: its key label lights for PRESS_SIGN_MS. */
  function pressSign(code) {
    const side = m && m.sides && m.sides[code];
    if (!side) return;
    clearTimeout(side.timer);
    side.node.classList.add("is-pressed");
    side.timer = setTimeout(() => side.node.classList.remove("is-pressed"), PRESS_SIGN_MS);
  }

  function onKey(event) {
    if (!m || isTyping(event.target) || onControl(event.target)) return;
    const t = m.trial;
    if (!t) {
      // On the prompt only Enter does something; between trials and on any other screen a key
      // does what it usually does.
      if (onEnter && (event.code === "Enter" || event.code === "NumpadEnter") && !event.repeat && enterAction()) {
        event.preventDefault();
        enter();
      }
      return;
    }
    if (ownKeys.has(event.code)) event.preventDefault();
    if (event.repeat) return;
    const awaiting = t.awaiting;
    if (awaiting && awaiting.collect && awaiting.keys.has(event.code)) {
      // A response window: every press is logged and lights its key; the window goes on.
      const time = event.timeStamp;
      const rt = awaiting.from === null || awaiting.from === undefined ? null : time - awaiting.from;
      const answer = keyMap && Object.prototype.hasOwnProperty.call(keyMap, event.code) ? keyMap[event.code] : event.code;
      const press = { key: event.code, answer, time, rt_ms: rt };
      const index = t.presses.length;
      t.presses.push(press);
      t.response = press;
      pressSign(event.code);
      emit(m, `${name}.resp`, { key: event.code, response: answer, rt_ms: rt, index, ...where(t) }, time);
      return;
    }
    if (awaiting && awaiting.keys.has(event.code)) {
      t.awaiting = null;
      if (awaiting.timer !== null) clearTimeout(awaiting.timer);
      if (awaiting.frame !== null) cancelAnimationFrame(awaiting.frame);
      m.area.dataset.awaiting = "false";
      const time = event.timeStamp;
      const rt = awaiting.from === null || awaiting.from === undefined ? null : time - awaiting.from;
      const answer = keyMap && Object.prototype.hasOwnProperty.call(keyMap, event.code) ? keyMap[event.code] : event.code;
      t.response = { key: event.code, answer, time, rt_ms: rt };
      t.nextDue = null; // a wait after the key counts from the key, on the frame grid
      t.anchor = onGrid(t, time);
      emit(m, `${name}.resp`, { key: event.code, response: answer, rt_ms: rt, ...where(t) }, time);
      awaiting.resolve(t.response);
      return;
    }
    if (!t.stray) {
      t.stray = true;
      emit(m, `${name}.stray`, { key: event.code, awaiting: !!awaiting, ...where(t) }, event.timeStamp);
    }
  }

  function onHide() {
    abort("abandoned", "page_closed");
  }

  /** Keeps the last frame timestamps while the screen is mounted; their median interval is the
   *  frame period (`io.frameMs`), so it follows the display the page is on. */
  function meter(screen) {
    const times = [];
    const step = (ts) => {
      if (m !== screen) return;
      times.push(ts);
      if (times.length > FRAME_WINDOW) times.shift();
      const period = framePeriod(times);
      if (period !== null) {
        measuredFrameMs = period; // the page's value is replaced only by a full new measurement
        screen.area.dataset.frameMs = period.toFixed(3);
      }
      screen.meterId = requestAnimationFrame(step);
    };
    screen.meterId = requestAnimationFrame(step);
  }

  function mount(root, api) {
    const area = el("div", { class: "task", id: "task-area", tabindex: "-1", "data-task": name });
    const intro = el("div", { class: "task-intro" });
    const title = el("h1", { id: "task-title" });
    const where_ = el("p", { class: "where", id: "task-block" });
    const instructions = el("p", { class: "task-instructions", id: "task-instructions" });
    const ready = el("p", { class: "sub", id: "task-ready" });
    const enterLine = el("p", { class: "task-enter-line", id: "task-enter-line" });
    enterLine.hidden = true;
    const actions = el("div", { class: "task-actions" });
    const leave = leaveControls(api);
    const example = el("ol", { class: "task-example", id: "task-example", "aria-label": "Example" });
    example.hidden = true;
    // One order on every instruction screen: the name, the rule and its example, the keys, then how
    // to start. A task that starts on Enter shows the block line ("Practice: 10 trials") under its
    // Enter prompt, between the key labels; the others keep it under the name.
    intro.append(title, ...(onEnter ? [] : [where_]), instructions, example, ready, enterLine, actions);
    const stage = el("div", { class: "task-stage", id: "task-stage" });
    // The shared stimulus area: a square with no ground of its own that stays while the trials
    // run; only what is drawn in it comes and goes.
    const panel = spec.panel ? el("div", { class: "task-panel", id: "task-panel" }) : null;
    const sides = {};
    let prompt = null;
    if (spec.sides) {
      // The key labels left and right of the stimulus area, in place of the legend.
      const row = el("div", { class: "task-row", id: "task-row" });
      const side = (where, [keyText, meaning], code) => {
        const node = el("div", { class: `task-side task-side--${where}`, id: `task-side-${where}`, "data-key": code });
        node.append(el("kbd", {}, keyText), el("span", {}, meaning));
        sides[code] = { node, timer: null };
        return node;
      };
      row.append(side("left", spec.sides.left, spec.sides.keys[0]));
      if (panel) row.append(panel);
      row.append(side("right", spec.sides.right, spec.sides.keys[1]));
      stage.append(row);
    } else if (panel) stage.append(panel);
    if (onEnter) {
      prompt = el("p", { class: "task-prompt", id: "task-enter", role: "status" });
      prompt.append(el("span", {}, ENTER_PROMPT[0]), el("span", {}, ENTER_PROMPT[1]));
      const cue = el("div", { class: "task-cue" });
      where_.classList.add("task-cue-where");
      cue.append(prompt, where_);
      (spec.sides ? stage.querySelector(".task-row") : stage).append(cue);
    }
    const message = el("p", { class: "sub", id: "task-message", role: "status" });
    const legend = el("ul", { class: "task-legend", id: "task-legend", "aria-label": "Keys" });
    for (const [keyText, meaning] of spec.sides ? [] : spec.legend || []) {
      const item = el("li");
      item.append(el("kbd", {}, keyText), el("span", {}, meaning));
      legend.append(item);
    }
    area.append(intro, stage, message);
    if (!spec.sides) area.append(legend);
    area.append(leave.root);
    root.replaceChildren(area);
    m = {
      api,
      area,
      intro,
      title,
      where: where_,
      instructions,
      example,
      ready,
      enterLine,
      actions,
      stage,
      surface: panel || stage,
      sides,
      prompt,
      leadIn: null,
      enterSentAt: null,
      message,
      leave,
      state: null,
      blockSpec: blockSpec(0),
      generators: {},
      mode: "",
      shown: "",
      trial: null,
      loopId: 0,
      endsAt: Infinity,
      pauseWanted: false,
      pauseSentAt: null,
      meterId: null,
    };
    setMode("");
    meter(m);
    reporter = api;
    clearOtherSessions(api.ctx.state && api.ctx.state.session_id);
    document.addEventListener("keydown", onKey);
    window.addEventListener("pagehide", onHide);
    area.focus({ preventScroll: true });
  }

  function update(state, api) {
    if (!m) return;
    m.state = state;
    if (state !== staleState) stateSinceConnect = true;
    clearOtherSessions(state.session_id);
    reportStorageFailure();
    m.blockSpec = blockSpec(state.blocks_done);
    m.leave.update(state);
    if (state.phase === "running") m.endsAt = api.ctx.stateAt + state.remaining * 1000;
    if (api.ctx.conn !== "open") {
      if (m.trial || m.mode === "practice") lost();
      if (m.mode === "practice") {
        setMode("");
        draw();
      }
      return;
    }
    if (state.phase !== "running") {
      m.pauseWanted = false;
      m.pauseSentAt = null;
    }
    if (state.phase !== "running") m.endsAt = Infinity;
    if (state.phase === "paused") {
      abort("abandoned", state.reason || "paused");
      clearSurface(m.surface, true); // the background layer too, between trials as well
      m.loopId += 1;
      m.leadIn = null;
      // A pause abandons the block: its trials do not go on, even if the block runs again.
      if (onEnter) store(`${blockKey(state)}/abandoned`, true);
      return;
    }
    if (onEnter && state.phase === "between") {
      // The block is over (its time, its last trial, or a pause that abandoned it): the next
      // block of this round starts from its prompt (app.js shows this screen between them).
      if (m.trial) abort("cut_short", null);
      if (m.mode !== "") {
        m.loopId += 1;
        m.leadIn = null;
        clearSurface(m.surface, true);
        setMode("");
      }
      m.enterSentAt = null;
      draw();
      return;
    }
    if (state.phase === "running") {
      if (m.pauseWanted) {
        if (stateSinceConnect) requestPause(); // not on the state of the connection before
        return;
      }
      if (m.mode === "practice") {
        abort("abandoned", "block_started");
        m.loopId += 1;
      }
      if (onEnter && stored(`${blockKey(state)}/abandoned`, false)) {
        // Paused before: this block's trials never go on; a blank stage until it ends. The page
        // has no more trials for it, so it says so (an engine with the command ends it at once).
        if (m.mode !== "over") setMode("over");
        blockFinished(m);
        return;
      }
      // Not in full screen (a reloaded page, full screen just closed): the page pauses the block,
      // so no trial is started that the pause would cut off.
      if (m.mode !== "over" && !m.trial && !m.looping && api.fullscreen.active) {
        m.looping = true;
        loop("trials").finally(() => {
          if (m) m.looping = false;
        });
      }
      return;
    }
    if (state.phase === "waiting" && m.mode === "practice" && !api.fullscreen.active) {
      stopPractice("fullscreen_lost");
      return;
    }
    if (state.phase === "waiting" && m.mode === "") draw();
  }

  /** True for a task that starts on Enter: app.js then shows this screen, not the between-blocks
   *  screen, between two blocks of the round. */
  const startsOnEnter = onEnter;

  function tick(api) {
    if (m && m.pauseWanted && api.ctx.conn === "open" && stateSinceConnect && m.state && m.state.phase === "running") {
      requestPause();
    }
  }

  /** The screen is left (the block ended, the round was skipped, the session ended). */
  function unmount(api) {
    if (!m) return;
    const s = api.ctx.state;
    const ended = s && s.step === m.state?.step && !s.stopped && (s.phase === "between" || s.phase === "done");
    abort(ended ? "cut_short" : "abandoned", ended ? null : s && s.stopped ? "stopped" : "left");
    m.loopId += 1;
    clearSurface(m.surface, true);
    if (m.meterId !== null) cancelAnimationFrame(m.meterId);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("pagehide", onHide);
    m = null;
  }

  /** The instructions of block `index` (the between-blocks screen shows those of the next block). */
  function instructions(index) {
    return blockSpec(index).instructions;
  }

  return { mount, update, tick, unmount, instructions, startsOnEnter };
}

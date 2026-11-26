// The Words round (step `words`: study 12 words, then type the ones remembered) and the Delayed
// recall round (step `recall`: type the words of both lists, no study). Both are screens made with
// `taskScreen` and drawn in the shared stimulus area; each block is one trial of the runner (one
// list, or the delayed recall). The pure parts (pool, lists, timeline, scoring) are in
// ./words-logic.js.
//
// The lists come from one draw: `rng(seed, "words", 0)` over the pool `assets/wordpool.txt`, in the
// Words round and again in Delayed recall, so a session with the same seed (a continued one, or
// delayed recall played alone) gets the same lists. A pause abandons the trial; the words already
// shown, the entries already typed and whether study is over are kept with `io.store` (per session
// and step), so after the resume study goes on with the next word not yet shown (after a new
// fixation cross), or the recall field comes back (empty: entries are never shown, only a short
// sign on the field's border that each one was logged).

import { el, formatTime } from "../format.js";
import { rng } from "./rng.js";
import { sendEvent, taskScreen } from "./runner.js";
import {
  buildLists,
  CONFIRM_TEXT,
  CUE_TEXT,
  DELAYED_PROMPT,
  DELAYED_RECALL_MS,
  DONE_LABEL,
  drawJitter,
  ENTER_LINE,
  FIXATION_MS,
  IMMEDIATE_PROMPT,
  LIST_LENGTH,
  parsePool,
  RECALL_MS,
  recallEnd,
  scoreRecall,
  START_LINE,
  studySchedule,
  WORD_MS,
} from "./words-logic.js";

/** Recall ends at the latest this long before the page expects the block to end, so the list's
 *  trial record (with the entries) is sent while the block still runs. */
export const END_MARGIN_MS = 250;
/** How long the field's border shows that an entry was logged (the same for every entry). */
export const LOGGED_MS = 200;
/** "Yes" in the end-recall question takes no press for this long after the question appears, so a
 *  bounced or doubled Enter cannot answer it. */
export const CONFIRM_GUARD_MS = 300;
/** Longest entry the field takes. */
const MAX_ENTRY = 64;
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "CapsLock", "Enter", "Tab", "Escape"]);
const POOL_PAUSE_REASON = "the word list could not be loaded";
/** What the stage says once recall is over and the block has not ended yet. */
export const WAIT_TEXT = "Waiting for the time to finish";
export const TIME_UP_TEXT = "Time is up.";

let poolPromise = null;
/** The word pool, fetched once (again after a failure). */
function pool() {
  if (!poolPromise) {
    poolPromise = fetch("/static/assets/wordpool.txt").then(async (r) => {
      if (!r.ok) throw new Error(`the word list could not be loaded (HTTP ${r.status})`);
      return parsePool(await r.text());
    });
    poolPromise.catch(() => {
      poolPromise = null;
    });
  }
  return poolPromise;
}

/** The two study lists of a session seed: stream "words", block index 0. */
export async function listsFor(seed) {
  return buildLists(await pool(), rng(seed, "words", 0));
}

/** The jitter of a block's list: stream "words-timing" and the block id, so it is the same for
 *  the same session seed and block (after a reload too). */
export function jitterFor(seed, block) {
  return drawJitter(rng(seed, "words-timing", block));
}

// -- the recall tone (PEERS: "a tone sounded, a row of asterisks appeared") --------------------

let audio = null;
/** Makes the audio context during a key press or click (the browser lets a page start sound only
 *  after one), so the tone can play when recall starts. */
function armAudio() {
  try {
    if (!audio) audio = new AudioContext();
    if (audio.state === "suspended") audio.resume().catch(() => {});
  } catch (error) {
    audio = null;
  }
}
/** A 440 Hz tone of 300 ms with 10 ms ramps; true if the audio context is running. */
function tone() {
  armAudio();
  if (!audio) return false;
  try {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = 440;
    const t0 = audio.currentTime;
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(0.2, t0 + 0.01);
    gain.gain.setValueAtTime(0.2, t0 + 0.29);
    gain.gain.linearRampToValueAtTime(0, t0 + 0.3);
    osc.connect(gain).connect(audio.destination);
    osc.start(t0);
    osc.stop(t0 + 0.31);
    return audio.state === "running";
  } catch (error) {
    return false;
  }
}

// -- Enter starts a list (and delayed recall) --------------------------------------------------

/** Enter on the instruction screen of a Words or Delayed recall block, or on the between screen
 *  that leads to the next list, presses the screen's Start / Continue button (which disables itself,
 *  so one press starts one block). Listened to on window in the capture phase, so the key reaches
 *  nothing else. */
function gateKey(event) {
  if (event.key !== "Enter" || event.repeat || event.isComposing) return;
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const root = document.getElementById("screen");
  if (!root) return;
  let ours = false;
  if (root.dataset.screen === "task" && root.dataset.phase === "waiting") {
    const area = document.getElementById("task-area");
    ours = !!area && (area.dataset.task === "words" || area.dataset.task === "recall") && area.dataset.mode === "instructions";
  } else if (root.dataset.screen === "between") {
    const next = document.getElementById("between-instructions");
    ours = !!next && next.textContent.endsWith(ENTER_LINE);
  }
  if (!ours) return;
  const button = document.getElementById("advance");
  if (!button || button.disabled || button.hidden || !button.isConnected) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  armAudio();
  button.click();
}
if (typeof window !== "undefined") window.addEventListener("keydown", gateKey, true);

function blank() {
  return { next: 1, studied: false, typed: [], shown: [], segments: 0, used_ms: 0, done: false, ended_by: null };
}

/**
 * One task screen. `kind` is "words" (study, then immediate recall of one list) or "recall"
 * (delayed recall of both lists).
 */
function makeScreen(kind) {
  const name = kind;
  let api = null; // the api of the mounted screen
  let sender = null; // the api captured at mount, kept so that late events still go out
  let state = null;
  let endsAt = Infinity;
  let area = null;
  let observer = null;
  let lastView = null; // what stays in the panel once the block's trial is over
  /** What is on screen now and has to be noted if the trial is cut: `word` or `recall`. */
  let live = null;

  /** What is kept for `block` in this session and step (`store` is `io.store` or `api.store`). */
  const storeOf = (store, block) => store(`list_${block}`);

  function where(io) {
    return { block: io.block, trial: io.index, practice: io.practice };
  }

  /** Sends an event even after the trial is over. */
  function send(io, what, payload, time = performance.now()) {
    if (sender) sendEvent(sender, `${name}.${what}`, { ...payload, ...where(io) }, time);
  }

  /** Notes what a pause, a stop or the end of the screen cuts off, before the runner ends the
   *  trial: how long the word on screen was visible (the runner logs its `stim_off`), and the
   *  text in the recall field (`unsubmitted`). */
  function flush(ended) {
    const now = live;
    live = null;
    if (!now) return;
    if (now.kind === "word") {
      now.shown.visible_ms = performance.now() - now.shown.onset_ms;
      now.shown.cut = true;
      now.save();
    }
    if (now.kind === "recall") now.close(ended);
  }

  const fixation = (a) => a.replaceChildren(el("div", { class: "words-fixation", id: "words-fixation" }, "+"));

  /** Study from the next word not yet shown: a fixation cross (the lead-in), then each word on the
   *  list's jittered schedule. Resolves with the time the recall cue is due. */
  async function study(io, mem, save, list, words, jitter) {
    const first = mem.next;
    const schedule = studySchedule(words, jitter);
    const anchor = await io.show(fixation, { part: "fixation", list, next_position: first, lead_in_ms: FIXATION_MS });
    // The schedule from the fixation onset, shifted so that the next word comes FIXATION_MS after it.
    const shift = schedule[first - 1].onset_ms - FIXATION_MS;
    for (let position = first; position <= LIST_LENGTH; position += 1) {
      const item = schedule[position - 1];
      const due = anchor + item.onset_ms - shift;
      const onset = await io.showAt(
        due,
        (a) => a.replaceChildren(el("div", { class: "words-word", id: "words-word" }, item.word)),
        { part: "word", word: item.word, list, position, blank_ms: item.blank_ms },
      );
      mem.next = position + 1;
      const shown = { position, word: item.word, onset_ms: onset, visible_ms: null, cut: false };
      mem.shown.push(shown);
      save();
      const now = { kind: "word", shown, save };
      live = now;
      const offset = await io.clearAt(due + WORD_MS);
      if (live === now) live = null;
      shown.visible_ms = offset - onset;
      save();
    }
    mem.studied = true;
    save();
    const last = schedule[LIST_LENGTH - 1];
    return anchor + last.offset_ms - shift + last.blank_ms;
  }

  /** The quiet view once recall is over: no field, one line. */
  function waitView(text) {
    return el("div", { class: "words-recall words-over", id: "words-wait" }, text);
  }

  /** Recall: at `cueDue` (or after a fixation lead-in when it is null) the recall cue (a tone and a
   *  row of asterisks) and the field come up together; that frame starts the recall clock, which runs
   *  for what is left of `totalMs` (its own clock: a longer study does not shorten it; only the
   *  block's end can). Entries are logged as `<name>.resp`; nothing typed is shown again and nothing
   *  is judged: each logged entry clears the field and lights its border for LOGGED_MS. "I can't
   *  remember any more" (a button: Tab from the field, then Enter) asks once, then ends recall.
   *  Resolves with `{unsubmitted, ended_by}`. */
  async function recallPhase(io, mem, save, prompt, list, cueDue, totalMs) {
    const extra = list ? { list } : {};
    if (cueDue === null) {
      const lead = await io.show(fixation, { part: "fixation", ...extra, lead_in_ms: FIXATION_MS });
      cueDue = lead + FIXATION_MS;
    }
    const box = el("div", { class: "words-recall", id: "words-recall" });
    const cue = el("p", { class: "words-cue", id: "recall-cue", "aria-hidden": "true" }, CUE_TEXT);
    const text = el("p", { class: "words-prompt", id: "recall-prompt" }, prompt);
    const input = el("input", {
      class: "words-input",
      id: "recall-input",
      type: "text",
      maxlength: String(MAX_ENTRY),
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "off",
      spellcheck: "false",
      "aria-label": "Word",
      enterkeyhint: "enter",
    });
    const finish = el("div", { class: "words-finish", id: "recall-finish" });
    const done = el("button", { class: "words-done", id: "recall-done", type: "button" }, DONE_LABEL);
    const hint = el("span", { class: "words-hint", id: "recall-done-hint" }, "Tab, then Enter");
    const confirm = el("div", { class: "words-confirm", id: "recall-confirm", role: "group", "aria-label": CONFIRM_TEXT });
    confirm.hidden = true;
    const question = el("span", { class: "words-question", id: "recall-confirm-text" }, CONFIRM_TEXT);
    const yes = el("button", { class: "words-done words-yes", id: "recall-yes", type: "button" }, "Yes");
    const keep = el("button", { class: "words-done", id: "recall-keep", type: "button" }, "Keep going");
    confirm.append(question, yes, keep);
    const ask = el("div", { class: "words-ask", id: "recall-ask" });
    ask.append(done, hint);
    finish.append(ask, confirm);
    const clock = el("p", { class: "words-time", id: "recall-time", "aria-live": "off" });
    box.append(cue, text, input, finish, clock);
    let active = false;
    let signTimer = null;
    /** The sign that an entry was logged: the border lights for LOGGED_MS, from the start again
     *  when another entry comes first. */
    const signLogged = () => {
      clearTimeout(signTimer);
      input.classList.remove("words-logged");
      void input.offsetWidth; // a fresh start of the sign
      input.classList.add("words-logged");
      signTimer = setTimeout(() => input.classList.remove("words-logged"), LOGGED_MS);
    };
    let firstKey = null;
    let started = null;
    let endAt = Infinity;
    let askedAt = null;
    let participantDone = null;
    const ended = new Promise((resolve) => {
      participantDone = resolve;
    });
    /** The field takes the focus back whenever it goes anywhere but the finish controls. */
    const refocus = () => {
      if (!active) return;
      setTimeout(() => {
        if (!active) return;
        const at = document.activeElement;
        if (!at || at === document.body || !finish.contains(at)) input.focus({ preventScroll: true });
      }, 0);
    };
    const showClock = () => {
      const left = Math.max(0, endAt - performance.now()) / 1000;
      clock.textContent = `Time left ${formatTime(left)}`;
      clock.dataset.remaining = left.toFixed(1);
    };
    const onKey = (event) => {
      if (!active || started === null) return;
      if (event.key === "Enter") {
        if (event.isComposing || event.keyCode === 229) return; // an input method commits its text
        event.preventDefault();
        if (event.repeat) return;
        const raw = input.value;
        if (raw.trim() === "") {
          input.value = "";
          firstKey = null;
          return;
        }
        const entry = {
          text: raw,
          first_key_ms: firstKey === null ? null : firstKey - started,
          enter_ms: event.timeStamp - started,
          segment: mem.segments,
        };
        mem.typed.push(entry);
        save();
        input.value = "";
        firstKey = null;
        signLogged();
        io.event("resp", { ...entry, ...extra }, event.timeStamp);
        return;
      }
      if (firstKey === null && !MODIFIERS.has(event.key)) firstKey = event.timeStamp;
    };
    const showAsk = (focusField) => {
      confirm.hidden = true;
      ask.hidden = false;
      askedAt = null;
      if (focusField) input.focus({ preventScroll: true });
    };
    const onDone = () => {
      if (!active) return;
      ask.hidden = true;
      confirm.hidden = false;
      askedAt = performance.now();
      io.event("recall_confirm", { ...extra, segment: mem.segments });
      yes.focus({ preventScroll: true });
    };
    const onYes = (event) => {
      if (!active || askedAt === null) return;
      if (performance.now() - askedAt < CONFIRM_GUARD_MS) return;
      participantDone(event.timeStamp || performance.now());
    };
    const onYesKey = (event) => {
      if (event.key === "Enter" && event.repeat) event.preventDefault(); // a held Enter is not an answer
    };
    const onKeep = () => {
      if (!active) return;
      io.event("recall_continue", { ...extra, segment: mem.segments });
      showAsk(true);
    };
    let timer = null;
    let closed = false;
    /** Ends the field; text still in it is logged as `unsubmitted` (`why` says why). */
    const close = (why) => {
      if (closed) return null;
      closed = true;
      active = false;
      clearInterval(timer);
      clearTimeout(signTimer);
      input.classList.remove("words-logged");
      input.removeEventListener("keydown", onKey);
      box.removeEventListener("focusout", refocus);
      input.disabled = true;
      for (const b of [done, yes, keep]) b.disabled = true;
      const left = input.value.trim() === "" ? null : input.value;
      if (left !== null) {
        send(io, "unsubmitted", {
          text: left,
          first_key_ms: firstKey === null || started === null ? null : firstKey - started,
          ended: why,
          ...extra,
        });
      }
      return left;
    };
    const now = { kind: "recall", close };
    live = now;
    mem.segments += 1;
    save();
    const segment = mem.segments;
    const totalLeft = Math.max(0, totalMs - mem.used_ms);
    let toneOn = false;
    try {
      started = await io.showAt(
        cueDue,
        (a) => {
          a.replaceChildren(box);
          input.focus({ preventScroll: true });
          toneOn = tone();
        },
        { part: "cue", ...extra, segment },
      );
    } catch (error) {
      close("abandoned");
      if (live === now) live = null;
      throw error;
    }
    lastView = box;
    const window_ = recallEnd(started, totalLeft, endsAt, END_MARGIN_MS);
    endAt = window_.end;
    active = true;
    input.addEventListener("keydown", onKey);
    box.addEventListener("focusout", refocus);
    done.addEventListener("click", onDone);
    yes.addEventListener("click", onYes);
    yes.addEventListener("keydown", onYesKey);
    keep.addEventListener("click", onKeep);
    if (document.activeElement !== input) input.focus({ preventScroll: true });
    timer = setInterval(showClock, 200);
    showClock();
    const planned = window_.capped ? endAt - started : totalLeft;
    io.event("recall_start", { segment, ...extra, recall_ms: planned, tone: toneOn }, started);
    let endedBy;
    let endTime;
    try {
      const result = await Promise.race([
        io.wait(Math.max(0, endAt - performance.now()), performance.now()).then(() => null),
        ended,
      ]);
      if (result === null) {
        endedBy = window_.capped ? "block_end" : "time";
        // The clock's end is the recall's end (the wait resolves up to a frame before it, so that
        // a change could make its frame; nothing is drawn then).
        endTime = endAt;
      } else {
        endedBy = "participant";
        endTime = result;
      }
    } catch (error) {
      mem.used_ms += Math.max(0, performance.now() - started);
      save();
      close("abandoned");
      if (live === now) live = null;
      throw error;
    }
    if (live === now) live = null;
    endAt = Math.min(endAt, endTime);
    showClock();
    const unsubmitted = close(endedBy);
    const used = Math.max(0, endTime - started);
    mem.used_ms += used;
    mem.ended_by = endedBy;
    save();
    io.event("recall_end", { segment, ...extra, ended_by: endedBy, recall_ms: used, recall_total_ms: mem.used_ms }, endTime);
    lastView = waitView(endedBy === "participant" ? WAIT_TEXT : TIME_UP_TEXT);
    io.area.replaceChildren(lastView);
    return { unsubmitted, ended_by: endedBy };
  }

  /** After the trial record of a recall the participant ended has gone out: asks the engine to end
   *  the block now. Until the engine knows the command, the block ends by its own time and the stage
   *  says so (WAIT_TEXT). */
  function finishBlock() {
    setTimeout(() => {
      if (sender) sender.send({ cmd: "block_finished", reason: "participant_done" });
    }, 0);
  }

  /** The pool cannot be had: say so, log it, and pause the block so that nothing is lost. */
  async function cannotLoad(io, error) {
    lastView = null;
    io.area.replaceChildren(
      el(
        "p",
        { class: "words-problem", id: "words-problem", role: "alert" },
        "The word list could not be loaded, so this round cannot be played. The block is paused. " +
          "Choose Skip this round to go on with the next round, or Stop here to end the session.",
      ),
    );
    io.event("pool_failed", { error: String(error && error.message ? error.message : error) });
    if (sender) sender.send({ cmd: "pause", reason: POOL_PAUSE_REASON });
    await io.wait(1e9, performance.now()); // until the pause (or a skip or a stop) ends the trial
  }

  const screen = taskScreen({
    name,
    keys: [],
    // No key legend: the instructions and the recall prompt say what Enter does, and the finish
    // control carries its own hint.
    legend: [],
    panel: true,
    block(index) {
      if (kind === "recall") {
        return { instructions: `${DELAYED_PROMPT} ${START_LINE}` };
      }
      // The block line above already says "Block 1 of 2"; how to start is its own line by Start.
      return {
        instructions:
          `Watch ${LIST_LENGTH} words, one at a time. Remember as many as you can. ` +
          "Then type the words you remember, pressing Enter after each word.",
        enterLine: ENTER_LINE,
      };
    },
    trials({ block, blockIndex }) {
      return () => {
        if (api && storeOf(api.store, block).get(blank()).done) return null;
        return kind === "words" ? { list: blockIndex + 1 } : {};
      };
    },
    async run(params, io) {
      const kept = storeOf(io.store, io.block);
      const mem = kept.get(blank());
      const save = () => kept.set(mem);
      let lists;
      try {
        lists = await listsFor(state.seed);
      } catch (error) {
        await cannotLoad(io, error);
      }
      if (kind === "words") {
        const list = params.list;
        const words = lists[list - 1];
        const jitter = jitterFor(state.seed, io.block);
        let cueDue = null;
        if (!mem.studied) cueDue = await study(io, mem, save, list, words, jitter);
        const out = await recallPhase(io, mem, save, IMMEDIATE_PROMPT, list, cueDue, RECALL_MS);
        mem.done = true;
        save();
        const typed = mem.typed.map((e) => e.text);
        const before = lists.slice(0, list - 1).map((w, i) => ({ list: i + 1, words: w }));
        const s = scoreRecall([{ list, words }], typed, before);
        if (out.ended_by === "participant") finishBlock();
        return {
          words,
          typed,
          matched: s.matched,
          intrusions: s.intrusions,
          intrusions_tagged: s.intrusions_tagged,
          repeats: s.repeats,
          unsubmitted: out.unsubmitted,
          shown: mem.shown.map(({ position, word, visible_ms, cut }) => ({ position, word, visible_ms, cut })),
          blanks_ms: jitter.blanks_ms,
          cue_delay_ms: jitter.cue_delay_ms,
          ended_by: out.ended_by,
          recall_ms: mem.used_ms,
        };
      }
      const out = await recallPhase(io, mem, save, DELAYED_PROMPT, null, null, DELAYED_RECALL_MS);
      mem.done = true;
      save();
      const typed = mem.typed.map((e) => e.text);
      const s = scoreRecall(lists.map((words, i) => ({ list: i + 1, words })), typed);
      if (out.ended_by === "participant") finishBlock();
      return {
        lists,
        typed,
        matched: s.matched,
        intrusions: s.intrusions,
        intrusions_tagged: s.intrusions_tagged,
        repeats: s.repeats,
        unsubmitted: out.unsubmitted,
        ended_by: out.ended_by,
        recall_ms: mem.used_ms,
      };
    },
  });

  /** The runner empties the panel after the block's one trial and says "no more trials"; the last
   *  view (the recall field, or a fixation cross) stays in the panel instead. */
  function holdStage() {
    if (!area || area.dataset.mode !== "over") return;
    const stage = area.querySelector("#task-stage");
    const panel = area.querySelector("#task-panel");
    const message = area.querySelector("#task-message");
    if (message && message.textContent) message.textContent = "";
    if (stage && stage.hidden) stage.hidden = false;
    if (panel) {
      const view = lastView || el("div", { class: "words-fixation", id: "words-fixation" }, "+");
      if (panel.firstChild !== view) panel.replaceChildren(view);
    }
  }

  return {
    mount(root, a) {
      api = a;
      sender = a;
      live = null;
      lastView = null;
      pool().catch(() => {}); // fetched while the instructions are read
      screen.mount(root, a);
      area = root.querySelector("#task-area");
      if (area) {
        area.classList.add("words-task");
        observer = new MutationObserver(holdStage);
        observer.observe(area, {
          attributes: true,
          attributeFilter: ["data-mode", "hidden"],
          subtree: true,
          childList: true,
          characterData: true,
        });
      }
    },
    update(s, a) {
      api = a;
      state = s;
      if (s.phase === "running") endsAt = a.ctx.stateAt + s.remaining * 1000;
      if (s.phase === "paused" || a.ctx.conn !== "open") flush("abandoned");
      screen.update(s, a);
    },
    tick(a) {
      screen.tick(a);
    },
    unmount(a) {
      const s = a.ctx.state;
      flush(s && !s.stopped && (s.phase === "between" || s.phase === "done") ? "block_end" : "abandoned");
      screen.unmount(a);
      if (observer) observer.disconnect();
      observer = null;
      area = null;
      api = null;
    },
    /** The next list's instructions on the between screen, with how to start it at the end. */
    instructions(index) {
      const text = screen.instructions(index);
      return text ? `${text} ${ENTER_LINE}` : text;
    },
  };
}

const words = makeScreen("words");
export const { mount, update, tick, unmount, instructions } = words;
/** The Delayed recall round (registered in index.js as `recall`). */
export const recall = makeScreen("recall");

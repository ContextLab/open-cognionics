// "Skip this round" and "Stop here": two quiet buttons, each asking once more before it sends.
// They are small, set apart from the main button, and an answer is needed to go on, so that
// neither is easy to press by accident. The server decides what is allowed; this only hides the
// buttons where the server would refuse (participant and device steps, the summary, a running block).
import { el } from "../format.js";

/** How long a question stays open if nobody answers it. */
const ASK_MS = 10000;
/** After "yes" the question stays closed for this long, so a second press cannot repeat it. */
const SENT_MS = 3000;

/** Steps the server refuses to skip. */
const NO_SKIP = ["participant", "device"];

/**
 * Set when "yes" was pressed: the screen that follows may be a new one (the old buttons are
 * gone), so the first controls to appear take the keyboard focus, which would otherwise fall to
 * the page body. "skip" or "stop", which button to prefer.
 */
let carryFocus = null;

/** Where the session was when "yes" was pressed; the focus is carried only once it has moved on. */
function where(state) {
  return state ? `${state.step}/${state.block}/${state.phase}/${state.stopped}` : "";
}

function focusIsLost() {
  return !document.activeElement || document.activeElement === document.body;
}

/**
 * The two buttons and their question. `prefix` keeps the element ids apart when the controls
 * are in the page twice (the pause overlay and the screen under it).
 * `whileRunning` shows them in a running block as well (the video round, over its player).
 * Returns `{ root, update(state) }`.
 */
export function leaveControls(api, prefix = "", { whileRunning = false } = {}) {
  const root = el("div", { class: "leave", id: `${prefix}leave` });
  const row = el("div", { class: "leave-row" });
  const skip = el("button", { class: "leave-btn", id: `${prefix}skip-round`, type: "button" }, "Skip this round");
  const stop = el("button", { class: "leave-btn", id: `${prefix}stop-here`, type: "button" }, "Stop here");
  row.append(skip, stop);

  // The group is named by its question, which is also a status, so a screen reader says it when it opens.
  const ask = el("div", {
    class: "leave-ask",
    id: `${prefix}leave-ask`,
    role: "group",
    "aria-labelledby": `${prefix}leave-text`,
  });
  ask.hidden = true;
  const text = el("p", { id: `${prefix}leave-text`, role: "status" });
  // What was done, said once "yes" was pressed; the buttons stay (so the focus stays) but do nothing for a moment.
  const sent = el("p", { class: "hint", id: `${prefix}leave-sent`, role: "status" });
  sent.hidden = true;
  const answers = el("div", { class: "leave-row" });
  const yes = el("button", { class: "leave-yes", id: `${prefix}leave-yes`, type: "button" });
  const no = el("button", { class: "leave-btn", id: `${prefix}leave-no`, type: "button" }, "Keep going");
  answers.append(yes, no);
  ask.append(text, answers);
  root.append(row, sent, ask);

  let pending = null; // "skip" | "stop"
  let timer = null;
  let sentTimer = null;
  let waiting = false; // "yes" was pressed a moment ago

  /** Closes the question. The focus, if it was inside it, goes back to the button that opened it. */
  function close() {
    const opener = pending === "skip" ? skip : stop;
    const inside = ask.contains(document.activeElement);
    pending = null;
    clearTimeout(timer);
    ask.hidden = true;
    row.hidden = false;
    if (inside) {
      const target = !opener.hidden ? opener : [skip, stop].find((b) => !b.hidden);
      if (target) target.focus();
    }
  }

  function open(what) {
    const state = api.ctx.state;
    pending = what;
    if (what === "skip") {
      text.textContent =
        `Skip ${api.stepTitle(state.step)}? The rest of this round is not played; ` +
        "trials already played keep their points.";
      yes.textContent = "Yes, skip this round";
    } else {
      // Says what is true for this session: before the headset link first came up there is no EEG.
      // A session exists here, so a live link means its recording has started (it starts with the
      // link); `recording` covers a link told before the label was accepted.
      const link = api.ctx.link;
      const recording = !!(link && (link.recording || link.state === "live"));
      text.textContent =
        (recording
          ? "Stop here? The session ends and the recording is saved. "
          : "Stop here? The session ends and what happened so far is saved; the headset link is not up, so no EEG is being recorded. ") +
        "You can carry on later from the next round that was not reached.";
      yes.textContent = "Yes, stop here";
    }
    row.hidden = true;
    ask.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(close, ASK_MS);
    no.focus();
  }

  skip.addEventListener("click", () => {
    if (!waiting) open("skip");
  });
  stop.addEventListener("click", () => {
    if (!waiting) open("stop");
  });
  no.addEventListener("click", () => {
    const back = pending === "skip" ? skip : stop;
    close();
    back.focus();
  });
  yes.addEventListener("click", () => {
    const what = pending;
    close(); // the focus goes back to the button that was pressed, and stays there
    waiting = true;
    carryFocus = { what, from: where(api.ctx.state) };
    for (const button of [skip, stop]) button.setAttribute("aria-disabled", "true");
    sent.textContent = what === "skip" ? "Skipping this round." : "Stopping the session.";
    sent.hidden = false;
    clearTimeout(sentTimer);
    sentTimer = setTimeout(() => {
      waiting = false;
      for (const button of [skip, stop]) button.removeAttribute("aria-disabled");
      sent.hidden = true;
      sent.textContent = "";
    }, SENT_MS);
    if (what === "skip") api.skip();
    else api.stop();
  });

  function update(state) {
    // `whileRunning`: the video round offers them during its block too (the server allows it).
    const live = !!state && state.phase !== "done" && (whileRunning || state.phase !== "running");
    const canSkip = live && state.kind !== "summary" && !NO_SKIP.includes(state.step);
    const canStop = live && state.step !== "participant";
    skip.hidden = !canSkip;
    stop.hidden = !canStop;
    root.hidden = !(canSkip || canStop);
    if (root.hidden || (pending === "skip" && !canSkip) || (pending === "stop" && !canStop)) close();
    if (carryFocus && state && where(state) !== carryFocus.from) {
      if (state.phase === "done") carryFocus = null; // the summary takes the focus
      else if (!root.hidden && root.isConnected) {
        if (focusIsLost()) {
          const first = carryFocus.what === "skip" ? [skip, stop] : [stop, skip];
          const target = first.find((b) => !b.hidden);
          if (target) target.focus();
        }
        carryFocus = null;
      }
    }
  }

  return { root, update };
}

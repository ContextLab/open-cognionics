// A timed round that is not built yet: its title, the block and the time left.
import { el, formatTime } from "../format.js";
import { leaveControls } from "./leave.js";

let nodes = null;
let leave = null;

export function mount(root, api) {
  const wrap = el("div", { class: "round" });
  nodes = {
    title: el("h1", { id: "round-title" }),
    where: el("p", { class: "where", id: "round-block" }),
    time: el("div", { id: "time-left" }),
    caption: el("p", { class: "time-caption" }, "left in this block"),
    actions: el("div"),
  };
  leave = leaveControls(api);
  wrap.append(
    nodes.title,
    el("p", { class: "sub" }, "This round is not built yet."),
    nodes.where,
    nodes.time,
    nodes.caption,
    nodes.actions,
    leave.root,
  );
  root.replaceChildren(wrap);
  nodes.shown = "";
}

export function update(state, api) {
  nodes.title.textContent = api.stepTitle(state.step);
  const n = Math.min(state.blocks_done + 1, state.blocks_total);
  nodes.where.textContent = `Block ${n} of ${state.blocks_total}`;
  tick(api);
  leave.update(state);
  const wanted = state.phase === "waiting" ? "start" : "";
  if (nodes.shown !== wanted) {
    nodes.shown = wanted;
    nodes.actions.replaceChildren(...(wanted ? [api.advanceButton("Start")] : []));
  }
}

/** Draws the time left; the server's value, run down from when it arrived. */
export function tick(api) {
  const remaining = api.remaining();
  if (remaining === null) return;
  nodes.time.dataset.remaining = remaining.toFixed(3);
  const text = formatTime(remaining);
  if (nodes.time.textContent !== text) nodes.time.textContent = text;
}

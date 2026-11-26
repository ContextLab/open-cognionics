// The session ended early, or could not go on: says so, with the reason, and shows the record of
// the session (record.js) with where its zip was saved.
import { el } from "../format.js";
import { chooseTip } from "../tips.js";
import { drawRecord, recordKey } from "./record.js";

let reason = null;
let tip = null;
let record = null;
let lists = null;
let shown = "";

export function mount(root) {
  const wrap = el("div", { class: "ended" });
  reason = el("p", { id: "ended-reason" });
  tip = el("p", { class: "device-tip", id: "ended-tip" });
  tip.hidden = true;
  record = el("section", { id: "summary-record", class: "record", "aria-label": "Session record" });
  record.hidden = true;
  // Two columns on a wide window: what happened and the zip; the points and the timeline.
  const main = el("div", { class: "summary-main" });
  main.append(
    el("h1", {}, "The session has ended"),
    reason,
    tip,
    el("p", {}, "What happened up to this point is saved on this computer. Start brain-mapper again for a new session."),
    record,
  );
  lists = el("div", { class: "record-lists" });
  const side = el("div", { class: "summary-side" });
  side.append(lists);
  wrap.append(main, side);
  root.replaceChildren(wrap);
  shown = "";
}

export function update(_state, api) {
  const text = api.endedReason();
  if (reason.textContent !== text) reason.textContent = text;
  // A live link that was lost says how to get it back (a replay that ran out has nothing to fix).
  const link = api.ctx.link;
  const lost = link && link.state === "ended" && (link.fault || (api.ctx.hello && api.ctx.hello.kind === "live"));
  const found = lost ? chooseTip(api.ctx.link, null, true) : null;
  tip.hidden = !found;
  tip.textContent = found ? found.text : "";
  tip.dataset.tip = found ? found.code : "";
  const current = api.record();
  const key = recordKey(current);
  if (key !== shown) {
    shown = key;
    drawRecord(record, current, api, lists, { zipAside: true });
  }
}

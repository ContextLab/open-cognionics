// The record of a session that is over, shown by the summary and ended screens: who and when,
// the points of each task round as earned of possible and the session total (the scores of
// session.json, see scores.js), the timeline of the blocks, and where the session zip was saved,
// with its download. It is drawn from the server's `package` message (api.record()).
import { el } from "../format.js";
import { DASH, scoreRows, totalText } from "../scores.js";
import { resultsEntry } from "./results.js";

/** "November 25, 2026, 4:39 PM" (US order; the data files keep ISO times). */
export function when(summary) {
  const date = new Date(summary.started_unix * 1000);
  const day = date.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  const time = date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  return `${day}, ${time}`;
}

/** "4:39:05 PM". */
function clock(unix) {
  return new Date(unix * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function blockName(api, stepId, blockId) {
  const step = api.planFlow().find((s) => s.id === stepId);
  const index = step ? step.blocks.findIndex((b) => b.id === blockId) : -1;
  return index >= 0 ? `Block ${index + 1} of ${step.blocks.length}` : blockId;
}

/** The points of each task round and the session total. */
function pointsPart(api, summary) {
  const state = api.ctx.state;
  // The scores saved in session.json; before the record has them, the live ones.
  const scores = summary.scores || (state && state.scores) || null;
  const total = totalText(scores);
  const part = el("div", { class: "record-points" });
  part.append(
    el("h2", {}, "Points"),
    el("p", { id: "summary-points", class: "total" }, total === DASH ? "Total: nothing scored" : `Total: ${total} points`),
  );
  const list = el("ul", { id: "summary-scores", class: "scores" });
  for (const row of scoreRows(api.planFlow(), state, scores)) {
    const item = el("li", { "data-step": row.id });
    item.append(el("span", { class: "what" }, row.title), el("span", { class: "how" }, row.text));
    if (row.reason) item.append(el("span", { class: "why" }, row.reason));
    list.append(item);
  }
  if (!list.children.length) list.append(el("li", { class: "none" }, "No round of this session is scored."));
  part.append(list);
  return part;
}

// The parts of the calibration step, as the timeline names them (`part`).
const PARTS = {
  contact: "Contact check",
  settling: "Settling",
  signal: "Signal check",
  eyes_open: "Eyes open",
  eyes_closed: "Eyes closed",
  blinks: "Blinks",
};

function timelineTable(api, items) {
  const table = el("table", { id: "summary-timeline", class: "timeline" });
  const head = el("tr");
  for (const name of ["Round", "Block", "Start", "Length"]) head.append(el("th", { scope: "col" }, name));
  const body = el("tbody");
  for (const item of items) {
    const attrs = { "data-step": item.step, "data-block": item.block, "data-outcome": item.outcome || "" };
    if (item.part) attrs["data-part"] = item.part;
    const row = el("tr", attrs);
    const length = item.seconds === null ? "not recorded" : `${item.seconds.toFixed(1)} s`;
    const left = item.outcome === "left" ? " (left early)" : item.outcome === "interrupted" ? " (interrupted)" : "";
    row.append(
      el("td", {}, api.stepTitle(item.step)),
      el("td", {}, item.part ? PARTS[item.part] || item.part : blockName(api, item.step, item.block)),
      el("td", {}, clock(item.start_unix)),
      el("td", {}, length + left),
    );
    body.append(row);
  }
  table.append(el("thead"), body);
  table.tHead.append(head);
  return table;
}

function zipLine(record) {
  const line = el("p", { id: "summary-zip", class: "zip", "data-state": record.state });
  if (record.state === "saved") {
    line.append("Saved as ", el("strong", {}, record.file), " in ", el("span", { class: "path" }, record.folder), ".");
  } else if (record.state === "making") {
    line.textContent = "Saving the session zip.";
  } else {
    line.setAttribute("role", "alert");
    line.textContent = record.msg;
  }
  return line;
}

/** Fills `root` with the record, or empties it while the session has none (it is not over yet).
 *  With `aside`, the results entry, the points and the timeline go there instead (beside the rest on a
 *  wide window, the results entry above the points and the timeline), and with `zipAside` the zip and
 *  its download too (first). With `resultsAside` false the results entry stays in `root`, under the
 *  zip (the summary after a stop, whose second column already lists every round). */
export function drawRecord(root, record, api, aside = null, { zipAside = false, resultsAside = true } = {}) {
  if (aside) aside.replaceChildren();
  if (!record) {
    root.replaceChildren();
    root.hidden = true;
    return;
  }
  root.hidden = false;
  const parts = [];
  const summary = record.summary;
  if (summary) {
    const who = el("p", { id: "summary-who", class: "who", "data-date": (summary.start_local || "").slice(0, 10) });
    who.append(el("strong", {}, summary.label), ` · ${when(summary)}`);
    parts.push(who, el("p", { id: "summary-recording", "data-recordings": (summary.recordings || []).join(" ") }, recordingSentence(record)));
  }
  // Where the zip is, and its download, come before the lists, which can be long.
  const saved = el("div", { class: "record-zip" });
  saved.append(el("h2", {}, "Session zip"), zipLine(record));
  if (record.state === "saved" && record.download) {
    saved.append(el("a", { id: "summary-download", class: "btn", href: record.download, download: record.file }, "Download"));
  }
  if (record.notes && record.notes.length) {
    const notes = el("ul", { id: "summary-notes", class: "notes" });
    for (const note of record.notes) notes.append(el("li", {}, note));
    saved.append(notes);
  }
  if (aside && zipAside) aside.append(saved);
  else parts.push(saved);
  // Results come from the EEG: a session without any says nothing about them (results.js).
  // They come before the points and the timeline: at the top of their column when there is an aside.
  const results = resultsEntry(record, api);
  if (results && aside && resultsAside) aside.append(results);
  else if (results) parts.push(results);
  if (summary) {
    const points = pointsPart(api, summary);
    const times = el("div", { class: "record-timeline" });
    times.append(el("h2", {}, "Timeline"));
    if (summary.timeline.length) times.append(timelineTable(api, summary.timeline));
    else times.append(el("p", { class: "none" }, "No block was started."));
    const grid = el("div", { class: "record-grid" });
    grid.append(points, times);
    if (aside) aside.append(grid);
    else parts.push(grid);
  }
  root.replaceChildren(...parts);
}

/** A key that changes whenever what drawRecord shows would change. */
export function recordKey(record) {
  const notes = record && record.notes ? record.notes.length : 0;
  const total = record && record.summary && record.summary.scores ? totalText(record.summary.scores) : "";
  return record ? `${record.state}/${record.file}/${record.download}/${record.msg}/${notes}/${total}` : "none";
}

/**
 * What the screen says about the recording, once the record is in (before that, nothing is claimed):
 * the EEG is saved, only the part before a lost link is saved, or no EEG was recorded at all.
 */
export function recordingSentence(record) {
  if (!record || !record.summary) return "";
  const made = record.summary.recordings || [];
  if (made.includes("eeg")) return "The recording is saved on this computer.";
  if (made.length) {
    return "Only the EEG recorded before the headset link was lost is saved on this computer; the link did not come back.";
  }
  return "No EEG was recorded in this session: the headset link never came up. What happened in the session is saved on this computer.";
}

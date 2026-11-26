// The summary step. Once the session is over it shows the record of the session (record.js): who
// and when, the points earned of possible and the timeline, and where the zip was saved, with its
// download. It sits on the page's dark ground like the other screens. When the
// participant stopped the session, it also says so and lists each round with what happened to it.
import { el, rounds } from "../format.js";
import { drawRecord, recordKey } from "./record.js";
import { DASH, totalText } from "../scores.js";

let nodes = null;
let shown = "";

const WORDS = {
  finished: "Finished",
  partial: "Partly played",
  skipped: "Skipped",
  not_reached: "Not reached",
  running: "Not reached",
};

export function mount(root) {
  const wrap = el("div", { class: "plain summary" });
  nodes = {
    text: el("p", { id: "summary-text" }),
    stopped: el("div", { id: "summary-stopped" }),
    actions: el("div"),
    record: el("section", { id: "summary-record", class: "record", "aria-label": "Session record" }),
    lists: el("div", { class: "record-lists" }),
  };
  nodes.stopped.hidden = true;
  nodes.record.hidden = true;
  // tabindex -1: after "Stop here" the focus goes to the heading, so it does not fall to the page body.
  nodes.title = el("h1", { id: "summary-title", tabindex: "-1" }, "Summary");
  // Two columns on a wide window: what happened and the zip; the rounds, points and timeline.
  const main = el("div", { class: "summary-main" });
  nodes.side = el("div", { class: "summary-side" });
  main.append(nodes.title, nodes.text, nodes.actions, nodes.record);
  nodes.side.append(nodes.stopped, nodes.lists);
  wrap.append(main, nodes.side);
  root.replaceChildren(wrap);
  shown = "";
}

function stoppedList(state, api) {
  const flow = api.planFlow();
  const list = el("ul", { class: "outcomes", id: "stopped-rounds" });
  for (const step of rounds(flow)) {
    const item = el("li", { "data-step": step.id, "data-outcome": state.outcomes[step.id] });
    item.append(el("span", { class: "what" }, step.title), el("span", { class: "how" }, WORDS[state.outcomes[step.id]]));
    list.append(item);
  }
  const all = rounds(flow);
  const next = all.find((s) => state.outcomes[s.id] === "not_reached");
  // Nothing played means the earlier session is not offered when the participant ID is
  // typed again, so the page does not promise a continuation.
  const played =
    state.points_total > 0 || all.some((s) => ["finished", "partial", "skipped"].includes(state.outcomes[s.id]));
  const parts = [list];
  let text = "Every round was finished, partly played or skipped.";
  if (next && played && state.only) {
    // A session of rounds chosen on the start screen is never offered to continue.
    text =
      "Rounds chosen on the start screen are not continued in a later session. " +
      "To play the rounds not reached, choose them again on the start screen.";
  } else if (next && played) {
    text = `Start brain-mapper again with the same participant ID to continue from ${next.title}.`;
  } else if (next) {
    text = "No round was played, so there is nothing to continue from.";
  }
  parts.push(el("p", { id: "stopped-next", class: "note" }, text));
  return parts;
}

export function update(state, api) {
  const record = state.phase === "done" ? api.record() : null;
  const total = totalText(record && record.summary && record.summary.scores ? record.summary.scores : state.scores);
  const key = `${state.phase}/${state.stopped}/${state.points_total}/${total}/${recordKey(record)}`;
  if (shown === key) return;
  shown = key;
  if (state.stopped) {
    nodes.text.textContent =
      `The session was stopped by the participant. Points so far: ${total === DASH ? "nothing scored" : `${total}`}.`;
    nodes.stopped.replaceChildren(...stoppedList(state, api));
    nodes.stopped.hidden = false;
    nodes.actions.replaceChildren();
    if (!document.activeElement || document.activeElement === document.body) nodes.title.focus();
  } else if (state.phase === "waiting") {
    nodes.text.textContent = "Every round is done. Finish ends the session and saves it as a zip on this computer.";
    nodes.stopped.hidden = true;
    nodes.actions.replaceChildren(api.advanceButton("Finish"));
  } else {
    nodes.text.textContent = "The session is finished.";
    nodes.stopped.hidden = true;
    nodes.actions.replaceChildren();
  }
  // After a stop the second column lists every round as well: the results entry stays under the zip.
  drawRecord(nodes.record, record, api, nodes.lists, { resultsAside: !state.stopped });
}

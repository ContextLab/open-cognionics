// Between blocks, on the full amber ground: the round's points so far as earned of possible, the
// session total the same way (the HUD shows the same total), and what comes next. The scores are the
// server's (`state.scores`, see scores.js); a round with nothing scored shows a dash and why.
import { el } from "../format.js";
import { DASH, entryOf, isScoredKind, ofText, totalText } from "../scores.js";
import { leaveControls } from "./leave.js";

let nodes = null;
let leave = null;

export function mount(root, api) {
  const wrap = el("div", { class: "between" });
  const gain = el("div", { class: "gain" });
  nodes = {
    lead: el("div", { class: "lead", id: "between-lead" }),
    gained: el("div", { id: "between-gained" }),
    why: el("p", { id: "between-why" }),
    total: el("strong", { id: "between-total" }),
    next: el("h2", { id: "between-next" }),
    detail: el("p", { class: "detail" }),
    // The next block's instructions, for a task: blocks after the first start from here.
    instructions: el("p", { class: "detail", id: "between-instructions" }),
  };
  // The session total is said only when it differs from the round's points (another round scored).
  const sentence = el("p", { id: "between-session" });
  nodes.sentence = sentence;
  sentence.append("Session total: ", nodes.total, ". Each round is listed at the end.");
  gain.append(nodes.lead, nodes.gained, nodes.why, sentence);

  const card = el("div", { class: "next-card" });
  leave = leaveControls(api);
  card.append(
    el("div", { class: "label" }, "Next"),
    nodes.next,
    nodes.detail,
    nodes.instructions,
    api.advanceButton("Continue"),
    el("p", { class: "aside" }, "Ready when you are."),
    leave.root,
  );
  wrap.append(gain, card);
  root.replaceChildren(wrap);
}

function set(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export function update(state, api) {
  const flow = api.planFlow();
  const step = flow.find((s) => s.id === state.step);
  const title = api.stepTitle(state.step);
  if (isScoredKind(step)) {
    const text = ofText(entryOf(state.scores, step));
    set(nodes.lead, `${title}, points so far`);
    set(nodes.gained, text || DASH);
    set(nodes.why, text ? "" : "No scored trials so far.");
  } else {
    set(nodes.lead, title);
    set(nodes.gained, "");
    set(nodes.why, "This round is not scored.");
  }
  // The block a pause ended: said once here, on the screen the participant goes on from.
  if (state.award && state.award.interrupted) {
    set(nodes.why, [nodes.why.textContent, "The last block was ended by the pause; its finished trials still count."].filter(Boolean).join(" "));
  }
  nodes.gained.hidden = !nodes.gained.textContent;
  nodes.why.hidden = !nodes.why.textContent;
  const total = totalText(state.scores);
  set(nodes.total, total === DASH ? "nothing scored yet" : `${total} points`);
  const same = !!step && isScoredKind(step) && (ofText(entryOf(state.scores, step)) || DASH) === total;
  nodes.sentence.hidden = same;
  leave.update(state);
  if (state.blocks_done < state.blocks_total) {
    nodes.next.textContent = `Block ${state.blocks_done + 1} of ${state.blocks_total}`;
    nodes.detail.textContent = api.stepTitle(state.step);
    const task = api.taskOf(state.step);
    nodes.instructions.textContent = task ? task.instructions(state.blocks_done) : "";
  } else {
    nodes.instructions.textContent = "";
    const index = flow.findIndex((s) => s.id === state.step);
    const next = flow[index + 1];
    nodes.next.textContent = next ? next.title : "The end";
    nodes.detail.textContent = next ? "The next round." : "";
  }
}

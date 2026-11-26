// Start screen: what will happen, the full-screen button, how to leave it, and the rounds to play
// (the whole route, or rounds chosen on the list: one with a click, several with "Choose several"
// and a check box on each; Shift + click, Ctrl + click and Space still add or remove a round).
import { el, formatSpan, formatTime, rounds } from "../format.js";

/** What a round asks for when it is played without another round (the server checks it when the ID is entered). */
const NEEDS = { recall: "words" };
const ALONE_NEEDS = {
  recall: "Delayed recall on its own needs the word lists from an earlier session of the same participant. This is checked when the participant ID is entered.",
};
const WITHOUT_NEEDS = {
  recall: (other) =>
    `Delayed recall without the ${other} round needs the word lists from an earlier session of the same ` +
    "participant. This is checked when the participant ID is entered.",
};
const HOW = {
  off: "Click a round to play it alone, or press Choose several to check more than one.",
  on: "Check each round to play. Click a checked round again to uncheck it.",
};

/** Titles in words: "A", "A and B", "A, B and C". */
function names(titles) {
  if (titles.length < 2) return titles.join("");
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}

let failures = null;

export function mount(root, api) {
  const flow = api.ctx.flow;
  const all = rounds(flow);
  const order = all.map((s) => s.id);
  const titleOf = (id) => all.find((s) => s.id === id).title;
  /** The ids in `ids`, once each, in route order. */
  const inOrder = (ids) => order.filter((id) => ids.includes(id));
  let rs = all;
  const total = (list) => [
    list.reduce((n, s) => n + s.blocks.length, 0),
    list.reduce((n, s) => n + s.blocks.reduce((m, b) => m + b.seconds, 0), 0),
  ];
  let [blocks, seconds] = total(all);

  const wrap = el("div", { class: "start" });

  const main = el("div", { class: "start-main" });
  const heading = el("h1", { id: "start-title" });
  const lede = el("p", { class: "lede", id: "start-lede" });
  main.append(el("div", { class: "brand" }, "Brain Mapper"), heading, lede);
  // The rounds marked on the list, with their total, next to what the heading says will happen.
  const pick = el("div", { class: "pick", id: "pick-panel" });
  pick.hidden = true;
  const pickText = el("p", { id: "pick-text", role: "status" });
  const pickNote = el("p", { class: "pick-note", id: "pick-note" });
  const playOnly = el("button", { class: "btn btn--quiet", id: "play-only", type: "button" }, "Play this round only");
  const playAll = el("button", { class: "btn btn--quiet", id: "play-all", type: "button" }, "Play the whole route");
  pick.append(pickText, pickNote, playOnly, playAll);
  main.append(pick);

  const actions = el("div", { class: "start-actions" });
  const row = el("div", { class: "row" });
  const enter = el("button", { class: "btn", id: "fs-enter", type: "button" }, "Start full screen");
  const onlooker = el(
    "button",
    { class: "btn btn--quiet", id: "open-onlooker", type: "button" },
    "Open the onlooker window",
  );
  row.append(enter, onlooker);
  const problem = el("p", { class: "problem", id: "fs-problem" });
  problem.hidden = true;
  // The start-up checks that failed (the server sends all of them; passing ones are not shown).
  failures = el("div", { class: "checks-failed", id: "checks-failed", role: "alert" });
  failures.hidden = true;
  actions.append(
    row,
    failures,
    problem,
    el(
      "p",
      { class: "hint" },
      "Press Esc at any time to leave full screen. Leaving full screen pauses the session; " +
        "a block that was running ends there, and the next block starts when you go on.",
    ),
    el(
      "p",
      { class: "hint" },
      "The onlooker window shows the live display to someone watching. " +
        "It cannot control the session.",
    ),
    el(
      "p",
      { class: "credit" },
      "Head: \u201cInfinite, 3D Head Scan\u201d by Lee Perry-Smith, CC BY 3.0 (stand-in). " +
        "Brain: FreeSurfer fsaverage. 3-D: three.js, MIT.",
    ),
  );
  main.append(actions);

  const route = el("div", { class: "route", "data-several": "false" });
  const buttons = new Map();
  // The rounds marked on the list (in route order), the round clicked last (where a Shift + click
  // range starts) and whether a plain click or tap adds and removes rounds ("Choose several").
  let picked = api.ctx.selection ? [...api.ctx.selection.only] : [];
  let anchor = null;
  let several = false;

  const head = el("div", { class: "route-head" });
  const severalButton = el(
    "button",
    { class: "btn btn--ghost pick-several", id: "pick-several", type: "button", "aria-pressed": "false" },
    "Choose several",
  );
  head.append(el("h2", { id: "route-title" }, "The route"), severalButton);
  const how = el("p", { class: "pick-how", id: "pick-how" }, HOW.off);
  route.append(head, how);
  const group = el("div", {
    class: "route-list",
    role: "group",
    "aria-labelledby": "route-title",
    "aria-describedby": "pick-how",
  });
  const list = el("ol");
  // Setup comes first and is listed apart, so the rounds are numbered 1 to n as the heading counts them.
  const setup = flow.filter((s) => s.kind === "setup");
  const setupRow = el("div", { class: "route-setup", id: "route-setup" });
  setupRow.append(el("div", { class: "what" }, "First: setup"), el("div", { class: "len" }, `${setup.length} steps`));
  setupRow.hidden = !setup.length;
  const items = [];
  for (const step of rs) {
    const total = step.blocks.reduce((n, b) => n + b.seconds, 0);
    const length =
      step.blocks.length > 1 ? `${step.blocks.length} blocks, ${formatTime(total)}` : formatTime(total);
    items.push([step.title, length, step.id]);
  }
  items.forEach(([title, length, id], i) => {
    const li = el("li");
    const what = el("div", { class: "what" });
    if (id) {
      // A button that covers the whole row, so the round can be chosen from anywhere on it.
      const choose = el("button", { class: "route-pick", type: "button", "data-step": id, "aria-pressed": "false" }, title);
      what.append(choose);
      buttons.set(id, choose);
      choose.addEventListener("click", (event) => onPick(id, event));
      // Space adds or removes the round in focus; Enter is a plain click (that round alone).
      choose.addEventListener("keydown", (event) => {
        if (event.key !== " ") return;
        event.preventDefault();
        if (!event.repeat) toggle(id);
      });
      choose.addEventListener("keyup", (event) => {
        if (event.key === " ") event.preventDefault();
      });
    } else {
      what.textContent = title;
    }
    li.append(el("div", { class: "num" }, String(i + 1)), what, el("div", { class: "len" }, length));
    list.append(li);
  });
  group.append(setupRow, list);
  route.append(
    group,
    el("p", { class: "note" }, "The points go up after each answer."),
  );

  /** Marks `next` on the list. A choice already made for the session is dropped if it differs. */
  function mark(next, clicked) {
    picked = inOrder(next);
    anchor = clicked;
    const chosen = api.ctx.selection ? api.ctx.selection.only : null;
    if (chosen && chosen.join() !== picked.join()) api.ctx.selection = null;
    draw();
  }

  function toggle(id) {
    mark(picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id], id);
  }

  function onPick(id, event) {
    if (event.shiftKey && anchor !== null && anchor !== id && Math.abs(order.indexOf(anchor) - order.indexOf(id)) > 1) {
      // Across a gap: every round from the one clicked last to this one.
      const [a, b] = [order.indexOf(anchor), order.indexOf(id)].sort((x, y) => x - y);
      mark([...picked, ...order.slice(a, b + 1)], id);
    } else if (event.shiftKey || event.ctrlKey || event.metaKey || several) {
      toggle(id);
    } else {
      mark(picked.length === 1 && picked[0] === id ? [] : [id], id);
    }
  }

  function draw() {
    const chosen = api.ctx.selection ? api.ctx.selection.only : null;
    const shown = chosen || picked;
    for (const [id, button] of buttons) {
      button.setAttribute("aria-pressed", String(shown.includes(id)));
      button.closest("li").dataset.chosen = String(!!chosen && chosen.includes(id));
    }
    rs = chosen ? all.filter((s) => chosen.includes(s.id)) : all;
    [blocks, seconds] = total(rs);
    const noun = blocks === 1 ? "block" : "blocks";
    if (chosen && chosen.length === 1) heading.textContent = `Play ${titleOf(chosen[0])} with the headset on.`;
    else if (chosen) heading.textContent = `Play ${chosen.length} chosen rounds with the headset on.`;
    else heading.textContent = `Play ${all.length} rounds with the headset on.`;
    lede.textContent =
      `${blocks} timed ${noun}, ${formatSpan(seconds)}${chosen && chosen.length === 1 ? "" : " in all"}, after the setup. ` +
      "Your recording stays on this computer.";
    pick.hidden = shown.length === 0;
    if (!shown.length) return;
    const titles = names(shown.map(titleOf));
    const [n, s] = total(all.filter((step) => shown.includes(step.id)));
    const span = `${n} ${n === 1 ? "block" : "blocks"}, ${formatSpan(s)}`;
    if (chosen) {
      pickText.textContent =
        shown.length === 1
          ? `Only ${titles} will be played, after the setup and before the summary.`
          : `Only ${titles} will be played, in route order, after the setup and before the summary.`;
    } else {
      pickText.textContent =
        shown.length === 1 ? `${titles}: ${span}.` : `${shown.length} rounds chosen: ${titles}. ${span} in all.`;
    }
    // A round that needs another (recall needs words) chosen without it: what that asks for.
    const lacking = shown.find((id) => NEEDS[id] && !shown.slice(0, shown.indexOf(id)).includes(NEEDS[id]));
    let note = "";
    if (lacking && shown.length === 1) note = ALONE_NEEDS[lacking] || "";
    else if (lacking && WITHOUT_NEEDS[lacking]) note = WITHOUT_NEEDS[lacking](titleOf(NEEDS[lacking]));
    pickNote.textContent = note;
    pickNote.hidden = !note;
    playOnly.textContent = shown.length === 1 ? "Play this round only" : `Play these ${shown.length} rounds only`;
    playOnly.hidden = !!chosen;
    playAll.hidden = !chosen;
  }
  severalButton.addEventListener("click", () => {
    several = !several;
    severalButton.setAttribute("aria-pressed", String(several));
    route.dataset.several = String(several);
    how.textContent = several ? HOW.on : HOW.off;
  });
  playOnly.addEventListener("click", () => {
    api.ctx.selection = { only: [...picked] };
    draw();
  });
  playAll.addEventListener("click", () => {
    api.ctx.selection = null;
    picked = [];
    anchor = null;
    draw();
  });
  draw();

  wrap.append(main, route);
  root.replaceChildren(wrap);

  enter.addEventListener("click", async () => {
    try {
      await api.fullscreen.enter();
    } catch (error) {
      problem.textContent = `Full screen could not be started (${error.message}). Use a browser that allows full screen and try again.`;
      problem.hidden = false;
      return;
    }
    api.start();
  });
  onlooker.addEventListener("click", () => {
    window.open("/onlooker", "brain-mapper-onlooker");
  });
}

export function update(_state, api) {
  const failed = (api.ctx.checks || []).filter((item) => !item.ok);
  const key = JSON.stringify(failed);
  if (failures.dataset.shown === key) return;
  failures.dataset.shown = key;
  failures.hidden = failed.length === 0;
  const parts = [];
  if (failed.length) {
    parts.push(el("h2", {}, failed.length === 1 ? "A start-up check failed" : "Start-up checks failed"));
  }
  for (const item of failed) {
    const entry = el("div", { class: "check", "data-check": item.id });
    entry.append(el("p", { class: "check-text" }, item.text), el("p", { class: "check-fix" }, `Fix: ${item.fix}`));
    parts.push(entry);
  }
  failures.replaceChildren(...parts);
}

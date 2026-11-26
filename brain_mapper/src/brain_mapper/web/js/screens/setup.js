// The three setup steps (participant, device, calibration): a title and Continue.
// Later tasks replace the body of each; the ids and the button stay.
import { el } from "../format.js";
import { leaveControls } from "./leave.js";

let leave = null;

export function mount(root, api) {
  const wrap = el("div", { class: "plain" });
  const title = el("h1", { id: "setup-title" });
  const text = el("p", {}, "This step is not built yet. Continue goes on to the next one.");
  leave = leaveControls(api);
  wrap.append(title, text, api.advanceButton("Continue"), leave.root);
  root.replaceChildren(wrap);
}

export function update(state, api) {
  document.getElementById("setup-title").textContent = api.stepTitle(state.step);
  leave.update(state);
}

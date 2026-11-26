// Shown until the session state and the flow have arrived.
import { el } from "../format.js";

export function mount(root) {
  const wrap = el("div", { class: "plain" });
  wrap.append(el("h1", {}, "Connecting"), el("p", { id: "connecting-text" }, "Waiting for the headset and the session."));
  root.replaceChildren(wrap);
}

export function update() {}

// A second participant window: refused, with a way to watch instead.
import { el } from "../format.js";

export function mount(root) {
  const wrap = el("div", { class: "ended" });
  const link = el("a", { class: "btn btn--light", id: "onlooker-link", href: "/onlooker" }, "Open the onlooker view");
  link.style.alignSelf = "flex-start";
  wrap.append(
    el("h1", {}, "The session is open in another window"),
    el("p", {}, "Only one window can run the session. To watch it from here, use the onlooker view."),
    link,
  );
  root.replaceChildren(wrap);
}

export function update() {}

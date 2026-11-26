// The contact check view: the 3-D fit view and the panel beside it (how many contacts are good,
// the legend, a pill for each contact that is not, and what is left out). Used by the calibration
// step and by the headset check of the paused screen, so both show the same view and states.
//
// `draw(state, api)` draws the `contact` message that belongs to the check that is on now
// (`state.contact_check`) and returns whether the contacts have been measured.
import { el } from "../format.js";
import { STATE_COLORS } from "../scene3d/headset.js";
import { toneNote } from "./signal-check.js";

/** The words on screen for the three states of the data. */
export const WORDS = { good: "Good", marginal: "Weak", bad: "No contact" };

function swatch(state) {
  const dot = el("span", { class: "swatch", "aria-hidden": "true" });
  dot.style.background = STATE_COLORS[state];
  return dot;
}

/**
 * `ids`: the element ids (`root`, `stage`, `count`, `pills`, `excluded`, `off`, `noTone`); `title`, `note`;
 * `offText` (shown while the check is off) and `excludedText(bad)` (the line about bad contacts).
 */
export function contactPanel({ ids, title, note, offText, excludedText }) {
  const root = el("div", { class: "calib-contact", id: ids.root });
  const stage = el("div", { class: "calib-view scene3d", id: ids.stage });
  const panel = el("div", { class: "calib-panel" });
  const count = el("p", { class: "contact-count", id: ids.count, role: "status" });
  const legend = el("ul", { class: "calib-legend", "aria-label": "What the colors mean" });
  for (const state of ["good", "marginal", "bad"]) {
    const item = el("li");
    item.append(swatch(state), document.createTextNode(WORDS[state]));
    legend.append(item);
  }
  const pills = el("div", { class: "contact-pills", id: ids.pills });
  const excluded = el("p", { class: "calib-excluded", id: ids.excluded });
  const off = el("p", { class: "calib-off", id: ids.off });
  off.hidden = true;
  // Said when the check is on but no impedance tone is seen in the data (signal-check.js).
  const noTone = el("p", { class: "calib-off", id: ids.noTone, role: "alert" });
  noTone.hidden = true;
  const heading = el("h1", {}, title);
  panel.append(heading, count, ...(note ? [el("p", { class: "calib-note" }, note)] : []), legend, pills, excluded, off, noTone);
  root.append(stage, panel);

  let view = null;
  let loading = false;
  let lastDrawn = "";

  async function load() {
    if (view || loading) return;
    loading = true;
    try {
      const [{ createFitView }, response] = await Promise.all([
        import("../scene3d/fit-view.js"),
        fetch("/static/assets/meshes.json"),
      ]);
      if (!response.ok) throw new Error(`the server answered ${response.status}`);
      const meshes = await response.json();
      // Only if the check is still shown: a load that ends after the participant moved on must
      // not leave a fit view (and its WebGL context) behind.
      if (root.isConnected && !root.hidden && !root.closest("[hidden]") && !view) {
        view = createFitView(stage, meshes);
        lastDrawn = "";
      }
    } catch (error) {
      stage.append(el("p", { class: "scene-message", role: "alert" }, `The head view could not be loaded (${error.message}).`));
    } finally {
      loading = false;
    }
  }

  function dispose() {
    if (view) view.dispose();
    view = null;
  }

  function draw(state, api, host) {
    const checking = state.contact_check !== null && state.contact_check !== undefined;
    const message = api.ctx.contact;
    // Only states measured for the check that is on now count.
    const mine = checking && message && message.check === state.contact_check ? message : null;
    const states = mine && mine.states ? mine.states : null;
    noTone.textContent = toneNote(mine);
    noTone.hidden = !noTone.textContent;
    if (host) host.dataset.check = checking ? "on" : "off";
    off.hidden = checking && !(mine && mine.error);
    off.textContent = !checking ? offText : mine && mine.error ? `The contacts cannot be measured: ${mine.error}.` : "";
    if (!states) {
      delete count.dataset.good;
      count.textContent = checking ? "Measuring the contacts…" : "Not measuring.";
      pills.replaceChildren();
      excluded.textContent = "";
      return false;
    }
    const names = Object.keys(states);
    const good = names.filter((n) => states[n] === "good").length;
    count.dataset.good = String(good);
    count.textContent = `${good} of ${names.length} contacts are good`;
    const key = JSON.stringify(states);
    if (view && !view.unavailable && key !== lastDrawn) {
      view.setStates(states);
      lastDrawn = key;
    }
    if (pills.dataset.key !== key) {
      pills.dataset.key = key;
      pills.replaceChildren(
        ...names
          .filter((n) => states[n] !== "good")
          .map((n) => {
            const pill = el("button", { class: "contact-pill", type: "button", "data-contact": n, "data-state": states[n] });
            pill.append(swatch(states[n]), document.createTextNode(`${n} · ${WORDS[states[n]]}`));
            pill.addEventListener("click", () => view && !view.unavailable && view.focus(n));
            return pill;
          }),
      );
    }
    const bad = names.filter((n) => states[n] === "bad");
    excluded.textContent = bad.length ? excludedText(bad) : "";
    return true;
  }

  /** The view was painted by someone else (the signal check): the next draw paints it again. */
  function repaint() {
    lastDrawn = "";
  }

  return {
    root,
    stage,
    panel,
    load,
    dispose,
    draw,
    repaint,
    get view() {
      return view;
    },
    get loading() {
      return loading;
    },
  };
}

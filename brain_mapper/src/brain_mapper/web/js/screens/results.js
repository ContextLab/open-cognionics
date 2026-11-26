// The results of a session, computed from its own recording after the zip is saved
// (brain_mapper/analysis). Two parts:
//
// resultsEntry(record, api) -> the block the session record shows in place of a line about
//   results: a progress bar with the time left while the computation runs, then "See your
//   results". It polls `record.results` (GET /api/results?key=..., participant page only).
// openResults(record, api) -> the results screen over the page, four steps in one path:
//   Points (the scores already on the summary, not recomputed), Rhythms (power spectra and the
//   share of each band), Eyes closed and open (the alpha ratio per electrode) and Networks (the
//   3-D view of this session's connectivity, per band and round, and over time).
//
// Every figure carries one sentence saying what was computed; a quantity that could not be
// computed for this session says so instead of drawing. Nothing here grades the person.
import { el } from "../format.js";
import { scoreRows } from "../scores.js";

const SVG = "http://www.w3.org/2000/svg";
const BAND_ORDER = ["delta", "theta", "alpha", "beta", "gamma"];
const BAND_LABEL = { delta: "Delta 1–4 Hz", theta: "Theta 4–8 Hz", alpha: "Alpha 8–13 Hz", beta: "Beta 13–30 Hz", gamma: "Low gamma 30–45 Hz" };
// Categorical slots 1-5, stepped for the dark ground (checked with the dataviz validator against #121826).
const BAND_COLOR = { delta: "#3987e5", theta: "#d95926", alpha: "#199e70", beta: "#c98500", gamma: "#d55181" };
const HIGHLIGHT = "#3987e5";
const MUTED = "#5d6b8f";
const STEPS = [
  { id: "points", title: "Points" },
  { id: "rhythms", title: "Rhythms" },
  { id: "eyes", title: "Eyes closed and open" },
  { id: "networks", title: "Networks" },
];
// Electrodes are drawn this far from the head's core toward their place on the scalp.
const INSET = 0.8;
const CORE = [0, -18, 22];
const NOT_ENOUGH = "Not enough clean recording in this round to estimate links.";
const NO_GROUPS = "The links in this round and band do not split into groups more than shuffled links do.";
const NO_LINKS = "No link in this round and band is stronger than in time-shifted copies of the same recording.";

const entries = new Map();

function svg(tag, attrs = {}, text = "") {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text) node.textContent = text;
  return node;
}

function minutesText(seconds) {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
  return `${Math.round(seconds / 60)} min`;
}

/** The progress bar and the button, one element per session (kept across redraws of the record). */
export function resultsEntry(record, api) {
  if (!record || !record.summary || !(record.summary.recordings || []).length) return null;
  const url = record.results;
  if (!url) {
    if (record.state !== "saved") return null;
    return el("p", { id: "results-entry", class: "note" }, "Results are computed on the participant's page.");
  }
  if (entries.has(url)) return entries.get(url).node;
  const node = el("div", { id: "results-entry", class: "results-entry", "data-state": "running" });
  const title = el("h2", {}, "Your results");
  const bar = el("div", { id: "results-progress", class: "results-bar", role: "progressbar", "aria-label": "Computing the results", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" });
  const fill = el("span");
  bar.append(fill);
  const line = el("p", { id: "results-status", class: "results-status", "aria-live": "polite" }, "Computing the results from your recording.");
  const open = el("button", { id: "results-open", class: "btn", type: "button", disabled: "" }, "See your results");
  node.append(title, bar, line, open);
  const entry = { node, data: null };
  entries.set(url, entry);
  open.addEventListener("click", () => {
    if (entry.data) openResults(record, api, entry.data, url);
  });
  const poll = async () => {
    let status;
    try {
      const response = await fetch(url, { cache: "no-store" });
      status = await response.json();
      if (!response.ok) throw new Error(status.error || "refused");
    } catch {
      line.textContent = "Waiting for brain-mapper to answer.";
      setTimeout(poll, 2000);
      return;
    }
    const percent = Math.round((status.progress || 0) * 100);
    fill.style.width = `${percent}%`;
    bar.setAttribute("aria-valuenow", String(percent));
    node.dataset.state = status.state;
    if (status.state === "done" && status.results) {
      entry.data = status.results;
      bar.hidden = true;
      line.textContent = "Computed from this session's recording.";
      open.disabled = false;
      return;
    }
    if (status.state === "failed") {
      bar.hidden = true;
      line.setAttribute("role", "alert");
      line.textContent = status.message || "The results could not be computed.";
      open.hidden = true;
      return;
    }
    if (status.state === "running") {
      const p = status.progress || 0;
      const left = p > 0.05 && status.elapsed ? (status.elapsed / p) * (1 - p) : null;
      const stage = status.stage ? ` ${status.stage}.` : "";
      line.textContent =
        left === null
          ? `Computing the results: ${percent} %.${stage}`
          : `Computing the results: ${percent} %, about ${minutesText(left)} left (estimated from the time so far).${stage}`;
    } else {
      line.textContent = "The results are computed once the session zip is saved.";
    }
    setTimeout(poll, 700);
  };
  poll();
  return node;
}

// -- the results screen ---------------------------------------------------------------------

let view = null;

function close() {
  if (!view) return;
  if (view.net) view.net.dispose();
  clearInterval(view.timer);
  view.observer.disconnect();
  view.root.remove();
  document.documentElement.classList.remove("results-open");
  document.removeEventListener("keydown", view.onKey);
  const back = document.getElementById("results-open");
  view = null;
  if (back) back.focus();
}

export function openResults(record, api, results, url) {
  close();
  const root = el("section", { id: "results", class: "results", role: "dialog", "aria-modal": "true", "aria-labelledby": "results-title" });
  const head = el("header", { class: "results-head" });
  const tabs = el("nav", { class: "results-tabs", "aria-label": "Parts of the results" });
  const closeButton = el("button", { id: "results-close", class: "results-x", type: "button" }, "Back to the summary");
  head.append(el("h1", { id: "results-title", tabindex: "-1" }, "Your results"), tabs, closeButton);
  const page = el("div", { id: "results-page", class: "results-page" });
  const foot = el("footer", { class: "results-foot" });
  const prev = el("button", { id: "results-prev", class: "btn btn--ghost", type: "button" }, "Back");
  const next = el("button", { id: "results-next", class: "btn", type: "button" }, "Next");
  foot.append(prev, next);
  root.append(head, page, foot);
  document.body.append(root);
  document.documentElement.classList.add("results-open");
  view = { root, page, results, record, api, url, step: 0, timer: null, net: null, windows: null, condition: null, band: "alpha" };
  view.observer = new ResizeObserver(() => draw());
  view.observer.observe(page);
  STEPS.forEach((step, i) => {
    const tab = el("button", { class: "results-tab", type: "button", "data-step": step.id }, `${i + 1}. ${step.title}`);
    tab.addEventListener("click", () => show(i));
    tabs.append(tab);
  });
  closeButton.addEventListener("click", close);
  prev.addEventListener("click", () => show(view.step - 1));
  next.addEventListener("click", () => (view.step === STEPS.length - 1 ? close() : show(view.step + 1)));
  view.onKey = (event) => {
    if (event.key === "Escape") close();
  };
  document.addEventListener("keydown", view.onKey);
  show(0);
  root.querySelector("#results-title").focus();
}

function show(i) {
  if (!view) return;
  view.step = Math.max(0, Math.min(STEPS.length - 1, i));
  clearInterval(view.timer);
  view.timer = null;
  if (view.net) {
    view.net.dispose();
    view.net = null;
  }
  view.root.dataset.step = STEPS[view.step].id;
  for (const tab of view.root.querySelectorAll(".results-tab")) {
    const on = tab.dataset.step === STEPS[view.step].id;
    tab.classList.toggle("on", on);
    if (on) tab.setAttribute("aria-current", "step");
    else tab.removeAttribute("aria-current");
  }
  const prev = view.root.querySelector("#results-prev");
  const next = view.root.querySelector("#results-next");
  prev.hidden = view.step === 0;
  next.textContent = view.step === STEPS.length - 1 ? "Back to the summary" : `Next: ${STEPS[view.step + 1].title}`;
  view.page.replaceChildren();
  const build = { points: buildPoints, rhythms: buildRhythms, eyes: buildEyes, networks: buildNetworks }[STEPS[view.step].id];
  build();
  draw();
}

function draw() {
  if (!view) return;
  for (const fig of view.page.querySelectorAll("[data-draw]")) {
    const fn = fig._draw;
    if (fn) fn();
  }
}

/** A figure: heading, the plain sentence of what was computed, and a drawing area. */
function figure(id, heading, sentence) {
  const fig = el("figure", { id, class: "results-fig" });
  const plot = el("div", { class: "plot", "data-draw": "" });
  fig.append(el("h2", {}, heading), el("figcaption", {}, sentence), plot);
  return { fig, plot };
}

function one(fig) {
  const wrap = el("div", { class: "results-one" });
  wrap.append(fig);
  return wrap;
}

function cannot(plot, text) {
  plot.replaceChildren(el("p", { class: "cannot", role: "status" }, text));
  plot.dataset.computed = "false";
}

function sized(plot) {
  const width = Math.max(200, plot.clientWidth);
  const height = Math.max(140, plot.clientHeight);
  const s = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img" });
  plot.replaceChildren(s);
  plot.dataset.computed = "true";
  return { s, width, height };
}

function fontSize() {
  return Math.round(Math.min(22, Math.max(13, window.innerWidth / 105)));
}

// -- 1. Points ------------------------------------------------------------------------------

function buildPoints() {
  const { record, api } = view;
  const scores = record.summary && record.summary.scores;
  const rows = scoreRows(api.planFlow(), api.ctx.state, scores).filter((r) => r.text !== "—" || r.reason);
  const { fig, plot } = figure("results-points", "Points in each round", "Points earned of points possible in each round: the same numbers as on the summary.");
  view.page.append(one(fig));
  const tasks = (scores && scores.tasks) || {};
  fig.classList.add("short");
  plot._draw = () => {
    if (!rows.length) return cannot(plot, "No round was scored in this session.");
    plot.style.height = `${Math.round(rows.length * fontSize() * 3.2 + 8)}px`;
    const { s, width, height } = sized(plot);
    s.setAttribute("aria-label", rows.map((r) => `${r.title}: ${r.reason || r.text}`).join("; "));
    const fs = fontSize();
    const labelW = Math.min(fs * 14, width * 0.28);
    const valueW = fs * 9;
    const rowH = Math.min(height / rows.length, fs * 3.2);
    const barW = width - labelW - valueW - 16;
    rows.forEach((row, i) => {
      const step = api.planFlow().find((x) => x.id === row.id);
      const entry = tasks[(step && (step.module || step.id)) || ""];
      const y = i * rowH + rowH / 2;
      s.append(svg("text", { x: labelW - 12, y: y + fs * 0.35, "text-anchor": "end", class: "lbl", "font-size": fs }, row.title));
      s.append(svg("rect", { x: labelW, y: y - rowH * 0.22, width: barW, height: rowH * 0.44, rx: 4, class: "track" }));
      if (entry && entry.possible > 0) {
        const w = (barW * entry.earned) / entry.possible;
        s.append(svg("rect", { x: labelW, y: y - rowH * 0.22, width: Math.max(w, 0), height: rowH * 0.44, rx: 4, fill: HIGHLIGHT, "data-earned": entry.earned, "data-possible": entry.possible, "data-round": row.id, class: "bar" }));
      }
      s.append(svg("text", { x: labelW + barW + 12, y: y + fs * 0.35, class: "val", "font-size": fs }, row.reason ? row.reason : row.text));
    });
  };
}

// -- 2. Rhythms -----------------------------------------------------------------------------

function conditions() {
  return view.results.conditions || [];
}

function buildRhythms() {
  const list = conditions();
  const usable = list.filter((c) => c.spectrum);
  if (!view.condition || !usable.find((c) => c.id === view.condition)) {
    const preferred = usable.find((c) => c.id === "rest_closed") || usable[0];
    view.condition = preferred ? preferred.id : null;
  }
  const wrap = el("div", { class: "results-two" });
  const spec = figure(
    "results-spectrum",
    "Power spectrum",
    "Power at each frequency (Welch's method, 2 s segments), averaged over the good electrodes, for each round; the chosen round is drawn in blue, the others in gray.",
  );
  const chips = el("div", { class: "chips", role: "group", "aria-label": "Round shown in blue" });
  for (const c of usable) {
    const b = el("button", { type: "button", class: "chip", "data-condition": c.id, "aria-pressed": String(c.id === view.condition) }, c.title);
    b.addEventListener("click", () => {
      view.condition = c.id;
      for (const x of chips.children) x.setAttribute("aria-pressed", String(x.dataset.condition === c.id));
      draw();
    });
    chips.append(b);
  }
  spec.fig.insertBefore(chips, spec.plot);
  const share = figure(
    "results-bands",
    "Share of each band",
    "The share of the 1–45 Hz power in each frequency band, averaged over the good electrodes, for each round.",
  );
  const legend = el("ul", { class: "legend" });
  for (const b of BAND_ORDER) {
    const item = el("li");
    item.append(el("span", { class: "swatch", style: `background:${BAND_COLOR[b]}` }), BAND_LABEL[b]);
    legend.append(item);
  }
  share.fig.insertBefore(legend, share.plot);
  wrap.append(spec.fig, share.fig);
  view.page.append(wrap);
  spec.plot._draw = () => drawSpectrum(spec.plot, usable);
  share.plot._draw = () => drawShares(share.plot, list);
}

function drawSpectrum(plot, usable) {
  const freqs = view.results.freqs || [];
  if (!usable.length || !freqs.length) return cannot(plot, "No round had enough clean data for a spectrum.");
  const { s, width, height } = sized(plot);
  const fs = fontSize();
  const m = { l: fs * 4, r: 12, t: fs * 2, b: fs * 2.6 };
  const all = usable.flatMap((c) => c.spectrum.filter((v) => v > 0));
  const lo = Math.floor(Math.log10(Math.min(...all)));
  const hi = Math.ceil(Math.log10(Math.max(...all)));
  const x = (f) => m.l + ((f - freqs[0]) / (freqs[freqs.length - 1] - freqs[0])) * (width - m.l - m.r);
  const y = (v) => m.t + (1 - (Math.log10(Math.max(v, 10 ** lo)) - lo) / Math.max(hi - lo, 1)) * (height - m.t - m.b);
  for (let e = lo; e <= hi; e++) {
    s.append(svg("line", { x1: m.l, x2: width - m.r, y1: y(10 ** e), y2: y(10 ** e), class: "grid" }));
    s.append(svg("text", { x: m.l - 6, y: y(10 ** e) + fs * 0.35, "text-anchor": "end", class: "tick", "font-size": fs * 0.85 }, e === 0 ? "1" : `10${sup(e)}`));
  }
  for (const f of [1, 10, 20, 30, 45]) {
    s.append(svg("text", { x: x(f), y: height - m.b + fs * 1.2, "text-anchor": "middle", class: "tick", "font-size": fs * 0.85 }, String(f)));
  }
  s.append(svg("text", { x: (m.l + width) / 2, y: height - 4, "text-anchor": "middle", class: "axis", "font-size": fs * 0.85 }, "Frequency (Hz)"));
  s.append(svg("text", { x: 4, y: fs, class: "axis", "font-size": fs * 0.85 }, "Power (µV²/Hz)"));
  const line = (c, cls, color) => {
    const d = c.spectrum.map((v, i) => `${i ? "L" : "M"}${x(freqs[i]).toFixed(1)},${y(v).toFixed(1)}`).join("");
    s.append(svg("path", { d, class: cls, stroke: color, fill: "none", "data-condition": c.id }));
  };
  for (const c of usable) if (c.id !== view.condition) line(c, "spec other", MUTED);
  const chosen = usable.find((c) => c.id === view.condition);
  if (chosen) line(chosen, "spec chosen", HIGHLIGHT);
  s.setAttribute("aria-label", `Power spectra of ${usable.length} rounds; ${chosen ? chosen.title : ""} highlighted`);
}

function sup(n) {
  const map = { "-": "⁻", 0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹" };
  return String(n)
    .split("")
    .map((ch) => map[ch])
    .join("");
}

function drawShares(plot, list) {
  if (!list.length) return cannot(plot, "No round was analyzed.");
  const { s, width, height } = sized(plot);
  const fs = fontSize();
  const labelW = Math.min(240, width * 0.32);
  const rowH = Math.min(height / list.length, fs * 2.6);
  const barW = width - labelW - 8;
  list.forEach((c, i) => {
    const y = i * rowH + rowH * 0.2;
    const h = rowH * 0.6;
    s.append(svg("text", { x: labelW - 10, y: y + h / 2 + fs * 0.35, "text-anchor": "end", class: "lbl", "font-size": fs }, c.title));
    if (!c.relative) {
      s.append(svg("text", { x: labelW, y: y + h / 2 + fs * 0.35, class: "val muted", "font-size": fs * 0.9, "data-condition": c.id }, "too little clean data to compute"));
      return;
    }
    let at = labelW;
    for (const b of BAND_ORDER) {
      const w = c.relative[b] * barW;
      const r = svg("rect", { x: at, y, width: Math.max(w - 2, 0), height: h, fill: BAND_COLOR[b], "data-condition": c.id, "data-band": b, "data-share": c.relative[b], class: "share" });
      r.append(svg("title", {}, `${c.title}, ${BAND_LABEL[b]}: ${(c.relative[b] * 100).toFixed(0)} %`));
      s.append(r);
      if (w > fs * 3) s.append(svg("text", { x: at + w / 2 - 1, y: y + h / 2 + fs * 0.33, "text-anchor": "middle", class: "inbar", "font-size": fs * 0.8 }, `${Math.round(c.relative[b] * 100)}%`));
      at += w;
    }
  });
}

// -- 3. Eyes closed and open ------------------------------------------------------------------

function buildEyes() {
  const a = view.results.alpha_ratio || {};
  const labels = view.results.recording.labels;
  const { fig, plot } = figure(
    "results-eyes",
    "Alpha with eyes closed and with eyes open",
    "Each bar: alpha power (8–13 Hz) with the eyes closed divided by alpha power with the eyes open, at one electrode. Alpha power at the back of the head is known to rise when the eyes close; this looks for that rise in the recording.",
  );
  const head = el("p", { id: "results-eyes-headline", class: "headline" });
  if (a.error) {
    head.textContent = "";
  } else if (a.posterior_median != null) {
    const n = a.posterior_median;
    head.textContent = `At the back of the head (${a.posterior_channels.join(", ")}) alpha power with the eyes closed was about ${n.toFixed(1)} times that with the eyes open (median of those electrodes), from ${a.closed_kept_segments} clean 2 s segments with the eyes closed and ${a.open_kept_segments} with them open.`;
  }
  const note = el("p", { class: "small" });
  if (!a.error) {
    const fromRest = a.source === "rest rounds";
    const few = Math.min(a.closed_kept_segments, a.open_kept_segments) < (view.results.parameters.min_segments_connectivity || 30);
    note.textContent =
      `From the ${fromRest ? "two rest rounds" : "eyes-open and eyes-closed parts of calibration"} (segments overlap by 1 s).` +
      (few
        ? ` ${fromRest ? "The rest rounds were" : "Those parts were"} short, so this number rests on few segments and could come out quite differently in another session.`
        : "");
    note.id = "results-eyes-note";
  }
  fig.insertBefore(head, plot);
  fig.append(note);
  plot.classList.add("plot--bars");
  view.page.append(one(fig));
  plot._draw = () => {
    if (a.error) return cannot(plot, a.error);
    const ratio = a.per_channel;
    const shown = labels.map((name, i) => ({ name, v: ratio[i] })).filter((d) => d.v != null && d.v > 0);
    if (!shown.length) return cannot(plot, "The ratio could not be computed at any electrode.");
    const { s, width, height } = sized(plot);
    const fs = fontSize();
    const m = { l: fs * 3.5, r: 8, t: 10, b: fs * 2 };
    const values = shown.map((d) => Math.log10(d.v));
    const lo = Math.min(-0.5, Math.floor(Math.min(...values) * 2) / 2);
    const hi = Math.max(0.5, Math.ceil(Math.max(...values) * 2) / 2);
    const y = (lv) => m.t + (1 - (lv - lo) / (hi - lo)) * (height - m.t - m.b);
    const step = (width - m.l - m.r) / shown.length;
    for (const t of [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]) {
      const e = Math.log10(t);
      if (e < lo - 1e-9 || e > hi + 1e-9) continue;
      s.append(svg("line", { x1: m.l, x2: width - m.r, y1: y(e), y2: y(e), class: t === 1 ? "one" : "grid" }));
      s.append(svg("text", { x: m.l - 6, y: y(e) + fs * 0.35, "text-anchor": "end", class: "tick", "font-size": fs * 0.85 }, `×${t}`));
    }
    const posterior = new Set(a.posterior_channels);
    shown.forEach((d, i) => {
      const lv = Math.log10(d.v);
      const top = Math.min(y(lv), y(0));
      const bar = svg("rect", { x: m.l + i * step + step * 0.18, y: top, width: step * 0.64, height: Math.max(1, Math.abs(y(lv) - y(0))), rx: 3, fill: posterior.has(d.name) ? HIGHLIGHT : MUTED, "data-channel": d.name, "data-ratio": d.v, class: "ratio" });
      bar.append(svg("title", {}, `${d.name}: ×${d.v.toFixed(2)}`));
      s.append(bar);
      s.append(svg("text", { x: m.l + i * step + step / 2, y: height - m.b + fs * 1.1, "text-anchor": "middle", class: "tick", "font-size": fs * 0.75 }, d.name));
    });
    s.setAttribute("aria-label", shown.map((d) => `${d.name} ${d.v.toFixed(2)}`).join(", "));
  };
}

// -- 4. Networks ----------------------------------------------------------------------------

function nodePositions(meshes, labels) {
  const byName = Object.fromEntries(meshes.electrodes.map((e) => [e.name, e.p]));
  return labels.map((name) => {
    const p = byName[name] || [0, 0, 0];
    return p.map((v, k) => CORE[k] + (v - CORE[k]) * INSET);
  });
}

/** Edges for the view: the links between good electrodes above the threshold from time-shifted
 *  copies of the same recording (see brain_mapper/analysis/connectivity.py). */
function edgesOf(values, good, threshold) {
  const pairs = [];
  if (threshold != null) for (const [i, j, v] of values) if (good[i] && good[j] && v != null && v > threshold) pairs.push([i, j, v]);
  const possible = (good.filter(Boolean).length * (good.filter(Boolean).length - 1)) / 2;
  pairs.sort((a, b) => b[2] - a[2]);
  const max = pairs.length ? pairs[0][2] : 1;
  return { edges: pairs.map(([i, j, v]) => [i, j, 0.42 + 0.58 * (v / max)]), shown: pairs.length, possible };
}

function matrixValues(matrix) {
  const out = [];
  for (let i = 0; i < matrix.length; i++) for (let j = i + 1; j < matrix.length; j++) out.push([i, j, matrix[i][j]]);
  return out;
}

function upperValues(upper, n) {
  const out = [];
  let k = 0;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) out.push([i, j, upper[k++]]);
  return out;
}

function clock(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function buildNetworks() {
  const results = view.results;
  const labels = results.recording.labels;
  const good = results.channels.good;
  const usable = conditions().filter((c) => "connectivity" in c);
  // the round chosen on Rhythms stays chosen if it has links estimated; otherwise the first that does
  const current = usable.find((c) => c.id === view.condition);
  if (!current || !current.connectivity) view.condition = (usable.find((c) => c.connectivity) || current || usable[0] || {}).id ?? null;
  const wrap = el("div", { class: "results-net" });
  const stage = el("div", { id: "results-stage", class: "results-stage scene3d" });
  const side = el("div", { class: "results-side" });
  const bandChips = el("div", { class: "chips", role: "group", "aria-label": "Frequency band" });
  for (const b of BAND_ORDER) {
    const chip = el("button", { type: "button", class: "chip", "data-band": b, "aria-pressed": String(b === view.band) }, BAND_LABEL[b]);
    chip.addEventListener("click", () => {
      view.band = b;
      for (const x of bandChips.children) x.setAttribute("aria-pressed", String(x.dataset.band === b));
      stop();
      render();
    });
    bandChips.append(chip);
  }
  const condChips = el("div", { class: "chips", role: "group", "aria-label": "Round" });
  for (const c of usable) {
    const chip = el("button", { type: "button", class: "chip", "data-condition": c.id, "aria-pressed": String(c.id === view.condition) }, c.title);
    chip.addEventListener("click", () => {
      view.condition = c.id;
      view.frame = null;
      for (const x of condChips.children) x.setAttribute("aria-pressed", String(x.dataset.condition === c.id));
      stop();
      render();
    });
    condChips.append(chip);
  }
  const play = el("button", { id: "results-play", class: "btn", type: "button" }, "Play over time");
  const slider = el("input", { id: "results-time", type: "range", min: "0", max: "0", value: "0", "aria-label": "Time in the session" });
  const now = el("p", { id: "results-now", class: "now", "aria-live": "off" });
  const info = el("p", { id: "results-net-info", class: "small" });
  const caption = el(
    "p",
    { class: "caption" },
    "Dots: the electrodes, drawn just inside the head (bad ones left out). Lines: links by the debiased weighted phase-lag index, " +
      "how consistently one electrode's signal leads or lags another's in that band, drawn only where stronger than in 95 % of time-shifted copies of the same recording. " +
      "Colors: groups found by the Louvain method, shown only when the links split into groups clearly more than shuffled links do. " +
      "Electrodes are not brain regions, and a link is not a measure of communication between parts of the brain.",
  );
  side.append(
    el("h2", {}, "Band"),
    bandChips,
    el("h2", {}, "Round"),
    condChips,
    el("h2", {}, "Over time"),
    play,
    slider,
    now,
    info,
  );
  wrap.append(stage, side);
  const fig = el("figure", { id: "results-network", class: "results-fig" });
  fig.append(el("h2", {}, "Networks"), el("figcaption", {}, caption.textContent), wrap);
  view.page.append(fig);
  if (!usable.some((c) => c.connectivity)) {
    cannot(stage, "Links could not be estimated for any round: not enough clean recording.");
    play.disabled = true;
    return;
  }

  const titles = Object.fromEntries(conditions().map((c) => [c.id, c.title]));
  let meshes = null;
  const stop = () => {
    clearInterval(view.timer);
    view.timer = null;
    play.textContent = "Play over time";
  };
  const frames = () => (view.windows ? view.windows.frames.filter((f) => f.bands[view.band]) : []);

  function render() {
    if (!view || !view.net || !meshes) return;
    const nodes = nodePositions(meshes, labels);
    let values, band, text;
    const list = frames();
    slider.max = String(Math.max(0, list.length - 1));
    const keep = labels.map((_, i) => good[i]);
    const index = keep.map((k, i) => (k ? i : -1)).filter((i) => i >= 0);
    stage.dataset.band = view.band;
    stage.dataset.condition = view.condition;
    if (view.frame != null && list[view.frame]) {
      const f = list[view.frame];
      band = f.bands[view.band];
      values = upperValues(band.upper, labels.length);
      text = `${titles[f.part || f.step] || f.step}, ${clock(f.t)} into the recording (a ${results.parameters.window_seconds} s window).`;
      slider.value = String(view.frame);
      stage.dataset.frame = String(view.frame);
      stage.dataset.t = String(f.t);
    } else {
      const c = usable.find((x) => x.id === view.condition);
      delete stage.dataset.frame;
      band = c.connectivity ? c.connectivity[view.band] : null;
      if (!band) {
        now.textContent = `${c.title}. ${c.connectivity_note === "too_few_electrodes" ? "Too few good electrodes to estimate links." : NOT_ENOUGH}`;
        info.textContent = "";
        view.net.setNetwork({ nodes: index.map((i) => nodes[i]), communities: index.map(() => -1), edges: [] });
        stage.dataset.links = "0";
        stage.dataset.grouped = "false";
        return;
      }
      values = matrixValues(band.matrix);
      text = `${c.title}, the whole round (${c.kept} clean 2 s segments).`;
    }
    const remap = new Map(index.map((i, k) => [i, k]));
    const { edges, shown, possible } = edgesOf(values, keep, band.threshold);
    const grouped = Boolean(band.grouped && band.communities && shown);
    view.net.setNetwork({
      nodes: index.map((i) => nodes[i]),
      communities: index.map((i) => (grouped ? band.communities[i] ?? -1 : -1)),
      edges: edges.map(([i, j, w]) => [remap.get(i), remap.get(j), w]),
    });
    stage.dataset.links = String(shown);
    stage.dataset.grouped = String(grouped);
    now.textContent = `${text} ${shown} of ${possible} links drawn.`;
    if (!shown) info.textContent = NO_LINKS;
    else if (!grouped) info.textContent = NO_GROUPS;
    else
      info.textContent = `The links split into groups more than shuffled links do: modularity Q = ${band.q.toFixed(2)}, shuffled ${band.q_null_mean.toFixed(2)} ± ${band.q_null_sd.toFixed(2)}.`;
  }
  view.renderNet = render;

  play.addEventListener("click", () => {
    if (view.timer) return stop();
    const list = frames();
    if (!list.length) {
      now.textContent = `No window of ${results.parameters.window_seconds} s had enough clean recording to estimate links.`;
      return;
    }
    if (view.frame == null || view.frame >= list.length - 1) view.frame = 0;
    play.textContent = "Pause";
    render();
    view.timer = setInterval(() => {
      const n = frames().length;
      if (view.frame >= n - 1) return stop();
      view.frame += 1;
      render();
    }, 700);
  });
  slider.addEventListener("input", () => {
    stop();
    view.frame = Number(slider.value);
    render();
  });

  Promise.all([
    import("../scene3d/network-view.js"),
    fetch("/static/assets/meshes.json").then((r) => r.json()),
    view.windows ? Promise.resolve(view.windows) : fetch(view.url.replace("/api/results?", "/api/results/windows?")).then((r) => (r.ok ? r.json() : null)),
  ])
    .then(([module, m, w]) => {
      if (!view || STEPS[view.step].id !== "networks") return;
      meshes = m;
      view.windows = w;
      view.net = module.createNetworkView(stage, meshes, {
        onSelect: (i, d) => {
          const hint = stage.querySelector(".hint");
          if (hint) hint.textContent = "Drag to turn. Scroll to zoom. Tap an electrode to see only its links.";
          if (i >= 0 && d) {
            const index = good.map((k, n) => (k ? n : -1)).filter((n) => n >= 0);
            const grouped = stage.dataset.grouped === "true";
            info.textContent = `${labels[index[i]]}: ${d.links} links drawn${grouped ? `, in a group of ${d.group} electrodes` : ""}.`;
          }
        },
      });
      if (view.net.unavailable) return;
      const hint = stage.querySelector(".hint");
      if (hint) hint.textContent = "Drag to turn. Scroll to zoom. Tap an electrode to see only its links.";
      stage.dataset.ready = "true";
      render();
    })
    .catch(() => cannot(stage, "The network view could not be loaded."));
}

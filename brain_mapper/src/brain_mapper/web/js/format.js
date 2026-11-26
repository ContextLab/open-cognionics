// Small text helpers shared by the screens.

/** Seconds as m:ss, rounded up (a clock that shows 0:00 only when time is up). */
export function formatTime(seconds) {
  const whole = Math.max(0, Math.ceil(seconds - 1e-9));
  const m = Math.floor(whole / 60);
  const s = String(whole % 60).padStart(2, "0");
  return `${m}:${s}`;
}

/** A length of time in words: "40 seconds" or "3 minutes". */
export function formatSpan(seconds) {
  if (seconds < 90) {
    const s = Math.round(seconds);
    return `${s} ${s === 1 ? "second" : "seconds"}`;
  }
  return `${Math.round(seconds / 60)} minutes`;
}

/** Element with attributes and text: el("p", {class: "x"}, "text"). */
export function el(tag, attrs = {}, text = "") {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text) node.textContent = text;
  return node;
}

/** The rounds of a flow: the steps that have blocks. */
export function rounds(flow) {
  return flow.filter((s) => s.blocks.length > 0);
}

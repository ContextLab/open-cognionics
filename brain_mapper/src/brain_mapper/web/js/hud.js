// The session score (points earned of possible, see scores.js) and the progress bar.
//
// app.js gives the score from the server's state, and only when no block is
// running (a running block never changes what it shows).

export class Hud {
  constructor() {
    this.root = document.getElementById("hud");
    this.label = document.getElementById("hud-label");
    this.points = document.getElementById("hud-points");
    this.bar = document.getElementById("hud-progress");
    this.fill = this.bar.querySelector("span");
    this.text = document.getElementById("hud-progress-text");
  }

  show(visible) {
    this.root.hidden = !visible;
  }

  /** `setup`: the score and its caption are not shown (css/layout.css). */
  setMode(mode) {
    if (this.root.dataset.mode !== mode) this.root.dataset.mode = mode;
  }

  setLabel(text) {
    if (this.label.textContent !== text) this.label.textContent = text;
  }

  setProgress(done, total) {
    const percent = total > 0 ? Math.round((100 * done) / total) : 0;
    this.fill.style.width = `${percent}%`;
    this.bar.setAttribute("aria-valuenow", String(percent));
    const text = total > 0 ? `${done} of ${total} blocks done` : "";
    if (this.text.textContent !== text) this.text.textContent = text;
  }

  /** Shows the session score: "15 of 64", or the dash before any point was possible. */
  setScore(text) {
    if (this.points.textContent !== text) this.points.textContent = text;
  }
}

// Live traces: one row per EEG channel, the last few seconds, drawn on a canvas.
//
// The server sends rows at 50 a second (`trace` messages, see server.py). `push` only puts them
// into a ring buffer of `SECONDS * HZ` rows per channel, so a page that is not being drawn (a tab
// in the background) holds the same small amount of memory however many messages arrive; the
// drawing runs on animation frames, which the browser stops for a hidden page.
//
// Each channel has its large constant offset taken away (a running mean with a one second time
// constant), then the rows are drawn at one common scale: six times the middle channel's root
// mean square, at least 10 microvolts.

const HZ = 50;
const SECONDS = 8;
const CAPACITY = HZ * SECONDS;
/** Time constant of the offset that is taken away, in rows. */
const OFFSET_ROWS = HZ;
/** Rows the scale is measured from. */
const SCALE_ROWS = HZ * 2;
const MIN_SCALE = 10;
const LEFT = 44;

export class TraceView {
  /** `canvas` is drawn into; `labels` are the channel names, in the order of the rows. */
  constructor(canvas, labels) {
    this.canvas = canvas;
    this.labels = labels;
    this.ctx = canvas.getContext("2d");
    const n = labels.length;
    this.buffers = labels.map(() => new Float32Array(CAPACITY));
    this.offsets = new Float64Array(n);
    this.seeded = false;
    this.head = 0; // where the next row goes
    this.total = 0;
    this.frame = 0;
    this.scale = MIN_SCALE;
    canvas.dataset.channels = String(n);
    canvas.dataset.capacity = String(CAPACITY);
    canvas.dataset.rows = "0";
    canvas.dataset.buffered = "0";
  }

  /** Adds rows (arrays of microvolts, one number per channel). Bad rows are ignored. */
  push(rows) {
    const n = this.labels.length;
    for (const row of rows) {
      if (!Array.isArray(row) || row.length < n) continue;
      if (!this.seeded) {
        for (let c = 0; c < n; c++) this.offsets[c] = row[c];
        this.seeded = true;
      }
      const k = 1 / OFFSET_ROWS;
      for (let c = 0; c < n; c++) {
        const v = Number(row[c]);
        this.offsets[c] += k * (v - this.offsets[c]);
        this.buffers[c][this.head] = v - this.offsets[c];
      }
      this.head = (this.head + 1) % CAPACITY;
      this.total += 1;
    }
    this.canvas.dataset.rows = String(this.total);
    this.canvas.dataset.buffered = String(Math.min(this.total, CAPACITY));
  }

  /** Starts drawing on animation frames. */
  start() {
    const step = () => {
      this.frame = requestAnimationFrame(step);
      if (!document.hidden) this.draw();
    };
    if (!this.frame) this.frame = requestAnimationFrame(step);
  }

  stop() {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  draw() {
    const canvas = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const style = getComputedStyle(canvas);
    const ink = style.getPropertyValue("--trace-ink").trim() || "#121826";
    const faint = style.getPropertyValue("--trace-grid").trim() || "#d3d9e6";
    const n = this.labels.length;
    const rowH = (h - 6) / n;
    const plotW = w - LEFT - 6;
    ctx.lineWidth = 1;
    ctx.strokeStyle = faint;
    for (let s = 0; s <= SECONDS; s++) {
      const x = Math.round(LEFT + (plotW * s) / SECONDS) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 2);
      ctx.lineTo(x, h - 4);
      ctx.stroke();
    }
    const have = Math.min(this.total, CAPACITY);
    if (have > 0) this.scale = this.measureScale(have);
    ctx.font = `600 ${Math.max(9, Math.min(12, rowH * 0.7))}px system-ui, sans-serif`;
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    for (let c = 0; c < n; c++) {
      const mid = 3 + rowH * (c + 0.5);
      ctx.fillStyle = ink;
      ctx.fillText(this.labels[c], 4, mid);
      ctx.strokeStyle = ink;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      const buffer = this.buffers[c];
      const first = (this.head - have + CAPACITY) % CAPACITY;
      for (let i = 0; i < have; i++) {
        const v = buffer[(first + i) % CAPACITY];
        let y = mid - (v / this.scale) * rowH;
        y = Math.max(mid - rowH * 0.9, Math.min(mid + rowH * 0.9, y));
        // The newest row is at the right edge; the picture moves left as rows arrive.
        const x = LEFT + (plotW * (CAPACITY - have + i)) / CAPACITY;
        if (i) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
      }
      ctx.stroke();
    }
    canvas.dataset.scale = this.scale.toFixed(1);
    canvas.dataset.drawn = String(Number(canvas.dataset.drawn || 0) + 1);
  }

  /** Six times the middle channel's root mean square over the last rows, at least `MIN_SCALE`. */
  measureScale(have) {
    const rows = Math.min(have, SCALE_ROWS);
    const rms = this.buffers.map((buffer) => {
      let sum = 0;
      for (let i = 1; i <= rows; i++) {
        const v = buffer[(this.head - i + CAPACITY) % CAPACITY];
        sum += v * v;
      }
      return Math.sqrt(sum / rows);
    });
    rms.sort((a, b) => a - b);
    return Math.max(MIN_SCALE, 6 * rms[Math.floor(rms.length / 2)]);
  }
}

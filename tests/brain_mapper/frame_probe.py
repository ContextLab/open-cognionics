"""What the page did around a frame: a test-only probe installed with ``page.add_init_script``.

It records, on the page's own clock (``performance.now``): every animation-frame timestamp, the
long animation frames and long tasks Chromium reports (``PerformanceObserver``), each websocket
message the page receives (its time and ``t``) and each ``sessionStorage``/``localStorage`` write
(start, duration, key, size). ``around(page, t)`` returns what of that lies near time ``t``, so a
test that finds a change painted late can say what ran in the frames before it.
"""

from __future__ import annotations

import os

INIT = """
(() => {
  const P = (window.__probe = { frames: [], loaf: [], long: [], ws: [], store: [] });
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        P.loaf.push({ start: e.startTime, dur: e.duration, blocking: e.blockingDuration,
          scripts: (e.scripts || []).map((s) => ({ invoker: s.invoker, fn: s.sourceFunctionName,
            src: (s.sourceURL || '').split('/').pop(), start: s.startTime, dur: s.duration })) });
      }
    }).observe({ type: 'long-animation-frame', buffered: true });
  } catch (e) { P.loafError = String(e); }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) P.long.push({ start: e.startTime, dur: e.duration });
    }).observe({ type: 'longtask', buffered: true });
  } catch (e) { P.longError = String(e); }
  const frame = (t) => {
    P.frames.push(t);
    if (P.frames.length > 40000) P.frames.splice(0, 20000);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  const Socket = window.WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      this.addEventListener('message', (m) => {
        let t = '?';
        try { t = JSON.parse(m.data).t; } catch (e) { /* not JSON */ }
        P.ws.push([performance.now(), t]);
      });
    }
  };
  const setItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    const start = performance.now();
    try {
      return setItem.call(this, key, value);
    } finally {
      P.store.push([start, performance.now() - start, String(key).slice(0, 48), String(value).length]);
    }
  };
})();
"""

AROUND = """([from, to]) => {
  const P = window.__probe;
  const inside = (t) => t >= from && t <= to;
  return {
    frames: P.frames.filter(inside),
    loaf: P.loaf.filter((e) => e.start + e.dur >= from && e.start <= to),
    long: P.long.filter((e) => e.start + e.dur >= from && e.start <= to),
    ws: P.ws.filter(([t]) => inside(t)),
    store: P.store.filter(([t]) => inside(t)),
    errors: [P.loafError, P.longError].filter(Boolean),
  };
}"""


def install(page) -> None:
    page.add_init_script(INIT)


def around(page, t: float, before: float = 120.0, after: float = 20.0) -> dict:
    """What the probe saw from ``before`` ms before ``t`` to ``after`` ms after it, with the
    machine's load average."""
    seen = page.evaluate(AROUND, [t - before, t + after])
    seen["load"] = os.getloadavg()
    return seen

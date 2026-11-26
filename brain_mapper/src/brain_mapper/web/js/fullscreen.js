// Full screen: entered from a button (browsers need a user action) and watched.
// The webkit-prefixed names are for Safari; they have not been tried here.

/** Step kinds that run timed blocks and so need full screen. */
const NEEDS = ["task", "video", "rest", "recall"];
/** The timed parts of the calibration step, which pause like a block. */
const TIMED_SUBSTEPS = ["eyes_open", "eyes_closed", "blinks", "signal"];

export function needsFullscreen(state) {
  return !!state && !state.stopped && (NEEDS.includes(state.kind) || TIMED_SUBSTEPS.includes(state.substep));
}

export class Fullscreen {
  constructor(onChange) {
    const changed = () => onChange(this.active);
    document.addEventListener("fullscreenchange", changed);
    document.addEventListener("webkitfullscreenchange", changed);
  }

  get active() {
    const element = document.fullscreenElement ?? document.webkitFullscreenElement ?? null;
    return element !== null;
  }

  /** Enters full screen; call it from a click. Rejects if the browser refuses. */
  async enter() {
    const root = document.documentElement;
    if (root.requestFullscreen) await root.requestFullscreen();
    else if (root.webkitRequestFullscreen) await root.webkitRequestFullscreen();
    else throw new Error("this browser has no full-screen mode");
  }
}

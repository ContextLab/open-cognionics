// Tones made with the Web Audio API: a sine, faded in and out (calibration and rest use this).
//
// Browsers let a page play sound only after a press on it, so `ensureAudio()` is called from a
// press (a Start button, or any key or click on a screen that plays a tone later). `playTone`
// returns the audio context's scheduled start time in seconds, or null when no sound can be
// played in this window.

let audio = null;

/** Makes (or wakes) the audio context; call it from a press. */
export function ensureAudio() {
  try {
    if (!audio) {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (Context) audio = new Context();
    }
    if (audio && audio.state === "suspended") audio.resume().catch(() => {});
  } catch (error) {
    audio = null;
  }
}

/** Whether a tone can be played now. */
export function audioReady() {
  return !!audio && audio.state === "running";
}

/**
 * Plays `{hz, ms, level, rampMs}` now: the level rises over `rampMs` (20 ms if not given), holds,
 * and falls over `rampMs` before the end. Returns `{audioTime, outputLatency, baseLatency,
 * contextTime, performanceTime}` (the scheduled start on the audio context's clock and that clock
 * read against `performance.now()`), or null when no sound can be played.
 */
export function playTone(tone) {
  if (!audioReady()) return null;
  const ramp = (tone.rampMs ?? 20) / 1000;
  const t = audio.currentTime;
  const end = t + tone.ms / 1000;
  const osc = audio.createOscillator();
  const gain = audio.createGain();
  osc.frequency.value = tone.hz;
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(tone.level, t + ramp);
  gain.gain.setValueAtTime(tone.level, end - ramp);
  gain.gain.linearRampToValueAtTime(0, end);
  osc.connect(gain).connect(audio.destination);
  osc.start(t);
  osc.stop(end + 0.01);
  const stamp = typeof audio.getOutputTimestamp === "function" ? audio.getOutputTimestamp() : {};
  return {
    audioTime: t,
    outputLatency: Number.isFinite(audio.outputLatency) ? audio.outputLatency : null,
    baseLatency: Number.isFinite(audio.baseLatency) ? audio.baseLatency : null,
    contextTime: Number.isFinite(stamp.contextTime) ? stamp.contextTime : null,
    performanceTime: Number.isFinite(stamp.performanceTime) ? stamp.performanceTime : null,
  };
}

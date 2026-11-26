// Frame arithmetic for the trial runner: pure functions of animation-frame timestamps
// (`requestAnimationFrame` times, the performance.now clock, ms). The runner measures the frame
// period while a task screen is mounted and uses these to put each change on the frame nearest to
// the time it is due. tests/brain_mapper/test_tasks_browser.py checks them on a frame series
// recorded in Chromium and on exact 60, 120 and 144 Hz series.

/** The median of the intervals between successive timestamps; null with fewer than `min` intervals. */
export function framePeriod(timestamps, min = 30) {
  const gaps = [];
  for (let i = 1; i < timestamps.length; i += 1) {
    const gap = timestamps[i] - timestamps[i - 1];
    if (gap > 0) gaps.push(gap);
  }
  if (gaps.length < min) return null;
  gaps.sort((a, b) => a - b);
  const mid = gaps.length >> 1;
  return gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2;
}

/** Whether a change due at `dueMs` belongs on the frame at `frameTs`: true once that frame is
 *  nearer to the due time than the next one would be (it is no more than half a frame early). A
 *  frame later than that (the due frame was missed) also takes it, at once. */
export function isDueFrame(frameTs, dueMs, frameMs) {
  return frameTs >= dueMs - frameMs / 2;
}

/** The index of the frame of `timestamps` that takes a change due at `dueMs`, as the runner picks
 *  it frame by frame: the first frame for which `isDueFrame` holds (-1 if none does). */
export function pickFrame(timestamps, dueMs, frameMs) {
  for (let i = 0; i < timestamps.length; i += 1) {
    if (isDueFrame(timestamps[i], dueMs, frameMs)) return i;
  }
  return -1;
}

/** Whole frames that the frame at `frameTs` is late for `dueMs` (0 when it is the nearest frame). */
export function lateFrames(frameTs, dueMs, frameMs) {
  return Math.max(0, Math.round((frameTs - dueMs) / frameMs));
}

/** Whole frames that the frame at `frameTs` is late for `dueMs` when the frame before it, at
 *  `prevTs`, was checked and was not yet due (`isDueFrame` false): the frames that would have
 *  come between the two (the gap, in whole frames, minus one) and were due. With no frame
 *  dropped this is 0, also when the due time lies half a frame from both frames and timestamp
 *  jitter made the earlier one just too early (`lateFrames` would round that to 1). */
export function missedFrames(prevTs, frameTs, dueMs, frameMs) {
  const between = Math.max(0, Math.round((frameTs - prevTs) / frameMs) - 1);
  let missed = 0;
  for (let k = 1; k <= between; k += 1) {
    if (isDueFrame(prevTs + k * frameMs, dueMs, frameMs)) missed += 1;
  }
  return missed;
}

/** The nearest whole number of frames to `durationMs`, at least 1. */
export function framesFor(durationMs, frameMs) {
  return Math.max(1, Math.round(durationMs / frameMs));
}

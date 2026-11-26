// Perception: drawing of the grating patch (a Gabor) on a canvas. Needs only the canvas.

/** The page's navy ground (--stim-ground in css/tokens.css), 0..255 per channel: the default
 *  background; the task passes the color the page is actually drawn with. */
export const BACKGROUND = Object.freeze([18, 24, 38]);
/** The light color the stripes rise to (--stim in css/tokens.css, white), 0..255 per channel: the
 *  default; the task passes the color the page actually defines. */
export const LIGHT = Object.freeze([255, 255, 255]);
/** Contrast of the grating: how far the peak of the stripes (at the center of the window) goes
 *  from the background toward the light color, per channel in pixel values (1: the peak is the
 *  light color itself). */
export const CONTRAST = 1;
/** Spatial frequency, cycles per 100 CSS px. */
export const CYCLES_PER_100_CSS_PX = 3;
/** Width of the patch as a fraction of the stimulus square. */
export const PATCH_FRACTION = 0.3;

/**
 * The peak color of the stripes: per channel
 * peak = background + contrast * (light - background), rounded, and never below the background
 * (a channel where the light color is darker than the background stays at the background), so the
 * stripes are never darker than the ground.
 * @param {number[]} background [r, g, b], 0..255
 * @param {number[]} light [r, g, b], 0..255
 * @param {number} contrast 0..1
 * @returns {number[]} [r, g, b], whole numbers
 */
export function peakColor(background, light, contrast) {
  return background.map((c, i) =>
    Math.min(255, Math.max(c, Math.round(c + contrast * (light[i] - c)))),
  );
}

/** "#rrggbb" of an [r, g, b]. */
export function hex(rgb) {
  return `#${rgb.map((c) => c.toString(16).padStart(2, '0')).join("")}`;
}

const images = new WeakMap();

/**
 * Draws a Gabor-like patch into `canvas` and sizes the canvas's pixel buffer to the stimulus
 * square at device pixel ratio (the caller sets the canvas's CSS size to `sizeCss`).
 *
 * The square is the background color, the page's own ground, so the canvas has no visible edge.
 * The patch is a sinusoidal grating between the background and a light peak color, times a
 * Gaussian window of standard deviation patch/6: per channel c,
 *   value_c = background_c + (peak_c - background_c) * w,  w = exp(-r^2 / (2 sigma^2)) * (1 + cos(2 pi f u + phase)) / 2,
 * where u = dx cos(tilt) + dy sin(tilt) in screen coordinates (y down) from the square's center
 * and peak = peakColor(background, light, contrast) (with the defaults, the light color itself). The stripes are lighter than the ground and fade
 * into it: at the window's edge the patch equals the background. Pixels farther than 4 sigma
 * from the center (where w * 255 < 0.1) are the background. Pixel values, no gamma correction.
 * With tilt 0 the stripes are vertical; a positive tilt turns them clockwise on screen (the top
 * of each stripe leans right), a negative tilt counter-clockwise.
 *
 * @param {HTMLCanvasElement} canvas
 * @param {object} options
 * @param {number} options.sizeCss side of the stimulus square, CSS px
 * @param {number} [options.dpr=1] device pixel ratio
 * @param {number} options.tiltDeg signed tilt from vertical, degrees (+ clockwise)
 * @param {number} [options.phaseDeg=0] phase of the grating at the center, degrees
 * @param {number} [options.contrast=CONTRAST]
 * @param {number[]} [options.background=BACKGROUND] [r, g, b], 0..255
 * @param {number[]} [options.light=LIGHT] [r, g, b], 0..255: the color the stripes rise toward
 * @param {number} [options.cyclesPer100Css=CYCLES_PER_100_CSS_PX]
 * @param {number} [options.patchFraction=PATCH_FRACTION]
 * @returns {{patch_px: number, size_px: number, background: number[], peak: number[]}} patch
 *   width in CSS px; canvas side in device px; the background and peak colors used
 */
export function drawGrating(canvas, {
  sizeCss,
  dpr = 1,
  tiltDeg,
  phaseDeg = 0,
  contrast = CONTRAST,
  background = BACKGROUND,
  light = LIGHT,
  cyclesPer100Css = CYCLES_PER_100_CSS_PX,
  patchFraction = PATCH_FRACTION,
}) {
  const size = Math.round(sizeCss * dpr);
  if (canvas.width !== size) canvas.width = size;
  if (canvas.height !== size) canvas.height = size;
  const patchCss = Math.round(patchFraction * sizeCss);
  const sigma = (patchCss * dpr) / 6;
  const reach = 4 * sigma;
  const freq = cyclesPer100Css / 100 / dpr; // cycles per device pixel
  const theta = (tiltDeg * Math.PI) / 180;
  const cos = Math.cos(theta);
  const sin = Math.sin(theta);
  const phase = (phaseDeg * Math.PI) / 180;
  const center = size / 2;
  const bg = background.map((c) => Math.round(c));
  const peak = peakColor(bg, light.map((c) => Math.round(c)), contrast);
  const span = [peak[0] - bg[0], peak[1] - bg[1], peak[2] - bg[2]];
  const ctx = canvas.getContext('2d');
  // One ImageData per canvas and size, reused, so that drawing a trial makes no garbage.
  let image = images.get(canvas);
  if (!image || image.width !== size) {
    image = ctx.createImageData(size, size);
    images.set(canvas, image);
  }
  const data = image.data;
  for (let y = 0; y < size; y++) {
    const dy = y + 0.5 - center;
    for (let x = 0; x < size; x++) {
      const dx = x + 0.5 - center;
      const r2 = dx * dx + dy * dy;
      const i = 4 * (y * size + x);
      if (r2 <= reach * reach) {
        const env = Math.exp(-r2 / (2 * sigma * sigma));
        const u = dx * cos + dy * sin;
        const w = (env * (1 + Math.cos(2 * Math.PI * freq * u + phase))) / 2;
        data[i] = Math.round(bg[0] + span[0] * w);
        data[i + 1] = Math.round(bg[1] + span[1] * w);
        data[i + 2] = Math.round(bg[2] + span[2] * w);
      } else {
        data[i] = bg[0];
        data[i + 1] = bg[1];
        data[i + 2] = bg[2];
      }
      data[i + 3] = 255;
    }
  }
  ctx.putImageData(image, 0, 0);
  return { patch_px: patchCss, size_px: size, background: bg, peak };
}

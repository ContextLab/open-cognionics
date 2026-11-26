// The onlooker's line for the signal check (see screens/signal-check.js): the same headline as
// the participant's screen, below the contact line, once the calibration result has a signal.
import { headline } from "./screens/signal-check.js";

export function showSignal(calibration) {
  const contactLine = document.getElementById("ol-contact");
  if (!contactLine) return;
  let line = document.getElementById("ol-signal");
  const signal = calibration && calibration.signal;
  if (!signal) {
    if (line) line.hidden = true;
    return;
  }
  if (!line) {
    line = document.createElement("p");
    line.id = "ol-signal";
    line.className = "ol-contact";
    contactLine.after(line);
  }
  line.hidden = false;
  const text = `Signal check: ${headline(calibration)}`;
  if (line.textContent !== text) line.textContent = text;
  line.dataset.notFine = String(((signal && signal.not_fine) || []).length);
}

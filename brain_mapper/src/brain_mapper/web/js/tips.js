// The tips of the device screen: what the software sees, and what is said about it.
//
// The words are plain and factual. Where the cause is not known, the tip says so. There is no
// judgement of the battery: its cut-off voltage is not known (docs/device.md).
//
// `chooseTip(link, status)` takes the last `link` message and the last `status` message and
// returns `{ code, text }` for the one thing the software sees, or null when there is nothing
// to say. The codes are those of `source.LinkProblem` (no_headset, several_headsets, open_error,
// no_data, no_configuration, disconnected) plus `dropped` (a link that was live was lost) and
// `lost_samples` (samples lost in the last 10 s).

/** Seconds between two attempts; the server says it in each `problem`, this is the fallback. */
const RETRY_S = 2;

const retry = (problem) => `brain-mapper tries again every ${(problem && problem.retry_s) || RETRY_S} seconds.`;

/** One entry per thing the software can see. `text(problem, status)` gives the words. */
export const TIPS = {
  no_headset: {
    seen: "No dongle found",
    text: (p) =>
      "No headset dongle was found on this computer. Plug the dongle into a USB port. " +
      `If it is already plugged in, take it out and plug it in again. ${retry(p)}`,
  },
  several_headsets: {
    seen: "More than one dongle",
    text: (p) =>
      `More than one headset dongle was found: ${(p.ports || []).join(", ") || "the ports are not known"}. ` +
      "brain-mapper needs exactly one. Unplug the others, or start brain-mapper again with " +
      `--port and the port of the dongle to use. ${retry(p)}`,
  },
  open_error: {
    seen: "Port cannot be opened",
    text: (p) =>
      `The port could not be opened (${p.msg}). Another program may be using it, or ` +
      `the dongle may have been unplugged. ${retry(p)}`,
  },
  no_data: {
    seen: "Dongle found, nothing received",
    text: (p) =>
      "The dongle is connected, but nothing has been received from the headset. " +
      `Check that the headset is switched on and in range of the dongle. ${retry(p)}`,
  },
  no_configuration: {
    seen: "Data arriving, no configuration reply",
    text: (p) =>
      "Data is arriving from the headset, but it has not sent its configuration, which " +
      `brain-mapper needs to read the data. The cause is not known. ${retry(p)}`,
  },
  dropped: {
    seen: "Link dropped",
    text: (p, s, ended) =>
      "The link to the headset dropped. If the dongle was unplugged, plug it in again; if the " +
      "headset was switched off, switch it on." +
      (ended
        ? " The session has ended. What was recorded up to the drop is saved on this computer."
        : ` ${retry(p)}`),
  },
  fault: {
    seen: "An error inside brain-mapper",
    text: (p, s, ended, link) =>
      "brain-mapper stopped reading the headset because of an error inside brain-mapper" +
      `${link && link.reason ? ` (${link.reason})` : ""}. The session has ended. What was ` +
      "recorded up to then is saved on this computer.",
  },
  lost_samples: {
    seen: "Samples being lost",
    text: (p, s) =>
      `Samples are being lost: ${s.lost10} in the last 10 seconds. Each one is a gap in the ` +
      "recording. Check that the headset is in range of the dongle.",
  },
};

/**
 * The tip for what the page last heard, or null.
 * `ended` is true on the screen shown after the session has ended with the link.
 */
export function chooseTip(link, status, ended = false) {
  if (!link) return null;
  const problem = link.problem;
  if (link.fault) {
    // The hub thread itself failed: not a dropped link, and nothing is tried again.
    return { code: "fault", text: TIPS.fault.text(problem, status, true, link) };
  }
  if (ended || (problem && problem.dropped)) {
    return { code: "dropped", text: TIPS.dropped.text(problem, status, ended) };
  }
  if (problem && TIPS[problem.code]) {
    return { code: problem.code, text: TIPS[problem.code].text(problem, status) };
  }
  if (problem) {
    // An error that has no tip of its own: the words of the error itself.
    return { code: "other", text: `${problem.kind}: ${problem.msg}. ${retry(problem)}` };
  }
  if (link.state === "live" && status && status.lost10 > 0) {
    return { code: "lost_samples", text: TIPS.lost_samples.text(problem, status) };
  }
  return null;
}

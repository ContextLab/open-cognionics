// Scores as the page shows them: points earned of points possible, per task and for the session.
// The server computes them from the trial events (brain_mapper/scores.py) and sends them in
// `state.scores` and in the session record (`summary.scores`): `{tasks: {<task>: {earned, possible,
// trials, rule}}, total: {earned, possible}}`, a task keyed by the module of its round.
import { rounds } from "./format.js";

/** What shows where nothing was scored. */
export const DASH = "—";

/** "14 of 20", or null when nothing was possible. */
export function ofText(entry) {
  return entry && entry.possible > 0 ? `${entry.earned} of ${entry.possible}` : null;
}

/** The session total as text, or the dash before any point was possible. */
export function totalText(scores) {
  return ofText(scores && scores.total) || DASH;
}

/** Whether a round of the flow is one of the scored kinds (a task, the delayed recall, or the
 *  video: a point for each full 10 s watched). */
export function isScoredKind(step) {
  return !!step && (step.kind === "task" || step.kind === "recall" || step.kind === "video");
}

/** The score entry of a round (by its module), or null. */
export function entryOf(scores, step) {
  const tasks = (scores && scores.tasks) || {};
  return tasks[(step && (step.module || step.id)) || ""] || null;
}

/**
 * One row per task round of the flow: `{id, title, text, reason}`; `text` is "14 of 20" or the dash,
 * and `reason` says plainly why there is a dash (null otherwise).
 */
export function scoreRows(flow, state, scores) {
  const outcomes = (state && state.outcomes) || {};
  return rounds(flow || [])
    .filter(isScoredKind)
    .map((step) => {
      const entry = entryOf(scores, step);
      const text = ofText(entry);
      let reason = null;
      if (!text) {
        const outcome = outcomes[step.id];
        if (outcome === "skipped") reason = "Skipped";
        else if (!entry && (outcome === "not_reached" || outcome === "running")) {
          reason = state && state.stopped ? "Stopped before it started" : "Not reached";
        } else reason = "No scored trials";
      }
      // A block that a pause ended is said so beside the score (its finished trials still count).
      const cut = ((state && state.interrupted_blocks) || {})[step.id] || [];
      const note = cut.length ? `${cut.length === 1 ? "1 block" : `${cut.length} blocks`} interrupted by a pause` : null;
      return { id: step.id, title: step.title, text: text || DASH, reason: reason && note ? `${reason}; ${note}` : reason || note };
    });
}

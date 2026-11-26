// The example task: a square appears after a short wait; press the space bar. It exists to test
// the task framework and is played only by test flows (`tests/brain_mapper/flows/example.json`).
// Each trial's parameters depend on the history (`done_before`: the trials done before it in the
// block), and one parameter is called `response` on purpose: the runner keeps it as
// `param_response`, so the measured response is never replaced. The key map gives the answer
// `seen` for the space bar: `response` is `seen` and `key` is `Space` in every record.
import { el } from "../format.js";
import { taskScreen } from "./runner.js";

export const { mount, update, tick, unmount, instructions } = taskScreen({
  name: "example",
  keys: { Space: "seen" },
  legend: [["Space", "the square is on the screen"]],
  panel: true, // the shared stimulus area, drawn on the page ground
  block: () => ({
    instructions: "A dark square appears after a short wait. Press the space bar as soon as you see it.",
    practice: 2,
  }),
  trials: () => ({ rng, history }) => ({
    delay_ms: 300 + rng.int(400),
    done_before: history.length,
    response: "space bar",
    cid: "square", // a parameter named like the server's client id: kept as param_cid
  }),
  async run(trial, io) {
    // A fixation cross in the background layer: drawn once, it stays across the trials.
    if (!io.area.querySelector(".example-cross")) {
      await io.background((layer) => layer.append(el("div", { class: "example-cross" }, "+")));
    }
    await io.wait(trial.delay_ms);
    await io.show((area) => area.append(el("div", { class: "example-square" })));
    const answer = await io.response({ timeout: 5000 });
    await io.clear();
    return { correct: answer.answer === "seen" };
  },
});

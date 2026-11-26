// A check of the frame timing, played only by tests (a flow step with `"module": "timing"`). What
// it plays depends on the block id:
//   block1         one trial of 30 flashes of a dark square, each 200 ms (`io.wait`, whole frames
//                  from its onset), their onsets anchored to the first one (onset k at
//                  first + k x 2500 ms, with `io.showAt`). Nothing to press.
//   wait_then_key  trials: show; wait 200 ms; the space bar; wait 500 ms; clear.
//   key_then_wait  trials: show; the space bar; wait 300 ms; clear.
//   timeouts       trials: show; a response window of 1500 ms that times out; clear at onset + 1500.
// The waits after the key count from the key (on the frame grid), not from the wait before it.
import { el } from "../format.js";
import { taskScreen } from "./runner.js";

const draw = (area) => area.append(el("div", { class: "example-square" }));

export const { mount, update, tick, unmount, instructions } = taskScreen({
  name: "timing",
  keys: ["Space"],
  legend: [],
  panel: true,
  block: () => ({ instructions: "Watch the square. Press the space bar when asked.", practice: 0 }),
  trials: ({ block }) => ({ index }) => {
    if (block === "block1") return index === 0 ? { mode: "flashes", count: 30, spacing_ms: 2500, duration_ms: 200 } : null;
    return { mode: block };
  },
  async run(trial, io) {
    if (trial.mode === "flashes") {
      let first = null;
      for (let k = 0; k < trial.count; k += 1) {
        const onset =
          first === null
            ? await io.show(draw, { flash: k })
            : await io.showAt(first + k * trial.spacing_ms, draw, { flash: k });
        if (first === null) first = onset;
        await io.wait(trial.duration_ms);
        await io.clear();
      }
      return {};
    }
    if (trial.mode === "timeouts") {
      const onset = await io.show(draw);
      await io.response({ timeout: 1500 });
      await io.clearAt(onset + 1500);
      return {};
    }
    await io.show(draw);
    if (trial.mode === "wait_then_key") {
      await io.wait(200);
      await io.response({ timeout: 10000 });
      await io.wait(500);
    } else {
      await io.response({ timeout: 10000 });
      await io.wait(300);
    }
    await io.clear();
    return {};
  },
});

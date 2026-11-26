// The task modules. A step of kind `task`, `video` or `recall` whose `module` (the step id unless
// the flow says otherwise) is here shows that module; any other shows the placeholder. One line
// per module, in alphabetical order; adding a task is adding its line. Each module is a screen
// made with `taskScreen` from `./runner.js`. `example` and `timing` are played only by test flows.

export * as example from "./example.js";
export * as flanker from "./flanker.js";
export * as nback from "./nback.js";
export * as perception from "./perception.js";
export { recall } from "./words.js";
export * as spatial from "./spatial.js";
export * as timing from "./timing.js";
export * as words from "./words.js";

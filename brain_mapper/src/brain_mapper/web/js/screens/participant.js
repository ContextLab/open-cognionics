// Participant screen: a label field, the labels remembered on this computer as buttons,
// and Continue. The server decides what a good label is and starts the recording when it
// accepts one; this screen shows what the server answers. Every refusal carries a `code`
// (invalid_label, bad_body, body_too_large, already_set, source_not_ready, link_ended,
// recording_failed, write_failed, store_damaged, bad_selection, unknown_step, not_continuable,
// recall_needs_words) next to the words; the page switches on the code. It also asks
// `POST /api/progress` (the label goes in the body, never in a URL) whether the typed label
// has an earlier session to continue.
//
// "Load a saved session file" (a file input, or a .zip dropped on the screen) sends the zip a
// session ended with to `POST /api/import` on this server only. The answer fills in the file's
// participant ID, says when the saved session was and how many rounds it completed, and offers a
// new session for the same person (its next sample point, `import` sent with the label) and, only
// when that same session's folder is unfinished on this computer, the usual continue offer.
import { el } from "../format.js";

async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    return {};
  }
}

/** How long Continue is ignored after a label was accepted and the next step was asked for. */
const RETRY_MS = 3000;
/** How long after the last key the page asks whether the label has an earlier session. */
const LOOKUP_MS = 250;
/** The shape a label can have; the server decides what is allowed, this only saves a request. */
const LOOKS_RIGHT = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
/** What a round chosen alone needs, in words (the server refuses it otherwise, with `recall_needs_words`). */
const ONLY_NEEDS = {
  recall:
    "Delayed recall on its own needs the word lists from an earlier session of this participant, and none " +
    "was found for this label. Play the whole route instead, or use a label that studied the lists.",
};
/** The same, for a round chosen with others but without the round it needs. */
const WITHOUT_NEEDS = {
  recall: (other) =>
    `Delayed recall without the ${other} round needs the word lists from an earlier session of this ` +
    "participant, and none was found for this label. Play the whole route instead, or use a label " +
    "that studied the lists.",
};
/** The round each of those needs. */
const NEEDS = { recall: "words" };

/** Titles in words: "A", "A and B", "A, B and C". */
function names(titles) {
  if (titles.length < 2) return titles.join("");
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}
/** Refusals that do not get better by pressing Continue again. */
const FINAL = new Set(["store_damaged"]);
/** The largest saved session file the server takes (importer.MAX_UPLOAD). */
const MAX_FILE = 1024 * 1024 * 1024;

/** "November 25, 2026" for a Unix time in seconds. */
function dateOf(unix) {
  return new Date(unix * 1000).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
}

export function mount(root, api) {
  const main = el("div", { class: "participant-main" });
  main.append(
    el("div", { class: "kicker" }, "Setup"),
    el("h1", {}, "Who is wearing the headset?"),
    el(
      "p",
      { class: "lede" },
      "Type a label for this person, such as P001, or pick one used before on this computer. " +
        "The recording is saved under this label.",
    ),
  );

  const form = el("form", { class: "participant-form", novalidate: "" });
  const input = el("input", {
    id: "participant-label",
    name: "label",
    type: "text",
    autocomplete: "off",
    autocapitalize: "off",
    spellcheck: "false",
    "aria-describedby": "participant-rule participant-hint participant-problem",
  });
  // These two stay in the page and only their words change, so that a screen reader announces them.
  const problem = el("p", { class: "problem", id: "participant-problem", role: "alert" });
  const caseHint = el("p", { class: "hint", id: "participant-hint", role: "status" });
  const waiting = el("p", { class: "hint", id: "participant-wait", role: "status" });
  // Continue is not given the `disabled` attribute while the label is sent (that would drop the
  // keyboard focus); it ignores presses until the answer is in.
  const go = el("button", { class: "btn", id: "advance", type: "submit" }, "Continue");
  const startOver = el("button", { class: "btn btn--quiet", id: "start-over", type: "button" }, "Start from the beginning");
  startOver.hidden = true;
  const offer = el("p", { class: "offer", id: "continue-offer", role: "status" });
  offer.hidden = true;
  // The rounds chosen on the first screen: said in one line, with the way back to the whole route.
  const chosenRound = el("p", { class: "hint", id: "participant-selection" });
  const wholeRoute = el("button", { class: "btn btn--quiet", id: "whole-route", type: "button" }, "Play the whole route instead");
  const chosenBox = el("div", { class: "chosen-round", id: "participant-chosen" });
  chosenBox.append(chosenRound, wholeRoute);
  chosenBox.hidden = true;
  const progressNote = el("p", { class: "hint", id: "participant-progress-note", role: "status" });
  // A saved session file: chosen with the button (the file input itself stays out of sight) or
  // dropped anywhere on this screen.
  const fileInput = el("input", { id: "import-file", type: "file", accept: ".zip,application/zip" });
  fileInput.hidden = true;
  const choose = el("button", { class: "btn btn--quiet", id: "import-choose", type: "button" }, "Load a saved session file");
  const clearImport = el("button", { class: "btn btn--quiet", id: "import-clear", type: "button" }, "Type an ID instead");
  clearImport.hidden = true;
  const importHint = el(
    "p",
    { class: "hint", id: "import-hint" },
    "Or drop the .zip file saved at the end of that session here.",
  );
  const importSummary = el("p", { class: "import-summary", id: "import-summary", role: "status" });
  const importPoint = el("p", { class: "hint", id: "import-point" });
  const importLabelNote = el("p", { class: "hint", id: "import-label-note", role: "status" });
  const importProblem = el("p", { class: "problem", id: "import-problem", role: "alert" });
  const importRow = el("div", { class: "row" });
  importRow.append(choose, clearImport);
  const importBox = el("div", { class: "import-box", id: "import-box" });
  importBox.append(fileInput, importRow, importHint, importSummary, importPoint, importLabelNote, importProblem);
  // Continue an unfinished session named in the file (only when its folder is on this computer).
  const importContinue = el("button", { class: "btn btn--quiet", id: "import-continue", type: "button" }, "Continue");
  importContinue.hidden = true;
  const actions = el("div", { class: "row" });
  actions.append(go, startOver, importContinue);
  form.append(
    el("label", { class: "field-label", for: "participant-label" }, "Participant label"),
    input,
    el(
      "p",
      { class: "hint", id: "participant-rule" },
      "1 to 32 characters: the letters A to Z and digits, and _ or - after the first character.",
    ),
    caseHint,
    chosenBox,
    offer,
    progressNote,
    problem,
    waiting,
    // Just above the action: the one thing to check before it.
    el("p", { class: "participant-check", id: "participant-check" }, "Check this label: it cannot be changed during this session"),
    actions,
  );

  const remembered = el("div", { class: "remembered" });
  remembered.hidden = true;
  const buttons = el("div", { class: "remembered-buttons" });
  remembered.append(el("h2", {}, "Used before on this computer"), buttons);

  const note = el("p", { class: "note" }, "The list of labels is kept in a file on this computer. It is not sent anywhere.");
  // Where the files are, for whoever needs it: behind a disclosure.
  const help = el("details", { class: "participant-help", id: "participant-help" });
  help.append(
    el("summary", {}, "About these files"),
    el(
      "p",
      { class: "hint" },
      "The list of labels is participants.json in the brain-mapper data folder. A saved session " +
        "file is the .zip saved at the end of a session: loading it fills in the label, and the new " +
        "session is saved as a later sample point of the same person.",
    ),
  );

  // One column: the label and Continue first; the saved session below it, as the other way in.
  const saved = el("section", { class: "participant-saved", id: "participant-saved" });
  saved.append(el("h2", {}, "Use a participant from a saved session"), importBox);
  const side = el("div", { class: "participant-side" });
  side.append(remembered, saved, note, help);
  main.append(form, side);
  root.replaceChildren(main);

  let known = [];
  let busy = false;
  let blocked = false;
  // What the server said about the typed label's earlier session, for the label it was asked about.
  let earlier = null;
  let asked = 0;
  let lookup = null;
  let settled = true; // false from a key press until the server has answered for that label

  // The answer of `POST /api/import` for the file loaded, or null.
  let loaded = null;
  let loading = false;

  // The rounds chosen on the first screen (ids in route order), or null. Clearing it plays the whole route.
  let only = api.ctx.selection ? api.ctx.selection.only : null;

  /** The chosen rounds as sent: one round as its id (as before), several as a list. */
  function onlyField() {
    return only ? { only: only.length === 1 ? only[0] : only } : {};
  }

  function showChoice() {
    chosenBox.hidden = !only;
    if (only) {
      const titles = names(only.map((id) => api.stepTitle(id)));
      chosenRound.textContent =
        only.length === 1
          ? `Only ${titles} will be played, as a new session, after the setup and before the summary.`
          : `Only ${titles} will be played, in route order, as a new session, after the setup and before the summary.`;
    }
  }

  /** The chosen round that is played without the round it needs before it, or undefined. */
  function lacking() {
    if (!only) return undefined;
    return only.find((id) => NEEDS[id] && !only.slice(0, only.indexOf(id)).includes(NEEDS[id]));
  }

  /** What the chosen rounds need that the typed label has not studied, in words, or "" (known once asked). */
  function whatItNeeds() {
    const id = lacking();
    if (!id || !earlier || earlier.words_done !== false) return "";
    return only.length === 1 ? ONLY_NEEDS[id] || "" : WITHOUT_NEEDS[id] ? WITHOUT_NEEDS[id](api.stepTitle(NEEDS[id])) : "";
  }

  /** Whether the chosen rounds need lists that the typed label has not studied (known once asked). */
  function lacksWhatItNeeds() {
    return whatItNeeds() !== "";
  }

  function showOffer() {
    showChoice();
    if (loaded) {
      // A saved session file is loaded: a new session of that person, or (only when its folder is
      // here, unfinished) the usual continue offer for that same session.
      const again = loaded.continue && !only ? loaded.continue : null;
      offer.hidden = !again;
      if (again) {
        offer.textContent =
          `The session in this file is unfinished on this computer: it stopped before ${again.next_title}. ` +
          "Continuing runs the setup again and then picks up there.";
      }
      importContinue.hidden = !again;
      if (again) importContinue.textContent = `Continue from ${again.next_title}`;
      startOver.hidden = true;
      go.textContent = "Start a new session for this person";
      progressNote.textContent = "";
      go.disabled = blocked;
      return;
    }
    importContinue.hidden = true;
    const offered = !!earlier && !!earlier.next_step && !only;
    offer.hidden = !offered;
    startOver.hidden = !offered;
    go.textContent = offered ? `Continue from ${earlier.next_title}` : "Continue";
    if (offered) {
      offer.textContent =
        `${input.value} has an earlier session that stopped before ${earlier.next_title}. ` +
        "Continuing runs the setup again and then picks up there. " +
        "Starting from the beginning plays every round again.";
    }
    let note = "";
    if (earlier && earlier.note) note = earlier.note;
    else if (lacksWhatItNeeds()) note = whatItNeeds();
    progressNote.textContent = note;
    // Continue would only be refused: it is off while the reason stands, and the way back is on show.
    go.disabled = blocked || lacksWhatItNeeds();
  }

  wholeRoute.addEventListener("click", () => {
    api.ctx.selection = null;
    only = null;
    show("");
    showOffer();
    input.focus(); // the button is gone now; the label field is where the person goes on
  });

  async function lookUp() {
    if (loaded) return; // the ID is the file's; what to offer came with the file
    const typed = input.value;
    const mine = ++asked;
    if (!LOOKS_RIGHT.test(typed)) {
      earlier = null;
      settled = true;
      showOffer();
      return;
    }
    try {
      const response = await fetch("/api/progress", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ participant: typed }),
      });
      const body = await readJson(response);
      if (mine !== asked) return; // the label changed while this was on its way
      earlier = response.ok ? body : null;
    } catch (error) {
      if (mine !== asked) return;
      earlier = null;
    }
    settled = true;
    showOffer();
  }

  function lookUpSoon() {
    asked += 1;
    settled = false;
    earlier = null;
    showOffer();
    clearTimeout(lookup);
    lookup = setTimeout(lookUp, LOOKUP_MS);
  }

  function show(text) {
    problem.textContent = text;
    input.setAttribute("aria-invalid", text ? "true" : "false");
  }

  function updateCaseHint() {
    const typed = input.value;
    const twin = known.find((label) => label !== typed && label.toLowerCase() === typed.toLowerCase());
    caseHint.textContent = twin
      ? `${twin} was used before on this computer. ${typed} would be a different participant.`
      : "";
  }
  input.addEventListener("input", () => {
    updateCaseHint();
    lookUpSoon();
  });

  async function listLabels() {
    try {
      const response = await fetch("/api/participants");
      const body = await readJson(response);
      if (!response.ok) {
        show(body.error || `The participant list could not be read (status ${response.status}).`);
        if (FINAL.has(body.code)) {
          blocked = true;
          go.disabled = true;
        }
        return;
      }
      known = body.labels;
      buttons.replaceChildren();
      for (const label of known) {
        const choice = el("button", { class: "btn btn--quiet", type: "button" }, label);
        choice.addEventListener("click", () => {
          if (loaded) unload();
          input.value = label;
          show("");
          updateCaseHint();
          lookUpSoon();
          input.focus();
        });
        buttons.append(choice);
      }
      remembered.hidden = known.length === 0;
      updateCaseHint();
    } catch (error) {
      show("The participant list could not be reached. Is brain-mapper still running?");
    }
  }

  function refused(text, code) {
    show(text);
    busy = false;
    waiting.textContent = "";
    // A refused choice of rounds is mended with the way back, so that is where the focus goes.
    if (only && ["recall_needs_words", "bad_selection", "unknown_step"].includes(code)) wholeRoute.focus();
    else input.focus();
  }

  async function submit(continuing) {
    if (blocked || busy || loading || lacksWhatItNeeds()) return;
    busy = true;
    if (!settled && !loaded) {
      // A key was pressed a moment ago: find out first whether this label has an earlier session.
      clearTimeout(lookup);
      await lookUp();
      if (earlier && earlier.next_step && !only) {
        // The offer has only just appeared: the person has not seen it yet, so nothing is sent.
        busy = false;
        return;
      }
      if (lacksWhatItNeeds()) {
        // The reason is on the screen now; there is nothing to send.
        busy = false;
        wholeRoute.focus();
        return;
      }
    }
    waiting.textContent = "Saving the label. This waits a moment for the first attempt to connect to the headset; the next screen helps if it is not linked.";
    try {
      const response = await fetch("/api/participant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          loaded
            ? {
                label: loaded.label,
                // Continuing the file's own unfinished session, or a new session of its person.
                ...(continuing === "loaded" && loaded.continue && !only
                  ? { continues: loaded.continue.session }
                  : { import: loaded.token, ...onlyField() }),
              }
            : {
                label: input.value,
                // `continues` and `only` together are refused by the server, so only one is ever sent.
                ...onlyField(),
                ...(!only && continuing && earlier && earlier.next_step ? { continues: earlier.session } : {}),
              },
        ),
      });
      const body = await readJson(response);
      if (!response.ok) {
        refused(body.error || `The label was not accepted (status ${response.status}).`, body.code);
        if (FINAL.has(body.code)) {
          blocked = true;
          go.disabled = true;
        }
        return;
      }
      show("");
      waiting.textContent = "";
      if (!api.send({ cmd: "advance" })) busy = false;
      // If the step does not change (a lost command), Continue can be pressed again.
      setTimeout(() => {
        busy = false;
      }, RETRY_MS);
    } catch (error) {
      refused("brain-mapper could not be reached. Is it still running?");
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(!loaded);
  });
  startOver.addEventListener("click", () => submit(false));
  importContinue.addEventListener("click", () => submit("loaded"));

  /** Back to a typed ID: the loaded file is set aside (its copy on this computer stays). */
  function unload() {
    loaded = null;
    input.readOnly = false;
    clearImport.hidden = true;
    importSummary.textContent = "";
    importPoint.textContent = "";
    importLabelNote.textContent = "";
    earlier = null;
    showOffer();
  }

  clearImport.addEventListener("click", () => {
    unload();
    input.value = "";
    show("");
    updateCaseHint();
    input.focus();
  });

  async function load(file) {
    if (!file || loading || busy || blocked) return;
    importProblem.textContent = "";
    if (file.size > MAX_FILE) {
      importProblem.textContent = "That file is larger than 1024 MB, too large for a saved session file.";
      return;
    }
    loading = true;
    const typed = loaded ? "" : input.value.trim();
    importSummary.textContent = "Reading the file.";
    try {
      // The file goes to this computer's brain-mapper only, as the body; nothing is in the address.
      const response = await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: file,
      });
      const body = await readJson(response);
      if (!response.ok) {
        importSummary.textContent = "";
        importProblem.textContent = body.error || `The file could not be loaded (status ${response.status}).`;
        return;
      }
      loaded = body;
      clearTimeout(lookup);
      asked += 1;
      settled = true;
      earlier = null;
      input.value = body.label;
      input.readOnly = true;
      show("");
      updateCaseHint();
      const n = body.rounds_done;
      importSummary.textContent = `Saved session from ${dateOf(body.started_unix)}: ${n} ${n === 1 ? "round" : "rounds"} completed`;
      importPoint.textContent = `A new session is saved as sample point ${body.next_sample_point} for this person.`;
      importLabelNote.textContent =
        typed && typed !== body.label ? `The ID in the file, ${body.label}, is used instead of ${typed}.` : "";
      clearImport.hidden = false;
      showOffer();
      go.focus();
    } catch (error) {
      importSummary.textContent = "";
      importProblem.textContent = "brain-mapper could not be reached. Is it still running?";
    } finally {
      loading = false;
      fileInput.value = ""; // the same file can be chosen again
    }
  }

  choose.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => load(fileInput.files && fileInput.files[0]));
  // A file dropped anywhere on this screen is loaded (and the browser does not open it instead).
  main.addEventListener("dragover", (event) => {
    if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    importBox.classList.add("dragging");
  });
  main.addEventListener("dragleave", (event) => {
    if (!main.contains(event.relatedTarget)) importBox.classList.remove("dragging");
  });
  main.addEventListener("drop", (event) => {
    if (!event.dataTransfer || !event.dataTransfer.files || !event.dataTransfer.files.length) return;
    event.preventDefault();
    importBox.classList.remove("dragging");
    load(event.dataTransfer.files[0]);
  });

  showChoice();
  listLabels();
  input.focus();
}

export function update() {}

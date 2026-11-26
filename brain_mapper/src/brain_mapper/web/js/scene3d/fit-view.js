// Headset-fit view: the glass head with the see-through brain and the headset on it.
//
// It starts from behind and above, zoomed so the electrodes fill the view, and can be turned
// freely. Clicking a contact, its name on the head, or (from outside, with focus(name)) its
// status pill centres that contact and brings it toward the viewer.
import * as THREE from "../../vendor/three/build/three.module.js";
import { createBrain } from "./brain.js";
import { createHead } from "./head.js";
import { createHeadset, STATE_WORDS } from "./headset.js";
import { createStage, HOME, START_DIRECTION } from "./stage.js";

export const START_DISTANCE = 480;
const FOCUS_DISTANCE = 300;
const HINT = "Drag to turn. Pinch or scroll to zoom. Tap a contact or its name to bring it forward.";

/** Headset coordinates (RAS, z up) to scene coordinates (y up). */
const toScene = (v) => new THREE.Vector3(v.x, v.z, -v.y);

export function createFitView(container, meshes) {
  const stage = createStage(container);
  if (!stage) {
    const none = () => {};
    return { unavailable: true, setStates: none, focus: none, reset: none, setSample: none, dispose: none };
  }
  const head = createHead(stage, meshes);
  const brain = createBrain(stage, meshes, []);
  head.setMix(0);
  brain.setMix(0);
  const headset = createHeadset(stage, meshes.electrodes, meshes.lobes);
  stage.place(START_DIRECTION, START_DISTANCE);

  // layers over the canvas: contact names, a hint, the sample-data badge
  const labels = document.createElement("div");
  labels.className = "labels";
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = HINT;
  const badge = document.createElement("div");
  badge.className = "sample-badge";
  badge.textContent = "Sample data";
  badge.hidden = true;
  container.append(labels, hint, badge);

  const byName = Object.fromEntries(headset.contacts.map((c) => [c.name, c]));
  const buttons = {};
  headset.contacts.forEach((c) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = c.name;
    b.addEventListener("click", () => focus(c.name));
    labels.appendChild(b);
    buttons[c.name] = b;
  });

  let anim = null; // the camera move in progress
  let focused = null;
  function moveTo(next) {
    anim = next;
    if (stage.reducedMotion) step(1);
  }
  function focus(name) {
    const c = byName[name];
    if (!c) throw new Error(`unknown contact ${name}`);
    focused = c;
    headset.setFocused(name);
    const sceneN = toScene(c.normal).normalize();
    moveTo({ dir: sceneN, target: toScene(c.position).lerp(HOME, 0.25), dist: FOCUS_DISTANCE });
    const state = headset.state(name);
    hint.textContent = `${name}: ${STATE_WORDS[state]}${state === "marginal" || state === "bad" ? ". Wiggle it gently until it turns green." : "."}`;
  }
  function reset() {
    focused = null;
    headset.setFocused(null);
    moveTo({ dir: START_DIRECTION, target: HOME, dist: START_DISTANCE });
    hint.textContent = HINT;
  }

  function step(k) {
    const { camera, controls } = stage;
    const off = camera.position.clone().sub(controls.target);
    const dNow = off.length();
    off.normalize().lerp(anim.dir, k).normalize();
    controls.target.lerp(anim.target, k);
    camera.position.copy(controls.target).addScaledVector(off, dNow + (anim.dist - dNow) * k);
    if (off.angleTo(anim.dir) < 0.01 && Math.abs(dNow - anim.dist) < 2) anim = null;
    else if (k >= 1) anim = null;
  }
  stage.onFrame((dt) => {
    if (anim) step(Math.min(1, dt * 4));
  });

  // The hint wraps to as many lines as the width needs, so its real top edge sets how much of
  // the stage's foot is kept free; a name is hidden once any part of it would reach it. The box
  // is measured when the hint or the stage changes size, not on every frame. A hint that is not
  // shown (display: none) keeps nothing free.
  let hintTop = Infinity;
  let half = 0;
  function measure() {
    const box = hint.getBoundingClientRect();
    hintTop = box.height > 0 ? box.top - container.getBoundingClientRect().top : Infinity;
    half = labels.firstElementChild ? labels.firstElementChild.offsetHeight / 2 : 0;
  }
  const watcher = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
  if (watcher) {
    watcher.observe(hint);
    watcher.observe(container);
    if (labels.firstElementChild) watcher.observe(labels.firstElementChild); // its height changes when the font loads
  }
  measure();

  // contact names, only on the side facing the viewer
  const wp = new THREE.Vector3();
  const wn = new THREE.Vector3();
  const camDir = new THREE.Vector3();
  stage.onAfterRender(() => {
    const w = container.clientWidth;
    const h = container.clientHeight;
    // `hintTop` and `half` are measured when the hint or the stage changes size (see below).
    stage.camera.getWorldDirection(camDir);
    headset.contacts.forEach((c) => {
      wp.copy(c.top).addScaledVector(c.normal, 12).applyMatrix4(stage.root.matrixWorld);
      wn.copy(c.normal).transformDirection(stage.root.matrixWorld);
      const facing = -wn.dot(camDir);
      wp.project(stage.camera);
      // a name is hidden while it would touch the hint at the foot of the stage
      const y = ((1 - wp.y) / 2) * h;
      const clear = y + half <= hintTop ? 1 : 0;
      const lo = Math.max(0, Math.min(1, (facing - 0.15) * 4)) * clear;
      const b = buttons[c.name];
      b.style.opacity = lo;
      b.style.pointerEvents = lo > 0.4 ? "auto" : "none";
      b.style.left = `${((wp.x + 1) / 2) * w}px`;
      b.style.top = `${y}px`;
    });
  });

  // tapping a disc; dragging to turn takes the camera back from any move in progress
  const ray = new THREE.Raycaster();
  const ptr = new THREE.Vector2();
  const canvas = stage.renderer.domElement;
  let down = null;
  const onDown = (e) => {
    down = [e.clientX, e.clientY];
    anim = null;
  };
  const onUp = (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 7) {
      down = null;
      return;
    }
    down = null;
    const r = canvas.getBoundingClientRect();
    ptr.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ptr, stage.camera);
    const hit = ray.intersectObjects(allDiscs())[0];
    if (hit) focus(hit.object.userData.contact);
  };
  function allDiscs() {
    const out = [];
    headset.group.traverse((o) => {
      if (o.userData && o.userData.part === "disc") out.push(o);
    });
    return out;
  }
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointerup", onUp);

  return {
    stage,
    head,
    brain,
    headset,
    setStates: (states) => headset.setStates(states),
    focus,
    reset,
    /** Shows or hides the "Sample data" badge. */
    setSample(on) {
      badge.hidden = !on;
    },
    /** The contact brought forward, or null. */
    focused: () => (focused ? focused.name : null),
    /** True when no camera move is in progress. */
    settled: () => anim === null,
    /** Degrees between the camera's direction from the target and the contact's outward normal. */
    angleToContact(name) {
      const c = byName[name];
      const off = stage.camera.position.clone().sub(stage.controls.target);
      return THREE.MathUtils.radToDeg(off.angleTo(toScene(c.normal)));
    },
    /** Where the contact's disc is on the canvas, in CSS pixels from its top left corner. */
    screenPosition(name) {
      const c = byName[name];
      stage.scene.updateMatrixWorld();
      const v = c.disc.getWorldPosition(new THREE.Vector3()).project(stage.camera);
      return [((v.x + 1) / 2) * container.clientWidth, ((1 - v.y) / 2) * container.clientHeight];
    },
    dispose() {
      if (watcher) watcher.disconnect();
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      labels.remove();
      hint.remove();
      badge.remove();
      stage.dispose();
    },
  };
}

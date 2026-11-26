// The 3-D stage: renderer, scene, camera, orbit controls and the frame loop.
//
// createStage(container) -> { scene, root, camera, controls, renderer, onFrame(fn),
//   onAfterRender(fn), dispose(), reducedMotion, ... } or null when WebGL is not
// available (a text message is then shown in the container).
//
// `root` is turned so that its children are in RAS millimetres (x right, y front,
// z up), the space of meshes.json. The scene itself has y up.
import * as THREE from "../../vendor/three/build/three.module.js";
import { OrbitControls } from "../../vendor/three/examples/jsm/controls/OrbitControls.js";

/** The point the cameras look at: the middle of the head, a little forward of the brain's centre. */
export const HOME = new THREE.Vector3(0, 30, 18);

/** Where the cameras start, as a direction from the target in scene coordinates: from behind and above. */
export const START_DIRECTION = new THREE.Vector3(0, 350, 197).normalize();

export function showMessage(container, text) {
  container.classList.add("scene3d");
  const note = document.createElement("div");
  note.className = "scene-message";
  note.setAttribute("role", "status");
  note.textContent = text;
  container.appendChild(note);
  return note;
}

/** A soft round sprite texture, shared by the glow of contacts and nodes. */
function makeGlowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const x = c.getContext("2d");
  const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.25, "rgba(255,255,255,0.55)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  x.fillStyle = g;
  x.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

export function createStage(container) {
  container.classList.add("scene3d");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch (error) {
    showMessage(container, "This device cannot show the 3-D view (WebGL is off).");
    return null;
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  container.insertBefore(renderer.domElement, container.firstChild);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 10, 4000);
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2; // RAS mm, up = superior
  scene.add(root);
  scene.add(new THREE.AmbientLight(0xffffff, 0.75));
  const key = new THREE.DirectionalLight(0xffffff, 0.7);
  key.position.set(-200, 400, -300);
  scene.add(key);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(HOME);
  controls.enablePan = false;
  controls.enableDamping = !motion.matches; // damping keeps the camera gliding after a drag
  controls.minDistance = 200;
  controls.maxDistance = 1500;
  controls.autoRotate = false;

  const glowTexture = makeGlowTexture();
  const before = [];
  const after = [];
  const clock = new THREE.Clock();
  let frames = 0;
  let handle = 0;
  let alive = true;

  function fit() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(fit);
  observer.observe(container);
  fit();

  function loop() {
    if (!alive) return;
    // The next frame is asked for first: a frame that throws (three.js reads a null shader log
    // when it compiles while the WebGL context is being restored) is reported, and the view goes
    // on with the next frame instead of stopping for good.
    handle = requestAnimationFrame(loop);
    const dt = Math.min(clock.getDelta(), 0.05);
    const t = clock.elapsedTime;
    controls.enableDamping = !motion.matches; // follows a change of the setting at once
    controls.update();
    for (const fn of before) fn(dt, t);
    scene.updateMatrixWorld();
    camera.updateMatrixWorld();
    renderer.render(scene, camera);
    for (const fn of after) fn(dt, t);
    frames += 1;
  }
  handle = requestAnimationFrame(loop);

  return {
    scene,
    root,
    camera,
    controls,
    renderer,
    container,
    /** True while the viewer asks for reduced motion; follows changes of the setting. */
    get reducedMotion() {
      return motion.matches;
    },
    glowTexture,
    /** Calls fn(dt, t) every frame, before the scene is drawn. */
    onFrame(fn) {
      before.push(fn);
    },
    /** Calls fn(dt, t) every frame, after the scene is drawn (matrices are current). */
    onAfterRender(fn) {
      after.push(fn);
    },
    /** Frames drawn so far. */
    frames() {
      return frames;
    },
    /** Puts the camera `distance` mm from the target along `direction` (scene coordinates). */
    place(direction, distance, target = HOME) {
      controls.target.copy(target);
      camera.position.copy(direction).normalize().multiplyScalar(distance).add(target);
      controls.update();
    },
    dispose() {
      alive = false;
      cancelAnimationFrame(handle);
      observer.disconnect();
      controls.dispose();
      glowTexture.dispose();
      scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) o.material.dispose();
      });
      renderer.dispose();
      renderer.forceContextLoss(); // give the browser its WebGL context back
      renderer.domElement.remove();
    },
  };
}

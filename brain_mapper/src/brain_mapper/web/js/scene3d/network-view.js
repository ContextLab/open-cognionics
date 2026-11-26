// Live-network view: the head faded back, the brain more solid, nodes inside it and the links
// between them drawn as bundles of thin fibres carrying a soft, slowly moving light.
//
// createNetworkView(container, meshes, { onSelect }) ->
//   { setNetwork({ nodes: [[x,y,z]], communities: number[], edges: [[i, j, weight]] }), select(i | -1), dispose() }
// A community of -1 means the node is in no group: it and its links are drawn in one neutral color.
// Weights run from 0 to 1; links with a weight below DISPLAY_THRESHOLD are not drawn.
// With reduced motion the light on the fibres and the breathing of the nodes are still.
import * as THREE from "../../vendor/three/build/three.module.js";
import { createBrain } from "./brain.js";
import { createHead } from "./head.js";
import { createStage, START_DIRECTION } from "./stage.js";
import { makeFibres, nodeSphere } from "./network-parts.js";

/** Links with a weight below this are not drawn. */
export const DISPLAY_THRESHOLD = 0.405;
export const NETWORK_DISTANCE = 500;
const HINT = "Drag to turn. Pinch or scroll to zoom. Tap a region to see its links.";
const PALETTE = ["#7FA0FF", "#F2B33D", "#3ED6C8", "#FF8A5C", "#C792FF"].map((c) => new THREE.Color(c));
const GREY = new THREE.Color("#8FA0CC");
const colorOf = (community) => (community < 0 ? GREY : PALETTE[community % PALETTE.length]);
const CORE = new THREE.Vector3(0, -18, 22);

const hash = (i, j) => {
  const s = Math.sin(i * 127.1 + j * 311.7) * 43758.5453;
  return s - Math.floor(s);
};
export function createNetworkView(container, meshes, options = {}) {
  const stage = createStage(container);
  if (!stage) {
    const none = () => {};
    return { unavailable: true, setNetwork: none, select: none, setSample: none, dispose: none };
  }
  const head = createHead(stage, meshes);
  const brain = createBrain(stage, meshes, []);
  head.setMix(1);
  brain.setMix(1);
  stage.place(START_DIRECTION, NETWORK_DISTANCE);

  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = HINT;
  const badge = document.createElement("div");
  badge.className = "sample-badge";
  badge.textContent = "Sample data";
  badge.hidden = true;
  container.append(hint, badge);

  const net = new THREE.Group();
  stage.root.add(net);
  const sphere = nodeSphere();

  let P = [];
  let assign = [];
  let nodeCol = [];
  let nodes = [];
  let links = [];
  let selected = -1;
  const tmp = new THREE.Color();

  function clearNet() {
    net.children.slice().forEach((o) => {
      net.remove(o);
      if (o.material) o.material.dispose();
      if (o.geometry && o.geometry !== sphere) o.geometry.dispose();
    });
    nodes = [];
    links = [];
  }

  function setNetwork(data) {
    const kept = nodeCol.length === data.nodes.length ? nodeCol : null;
    clearNet();
    selected = -1;
    P = data.nodes.map((a) => new THREE.Vector3(a[0], a[1], a[2]));
    assign = data.communities.slice();
    if (assign.length !== P.length) throw new Error("communities needs one entry per node");
    nodeCol = P.map((_, i) => (kept ? kept[i] : colorOf(assign[i]).clone()));
    brain.setNodes(data.nodes);
    nodes = P.map((p, i) => {
      const m = new THREE.Mesh(
        sphere,
        new THREE.MeshPhongMaterial({
          color: nodeCol[i],
          emissive: nodeCol[i],
          emissiveIntensity: 0.22,
          shininess: 110,
          transparent: true,
        }),
      );
      m.position.copy(p);
      m.scale.setScalar(4.5);
      m.userData = { part: "node", i };
      m.renderOrder = 9;
      m.material.depthTest = false;
      net.add(m);
      const s = new THREE.Sprite(
        new THREE.SpriteMaterial({
          map: stage.glowTexture,
          color: nodeCol[i],
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          transparent: true,
          opacity: 0.55,
        }),
      );
      s.position.copy(p);
      s.scale.setScalar(26);
      s.renderOrder = 7;
      s.material.depthTest = false;
      net.add(s);
      m.userData.glow = s;
      return m;
    });
    const shown = data.edges.filter(([i, j, w]) => w >= DISPLAY_THRESHOLD && i !== j && P[i] && P[j]);
    const top = Math.max(...shown.map((e) => e[2]), DISPLAY_THRESHOLD + 1e-6);
    links = shown.map(([i, j, w]) => {
      const mid = P[i].clone().add(P[j]).multiplyScalar(0.5);
      const cross = P[i].x * P[j].x < 0;
      const ctrl = cross
        ? mid.clone().lerp(new THREE.Vector3(0, mid.y * 0.6 - 6, Math.max(mid.z, 18) + 6), 0.75)
        : mid.clone().lerp(CORE, 0.3);
      const e = {
        i,
        j,
        w,
        h: hash(i, j),
        ctrl,
        n: (w - DISPLAY_THRESHOLD) / (top - DISPLAY_THRESHOLD + 1e-6),
        show: 0,
      };
      e.line = makeFibres(e, P);
      net.add(e.line);
      return e;
    });
    notify();
  }

  function wanted(e) {
    return selected < 0 || e.i === selected || e.j === selected ? 1 : 0.06;
  }
  function describe() {
    if (selected < 0) return null;
    return {
      node: selected,
      links: links.filter((e) => e.i === selected || e.j === selected).length,
      group: assign.filter((a) => a === assign[selected]).length,
    };
  }
  function notify() {
    if (options.onSelect) options.onSelect(selected, describe());
  }
  function select(i) {
    selected = i >= 0 && i < P.length ? i : -1;
    hint.textContent = HINT;
    notify();
  }

  stage.onFrame((dt, t) => {
    const reduce = stage.reducedMotion; // read every frame: the setting can change while the page is open
    const k = reduce ? 1 : Math.min(1, dt * 4);
    const clock = reduce ? 0 : t;
    const deg = new Array(P.length).fill(0);
    for (const e of links) {
      e.show += (wanted(e) - e.show) * k;
      const same = assign[e.i] === assign[e.j];
      const u = e.line.material.uniforms;
      e.line.visible = e.show > 0.01;
      tmp.copy(same ? nodeCol[e.i] : GREY);
      u.uCol.value.lerp(tmp, reduce ? 1 : 0.1);
      u.uT.value = clock;
      u.uOp.value = e.show * (same ? 0.8 : 0.32) * (0.35 + 0.65 * e.n);
      deg[e.i]++;
      deg[e.j]++;
    }
    nodes.forEach((m, i) => {
      nodeCol[i].lerp(colorOf(assign[i]), reduce ? 1 : Math.min(1, dt * 3));
      m.material.color.copy(nodeCol[i]);
      m.material.emissive.copy(nodeCol[i]);
      brain.nodeColors[i].copy(nodeCol[i]);
      const linked = links.some((e) => (e.i === selected && e.j === i) || (e.j === selected && e.i === i));
      const dim = selected >= 0 && selected !== i && !linked ? 0.25 : 1;
      const s = (2.8 + 0.3 * deg[i]) * (1 + 0.05 * (reduce ? 0 : Math.sin(clock * 3 + i)));
      m.scale.setScalar(s);
      m.material.opacity = dim;
      m.userData.glow.material.color.copy(nodeCol[i]);
      m.userData.glow.material.opacity = 0.14 * dim;
      m.userData.glow.scale.setScalar(s * 3.2);
    });
  });

  // tapping a region shows only its links; tapping empty space shows everything again
  const ray = new THREE.Raycaster();
  const ptr = new THREE.Vector2();
  const canvas = stage.renderer.domElement;
  let down = null;
  const onDown = (e) => {
    down = [e.clientX, e.clientY];
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
    const hit = ray.intersectObjects(nodes)[0];
    select(hit ? hit.object.userData.i : -1);
  };
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointerup", onUp);

  return {
    stage,
    head,
    brain,
    displayThreshold: DISPLAY_THRESHOLD,
    setNetwork,
    select,
    selected: () => selected,
    /** Shows or hides the "Sample data" badge. */
    setSample(on) {
      badge.hidden = !on;
    },
    /** What is drawn: the number of nodes and of links. */
    counts: () => ({ nodes: nodes.length, links: links.length }),
    /** Links shown at full strength (all of them, or those of the selected node). */
    litLinks: () => links.filter((e) => e.show > 0.9).length,
    /** The clock of every link's moving light; constant with reduced motion. */
    pulseUniforms: () => links.map((e) => e.line.material.uniforms.uT.value),
    /** True when every link has reached the strength it should have. */
    settled: () => links.length > 0 && links.every((e) => Math.abs(e.show - wanted(e)) < 0.01),
    dispose() {
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      hint.remove();
      badge.remove();
      stage.dispose();
    },
  };
}

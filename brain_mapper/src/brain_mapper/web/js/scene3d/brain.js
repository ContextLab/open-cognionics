// The brain: a glass surface tinted near each node by that node's group colour.
//
// In the network view the brain is mostly solid but writes no depth, so the far wall of a
// fold could be painted over the near surface (triangles are drawn in file order) and read as
// a tear. A depth-only copy, drawn just before the brain and visible only when the network mix
// is above one half, makes it one continuous surface.
import * as THREE from "../../vendor/three/build/three.module.js";
import { meshGeometry } from "./mesh-data.js";

export const MAX_NODES = 64;
const SIGMA = 22; // mm: how far a node's colour reaches over the surface

const VERTEX =
  "attribute float aNode; attribute float aW; uniform vec3 uNodeCol[" +
  MAX_NODES +
  "]; varying vec3 vN; varying vec3 vV; varying vec3 vC; varying float vW; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); vC = uNodeCol[int(aNode+0.5)]; vW = aW; gl_Position = projectionMatrix*mv; }";
const FRAGMENT =
  "uniform float uNet; varying vec3 vN; varying vec3 vV; varying vec3 vC; varying float vW; void main(){ vec3 n = normalize(vN); float ndv = abs(dot(n, normalize(vV))); float f = pow(1.0-ndv, 2.2); float d1 = max(dot(n, normalize(vec3(-0.45,0.65,0.6))), 0.0); float d2 = max(dot(n, normalize(vec3(0.7,0.2,0.4))), 0.0); float shade = 0.16 + 0.78*d1 + 0.2*d2; float sp = pow(d1, 18.0)*0.25; vec3 base = vec3(0.62,0.64,0.74); vec3 c = mix(base, mix(base, vC, 0.72), min(1.0, vW*1.3)*uNet); vec3 col = c*shade + vec3(sp) + vec3(0.45,0.55,0.85)*f*0.25; float a = mix(0.16 + 0.35*f, 0.62 + 0.3*f, uNet); gl_FragColor = vec4(col, a); }";

/** `nodes` are [x, y, z] positions in mm; the tint reaches the vertices near them. */
export function createBrain(stage, meshes, nodes = []) {
  const geometry = meshGeometry(meshes, meshes.brain, false);
  const nodeColors = Array.from({ length: MAX_NODES }, () => new THREE.Color(0x7fa0ff));
  const material = new THREE.ShaderMaterial({
    uniforms: { uNodeCol: { value: nodeColors }, uNet: { value: 0 } },
    vertexShader: VERTEX,
    fragmentShader: FRAGMENT,
    transparent: true,
    depthWrite: false,
    side: THREE.FrontSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = 4;
  stage.root.add(mesh);

  const depth = new THREE.Mesh(
    geometry,
    new THREE.MeshBasicMaterial({
      colorWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: 1,
      polygonOffsetUnits: 1,
    }),
  );
  depth.renderOrder = 3.9;
  depth.visible = false;
  stage.root.add(depth);

  function setNodes(positions) {
    if (positions.length > MAX_NODES) throw new Error(`at most ${MAX_NODES} nodes`);
    const pos = geometry.attributes.position.array;
    const nv = pos.length / 3;
    const idx = new Float32Array(nv);
    const w = new Float32Array(nv);
    for (let v = 0; v < nv; v++) {
      let best = 0;
      let bd = 1e9;
      const x = pos[3 * v];
      const y = pos[3 * v + 1];
      const z = pos[3 * v + 2];
      for (let i = 0; i < positions.length; i++) {
        const dx = positions[i][0] - x;
        const dy = positions[i][1] - y;
        const dz = positions[i][2] - z;
        const d = dx * dx + dy * dy + dz * dz;
        if (d < bd) {
          bd = d;
          best = i;
        }
      }
      idx[v] = best;
      w[v] = positions.length ? Math.exp(-bd / (2 * SIGMA * SIGMA)) : 0;
    }
    geometry.setAttribute("aNode", new THREE.BufferAttribute(idx, 1));
    geometry.setAttribute("aW", new THREE.BufferAttribute(w, 1));
  }
  setNodes(nodes);

  return {
    mesh,
    depth,
    material,
    /** The colours of the nodes (the first N entries are used); change them in place. */
    nodeColors,
    setNodes,
    /** mix 0 is the fit view (see-through), mix 1 the network view (mostly solid). */
    setMix(mix) {
      material.uniforms.uNet.value = mix;
      depth.visible = mix > 0.5;
    },
  };
}

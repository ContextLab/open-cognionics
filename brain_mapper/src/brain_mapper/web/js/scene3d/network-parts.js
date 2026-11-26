// The meshes of the network view: the sphere of a node and the fibre bundle of a link.
import * as THREE from "../../vendor/three/build/three.module.js";

const STRANDS = 9;
const SEGMENTS = 22;

const FIB_V =
  "attribute float aU; attribute float aPh; varying float vU; varying float vPh; void main(){ vU = aU; vPh = aPh; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }";
const FIB_F =
  "uniform vec3 uCol; uniform float uOp,uT,uSp; varying float vU; varying float vPh; void main(){ float x = fract(vU*0.9 - uT*uSp + vPh); float p = exp(-pow((x-0.5)/0.13, 2.0)); float ends = smoothstep(0.0,0.08,vU)*smoothstep(1.0,0.92,vU); float b = 0.30 + 0.85*p*ends; gl_FragColor = vec4((uCol*b + vec3(0.22)*p*ends)*uOp, 1.0); }";

export function nodeSphere() {
  return new THREE.SphereGeometry(1, 20, 14);
}

/** A small deterministic stream of numbers in [-0.5, 0.5), seeded by a number in [0, 1). */
function stream(seed) {
  let x = 1 + Math.floor(seed * 2147483645);
  return () => {
    x = (x * 16807) % 2147483647;
    return x / 2147483647 - 0.5;
  };
}

function bezier(A, C, B, u, out) {
  return out.set(0, 0, 0).addScaledVector(A, (1 - u) * (1 - u)).addScaledVector(C, 2 * u * (1 - u)).addScaledVector(B, u * u);
}

/**
 * A bundle of thin curved strands between nodes e.i and e.j of P, bending toward e.ctrl.
 * `e.h` in [0, 1) fixes the strands and the direction of the moving light. The uniforms
 * `uOp` (opacity), `uT` (the clock of the light) and `uCol` are set by the caller.
 */
export function makeFibres(e, P) {
  const nv = STRANDS * SEGMENTS * 2;
  const arr = new Float32Array(nv * 3);
  const uu = new Float32Array(nv);
  const ph = new Float32Array(nv);
  const A = new THREE.Vector3();
  const B = new THREE.Vector3();
  const C = new THREE.Vector3();
  const q0 = new THREE.Vector3();
  const q1 = new THREE.Vector3();
  const rnd = stream(e.h);
  let k = 0;
  let m = 0;
  for (let s = 0; s < STRANDS; s++) {
    A.copy(P[e.i]).add(q0.set(rnd(), rnd(), rnd()).multiplyScalar(5));
    B.copy(P[e.j]).add(q0.set(rnd(), rnd(), rnd()).multiplyScalar(5));
    C.copy(e.ctrl).add(q0.set(rnd(), rnd(), rnd()).multiplyScalar(14));
    const phase = e.h * 7.0 + rnd() * 0.16;
    for (let sg = 0; sg < SEGMENTS; sg++) {
      bezier(A, C, B, sg / SEGMENTS, q0);
      bezier(A, C, B, (sg + 1) / SEGMENTS, q1);
      arr[k++] = q0.x;
      arr[k++] = q0.y;
      arr[k++] = q0.z;
      arr[k++] = q1.x;
      arr[k++] = q1.y;
      arr[k++] = q1.z;
      uu[m] = sg / SEGMENTS;
      ph[m++] = phase;
      uu[m] = (sg + 1) / SEGMENTS;
      ph[m++] = phase;
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(arr, 3));
  g.setAttribute("aU", new THREE.BufferAttribute(uu, 1));
  g.setAttribute("aPh", new THREE.BufferAttribute(ph, 1));
  const line = new THREE.LineSegments(
    g,
    new THREE.ShaderMaterial({
      uniforms: {
        uCol: { value: new THREE.Color(0x8fa0cc) },
        uOp: { value: 0 },
        uT: { value: 0 },
        uSp: { value: (e.h < 0.5 ? -1 : 1) * (0.16 + 0.12 * e.h) },
      },
      vertexShader: FIB_V,
      fragmentShader: FIB_F,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    }),
  );
  line.frustumCulled = false;
  line.renderOrder = 8;
  line.visible = false;
  line.userData = { part: "link", i: e.i, j: e.j };
  return line;
}

// Decoding of meshes.json: base64 int16 vertices (divided by `scale`) and uint16 or uint32 faces.
import * as THREE from "../../vendor/three/build/three.module.js";

export async function loadJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}

function bytes(b64) {
  const s = atob(b64);
  const u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return u.buffer;
}

function vertices(b64, scale) {
  const a = new Int16Array(bytes(b64));
  const f = new Float32Array(a.length);
  for (let i = 0; i < a.length; i++) f[i] = a[i] / scale;
  return f;
}

/** A BufferGeometry from one mesh of meshes.json ({v, f} and, for the head, `wide`). */
export function meshGeometry(meshes, mesh, wide = false) {
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(vertices(mesh.v, meshes.scale), 3));
  const index = wide ? new Uint32Array(bytes(mesh.f)) : new Uint16Array(bytes(mesh.f));
  g.setIndex(new THREE.BufferAttribute(index, 1));
  g.computeVertexNormals();
  return g;
}

// The head: a hollow frosted-glass shell (inner surfaces are already removed in meshes.json).
import * as THREE from "../../vendor/three/build/three.module.js";
import { headMaterial } from "./glass.js";
import { meshGeometry } from "./mesh-data.js";

export function createHead(stage, meshes) {
  const material = headMaterial();
  const mesh = new THREE.Mesh(meshGeometry(meshes, meshes.head, meshes.head.wide), material);
  mesh.renderOrder = 5;
  stage.root.add(mesh);
  return {
    mesh,
    material,
    /** mix 0 is the fit view (full head), mix 1 the network view (faded back to 18 %). */
    setMix(mix) {
      material.uniforms.uFade.value = 1 - 0.82 * mix;
    },
  };
}

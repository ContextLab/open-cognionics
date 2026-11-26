// The two glass materials of the prototype: the frosted head and the black glass of the headset.
import * as THREE from "../../vendor/three/build/three.module.js";

/** Frosted-glass head: soft form shading, bright at grazing angles, the middle stays clear. `uFade` scales it. */
export function headMaterial() {
  return new THREE.ShaderMaterial({
    uniforms: { uFade: { value: 1 } },
    vertexShader:
      "varying vec3 vN; varying vec3 vV; varying float vZ; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); vZ = position.z; gl_Position = projectionMatrix*mv; }",
    fragmentShader:
      "uniform float uFade; varying vec3 vN; varying vec3 vV; varying float vZ; void main(){ vec3 n = normalize(vN); float ndv = abs(dot(n, normalize(vV))); float f = pow(1.0-ndv, 2.4); float d1 = max(dot(n, normalize(vec3(-0.55,0.55,0.62))), 0.0); float d2 = max(dot(n, normalize(vec3(0.7,0.1,0.5))), 0.0); float shade = 0.10 + 0.75*d1*d1 + 0.22*d2; vec3 col = mix(vec3(0.30,0.40,0.72), vec3(0.80,0.88,1.0), shade) * (0.35 + 0.9*shade) + vec3(0.55,0.68,1.0)*f*0.9; float cut = smoothstep(-215.0, -110.0, vZ); float a = (0.13 + 0.16*shade + 0.62*f) * uFade * cut; gl_FragColor = vec4(col, a); }",
    transparent: true,
    depthWrite: false,
    side: THREE.FrontSide,
  });
}

/** Black glass: dark body, cool rim light, a little see-through (`alpha` is the body's opacity). */
export function frameMaterial(alpha = 0.5) {
  return new THREE.ShaderMaterial({
    uniforms: { uFade: { value: 1 }, uA: { value: alpha } },
    vertexShader:
      "varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.0); vN = normalize(normalMatrix*normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix*mv; }",
    fragmentShader:
      "uniform float uFade,uA; varying vec3 vN; varying vec3 vV; void main(){ vec3 n = normalize(vN); float ndv = abs(dot(n, normalize(vV))); float f = pow(1.0-ndv, 2.6); float sp = pow(max(dot(n, normalize(vec3(-0.4,0.7,0.6))), 0.0), 24.0); vec3 col = vec3(0.012,0.016,0.03) + vec3(0.42,0.52,0.8)*f*0.55 + vec3(0.8,0.88,1.0)*sp*0.35; gl_FragColor = vec4(col, (uA + (1.0-uA)*0.8*f + sp*0.3) * uFade); }",
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
}

// The headset in a scene: materials, contact states and fading. The shapes come from
// headset-geometry.js; nothing here knows what the headset looks like.
import * as THREE from "../../vendor/three/build/three.module.js";
import { frameMaterial } from "./glass.js";
import { buildHeadset } from "./headset-geometry.js";

/** Disc colours by contact state; `unknown` is shown until a state is given. */
export const STATE_COLORS = {
  good: "#3ED6C8",
  marginal: "#F2B33D",
  bad: "#FF5C6C",
  unknown: "#9AA6C4",
};
export const STATE_WORDS = { good: "good", marginal: "weak", bad: "no contact", unknown: "not measured" };

function makeMaterials(stage) {
  return {
    frame(alpha) {
      return frameMaterial(alpha);
    },
    contact() {
      return new THREE.MeshPhongMaterial({ transparent: true, shininess: 80 });
    },
    grey() {
      const m = new THREE.MeshPhongMaterial({ color: 0x9aa6c4, transparent: true, shininess: 80 });
      m.userData.baseOpacity = 1;
      return m;
    },
    tip() {
      const m = new THREE.MeshBasicMaterial({ color: 0xb9c2d8, transparent: true, opacity: 0.28, depthWrite: false });
      m.userData.baseOpacity = 0.28;
      return m;
    },
    light(color) {
      const m = new THREE.MeshBasicMaterial({ color, transparent: true });
      m.userData.baseOpacity = 1;
      return m;
    },
    glow() {
      return new THREE.SpriteMaterial({
        map: stage.glowTexture,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        transparent: true,
        opacity: 0.5,
      });
    },
  };
}

/**
 * `electrodes` and `lobes` are the lists of meshes.json. The returned object has
 * `contacts` (see headset-geometry.js), `extras`, `group`, `setStates`, `setFade`, `state`
 * and `setFocused`.
 */
export function createHeadset(stage, electrodes, lobes) {
  const built = buildHeadset(THREE, electrodes, makeMaterials(stage), lobes);
  stage.root.add(built.group);
  const colors = Object.fromEntries(Object.entries(STATE_COLORS).map(([k, v]) => [k, new THREE.Color(v)]));
  const states = Object.fromEntries(built.contacts.map((c) => [c.name, "unknown"]));
  const byName = Object.fromEntries(built.contacts.map((c) => [c.name, c]));
  let fade = 1;
  let focused = null;

  function paint(c) {
    const col = colors[states[c.name]];
    c.disc.material.color.copy(col);
    c.disc.material.emissive.copy(col);
    c.disc.material.emissiveIntensity = 0.6;
    c.glow.material.color.copy(col);
  }
  built.contacts.forEach(paint);

  function applyFade() {
    built.group.visible = fade > 0.03;
    for (const m of built.glass) {
      if (m.uniforms && m.uniforms.uFade) m.uniforms.uFade.value = fade;
      else m.opacity = (m.userData.baseOpacity ?? 1) * fade;
    }
    built.contacts.forEach((c) => {
      c.disc.material.opacity = fade;
    });
  }

  // contact glow: steady for a good contact, a slow pulse for one that needs attention
  stage.onFrame((_, t) => {
    built.contacts.forEach((c) => {
      const s = states[c.name];
      const pulse = s === "marginal" || s === "bad" ? 0.55 + (stage.reducedMotion ? 0.12 : 0.25 * Math.sin(t * 4 + c.position.x)) : 0.3;
      c.glow.material.opacity = pulse * fade;
      c.glow.scale.setScalar(c === focused ? 46 : 30);
    });
  });
  applyFade();

  return {
    group: built.group,
    contacts: built.contacts,
    extras: built.extras,
    glass: built.glass,
    /** Colours the discs: { F7: "bad" }. Contacts not named keep their state. */
    setStates(next) {
      const known = Object.entries(next).filter(([name]) => {
        if (byName[name]) return true;
        console.warn(`setStates: the headset has no contact ${name}; ignored`);
        return false;
      });
      for (const [name, state] of known) {
        if (!(state in STATE_COLORS) || state === "unknown") throw new Error(`unknown contact state ${state} for ${name}`);
      }
      for (const [name, state] of known) {
        states[name] = state;
        paint(byName[name]);
      }
    },
    state(name) {
      return states[name];
    },
    /** 1 is the headset in full, 0 hides it (the network view). */
    setFade(x) {
      fade = x;
      applyFade();
    },
    /** The contact drawn larger (the one brought forward), or null. */
    setFocused(name) {
      focused = name ? byName[name] : null;
    },
  };
}

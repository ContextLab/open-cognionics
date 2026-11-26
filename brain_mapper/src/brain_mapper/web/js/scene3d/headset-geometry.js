// Headset geometry: the ONLY module that knows what the headset looks like.
//
// Contract
//   buildHeadset(THREE, electrodes, materials, lobes) -> { group, contacts, glass, extras }
//
//   electrodes  the list from meshes.json: { name, p: [x, y, z], n: [x, y, z] } in RAS mm, one
//               entry per recorded EEG label, in the order the labels are recorded.
//   materials   supplies the materials; this module never creates one:
//                 frame(alpha)  black glass (an opacity factor in `uFade`, or a base opacity)
//                 contact()     the opaque contact disc material (colour set by the caller)
//                 grey()        the disc of the left ear clip (no channel is recorded for it)
//                 tip()         the sensor tips, one shared material
//                 light(color)  an indicator light or the button on the box
//                 glow()        the soft sprite behind a contact disc
//   lobes       optional { A1: { p, n }, A2: { p, n } }, the bottom of each earlobe; without
//               it the left clip mirrors the right one.
//
//   group       THREE.Group holding every part, in RAS mm (add it to the stage root).
//   contacts    one { name, disc, glow, position, normal, top } per entry of `electrodes`, in
//               that order. `disc` is the mesh whose material colour shows the state (a forehead
//               pad has a second disc that shares the same material); `top` is where a label
//               goes. The left ear clip is not among them: it is in `extras`.
//   glass       every material of the headset that is not a contact disc or glow, so the caller
//               can fade them (`uniforms.uFade`, else `opacity` times `userData.baseOpacity`).
//   extras      [{ name, object }] for the other parts: the electronics box, its lights and
//               button, straps, side bands, leads, ear-clip hardware and the left clip's disc.
//
// The shape is a Cognionics Quick-20, modelled by eye: a rounded oblong box on the
// crown with two lights and one power button, thin flat straps looping out and down to teardrop
// pods, eight faint sensor tips around each scalp contact, two smooth contact discs on each
// forehead pad, a broad band down each side, and a lead from it to an alligator clip hanging
// from the bottom of each earlobe. There are no side housings or cushions.
//
// This file is to be replaced by a model made from photographs of the real headset. Nothing else
// reads headset dimensions or creates headset meshes.

export function buildHeadset(THREE, electrodes, materials, lobes) {
  const UP = new THREE.Vector3(0, 1, 0);
  const V = (a) => new THREE.Vector3(a[0], a[1], a[2]);
  const group = new THREE.Group();
  const glass = [];
  const extras = [];
  const part = (object, name, extra) => {
    object.userData = Object.assign({ part: name }, extra || {});
    return object;
  };
  const frame = (alpha) => {
    const m = materials.frame(alpha);
    glass.push(m);
    return m;
  };
  const sub = (name) => {
    const g = new THREE.Group();
    group.add(g);
    extras.push({ name, object: g });
    return g;
  };
  const straps = sub("straps");
  const leads = sub("leads");
  const bands = sub("side bands");
  const hubParts = sub("box");

  const headC = new THREE.Vector3(0, -15, 10);
  const tips = materials.tip();
  glass.push(tips);
  const tipGeo = new THREE.SphereGeometry(1.25, 8, 6);

  function earClip(p, n, name) {
    // returns the point where the lead joins the clip
    const X = n.clone().normalize();
    const Z = new THREE.Vector3(0, 0, -1); // the clip hangs from the bottom of the lobe: jaws, handles and lead point down
    Z.addScaledVector(X, -Z.dot(X)).normalize();
    const Y = Z.clone().cross(X);
    const g = new THREE.Group();
    g.position.copy(p);
    g.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(X, Y, Z));
    group.add(g);
    extras.push({ name: `${name} clip hardware`, object: g });
    [1, -1].forEach((sd) => {
      const jaw = part(new THREE.Mesh(new THREE.BoxGeometry(1.8, 6.5, 24), frame(0.7)), "clip-jaw");
      jaw.position.set(sd * 4.6, 0, 7);
      jaw.rotation.y = sd * 0.2;
      jaw.renderOrder = 6;
      g.add(jaw);
      const grip = new THREE.Mesh(new THREE.BoxGeometry(2.4, 7, 5), frame(0.7));
      grip.position.set(sd * 2.6, 0, -3.5);
      grip.renderOrder = 6;
      g.add(grip);
    });
    const hinge = new THREE.Mesh(new THREE.CylinderGeometry(2.3, 2.3, 7.5, 12), frame(0.7));
    hinge.position.set(0, 0, 8);
    hinge.renderOrder = 6;
    g.add(hinge);
    return p.clone().addScaledVector(X, 5.5).addScaledVector(Z, 17);
  }

  const E = {};
  const contacts = [];
  electrodes.forEach((e) => {
    const ear = e.name === "A2";
    const src = ear && lobes ? lobes.A2 : e;
    const p = V(src.p);
    const n = V(src.n).normalize();
    const q = new THREE.Quaternion().setFromUnitVectors(UP, n);
    const brow = e.name === "Fp1" || e.name === "Fp2";
    const at = (geo, mat, h) => {
      const m = new THREE.Mesh(geo, mat);
      m.position.copy(p).addScaledVector(n, h);
      m.quaternion.copy(q);
      m.renderOrder = 6;
      group.add(m);
      return m;
    };
    const disc = at(new THREE.CylinderGeometry(ear ? 4.2 : 6.6, ear ? 4.2 : 6.6, 2.8, 28), materials.contact(), 1.8);
    part(disc, "disc", { contact: e.name });
    if (brow) {
      const pad = at(new THREE.SphereGeometry(1, 24, 16), frame(0.44), 6.5);
      pad.scale.set(21, 6.5, 13);
      // each forehead pad holds two smooth contacts side by side; they share one channel, so one state colours both
      const lat = new THREE.Vector3(0, 0, 1).cross(n).normalize();
      const twin = part(new THREE.Mesh(disc.geometry, disc.material), "disc", { contact: e.name });
      twin.quaternion.copy(q);
      twin.renderOrder = 6;
      twin.position.copy(disc.position).addScaledVector(lat, 8.5);
      disc.position.addScaledVector(lat, -8.5);
      group.add(twin);
    } else if (!ear) {
      const bulb = at(new THREE.SphereGeometry(1, 24, 16), frame(0.46), 13);
      bulb.scale.set(11.5, 9.5, 11.5);
      const a = new THREE.Vector3(1, 0, 0).applyQuaternion(q);
      const b = new THREE.Vector3(0, 0, 1).applyQuaternion(q);
      for (let k = 0; k < 8; k++) {
        // sensor legs: silver tips on the scalp around the contact
        const th = (k * Math.PI) / 4;
        const tip = part(new THREE.Mesh(tipGeo, tips), "tip");
        tip.position
          .copy(p)
          .addScaledVector(a, 9 * Math.cos(th))
          .addScaledVector(b, 9 * Math.sin(th))
          .addScaledVector(n, 0.6);
        tip.renderOrder = 7;
        group.add(tip);
      }
    }
    const glow = new THREE.Sprite(materials.glow());
    glow.position.copy(p).addScaledVector(n, 3);
    glow.scale.setScalar(30);
    group.add(glow);
    const top = ear ? earClip(p, n, "A2") : p.clone().addScaledVector(n, brow ? 12 : 22);
    const o = { name: e.name, disc, glow, position: p, normal: n, top, brow };
    E[e.name] = o;
    contacts.push(o);
  });

  const add = (parent, m) => {
    m.renderOrder = 6;
    parent.add(m);
    return m;
  };
  const tube = (pts, r) =>
    add(
      leads,
      new THREE.Mesh(
        new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts), pts.length * 12, r, 8),
        frame(0.6),
      ),
    );
  function ribbon(parent, pts, width, alpha) {
    // flat strap following a curve over the scalp
    const cv = new THREE.CatmullRomCurve3(pts);
    const K = 40;
    const pos = [];
    const idx = [];
    for (let k = 0; k <= K; k++) {
      const c = cv.getPoint(k / K);
      const tg = cv.getTangent(k / K);
      const out = c.clone().sub(headC).normalize();
      const w = tg.clone().cross(out).normalize().multiplyScalar(width / 2);
      pos.push(c.x - w.x, c.y - w.y, c.z - w.z, c.x + w.x, c.y + w.y, c.z + w.z);
      if (k < K) idx.push(2 * k, 2 * k + 1, 2 * k + 2, 2 * k + 1, 2 * k + 3, 2 * k + 2);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    add(parent, new THREE.Mesh(g, frame(alpha === undefined ? 0.62 : alpha)));
  }

  // electronics box: rounded oblong, long axis front to back, lights and button toward the front
  const axis = E.Cz.normal.clone();
  const hubC = E.Cz.position.clone().addScaledVector(axis, 27).add(new THREE.Vector3(0, -4, 0));
  const hubQ = new THREE.Quaternion().setFromUnitVectors(UP, axis);
  const base = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 9, 40), frame(0.5));
  base.position.copy(hubC);
  base.quaternion.copy(hubQ);
  base.scale.set(28, 1, 48);
  add(hubParts, base);
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 12, 0, Math.PI * 2, 0, Math.PI / 2), frame(0.5));
  dome.position.copy(hubC).addScaledVector(axis, 4.5);
  dome.quaternion.copy(hubQ);
  dome.scale.set(28, 8, 48);
  add(hubParts, dome);
  const hubDot = (name, kind, dx, dy, r, color) => {
    const mat = materials.light(color);
    glass.push(mat);
    const l = part(new THREE.Mesh(new THREE.SphereGeometry(r, 10, 8), mat), kind);
    l.position.copy(hubC).addScaledVector(axis, 11.5).add(new THREE.Vector3(dx, dy, 0));
    l.renderOrder = 7;
    group.add(l);
    extras.push({ name, object: l });
  };
  hubDot("light 1", "light", -5, 29, 1.3, 0xbfefff); // two lights and the power button
  hubDot("light 2", "light", 5, 29, 1.3, 0xbfefff);
  hubDot("power button", "power-button", 0, 19, 3.2, 0x39425e);

  contacts.forEach((o) => {
    if (o.name === "A2" || o.name === "Cz") return; // Cz sits under the box
    const flat = o.position.clone().sub(hubC);
    flat.addScaledVector(axis, -flat.dot(axis));
    if (flat.length() < 1) flat.set(0, 1, 0);
    flat.normalize();
    const lift = o.brow ? 9 : 14;
    const body = o.position.clone().addScaledVector(o.normal, lift);
    const toHub = hubC.clone().sub(body); // along the scalp, toward the box
    toHub.addScaledVector(o.normal, -toHub.dot(o.normal)).normalize();
    const neckEnd = body.clone().addScaledVector(toHub, o.brow ? 15 : 17).addScaledVector(o.normal, 3);
    const neck = new THREE.Mesh(new THREE.CylinderGeometry(3.6, 5.6, o.brow ? 9 : 11, 14), frame(0.5));
    neck.position.copy(body).lerp(neckEnd, 0.72);
    neck.quaternion.setFromUnitVectors(UP, neckEnd.clone().sub(body).normalize());
    add(straps, neck);
    const e2 = Math.abs(flat.x) > 0.5 ? 28 : 48;
    const start = hubC.clone().addScaledVector(flat, e2 * 0.78).addScaledVector(axis, -5);
    const pts = [start];
    const r0 = headC.distanceTo(start);
    const r1 = headC.distanceTo(neckEnd);
    const span = start.distanceTo(neckEnd);
    for (const u of [0.25, 0.5, 0.75]) {
      const m = start.clone().lerp(neckEnd, u);
      const want = r0 * (1 - u) + r1 * u + (6 + 0.09 * span) * Math.sin(Math.PI * u);
      pts.push(headC.clone().addScaledVector(m.sub(headC).normalize(), want));
    }
    pts.push(neckEnd);
    ribbon(straps, pts, 6.5);
  });

  ["T3", "T4"].forEach((k) => {
    const o = E[k];
    const side = Math.sign(o.position.x);
    const c = E[side < 0 ? "C3" : "C4"];
    const boxC = o.position.clone().addScaledVector(o.normal, 13).add(new THREE.Vector3(0, -30, -32));
    ribbon(
      bands,
      [
        hubC.clone().add(new THREE.Vector3(side * 24, -20, -4)),
        c.position.clone().addScaledVector(c.normal, 26).add(new THREE.Vector3(0, -30, 0)),
        o.position.clone().addScaledVector(o.normal, 24).add(new THREE.Vector3(0, -30, 4)),
        boxC.clone().add(new THREE.Vector3(side * 3, 0, 22)),
      ],
      15,
      0.6,
    );
    let clipTop = E.A2.top;
    if (side < 0) {
      // left ear lead, mirrored from A2; grey because the sample data hold no channel for it
      const mirror = new THREE.Vector3(-1, 1, 1);
      const mp = lobes ? V(lobes.A1.p) : E.A2.position.clone().multiply(mirror);
      const mn = lobes ? V(lobes.A1.n) : E.A2.normal.clone().multiply(mirror);
      clipTop = earClip(mp, mn, "A1");
      const grey = materials.grey();
      glass.push(grey);
      const clip = new THREE.Mesh(new THREE.CylinderGeometry(4.2, 4.2, 2.8, 24), grey);
      clip.position.copy(mp).addScaledVector(mn, 1.8);
      clip.quaternion.setFromUnitVectors(UP, mn);
      part(clip, "a1-disc");
      group.add(clip);
      extras.push({ name: "A1 clip", object: clip });
    }
    tube(
      [
        boxC.clone().add(new THREE.Vector3(side * 3, 0, 22)),
        boxC.clone().add(new THREE.Vector3(side * 5, -6, -6)),
        clipTop.clone().add(new THREE.Vector3(side * 4, -14, -22)),
        clipTop.clone().add(new THREE.Vector3(0, -2, -9)),
        clipTop,
      ],
      1.0,
    );
  });

  return {
    group,
    contacts: contacts.map(({ name, disc, glow, position, normal, top }) => ({
      name,
      disc,
      glow,
      position,
      normal,
      top,
    })),
    glass,
    extras,
  };
}

"""Find the bottom of each earlobe on the head mesh in meshes.json (step 2 of 2).

Reads  a meshes.json written by scripts/export_meshes.py (the head mesh and the A2 electrode).
Writes the same file back with a ``lobes`` key added: ``{"A1": {p, n}, "A2": {p, n}}``, the
       lowest part of the sideways-protruding ear on each side, where the ear clips hang.
Run after scripts/export_meshes.py.

Usage: python -I scripts/brain_mapper_meshes_lobes.py <meshes.json>
"""
import base64
import json
import sys
import numpy as np

path = sys.argv[1]
D = json.load(open(path))
print("keys", list(D.keys()), "head keys", list(D["head"].keys()) if isinstance(D.get("head"), dict) else type(D.get("head")))
h = D["head"]
v = np.frombuffer(base64.b64decode(h["v"]), dtype="<i2").reshape(-1, 3).astype(float) / D["scale"]
f = np.frombuffer(base64.b64decode(h["f"]), dtype="<u2").reshape(-1, 3)
used = np.zeros(len(v), bool)  # ignore vertices whose faces were removed
used[f.ravel()] = True
a2 = np.array(next(e for e in D["electrodes"] if e["name"] == "A2")["p"], float)
print("A2", a2.round(1))
out = {}
for name, sx in (("A2", 1.0), ("A1", -1.0)):
    c = a2 * np.array([sx, 1, 1])
    near = used & (np.linalg.norm(v - c, axis=1) < 45) & (v[:, 0] * sx > 0)
    # the ear is what sticks out sideways: keep the outermost part of the neighbourhood, slice by slice in height
    cand = np.where(near)[0]
    zs = v[cand, 2]
    keep = []
    for z0 in np.arange(zs.min(), zs.max(), 3.0):
        sl = cand[(zs >= z0) & (zs < z0 + 3.0)]
        if len(sl) == 0:
            continue
        xo = np.abs(v[sl, 0])
        base = np.percentile(xo, 20)
        keep.extend(sl[xo > base + 6.0])                        # at least 6 mm proud of the skull at this height
    keep = np.array(keep)
    zk = v[keep, 2]
    low = keep[zk < np.percentile(zk, 6)]                        # the lowest part of the protruding ear
    p = v[low].mean(0)
    n = np.array([sx, 0.0, 0.0])
    print(name, "ear verts", len(keep), "z range", zk.min().round(1), zk.max().round(1), "lobe", p.round(1))
    out[name] = {"p": [round(float(x), 2) for x in p], "n": [float(x) for x in n]}
D["lobes"] = out
json.dump(D, open(path, "w"), separators=(",", ":"))
print("written")

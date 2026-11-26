"""Build the meshes of the brain-mapper 3-D views (step 1 of 2).

Reads  a glTF binary (.glb) of the "Infinite, 3D Head Scan" by Lee Perry-Smith (CC BY 3.0),
       the fsaverage5 pial surfaces (nilearn download) and the fsaverage head and
       standard_1020 montage that ship with mne.
Writes a meshes.json: the brain surface, the head scan registered to fsaverage with its
       inner surfaces (mouth, nostrils, ear canals, eye sockets) removed, the 20 electrode
       positions and normals snapped to the scalp, and 32 node positions. Vertices are
       int16 millimetres times ``scale``, faces are uint16, both base64.

Run order (the two scripts are one pipeline; the second adds the ``lobes`` key):
    python -I scripts/export_meshes.py <head scan.glb> <out>/meshes.json
    python -I scripts/brain_mapper_meshes_lobes.py <out>/meshes.json
then copy the result to brain_mapper/src/brain_mapper/web/assets/meshes.json.
Needs trimesh, mne, nilearn and scipy; none is a dependency of the packages.
The committed meshes.json was made this way and is not rebuilt by the tests.
"""
import base64
import json
import os
import sys
import numpy as np
import trimesh
import mne
from nilearn import datasets, surface
from scipy.spatial import cKDTree

glb, out = sys.argv[1], sys.argv[2]

def load(p):
    m = surface.load_surf_mesh(p)
    return np.asarray(m.coordinates, float), np.asarray(m.faces, int)
fs = datasets.fetch_surf_fsaverage("fsaverage5")
vl, fl = load(fs["pial_left"])
vr, fr = load(fs["pial_right"])
bv = np.vstack([vl, vr])
bf = np.vstack([fl, fr + len(vl)])

d = os.path.join(os.path.dirname(mne.__file__), "data", "fsaverage")
fsh = mne.read_bem_surfaces(os.path.join(d, "fsaverage-head.fif"), verbose=False)[0]
fv = fsh["rr"] * 1000.0

scene = trimesh.load(glb, force="mesh", process=True)
hv = np.asarray(scene.vertices, float)
hf = np.asarray(scene.faces, int)
print("scan", hv.shape)
NOSE = int(np.argmax(hv[:, 2] - 0.2 * np.abs(hv[:, 1] - 1.0)))
# scan: y up, face toward +z, subject's left = +x  ->  RAS
hv = np.stack([-hv[:, 0], hv[:, 2], hv[:, 1]], axis=1)

def crop(v, top_mm):
    return v[v[:, 2] > v[:, 2].max() - top_mm]
# rough scale and shift from the top 190 mm of each head (cranium + face, no neck)
tgt = crop(fv, 150.0)
s0 = 55.0
for _ in range(3):
    src = crop(hv * s0, 150.0)
    s0 *= np.mean(np.ptp(tgt, axis=0)[:2]) / np.mean(np.ptp(src, axis=0)[:2])
hv = hv * s0
src = crop(hv, 150.0)
hv = hv + ((tgt.max(0) + tgt.min(0)) / 2 - (src.max(0) + src.min(0)) / 2)
print("after rough fit nose", hv[NOSE], "scale", s0)

# centre the head on the brain (slide it toward wherever the brain is closest to the scalp), then grow it evenly
mesh = trimesh.Trimesh(hv, hf, process=False)
n = np.array(mesh.vertex_normals)
if np.mean(np.sum(n * (hv - hv.mean(0)), axis=1)) < 0:
    n = -n
c = bv.mean(0)
def depth(hv):
    t = cKDTree(hv)
    dd, ii = t.query(bv)
    return np.sum((hv[ii] - bv) * n[ii], axis=1)
print("depth before", "min %.1f" % depth(hv).min(), "median %.1f" % np.median(depth(hv)), "nose", np.round(hv[NOSE], 1))
best = np.percentile(depth(hv), 2)
for k in range(60):
    dep = depth(hv)
    w = bv[np.argsort(dep)[:150]] - c
    step = w.mean(0)
    step[0] = 0.0
    step = step / (np.linalg.norm(step) + 1e-9)
    trial = hv + step
    m = np.percentile(depth(trial), 2)
    if m <= best + 0.02:
        break
    hv, best = trial, m
print("slid", k, "mm; min depth %.1f" % best, "nose", np.round(hv[NOSE], 1))
scale = 1.0
while np.percentile(depth(hv), 2) < 7.0 and scale < 1.25:
    scale *= 1.01
    hv = c + (hv - c) * 1.01
print("grow %.2f" % scale, "p2 %.1f" % np.percentile(depth(hv), 2), "min %.1f" % depth(hv).min(), "median %.1f" % np.median(depth(hv)), "nose", np.round(hv[NOSE], 1))

# remove the mouth cavity: surface in the lower face from which almost no direction reaches open space
mesh = trimesh.Trimesh(hv, hf, process=False)
vn = np.array(mesh.vertex_normals)
if np.mean(np.sum(vn * (hv - hv.mean(0)), axis=1)) < 0:
    vn = -vn
nose = hv[NOSE]
box = hv[:, 2] > nose[2] - 130   # whole head down to the upper neck: mouth, nostrils, ear canals, eye sockets
cand = np.where(box)[0]
rng2 = np.random.default_rng(1)
dirs = rng2.normal(size=(80, 3))
dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
free = np.zeros(len(cand))
org = hv[cand] + vn[cand] * 1.0
for dvec in dirs:
    dd = np.tile(dvec, (len(cand), 1))
    free += ~mesh.ray.intersects_any(org, dd)
free /= len(dirs)                                           # open skin sees about half the sky, a cavity almost none
inner = np.zeros(len(hv), bool)
inner[cand[free < 0.12]] = True
drop = inner[hf].any(axis=1)
print("mouth cavity: candidates", len(cand), "hidden vertices", int(inner.sum()), "faces removed", int(drop.sum()), "of", len(hf),
      "free quartiles", np.round(np.percentile(free, [5, 25, 50, 75]), 2))
hf = hf[~drop]

# electrodes: standard_1020 in fsaverage space, snapped to the scan's scalp along the ray from the head centre
names = "F7 Fp1 Fp2 F8 F3 Fz F4 C3 Cz P8 P7 Pz P4 T3 P3 O1 O2 C4 T4 A2".split()
mont = mne.channels.make_standard_montage("standard_1020")
trans = mne.read_trans(os.path.join(d, "fsaverage-trans.fif"))
info = mne.create_info(names, 500.0, "eeg")
info.set_montage(mont)
ep = mne.transforms.apply_trans(trans, np.array([info["chs"][i]["loc"][:3] for i in range(len(names))])) * 1000.0
mesh = trimesh.Trimesh(hv, hf, process=False)
origin = np.array([0.0, -15.0, 10.0])
els = []
for name, p in zip(names, ep):
    dirv = (p - origin) / np.linalg.norm(p - origin)
    loc, _, tri = mesh.ray.intersects_location([origin], [dirv])
    if len(loc):
        j = int(np.argmax(np.linalg.norm(loc - origin, axis=1)))
        q = loc[j]
        nn = mesh.face_normals[tri[j]]
        if np.dot(nn, dirv) < 0:
            nn = -nn
        nn = nn * 0.5 + dirv * 0.5
        nn /= np.linalg.norm(nn)
    else:
        i = int(np.argmin(np.linalg.norm(hv - p, axis=1)))
        q = hv[i]
        nn = dirv
    print(name, np.round(q, 1), "moved %.1f mm" % np.linalg.norm(q - p))
    els.append({"name": name, "p": np.round(q, 2).tolist(), "n": np.round(nn, 4).tolist()})

rng = np.random.default_rng(3)
cl = vl.mean(0)
pts = [vl[int(rng.integers(len(vl)))]]
for _ in range(15):
    dist = np.min(np.linalg.norm(vl[:, None, :] - np.array(pts)[None], axis=2), axis=1)
    pts.append(vl[int(np.argmax(dist))])
left = cl + (np.array(pts) - cl) * 0.80
nodes = np.vstack([left, left * np.array([-1.0, 1.0, 1.0])])

def b64(a, t):
    return base64.b64encode(np.ascontiguousarray(a, dtype=t).tobytes()).decode()
S = 60.0
assert np.abs(bv * S).max() < 32767 and np.abs(hv * S).max() < 32767, (np.abs(hv).max())
wide = len(hv) >= 65536
data = {"scale": S,
        "brain": {"v": b64(np.round(bv * S), "<i2"), "f": b64(bf, "<u2")},
        "head": {"v": b64(np.round(hv * S), "<i2"), "f": b64(hf, "<u4" if wide else "<u2"), "wide": bool(wide)},
        "electrodes": els, "nodes": np.round(nodes, 2).tolist(),
        "credit": "Head: Infinite, 3D Head Scan by Lee Perry-Smith, CC BY 3.0. Brain: FreeSurfer fsaverage."}
json.dump(data, open(out, "w"))
print("head verts", len(hv), "faces", len(hf), "bytes", os.path.getsize(out), "zmin", hv[:, 2].min())

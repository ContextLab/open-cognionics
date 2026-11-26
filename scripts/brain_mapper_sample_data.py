"""Write the made-up sample data of the brain-mapper 3-D views.

Reads  brain_mapper/src/brain_mapper/web/assets/meshes.json  (the 32 node positions).
Writes brain_mapper/src/brain_mapper/web/assets/sample-contacts.json  (contact state per label)
       brain_mapper/src/brain_mapper/web/assets/sample-network.json   (nodes, communities, edges)

The values are those of the 3-D prototype: five contacts with a weak or missing
contact, k-means groups of the nodes (3 for the default, 2 and 5 as alternatives),
and the prototype's link weights at time zero (an exponential fall-off with
distance, left-right mirror pairs boosted, links inside a group kept and links
between groups cut to a fifth, plus a fixed pseudo-random part). Weights are
divided by the largest one. The 64 strongest links lie at or above the display
threshold of the network view (0.405, ``DISPLAY_THRESHOLD`` in network-view.js); the
next 16 lie below it and are not drawn.

Usage: python -I scripts/brain_mapper_sample_data.py [<output folder>]
(without a folder the files are written over those in assets/)
"""

import json
import math
from pathlib import Path

ASSETS = (
    Path(__file__).resolve().parents[1] / "brain_mapper" / "src" / "brain_mapper" / "web" / "assets"
)
START = {"F8": "marginal", "T4": "marginal", "P8": "marginal", "O2": "bad", "A2": "bad"}
SHOWN = 64
EXTRA = 16
THRESHOLD = 0.405


def kmeans(points, k):
    centres = [list(points[0])]
    while len(centres) < k:
        far = max(
            range(len(points)), key=lambda i: min(math.dist(c, points[i]) for c in centres)
        )
        centres.append(list(points[far]))
    assign = [0] * len(points)
    for _ in range(12):
        assign = [min(range(k), key=lambda j: math.dist(centres[j], p)) for p in points]
        for j in range(k):
            members = [p for p, a in zip(points, assign) if a == j]
            if members:
                centres[j] = [sum(m[axis] for m in members) / len(members) for axis in range(3)]
    return assign


def hashed(i, j):
    s = math.sin(i * 127.1 + j * 311.7) * 43758.5453
    return s - math.floor(s)


def main(out_dir=None):
    out_dir = Path(out_dir) if out_dir else ASSETS
    meshes = json.loads((ASSETS / "meshes.json").read_text())
    names = [e["name"] for e in meshes["electrodes"]]
    contacts = {name: START.get(name, "good") for name in names}
    (out_dir / "sample-contacts.json").write_text(json.dumps(contacts, indent=1) + "\n")

    nodes = meshes["nodes"]
    n = len(nodes)
    groups = {k: kmeans(nodes, k) for k in (2, 3, 5)}
    assign = groups[3]
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            d = math.dist(nodes[i], nodes[j])
            mirror = 0.3 if j == i + n // 2 else 0.0
            h = hashed(i, j)
            same = assign[i] == assign[j]
            w = (math.exp(-d / 55) + mirror) * (1 if same else 0.2) + 0.1 * h + 0.07 * math.sin(h * 40)
            edges.append((i, j, w))
    edges.sort(key=lambda e: -e[2])
    top = edges[0][2]
    keep = edges[: SHOWN + EXTRA]
    out = {
        "nodes": nodes,
        "communities": assign,
        "edges": [[i, j, round(w / top, 4)] for i, j, w in keep],
        "alternatives": {"2": groups[2], "5": groups[5]},
    }
    (out_dir / "sample-network.json").write_text(json.dumps(out) + "\n")
    above = sum(1 for e in out["edges"] if e[2] >= THRESHOLD)
    assert above == SHOWN, above
    print("edges", len(out["edges"]), "at or above", THRESHOLD, ":", above)


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else None)

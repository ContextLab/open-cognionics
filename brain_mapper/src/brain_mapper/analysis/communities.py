"""Communities of a weighted graph and how far its modularity stands above shuffled graphs.

The design of brain-mapper
names the "segmentation" score: modularity of the communities of the connectivity graph,
expressed relative to surrogate versions of the same data. What is computed here:

- Graph: the good channels; the weight of a link is the dwPLI value, with values below 0 set to 0.
- Modularity (Newman 2006, weighted): Q = (1/2m) sum_ij [W_ij - k_i k_j / 2m] delta(c_i, c_j).
- Communities: the Louvain method (Blondel et al. 2008; local moving of single nodes, then the
  same on the graph of communities, until nothing changes), run from ``RESTARTS`` random node
  orders; the partition with the highest Q is kept, and any community that is not connected by
  links of weight above 0 is split into its connected parts (which never lowers Q). This is not
  the Leiden method the design names, and no consensus over restarts is formed: the best
  partition is kept.
- Score: z = (Q - mean Q_null) / sd Q_null, where each Q_null is the highest Q found (with
  ``NULL_RESTARTS`` restarts) on the same graph with its link weights shuffled among the pairs.
  The design's second surrogate (time-shifted signals) is used for the links instead (see
  ``connectivity``).
- Groups shown: only when Q is above the shuffled graphs' mean plus ``GROUP_SD`` standard
  deviations (``grouped``); otherwise the graph is not said to split into groups at all.

What it means: how much more the graph splits into groups than a graph with the same weights
placed at random. It is a description of the scalp signal network; the literature does not
support reading it as good or bad.
"""

from __future__ import annotations

import numpy as np

RESTARTS = 20
NULL_RESTARTS = 4
NULLS = 50
GROUP_SD = 2.0


def modularity(w: np.ndarray, labels: np.ndarray) -> float:
    two_m = w.sum()
    if two_m <= 0:
        return 0.0
    k = w.sum(axis=1)
    same = labels[:, None] == labels[None, :]
    return float(((w - np.outer(k, k) / two_m) * same).sum() / two_m)


def _local_moving(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = len(w)
    labels = np.arange(n)
    k = w.sum(axis=1)
    two_m = k.sum()
    tot = k.copy()  # total strength of each community
    improved = True
    while improved:
        improved = False
        for i in rng.permutation(n):
            current = labels[i]
            tot[current] -= k[i]
            links = np.bincount(labels, weights=w[i], minlength=n)
            links[current] -= w[i, i]
            gains = links - tot * k[i] / two_m
            best = current
            best_gain = gains[current]
            for c in np.flatnonzero(links > 0):
                if gains[c] > best_gain + 1e-12:
                    best, best_gain = c, gains[c]
            labels[i] = best
            tot[best] += k[i]
            if best != current:
                improved = True
    _, labels = np.unique(labels, return_inverse=True)
    return labels


def louvain(w: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """One run of the Louvain method; returns a community number per node."""
    n = len(w)
    membership = np.arange(n)
    graph = w.copy()
    while True:
        level = _local_moving(graph, rng)
        if level.max() + 1 == len(graph):
            break
        membership = level[membership]
        size = level.max() + 1
        onehot = np.zeros((len(graph), size))
        onehot[np.arange(len(graph)), level] = 1.0
        graph = onehot.T @ graph @ onehot
    _, membership = np.unique(membership, return_inverse=True)
    return membership


def split_disconnected(w: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Splits every community into the parts connected by links of weight above 0."""
    out = -np.ones(len(labels), dtype=int)
    next_label = 0
    for c in np.unique(labels):
        members = set(np.flatnonzero(labels == c).tolist())
        while members:
            start = members.pop()
            stack, part = [start], [start]
            while stack:
                i = stack.pop()
                for j in list(members):
                    if w[i, j] > 0:
                        members.discard(j)
                        stack.append(j)
                        part.append(j)
            out[part] = next_label
            next_label += 1
    return out


def best_partition(w: np.ndarray, restarts: int, rng: np.random.Generator) -> tuple[np.ndarray, float]:
    best, best_q = np.zeros(len(w), dtype=int), -np.inf
    if w.sum() <= 0:
        return best, 0.0
    for _ in range(restarts):
        labels = split_disconnected(w, louvain(w, rng))
        q = modularity(w, labels)
        if q > best_q + 1e-12:
            best, best_q = labels, q
    return _canonical(best), float(best_q)


def _canonical(labels: np.ndarray) -> np.ndarray:
    """Communities numbered by size (largest 0), ties by first member."""
    order = sorted(np.unique(labels), key=lambda c: (-(labels == c).sum(), int(np.flatnonzero(labels == c)[0])))
    remap = {c: i for i, c in enumerate(order)}
    return np.array([remap[c] for c in labels], dtype=int)


def analyze(matrix: np.ndarray, good: np.ndarray, seed: int = 0, nulls: int = NULLS, restarts: int = RESTARTS) -> dict:
    """Communities and score of a dwPLI matrix over the good channels.

    Returns ``{"communities": [int or None per channel], "q", "q_null_mean", "q_null_sd", "z",
    "grouped"}``; bad channels get ``None``. ``grouped`` is whether Q is above the shuffled mean
    plus ``GROUP_SD`` standard deviations. ``z`` is ``None`` when the shuffled graphs all give the same Q.
    """
    rng = np.random.default_rng(seed)
    index = np.flatnonzero(good)
    w = np.clip(np.nan_to_num(matrix[np.ix_(index, index)]), 0.0, None)
    np.fill_diagonal(w, 0.0)
    labels, q = best_partition(w, restarts, rng)
    upper = np.triu_indices(len(w), 1)
    values = w[upper]
    null_q = []
    for _ in range(nulls):
        shuffled = np.zeros_like(w)
        shuffled[upper] = rng.permutation(values)
        shuffled = shuffled + shuffled.T
        null_q.append(best_partition(shuffled, NULL_RESTARTS, rng)[1])
    null_q = np.array(null_q) if null_q else np.zeros(0)
    sd = float(null_q.std(ddof=1)) if len(null_q) > 1 else 0.0
    mean = float(null_q.mean()) if len(null_q) else None
    communities: list[int | None] = [None] * len(good)
    for position, channel in enumerate(index):
        communities[channel] = int(labels[position])
    return {
        "communities": communities,
        "q": round(q, 4),
        "q_null_mean": round(mean, 4) if mean is not None else None,
        "q_null_sd": round(sd, 4),
        "z": round((q - mean) / sd, 2) if sd > 1e-9 and mean is not None else None,
        "grouped": bool(mean is not None and sd > 1e-9 and q > mean + GROUP_SD * sd),
    }

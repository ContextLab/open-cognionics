"""The analysis of one session folder: load, preprocess, spectra, connectivity, communities.

``compute(folder, progress)`` returns the results as two dicts (``results`` and ``windows``) and
writes nothing. ``write(folder, results, windows)`` saves them as new files. See
``docs/brain-mapper-results.md`` for every step and what each number means.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import numpy as np
import open_cognionics as oc

from brain_mapper import package

from . import communities, connectivity, spectra

VERSION = 2
#: The recording folder analyzed: the one the rounds were played in (recordings set aside when a
#: link was lost during device setup hold no rounds).
RECORDING = "eeg"
MAINS_HZ = 60.0
POSTERIOR = ("O1", "O2", "P3", "P4", "Pz", "P7", "P8")
CLOSED = ("rest_closed", "eyes_closed")
OPEN = ("rest_open", "eyes_open")
#: Fraction of the work done when each stage ends (for the progress bar).
STAGES = {"load": 0.03, "filter": 0.15, "spectra": 0.2, "connectivity": 0.6, "windows": 0.99, "done": 1.0}

#: Fewest kept 2 s segments a spectrum is computed from (connectivity needs
#: ``connectivity.MIN_SEGMENTS``).
MIN_SPECTRUM_SEGMENTS = 5
#: Shuffled graphs and restarts for the groups of a window (fewer than for a whole round, as
#: there are many windows; the same rule, Q above the shuffled mean plus 2 SD, decides).
WINDOW_NULLS = 20
WINDOW_RESTARTS = 6

#: Samples checked at once for the converter's limit (bounds the memory used).
BLOCK = 30_000

Progress = Callable[[float, str], None]


class NotComputable(Exception):
    """The session holds nothing that can be analyzed (the words say why)."""


def _lines(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _marker_samples(rec_dir: Path) -> dict[int, int]:
    found = {}
    for marker in _lines(rec_dir / "markers.jsonl"):
        label, sample = marker.get("label"), marker.get("sample")
        if isinstance(label, str) and "#" in label and isinstance(sample, int):
            try:
                found[int(label.rsplit("#", 1)[1])] = sample
            except ValueError:
                continue
    return found


def spans(folder: Path) -> list[dict]:
    """The analyzed spans of the session, in order: every block of every round with a start and an
    end in the recording, and the eyes-open and eyes-closed parts of calibration; paused stretches
    are cut out. ``[{"step", "block", "part", "start", "end"}]`` in samples of ``eeg/``."""
    meta = package.session_meta(folder)
    timeline = meta.get("timeline") if isinstance(meta.get("timeline"), list) else package.timeline(folder)
    samples = _marker_samples(folder / RECORDING)
    pauses = []
    opened = None
    for event in _lines(folder / "events.jsonl"):
        sample = samples.get(event.get("seq"))
        if event.get("name") == "paused" and sample is not None:
            opened = sample
        elif event.get("name") == "resumed" and opened is not None and sample is not None:
            pauses.append((opened, sample))
            opened = None
    out = []
    for item in timeline:
        start, end = item.get("start_sample"), item.get("end_sample")
        part = item.get("part")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            continue
        if item.get("block") is None and part not in ("eyes_open", "eyes_closed"):
            continue
        pieces = [(start, end)]
        for p0, p1 in pauses + ([(opened, end)] if opened is not None and opened < end else []):
            cut = []
            for a, b in pieces:
                if p1 <= a or p0 >= b:
                    cut.append((a, b))
                    continue
                if p0 > a:
                    cut.append((a, p0))
                if p1 < b:
                    cut.append((p1, b))
            pieces = cut
        for a, b in pieces:
            out.append({"step": item.get("step"), "block": item.get("block"), "part": part, "start": a, "end": b})
    return out


def _titles(meta: dict) -> dict[str, str]:
    titles = {"eyes_open": "Calibration, eyes open", "eyes_closed": "Calibration, eyes closed"}
    for step in meta.get("flow") or []:
        if isinstance(step, dict) and isinstance(step.get("id"), str):
            titles[step["id"]] = str(step.get("title") or step["id"])
    return titles


def _round(values, digits=4):
    if values is None:
        return None
    array = np.asarray(values, dtype=float)
    return [None if not np.isfinite(v) else round(float(v), digits) for v in array.ravel()] if array.ndim == 1 else [
        _round(row, digits) for row in array
    ]


def load(rec_dir: Path):
    """The recording in ``rec_dir`` as float32 microvolts (samples by channels), a row of NaN for
    each lost sample, read chunk by chunk (so a long session never holds float64 copies); with
    its configuration, ``meta.json`` and which samples were lost."""
    count = oc.replay(rec_dir)
    n = 0
    for chunk in count:
        n = chunk.index + chunk.samples
    n += count.lost_at_end
    config = count.config
    data = np.full((n, config.n_eeg), np.nan, dtype=np.float32)
    lost = np.ones(n, dtype=bool)
    for chunk in oc.replay(rec_dir):
        data[chunk.index : chunk.index + chunk.samples] = chunk.data * 1e6
        lost[chunk.index : chunk.index + chunk.samples] = np.isnan(chunk.data).any(axis=1)
    meta_path = rec_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return config, meta, data, lost


def _key(item: dict) -> str:
    return item["part"] or item["step"]


def compute(folder: Path, progress: Progress | None = None, mains: float = MAINS_HZ) -> tuple[dict, dict]:
    """The results of ``folder`` (``results``, ``windows``); writes nothing.

    Raises ``NotComputable`` when there is no recording or no analyzable span.
    """
    folder = Path(folder)
    report = progress or (lambda fraction, text: None)
    started = time.monotonic()
    report(0.0, "Reading the recording")
    meta = package.session_meta(folder)
    rec_dir = folder / RECORDING
    if not (rec_dir / "raw.bin").exists():
        raise NotComputable("no EEG was recorded in this session")
    config, rec_meta, data, lost_rows = load(rec_dir)
    fs = float(config.sample_rate)
    labels = list(config.labels)
    volts_per_count = float(rec_meta.get("eeg_volts_per_count") or 0.0)
    full_scale_uv = (2**23) * volts_per_count * 1e6 if volts_per_count else np.inf
    items = sorted((s for s in spans(folder) if s["end"] <= len(data)), key=lambda s: s["start"])
    if not items:
        raise NotComputable("the recording holds no finished part of a round")
    titles = _titles(meta)
    longest = max(items, key=lambda s: s["end"] - s["start"])
    mains_found = spectra.mains_check(
        np.nan_to_num(data[longest["start"] : min(longest["end"], longest["start"] + int(60 * fs))].astype(np.float64)), fs
    )
    report(STAGES["load"], "Filtering")

    # Spans are filtered where they lie in ``data``; a span overlapping an earlier one gets its own
    # copy first, so nothing is filtered twice.
    parts: list[np.ndarray] = []
    reach = -1
    clipped = np.zeros(len(labels))
    samples = 0
    for k, item in enumerate(items):
        piece = data[item["start"] : item["end"]]
        if item["start"] < reach:
            piece = piece.copy()
        reach = max(reach, item["end"])
        for a in range(0, len(piece), BLOCK):
            clipped += (np.abs(piece[a : a + BLOCK]) >= 0.99 * full_scale_uv).sum(axis=0)  # NaN counts as not
        samples += len(piece)
        lost_here = lost_rows[item["start"] : item["end"]]
        if lost_here.any():
            fill = np.zeros(len(labels)) if lost_here.all() else np.nan_to_num(np.nanmean(piece[~lost_here], axis=0))
            rows, cols = np.nonzero(np.isnan(piece[lost_here]))
            piece[np.flatnonzero(lost_here)[rows], cols] = fill[cols]
            piece[np.isnan(piece)] = 0.0
        spectra.filter_in_place(piece, fs, mains)
        parts.append(piece)
        report(STAGES["load"] + (STAGES["filter"] - STAGES["load"]) * (k + 1) / len(items), f"Filtering: {titles.get(_key(item), _key(item))}")
    clip_fraction = clipped / max(samples, 1)
    sd = np.array([spectra.robust_sd(np.concatenate([p[:, c] for p in parts]))[()] for c in range(len(labels))])
    ptp = [spectra.segment_ptp(p, fs) for p in parts]
    ptp = [p for p in ptp if len(p)]
    loud = np.mean(np.concatenate(ptp) > spectra.PEAK_TO_PEAK_UV, axis=0) if ptp else np.zeros(len(labels))
    bad, reasons = spectra.judge_channels(clip_fraction, sd, loud, labels)
    good = ~bad

    per_span = []
    kept_total = all_total = 0
    for item, piece in zip(items, parts):
        kept, coef = spectra.segment_coefficients(piece, lost_rows[item["start"] : item["end"]], good, fs)
        per_span.append((item, kept, coef))
        kept_total += int(kept.sum())
        all_total += len(kept)
    # the samples are no longer needed: everything below works on the coefficients
    del parts, piece, data
    report(STAGES["filter"], "Power spectra")

    _, freqs = spectra.pass_bins(fs)
    order = []
    for item in items:
        if _key(item) not in order:
            order.append(_key(item))
    # one array of coefficients per round; each span's coefficients become a view into it
    coef_of = {}
    for key in order:
        here = [n for n, (s, _, _) in enumerate(per_span) if _key(s) == key]
        coef_of[key] = np.concatenate([per_span[n][2] for n in here], axis=0)
        at = 0
        for n in here:
            item, kept, coef = per_span[n]
            per_span[n] = (item, kept, coef_of[key][at : at + len(coef)])
            at += len(coef)
        del coef
    conditions = []
    psd_of = {}
    for key in order:
        coef = coef_of[key]
        spans_here = [s for s in items if _key(s) == key]
        total_segments = sum(len(k) for (s, k, _) in per_span if _key(s) == key)
        entry = {
            "id": key,
            "title": titles.get(key, key),
            "kind": "calibration" if key in ("eyes_open", "eyes_closed") else "round",
            "blocks": len({(s["step"], s["block"]) for s in spans_here}),
            "seconds": round(sum(s["end"] - s["start"] for s in spans_here) / fs, 1),
            "segments": total_segments,
            "kept": int(len(coef)),
        }
        if len(coef) < MIN_SPECTRUM_SEGMENTS:
            entry["error"] = f"only {len(coef)} of {total_segments} segments were kept, too few to compute"
            entry["spectrum"] = None
            entry["band_power"] = None
            entry["band_power_mean"] = None
            entry["relative"] = None
        else:
            psd = spectra.welch_coefficients(coef, fs)
            psd_of[key] = psd
            entry["spectrum"] = _round(psd[:, good].mean(axis=1), 5)
            power = {name: spectra.band_power(freqs, psd, band) for name, band in spectra.BANDS.items()}
            entry["band_power"] = {name: _round(np.where(good, value, np.nan), 4) for name, value in power.items()}
            entry["band_power_mean"] = {name: round(float(value[good].mean()), 4) for name, value in power.items()}
            whole = sum(float(value[good].mean()) for value in power.values())
            entry["relative"] = {name: round(float(value[good].mean()) / whole, 4) for name, value in power.items()}
        conditions.append(entry)
    report(STAGES["spectra"], "Links per round")

    upper = np.triu_indices(len(labels), 1)
    pair_good = good[upper[0]] & good[upper[1]]
    weight = [max(len(coef_of[e["id"]]), 1) for e in conditions]
    done = 0.0
    for n, entry in enumerate(conditions):
        coef = coef_of[entry["id"]]
        here = f"Links: {entry['title']}"
        span = (STAGES["connectivity"] - STAGES["spectra"]) * weight[n] / sum(weight)
        base = STAGES["spectra"] + (STAGES["connectivity"] - STAGES["spectra"]) * done
        report(base, here)
        if good.sum() < 3:
            entry["connectivity"], entry["connectivity_note"] = None, "too_few_electrodes"
        elif len(coef) < connectivity.MIN_SEGMENTS:
            entry["connectivity"], entry["connectivity_note"] = None, "not_enough"
        else:
            matrices = connectivity.dwpli(coef, freqs)
            ticks = [0]

            def tick(ticks=ticks, base=base, span=span, here=here):
                ticks[0] += 1
                report(base + span * 0.9 * ticks[0] / connectivity.SURROGATES, here)

            thresholds = connectivity.surrogate_threshold(coef, freqs, good, connectivity.SURROGATES, np.random.default_rng(1000 + n), tick=tick)
            entry["connectivity"] = {}
            for b, name in enumerate(spectra.BANDS):
                matrix = matrices[name]
                matrix[~good, :] = np.nan
                matrix[:, ~good] = np.nan
                found = communities.analyze(matrix, good, seed=n * 10 + b)
                threshold = thresholds[name]
                links = int(((matrix[upper] > threshold) & pair_good).sum()) if threshold is not None else 0
                entry["connectivity"][name] = {
                    "matrix": _round(matrix, 4),
                    "threshold": round(threshold, 4) if threshold is not None else None,
                    "links": links,
                    **found,
                }
        done += weight[n] / sum(weight)
        report(STAGES["spectra"] + (STAGES["connectivity"] - STAGES["spectra"]) * done, here)

    alpha = _alpha_ratio(conditions, psd_of, freqs, labels, good)
    report(STAGES["connectivity"], "Links over time")

    frames = []
    seconds_total = sum(len(k) for (_, k, _) in per_span) or 1
    done_segments = 0
    for s_index, (item, kept, coef) in enumerate(per_span):
        here = f"Links over time: {titles.get(_key(item), _key(item))}"
        starts_s = spectra.segment_starts(item["end"] - item["start"], fs) / fs
        window_starts, members = connectivity.window_members(starts_s, kept, spectra.SEGMENT_SECONDS)
        valid = [len(m) >= connectivity.MIN_SEGMENTS for m in members]
        if not any(valid) or good.sum() < 3:
            done_segments += len(kept)
            report(STAGES["connectivity"] + (STAGES["windows"] - STAGES["connectivity"]) * done_segments / seconds_total, here)
            continue
        length = min(len(m) for m, v in zip(members, valid) if v)
        thresholds = connectivity.surrogate_threshold(coef, freqs, good, connectivity.WINDOW_SURROGATES, np.random.default_rng(5000 + s_index), length=length)
        for w, (t, chosen) in enumerate(zip(window_starts, members)):
            frame = {
                "t": round(item["start"] / fs + float(t) + connectivity.WINDOW_SECONDS / 2, 2),
                "step": item["step"],
                "block": item["block"],
                "part": item["part"],
                "kept": int(len(chosen)),
                "bands": {},
            }
            if not valid[w]:
                frame["bands"] = {name: None for name in spectra.BANDS}
                frames.append(frame)
                continue
            matrices = connectivity.by_band(connectivity.per_bin(coef[chosen]), freqs)
            for b, name in enumerate(spectra.BANDS):
                matrix = matrices[name]
                threshold = thresholds[name]
                values = np.where(pair_good, matrix[upper], np.nan)
                links = int(np.nansum(values > threshold)) if threshold is not None else 0
                band = {"upper": _round(values, 3), "threshold": round(threshold, 4) if threshold is not None else None, "links": links}
                if links:
                    found = communities.analyze(matrix, good, seed=w * 10 + b, nulls=WINDOW_NULLS, restarts=WINDOW_RESTARTS)
                    band.update(q=found["q"], q_null_mean=found["q_null_mean"], q_null_sd=found["q_null_sd"], grouped=found["grouped"])
                    band["communities"] = found["communities"] if found["grouped"] else None
                else:
                    band.update(q=None, q_null_mean=None, q_null_sd=None, grouped=False, communities=None)
                frame["bands"][name] = band
            frames.append(frame)
            report(
                STAGES["connectivity"]
                + (STAGES["windows"] - STAGES["connectivity"]) * (done_segments + len(kept) * (w + 1) / len(window_starts)) / seconds_total,
                here,
            )
        done_segments += len(kept)

    analyzed = sum(item["end"] - item["start"] for item in items) / fs
    results = {
        "version": VERSION,
        "created_unix": time.time(),
        "compute_seconds": None,
        "recording": {
            "folder": RECORDING,
            "sample_rate": fs,
            "labels": labels,
            "seconds": round(len(lost_rows) / fs, 1),
            "lost_samples": int(lost_rows.sum()),
            "reference": "The recording is analyzed against the average of the good channels. "
            "The library's documentation does not state the headset's own reference electrode.",
        },
        "mains": {"notch_hz": mains, **{k: (round(v, 1) if v is not None else None) for k, v in mains_found.items()}},
        "channels": {
            "good": [bool(g) for g in good],
            "bad": reasons,
            "robust_sd_uv": _round(sd, 2),
            "loud_fraction": _round(loud, 3),
        },
        "analyzed": {
            "seconds": round(analyzed, 1),
            "segments": all_total,
            "kept": kept_total,
            "fraction_rejected": round(1 - kept_total / all_total, 4) if all_total else None,
        },
        "parameters": {
            "pass_band_hz": list(spectra.PASS),
            "notch_hz": mains,
            "segment_seconds": spectra.SEGMENT_SECONDS,
            "segment_step_seconds": spectra.STEP_SECONDS,
            "peak_to_peak_uv": spectra.PEAK_TO_PEAK_UV,
            "flat_uv": spectra.FLAT_UV,
            "loud_fraction": spectra.LOUD_FRACTION,
            "bands": spectra.BANDS,
            "connectivity": "debiased squared weighted phase-lag index (Vinck et al. 2011)",
            "min_segments_spectrum": MIN_SPECTRUM_SEGMENTS,
            "min_segments_connectivity": connectivity.MIN_SEGMENTS,
            "window_seconds": connectivity.WINDOW_SECONDS,
            "window_step_seconds": connectivity.WINDOW_STEP_SECONDS,
            "surrogates": connectivity.SURROGATES,
            "window_surrogates": connectivity.WINDOW_SURROGATES,
            "link_rule": "above the 95th percentile of the largest link in time-shifted surrogates",
            "community_restarts": communities.RESTARTS,
            "null_graphs": communities.NULLS,
            "window_null_graphs": WINDOW_NULLS,
            "group_sd": communities.GROUP_SD,
        },
        "freqs": _round(freqs, 2),
        "conditions": conditions,
        "alpha_ratio": alpha,
    }
    windows = {"version": VERSION, "labels": labels, "window_seconds": connectivity.WINDOW_SECONDS, "frames": frames}
    results["compute_seconds"] = round(time.monotonic() - started, 1)
    report(STAGES["done"], "Done")
    return results, windows


def _alpha_ratio(conditions, psd_of, freqs, labels, good) -> dict:
    """Alpha power (8-13 Hz) with eyes closed divided by that with eyes open, per channel: from
    the rest rounds, or from calibration's parts when a rest round is missing."""
    by_id = {c["id"]: c for c in conditions}
    for closed, opened, source in (("rest_closed", "rest_open", "rest rounds"), ("eyes_closed", "eyes_open", "calibration")):
        if closed in psd_of and opened in psd_of:
            band = spectra.BANDS["alpha"]
            a_closed = spectra.band_power(freqs, psd_of[closed], band)
            a_open = spectra.band_power(freqs, psd_of[opened], band)
            ratio = np.where(good & (a_open > 0), a_closed / np.where(a_open > 0, a_open, 1.0), np.nan)
            posterior = [i for i, name in enumerate(labels) if name in POSTERIOR and good[i]]
            values = ratio[posterior]
            return {
                "source": source,
                "closed": by_id[closed]["title"],
                "open": by_id[opened]["title"],
                "closed_kept_segments": by_id[closed]["kept"],
                "open_kept_segments": by_id[opened]["kept"],
                "per_channel": _round(ratio, 3),
                "posterior_channels": [labels[i] for i in posterior],
                "posterior_median": round(float(np.median(values)), 3) if len(values) else None,
            }
    missing = [c for c in ("rest_closed", "rest_open") if c not in by_id]
    why = "no eyes-closed and eyes-open data" if missing else "too little clean data in the rest rounds"
    return {"error": f"The eyes-closed and eyes-open comparison could not be computed: {why}."}

"""``brain_mapper.analysis`` on synthetic EEG with known answers.

The signals are written as real headset packets (``synthetic_eeg``) and read back by the library,
so every check runs the whole path a real session takes. The numbers each check found are printed
(run with ``-s`` to see them) and quoted in docs/brain-mapper-results.md.
"""

from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from brain_mapper import analysis
from brain_mapper.analysis import communities, pipeline, run, spectra

import synthetic_eeg as syn
from synthetic_eeg import ch

PACKAGE = Path(__file__).resolve().parents[2] / "brain_mapper" / "src" / "brain_mapper" / "analysis"


def build(tmp: Path) -> Path:
    rng = np.random.default_rng(11)
    n60 = 60 * syn.FS
    # eyes closed: a 10 Hz rhythm of 20 uV on O1 and (opposite sign, so the average reference
    # adds none of it elsewhere) on O2; eyes open: the same noise without it.
    closed = syn.noise(rng, 60)
    alpha = syn.narrowband(rng, n60, 10.0) * 20
    closed[:, ch("O1")] += alpha
    closed[:, ch("O2")] -= alpha
    opened = syn.noise(rng, 60)
    # words: C3 and P3 share a source filling the alpha band (8-13 Hz) with a lag of a quarter
    # cycle at 10.5 Hz; F4 and C4 share another with no lag (what volume conduction gives).
    words = syn.noise(rng, 60)
    s = syn.narrowband(rng, n60, 10.5, 5.0) * 15
    words[:, ch("C3")] += s
    words[:, ch("P3")] += syn.lagged(s, 10.5)
    r = syn.narrowband(rng, n60, 10.5, 5.0) * 15
    words[:, ch("F4")] += r
    words[:, ch("C4")] += r
    # flanker: a 6 Hz lagged pair T3-T4 for the first 60 s, then P7-P8 for the next 60 s.
    flanker = syn.noise(rng, 120)
    a = syn.narrowband(rng, n60, 6.0) * 15
    b = syn.narrowband(rng, n60, 6.0) * 15
    flanker[:n60, ch("T3")] += a
    flanker[:n60, ch("T4")] += syn.lagged(a, 6.0)
    flanker[n60:, ch("P7")] += b
    flanker[n60:, ch("P8")] += syn.lagged(b, 6.0)
    # spatial: a 400 uV square wave in 6 s of 60 on three channels (on every channel the average
    # reference would remove it).
    spatial = syn.noise(rng, 60)
    spatial[30 * syn.FS : 36 * syn.FS, [ch("Fz"), ch("P3"), ch("O1")]] += 400 * np.sign(np.sin(np.arange(6 * syn.FS) / syn.FS * 2 * np.pi * 0.5))[:, None]
    # groups: two planted groups of five electrodes, each sharing one alpha-band source at five
    # phases 40 degrees apart (so every pair in a group has a consistent lag)
    groups = syn.noise(rng, 60)
    for names in (("Fp1", "Fp2", "F3", "Fz", "F4"), ("P3", "Pz", "P4", "O1", "O2")):
        g = syn.narrowband(rng, n60, 10.5, 5.0) * 12
        for k, name in enumerate(names):
            groups[:, ch(name)] += syn.lagged(g, 10.5, 40.0 * k) if k else g
    # short: 20 s, fewer clean segments than links need
    short = syn.noise(rng, 20)
    conditions = [
        ("rest_closed", "rest", closed),
        ("rest_open", "rest", opened),
        ("words", "list1", words),
        ("flanker", "block1", flanker),
        ("spatial", "block1", spatial),
        ("groups", "block1", groups),
        ("short", "block1", short),
    ]
    # all along: A2 flat and F7 loud (300 uV of white noise)
    for _, _, data in conditions:
        data[:, ch("A2")] = 0.0
        data[:, ch("F7")] = rng.standard_normal(len(data)) * 300
    return syn.write_session(tmp, conditions)


@pytest.fixture(scope="module")
def computed(tmp_path_factory):
    folder = build(tmp_path_factory.mktemp("syn"))
    seen = []
    results, windows = pipeline.compute(folder, progress=lambda f, s: seen.append((f, s)))
    return folder, results, windows, seen


def condition(results, key):
    return next(c for c in results["conditions"] if c["id"] == key)


def test_a_10_hz_source_on_known_channels_gives_alpha_there_and_the_closed_open_ratio(computed):
    _, results, _, _ = computed
    labels = results["recording"]["labels"]
    closed = condition(results, "rest_closed")["band_power"]["alpha"]
    others = [v for i, v in enumerate(closed) if v is not None and labels[i] not in ("O1", "O2")]
    print("alpha power O1, O2, median of others (uV^2):", closed[ch("O1")], closed[ch("O2")], np.median(others))
    # 20 uV of 10 Hz is 400 uV^2 in the band; 10 uV of 1/f noise from 0.5 to 250 Hz puts
    # 100 * ln(13/8) / ln(500) = 7.8 uV^2 there
    assert 330 < closed[ch("O1")] < 500 and 330 < closed[ch("O2")] < 500
    assert 4 < np.median(others) < 12
    ratio = results["alpha_ratio"]
    print("eyes-closed / eyes-open alpha: O1", ratio["per_channel"][ch("O1")], "F3", ratio["per_channel"][ch("F3")], "posterior median", ratio["posterior_median"])
    assert ratio["source"] == "rest rounds"
    # known answer: (400 + 7.8) / 7.8 = 52 at O1 and O2, 1 elsewhere
    assert 40 < ratio["per_channel"][ch("O1")] < 70 and 40 < ratio["per_channel"][ch("O2")] < 70
    assert 0.6 < ratio["per_channel"][ch("F3")] < 1.6
    relative = condition(results, "rest_closed")["relative"]
    assert max(relative, key=relative.get) == "alpha"


def test_a_lagged_pair_gives_high_dwpli_and_zero_lag_mixing_gives_about_zero(computed):
    _, results, _, _ = computed
    m = np.array(condition(results, "words")["connectivity"]["alpha"]["matrix"], dtype=float)
    lagged, zero = m[ch("C3"), ch("P3")], m[ch("F4"), ch("C4")]
    good = [i for i, g in enumerate(results["channels"]["good"]) if g]
    source = {ch("C3"), ch("P3")}
    # The average reference puts -(C3 + P3)/20 into every channel, so pairs with C3 or P3 pick up
    # a weak lagged relation of their own; pairs of neither channel do not.
    touching = [abs(m[i, j]) for i in good for j in good if i < j and len({i, j} & source) == 1]
    rest = [abs(m[i, j]) for i in good for j in good if i < j and not {i, j} & source and {i, j} != {ch("F4"), ch("C4")}]
    print(
        f"dwPLI alpha: lagged C3-P3 {lagged:.3f}, zero-lag F4-C4 {zero:.3f}, pairs with one of C3/P3 max {max(touching):.3f}, "
        f"other pairs max {max(rest):.3f} median {np.median(rest):.3f}"
    )
    assert lagged > 0.9
    assert abs(zero) < 0.1
    assert max(touching) < lagged / 2
    assert max(rest) < 0.1 and np.median(rest) < 0.03
    assert np.allclose(m[good][:, good], m[good][:, good].T, equal_nan=True)


def test_windows_recover_a_planted_switch_between_two_states(computed):
    _, results, windows, _ = computed
    labels = results["recording"]["labels"]
    frames = [f for f in windows["frames"] if f["step"] == "flanker"]
    upper = np.triu_indices(len(labels), 1)
    index = {(int(i), int(j)): k for k, (i, j) in enumerate(zip(*upper))}

    def value(frame, a, b):
        i, j = sorted((ch(a), ch(b)))
        v = frame["bands"]["theta"]["upper"][index[(i, j)]]
        return v

    half = windows["window_seconds"] / 2
    step_start = min(f["t"] for f in frames) - half
    rows = [(round(f["t"] - step_start, 1), value(f, "T3", "T4"), value(f, "P7", "P8")) for f in frames]
    print("window centre (s from the round's start), dwPLI theta T3-T4, P7-P8:", rows)
    first = [f for f, r in zip(frames, rows) if r[0] + half <= 60]
    second = [f for f, r in zip(frames, rows) if r[0] - half >= 60]
    assert first and second
    assert all(t > 0.6 and p < 0.25 for _, t, p in [r for r in rows if r[0] + half <= 60])
    assert all(p > 0.6 and t < 0.25 for _, t, p in [r for r in rows if r[0] - half >= 60])
    # the links drawn (above the time-shifted surrogates) are T3-T4 first and P7-P8 last
    for f in first:
        band = f["bands"]["theta"]
        assert value(f, "T3", "T4") > band["threshold"] > value(f, "P7", "P8")
    for f in second:
        band = f["bands"]["theta"]
        assert value(f, "P7", "P8") > band["threshold"] > value(f, "T3", "T4")


def test_flat_and_loud_channels_are_marked_bad_and_artifacts_are_rejected(computed):
    _, results, _, _ = computed
    bad = results["channels"]["bad"]
    print("bad channels:", bad, "analyzed:", results["analyzed"])
    assert set(bad) == {"A2", "F7"}
    assert bad["A2"].startswith("flat") and bad["F7"].startswith("above 200 uV")
    spatial = condition(results, "spatial")
    # a square wave of 400 uV from 30 to 36 s: the 2 s segments (every 1 s) that overlap it are
    # rejected, 7 of them (starts 29 to 35), plus at most one on each side reached by the filter's
    # ringing at its edges
    assert 7 <= spatial["segments"] - spatial["kept"] <= 9
    assert 0 < results["analyzed"]["fraction_rejected"] < 0.05
    assert results["recording"]["lost_samples"] == 0


def test_progress_rises_to_one_and_moves_per_stage_and_round(computed):
    _, _, _, seen = computed
    fractions = [f for f, _ in seen]
    assert fractions[0] == 0.0 and fractions[-1] == 1.0 and all(b >= a for a, b in zip(fractions, fractions[1:]))
    stages = {s for _, s in seen}
    for title in ("Words", "Flanker", "Groups"):
        assert f"Filtering: {title}" in stages and f"Links: {title}" in stages and f"Links over time: {title}" in stages, stages
    # the bar moves in many small steps, never more than a tenth at once
    assert len(set(fractions)) > 50
    assert max(b - a for a, b in zip(fractions, fractions[1:])) < 0.1


def test_modularity_and_communities_of_known_graphs():
    # two triangles with no link between them: Q = 1/2 exactly
    w = np.zeros((6, 6))
    for a, b in [(0, 1), (1, 2), (0, 2), (3, 4), (4, 5), (3, 5)]:
        w[a, b] = w[b, a] = 1.0
    labels, q = communities.best_partition(w, 5, np.random.default_rng(0))
    assert abs(q - 0.5) < 1e-9 and len(set(labels[:3])) == 1 and labels[0] != labels[3]
    # two planted blocks of 10 nodes in a weighted graph: recovered, and far above shuffled graphs
    rng = np.random.default_rng(3)
    w = rng.uniform(0, 0.1, (20, 20))
    w[:10, :10] += 0.4
    w[10:, 10:] += 0.4
    w = np.triu(w, 1) + np.triu(w, 1).T
    found = communities.analyze(w, np.ones(20, dtype=bool), seed=1)
    print("planted blocks: Q", found["q"], "null mean", found["q_null_mean"], "z", found["z"])
    assert found["communities"][:10] == [found["communities"][0]] * 10
    assert found["communities"][10:] == [found["communities"][10]] * 10
    assert found["communities"][0] != found["communities"][10]
    assert found["z"] > 5
    # no structure: weights drawn independently
    w = rng.uniform(0, 0.5, (20, 20))
    w = np.triu(w, 1) + np.triu(w, 1).T
    found = communities.analyze(w, np.ones(20, dtype=bool), seed=2)
    print("no structure: Q", found["q"], "z", found["z"])
    assert abs(found["z"]) < 3


def test_the_filter_passes_the_band_and_removes_mains_and_drift():
    t = np.arange(20 * syn.FS) / syn.FS
    x = np.stack([np.sin(2 * np.pi * 10 * t), np.sin(2 * np.pi * 60 * t), np.sin(2 * np.pi * 0.1 * t) * 100 + 5000], axis=1)
    y = spectra.filtered(x, syn.FS, 60.0)
    core = slice(2 * syn.FS, -2 * syn.FS)
    print("rms after the filter: 10 Hz", y[core, 0].std(), "60 Hz", y[core, 1].std(), "0.1 Hz + offset", y[core, 2].std())
    assert abs(y[core, 0].std() - 1 / np.sqrt(2)) < 0.01
    assert y[core, 1].std() < 1e-3
    assert y[core, 2].std() < 1.0
    assert spectra.gain(np.array([60.0, 50.0, 10.0]), 50.0)[1] == 0.0


def test_a_session_without_eeg_is_not_computable_and_a_job_says_so(tmp_path, caplog):
    folder = syn.write_session(tmp_path, [("words", "list1", syn.noise(np.random.default_rng(0), 30))])
    raw = folder / "eeg" / "raw.bin"
    raw.rename(folder / "eeg" / "raw.bin.moved")  # this test's own synthetic file
    with pytest.raises(analysis.NotComputable):
        pipeline.compute(folder)
    job = run.Job(folder, label="SYN01").start()
    job.thread.join(30)
    status = job.status()
    assert status["state"] == "failed"
    assert status["message"].startswith("The results could not be computed")
    assert not (folder / "analysis").exists()
    logged = [r.getMessage() for r in caplog.records if "results of the session could not be computed" in r.getMessage()]
    assert len(logged) == 1 and "SYN01" not in logged[0] and str(tmp_path) not in logged[0], logged


def test_saving_makes_new_files_and_an_additional_zip_and_never_replaces_anything(computed, tmp_path):
    folder, results, windows, _ = computed
    sessions = folder.parent
    first_zip = sessions / f"{folder.name}.zip"
    first_zip.write_bytes(b"the session zip stands for itself here")
    before = hashlib.sha256(first_zip.read_bytes()).hexdigest()
    out, made = run.save(folder, results, windows)
    assert out == folder / "analysis" and made == sessions / f"{folder.name}-analysis.zip"
    with zipfile.ZipFile(made) as z:
        assert sorted(z.namelist()) == ["analysis/results.json", "analysis/windows.json"]
        assert json.loads(z.read("analysis/results.json"))["version"] == pipeline.VERSION
    # a second save goes to analysis-2/ and -analysis-2.zip; nothing earlier is touched
    stamp = (out / "results.json").read_bytes()
    out2, made2 = run.save(folder, results, windows)
    assert out2.name == "analysis-2" and made2.name == f"{folder.name}-analysis-2.zip"
    assert (out / "results.json").read_bytes() == stamp
    assert hashlib.sha256(first_zip.read_bytes()).hexdigest() == before
    assert run.finished_dir(folder) == out2


def test_nothing_in_the_analysis_package_deletes_renames_over_or_truncates():
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"rmtree", "unlink", "rmdir", "removedirs", "remove", "replace", "rename"}, (path.name, node.lineno)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open" and len(node.args) > 1:
                mode = node.args[1].value if isinstance(node.args[1], ast.Constant) else ""
                assert "x" in mode, (path.name, node.lineno, mode)


def coefficients_without_reference(x: np.ndarray) -> np.ndarray:
    """The Fourier coefficients the analysis uses (2 s Hann segments every 1 s, 1 to 45 Hz), here
    without the average reference, which would tie together the few channels of these tests."""
    index, _ = spectra.pass_bins(syn.FS)
    seg = np.transpose(spectra.segment_view(x, syn.FS), (0, 2, 1))
    seg = seg - seg.mean(axis=1, keepdims=True)
    coef = np.fft.rfft(seg * np.hanning(seg.shape[1])[None, :, None], axis=1)[:, index, :]
    return coef.astype(np.complex64)


def test_the_minimum_of_clean_data_keeps_a_true_zero_pair_under_a_tenth_in_95_percent(computed):
    from brain_mapper.analysis import connectivity

    _, freqs = spectra.pass_bins(syn.FS)
    rng = np.random.default_rng(21)
    found = {}
    for k in (20, connectivity.MIN_SEGMENTS):
        values = {"delta": [], "alpha": []}
        for _ in range(300):
            coef = coefficients_without_reference(syn.noise(rng, k + 1)[:, :2])
            assert len(coef) == k
            m = connectivity.by_band(connectivity.per_bin(coef), freqs, {b: spectra.BANDS[b] for b in values})
            for b in values:
                values[b].append(m[b][0, 1])
        found[k] = {b: float(np.percentile(v, 95)) for b, v in values.items()}
    print("95th percentile of dwPLI for a true-zero pair:", found)
    assert found[connectivity.MIN_SEGMENTS]["delta"] < 0.1 and found[connectivity.MIN_SEGMENTS]["alpha"] < 0.1
    assert found[20]["delta"] > 0.1
    # a round with fewer kept segments gets no links, and the page is told why
    _, results, _, _ = computed
    short = condition(results, "short")
    assert short["kept"] < connectivity.MIN_SEGMENTS and short["connectivity"] is None
    assert short["connectivity_note"] == "not_enough" and short["spectrum"] is not None
    assert results["parameters"]["min_segments_connectivity"] == 30


def test_links_are_drawn_only_above_time_shifted_surrogates(computed):
    from brain_mapper.analysis import connectivity

    _, results, _, _ = computed
    words = condition(results, "words")["connectivity"]["alpha"]
    m = np.array(words["matrix"], dtype=float)
    print("words alpha: threshold", words["threshold"], "links", words["links"], "C3-P3", m[ch("C3"), ch("P3")], "F4-C4", m[ch("F4"), ch("C4")])
    assert m[ch("C3"), ch("P3")] > words["threshold"] > abs(m[ch("F4"), ch("C4")])
    # the drawn links are C3-P3 and pairs with one of C3 or P3, which the average reference ties
    # to the lagged pair (see the test above); none between other electrodes
    good = results["channels"]["good"]
    drawn = [(i, j) for i in range(20) for j in range(i + 1, 20) if good[i] and good[j] and m[i, j] > words["threshold"]]
    assert words["links"] == len(drawn) and (ch("C3"), ch("P3")) in drawn
    assert all({i, j} & {ch("C3"), ch("P3")} for i, j in drawn), drawn
    # with no relation between channels, a round shows any link at all in about 5 % of cases
    _, freqs = spectra.pass_bins(syn.FS)
    rng = np.random.default_rng(5)
    with_links = 0
    rounds = 20
    for r in range(rounds):
        coef = coefficients_without_reference(syn.noise(rng, 61))
        matrix = connectivity.dwpli(coef, freqs, {"alpha": spectra.BANDS["alpha"]})["alpha"]
        threshold = connectivity.surrogate_threshold(
            coef, freqs, np.ones(20, dtype=bool), connectivity.SURROGATES, np.random.default_rng(r), bands={"alpha": spectra.BANDS["alpha"]}
        )["alpha"]
        with_links += int((matrix[np.triu_indices(20, 1)] > threshold).any())
    print(f"rounds of independent noise with a link drawn: {with_links} of {rounds}")
    assert with_links <= 4  # binomial(20, 0.05): 5 or more has a probability of 0.3 %


def test_groups_are_shown_only_when_q_stands_above_shuffled_graphs(computed):
    rng = np.random.default_rng(8)
    planted = rng.uniform(0, 0.1, (20, 20))
    planted[:10, :10] += 0.4
    planted[10:, 10:] += 0.4
    planted = np.triu(planted, 1) + np.triu(planted, 1).T
    found = communities.analyze(planted, np.ones(20, dtype=bool), seed=3)
    assert found["grouped"] and found["q"] > found["q_null_mean"] + 2 * found["q_null_sd"]
    random = rng.uniform(0, 0.5, (20, 20))
    random = np.triu(random, 1) + np.triu(random, 1).T
    found = communities.analyze(random, np.ones(20, dtype=bool), seed=4)
    print("random graph: Q", found["q"], "shuffled", found["q_null_mean"], "+-", found["q_null_sd"])
    assert not found["grouped"]
    # in the recording: the two planted groups are found and shown; a round of noise is not grouped
    _, results, windows, _ = computed
    g = condition(results, "groups")["connectivity"]["alpha"]
    print("groups round alpha: Q", g["q"], "shuffled", g["q_null_mean"], "+-", g["q_null_sd"], "links", g["links"])
    assert g["grouped"]
    front = {g["communities"][ch(n)] for n in ("Fp1", "Fp2", "F3", "Fz", "F4")}
    back = {g["communities"][ch(n)] for n in ("P3", "Pz", "P4", "O1", "O2")}
    assert len(front) == 1 and len(back) == 1 and front != back
    noise = condition(results, "rest_open")["connectivity"]["alpha"]
    print("rest_open alpha: Q", noise["q"], "shuffled", noise["q_null_mean"], "+-", noise["q_null_sd"])
    assert not noise["grouped"]
    # the same rule everywhere, per round and per window
    for c in results["conditions"]:
        for band in (c["connectivity"] or {}).values():
            assert band["grouped"] == (band["q"] > band["q_null_mean"] + 2 * band["q_null_sd"])
    seen_grouped = seen_not = 0
    for f in windows["frames"]:
        for band in f["bands"].values():
            if band is None:
                continue
            if band["grouped"]:
                seen_grouped += 1
                assert band["q"] > band["q_null_mean"] + 2 * band["q_null_sd"] and band["communities"] is not None
            else:
                seen_not += 1
                assert band["communities"] is None
    assert seen_grouped and seen_not
    assert any(f["step"] == "groups" and f["bands"]["alpha"]["grouped"] for f in windows["frames"] if f["bands"]["alpha"])


def test_the_filter_in_pieces_matches_the_filter_of_the_whole_span():
    rng = np.random.default_rng(2)
    x = syn.noise(rng, 200) + np.linspace(0, 300, 200 * syn.FS)[:, None]
    whole = spectra.filtered(x, syn.FS, 60.0)
    pieces = x.astype(np.float32)
    spectra.filter_in_place(pieces, syn.FS, 60.0)
    error = np.abs(pieces - whole).max()
    print("largest difference (uV) between the filter in pieces and of the whole:", error, "signal SD", whole.std())
    assert error < 0.02 * whole.std()


def test_welch_from_coefficients_matches_welch_of_the_segments():
    rng = np.random.default_rng(4)
    x = syn.noise(rng, 30)
    index, _ = spectra.pass_bins(syn.FS)
    coef = coefficients_without_reference(x)
    freqs, psd = spectra.welch(spectra.segments(x, syn.FS), syn.FS)
    assert np.allclose(spectra.welch_coefficients(coef, syn.FS), psd[index], rtol=1e-4)


def test_loading_chunk_by_chunk_matches_the_library_reader_with_lost_samples(tmp_path):
    rng = np.random.default_rng(9)
    drop = np.r_[5000:5030, 9000:9001]
    folder = syn.write_session(tmp_path, [("words", "list1", syn.noise(rng, 30))], drop=drop)
    import open_cognionics as oc

    expected = oc.read_recording(folder / "eeg").data * 1e6
    config, meta, data, lost_rows = pipeline.load(folder / "eeg")
    lost = np.isnan(expected).any(axis=1)
    print("samples", len(data), "lost", int(lost.sum()))
    assert data.dtype == np.float32 and data.shape == expected.shape
    assert lost.sum() == len(drop) and np.array_equal(np.isnan(data).any(axis=1), lost) and np.array_equal(lost_rows, lost)
    assert np.allclose(data[~lost], expected[~lost], atol=1e-3)
    assert list(config.labels) == syn.LABELS and meta["eeg_volts_per_count"] == syn.VOLTS_PER_COUNT


def test_a_45_minute_session_is_analyzed_in_under_400_mb(tmp_path):
    """A synthetic 45-minute, 20-channel, 500 Hz session (nine rounds of 5 minutes): the
    analysis runs as its own process, as the CLI does, and its peak resident memory is measured."""
    import resource
    import subprocess
    import sys

    rng = np.random.default_rng(45)
    conditions = []
    for k in range(9):
        x = syn.noise(rng, 300)
        s = syn.narrowband(rng, 300 * syn.FS, 10.5, 5.0) * 10
        x[:, ch("C3")] += s
        x[:, ch("P3")] += syn.lagged(s, 10.5)
        conditions.append((f"round{k}", "block1", x))
    folder = syn.write_session(tmp_path, conditions)
    del conditions
    out = tmp_path / "out"
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    done = subprocess.run(
        [sys.executable, "-m", "brain_mapper.analysis", str(folder), "--out", str(out), "--no-zip"],
        capture_output=True,
        text=True,
        timeout=900,
        env={**__import__("os").environ, "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4", "VECLIB_MAXIMUM_THREADS": "4"},
    )
    print(done.stdout[-2000:], done.stderr[-2000:])
    assert done.returncode == 0
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    assert peak > before or before == 0
    mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3
    results = json.loads((out / "results.json").read_text())
    print(f"peak RSS of the analysis process: {mb:.0f} MB; {results['recording']['seconds']} s; compute {results['compute_seconds']} s")
    assert results["recording"]["seconds"] >= 45 * 60
    assert mb < 400
    alpha = condition(results, "round0")["connectivity"]["alpha"]
    assert np.array(alpha["matrix"], dtype=float)[ch("C3"), ch("P3")] > alpha["threshold"]

# brain-mapper: results of a session

After a session's zip is saved, brain-mapper computes results from that session's own recording
and shows them on a results screen reached from the summary. Nothing here grades the person: the
numbers describe this recording, nothing is compared with other people, and nothing is a
diagnosis, a trait or a score of ability.

## When and where it runs

- `SessionRecord` (server.py) starts the analysis (`brain_mapper.analysis.Job`, a thread of its
  own) only after the zip is saved and the page has been told so. The recording is closed and
  the zip is made by then, so the analysis cannot delay or change either. A session without
  `eeg/raw.bin` is not analyzed.
- The results are written as new files into `analysis/` inside the session folder:
  `windows.json` first, then `results.json` (so a folder with `results.json` is whole). Each file
  is created exclusively; nothing that exists is changed, replaced or deleted. If `analysis/`
  exists already (an attempt that did not finish), the next free name is used: `analysis-2/` ...
- Then an additional zip of that folder is written beside the session zip,
  `<session folder>-analysis.zip` (`-analysis-2.zip` ... when taken), read back and given its name
  the way the session zip is (`package.place`). The session zip is never touched.
- A failure is caught and logged as one line without the label or paths ("the results of the
  session could not be computed: ..."), and the page says "The results could not be computed."
  The session folder and zip stay as they are.
- By hand: `uv run python -m brain_mapper.analysis <session folder>` (writes `analysis/` and the
  additional zip as above), or with `--out <folder>` to write the two files there and only read the
  session folder; `--no-zip` leaves out the additional zip.

|Route (participant page only, with the key of the zip download)|Answer|
|-|-|
|`GET /api/results?key=`|`{"state": "none"/"running"/"done"/"failed", "progress", "stage", "elapsed", "message", "results"}`; `results` is the content of `results.json` once done|
|`GET /api/results/windows?key=`|The content of `windows.json` once done, else 404|

The `package` message gives the participant connection `results`, the first address with its
key, next to `download`. An onlooker gets neither.

The files (`"version": 2`): in `results.json`, each entry of `conditions` has `connectivity`,
per band `{"matrix", "threshold", "links", "communities", "q", "q_null_mean", "q_null_sd", "z",
"grouped"}`, or `null` with `connectivity_note` `"not_enough"` (fewer than 30 kept segments) or
`"too_few_electrodes"`. In `windows.json`, each of `frames` has per band `{"upper", "threshold",
"links", "q", "q_null_mean", "q_null_sd", "grouped", "communities"}` (`upper`: the values above
the diagonal, row by row; `communities` is `null` unless `grouped`), or `null` for a window with
fewer than 30 kept segments.

## What is computed

All of it uses numpy only. The constants are in `brain_mapper/analysis/*.py`.

**What is analyzed.** The recording `eeg/` (recordings set aside during device setup hold no
rounds). Every block of every round that has a start and an end in the recording, plus the
eyes-open and eyes-closed parts of calibration; stretches between `paused` and `resumed` are cut
out. A condition is a round (all its blocks together) or a calibration part.

**Reference.** The library's documentation does not state which electrode the headset records
against. The analysis subtracts the average of the good channels from every channel (the design's
step 6.5.1). A side effect, measured on synthetic data: a lagged source on two channels puts a
weak lagged copy into every channel, so pairs with one of those two channels show a dwPLI of up
to 0.24 where the true value is 0 (0.44 with white background noise).

**Memory.** The recording is read chunk by chunk into one float32 array (a lost sample is a row
of NaN); each span is filtered where it lies, in pieces of 40 s with 8 s of its neighbors on
each side; only the Fourier coefficients from 1 to 45 Hz of the kept 2 s segments (complex64)
are kept after that, and the samples are released. Cross-spectra are formed 32 segments at a
time. A synthetic 45-minute, 20-channel, 500 Hz session (nine rounds of 5 minutes) peaked at
262 MB of resident memory in its own process (the first version: 3.3 GB) and took 64 s
(`test_a_45_minute_session_is_analyzed_in_under_400_mb`).

**Filter.** Zero-phase, in the frequency domain, on each analyzed span (mean and linear trend
removed, 2 s mirrored at each end; spans longer than 56 s in pieces as above, which differ from
filtering the whole span by at most 1.4 % of the signal's standard deviation): band-pass 1-45 Hz (raised-cosine edges 0.5-1 Hz and 45-50 Hz)
times a notch at 60 Hz and its harmonics (0 within 1 Hz, back to 1 at 2 Hz). The mains frequency
is not in the library's documentation; 60 Hz is the mains frequency where the headset is used, and
`results.json` reports how far the unfiltered spectrum stands above its neighbors at 50 and at
60 Hz so it can be checked.

**Bad channels** (whole session): at the converter's limit (99 % of full scale) in more than 1 %
of samples; flat (robust standard deviation below 0.5 uV); or above 200 uV peak-to-peak in more
than half of the 2 s segments. Bad channels are left out of the reference, the spectra averages,
the ratio and the networks.

**Bad segments.** The data are cut into 2 s segments every 1 s. A segment is rejected if it holds
a lost sample or any good channel exceeds 200 uV peak-to-peak. The fraction rejected is reported.

**Spectra and band power.** Welch's method on the kept segments (Hann window, 0.5 Hz bins),
uV^2/Hz. Band power is the sum over the band's bins times 0.5 Hz: delta 1-4, theta 4-8,
alpha 8-13, beta 13-30, low gamma 30-45 Hz (lower edge included). The share of each band is its
power over the sum of the five, averaged over good channels. A condition with fewer than 5 kept
segments has no spectrum.

**Eyes closed and open.** Alpha power in the eyes-closed rest round divided by that in the
eyes-open rest round, per channel (from the calibration parts when a rest round is missing), and
the median over the good channels among O1, O2, P3, P4, Pz, P7, P8. The headline says "about"
and gives the numbers of clean segments it rests on; with fewer than 30 on either side it adds
that the rounds were short and the number could come out quite differently in another session.

**Connectivity.** The debiased estimator of the squared weighted phase-lag index (dwPLI):
Vinck M, Oostenveld R, van Wingerden M, Battaglia F, Pennartz CMA (2011), *An improved index of
phase-synchronization for electrophysiological data in the presence of volume-conduction, noise
and sample-size bias*, NeuroImage 55:1548-1565,
<https://doi.org/10.1016/j.neuroimage.2011.01.055>. Per frequency bin over the kept segments, then
averaged over the band's bins. What it means: how consistently one electrode's signal leads or
lags the other's at that frequency, weighted by the size of the imaginary part of the
cross-spectrum; activity both electrodes pick up at the same instant (volume conduction, a common
reference) adds nothing. What it does not mean: not the strength of communication between brain
regions, not a direction, not an anatomical connection; the nodes are scalp electrodes. It is
computed per condition and in windows of 40 s every 5 s inside each block, each from at least
30 kept segments (a review of the literature found no support
for estimates from a few seconds).

*Why 30.* For two independent channels of 1/f noise (true value 0), the 95th percentile of the
band value over 300 simulations per number of kept segments was, at 20 and at 30 segments:
delta 0.157 and 0.078, theta 0.092 and 0.070, alpha 0.104 and 0.063, beta 0.059 and 0.035, low
gamma 0.060 and 0.037. 30 is the fewest of the counts tried (10, 15, 20, 30, 40, 60, 90) at which
a true-zero pair stays under 0.1, a tenth of the scale, in 95 % of cases in every band. A round
or window with fewer has no links, and the page says "Not enough clean recording in this round
to estimate links." A window is 40 s so that it holds 39 segments and can lose 9.

*Which links are drawn.* Not a fixed share. Each surrogate shifts every good channel's sequence of
kept segments by its own whole number of segments, any two channels' shifts at least 2 apart
(segments overlap by 1 s), which keeps each channel's spectrum and breaks any consistent relation
between channels; the largest dwPLI over all pairs is taken. A link is drawn only above the 95th
percentile of those largest values: 100 surrogates per round, and 40 per block for its windows
(each surrogate the length of the block's shortest window). With no relation at all, a round
shows any link in about 5 % of cases: 2 of 20 rounds of independent noise did in the test. If
none survives, the page says "No link in this round and band is stronger than in time-shifted
copies of the same recording." The average reference's side effect (above) is a real lagged
relation in the referenced data, and such links do survive.

**Groups and modularity** (the design's "segmentation", steps 6.5.6-7, as far as built). The
graph is the good electrodes with dwPLI below 0 set to 0. Groups are found by the Louvain method
(Blondel et al. 2008, <https://doi.org/10.1088/1742-5468/2008/10/P10008>) from 20 random orders,
keeping the partition with the highest weighted modularity Q (Newman 2006,
<https://doi.org/10.1073/pnas.0601602103>), with any group that is not connected split into its
connected parts. Q is compared with 50 graphs whose link weights are shuffled among the pairs
(mean and standard deviation, and z; 20 graphs and 6 restarts for a window, computed only for
windows with a link drawn). Groups are shown only when Q is above the shuffled mean plus 2
standard deviations (`grouped`); otherwise electrodes and links are drawn in one neutral color
and the page says "The links in this round and band do not split into groups more than shuffled
links do." Not built: the Leiden method, a consensus over restarts, node (source) space. Q describes how the links divide into
groups; the literature does not support reading it as good or bad.

## Checks on synthetic signals with known answers

`tests/brain_mapper/test_analysis.py` writes signals as real headset packets
(`tests/brain_mapper/synthetic_eeg.py`), reads them back with the library and runs the whole
analysis. Background: 10 uV of independent 1/f noise per channel. Numbers found on 2026-11-26:

|Planted|Expected|Found|
|-|-|-|
|20 uV of 9-11 Hz noise on O1 and (opposite sign) O2, eyes closed only|alpha power about 400 + 7.8 uV^2 there, 7.8 elsewhere; closed/open ratio about 52 at O1, O2 and 1 elsewhere|415.5 and 415.3 uV^2; others median 7.5; ratio O1 57.9, F3 1.00|
|C3 and P3 share an 8-13 Hz source, a quarter cycle apart|dwPLI near 1|0.996|
|F4 and C4 share another source with no lag|about 0|0.019|
|Pairs of other channels|about 0|max 0.052, median 0.011|
|T3-T4 lagged at 6 Hz for 60 s, then P7-P8|windows wholly in the first part show T3-T4 only, then P7-P8 only|T3-T4 0.77-0.85 and P7-P8 below 0.02 in windows ending by 60 s; P7-P8 0.79-0.85 and T3-T4 below 0.03 in windows starting after 60 s|
|Two planted blocks of 10 nodes|both found, z well above shuffled|found; Q 0.388 against 0.111, z 24.3|
|Independent random weights|z near 0, not grouped|z -0.45; another graph Q 0.048 against 0.057 ± 0.008, not grouped|
|Two groups of 5 electrodes, each sharing an 8-13 Hz source at phases 40 degrees apart|grouped, the two groups found|Q 0.174 against 0.129 ± 0.012, grouped, both found|
|Independent noise (eyes-open round)|not grouped|Q 0.273 against 0.274 ± 0.026|
|C3-P3 lagged (above)|drawn; only pairs with C3 or P3 drawn besides|threshold 0.080; 27 links, all with C3 or P3|
|20 rounds of independent noise, 60 s each|a link in about 1 of 20|2 of 20|
|A true-zero pair at 20 and 30 segments|95th percentile above, then below 0.1|delta 0.121, then 0.080; alpha 0.097, then 0.065|
|A round of 20 s|no links, the page says why|19 kept, none|
|A2 flat, F7 300 uV white noise|both bad|A2 flat, F7 above 200 uV in 100 % of segments|
|400 uV square wave on 3 channels for 6 s of 60|7 segments rejected, up to 9 with filter ringing|9|
|10 Hz, 60 Hz and 0.1 Hz drift through the filter|10 Hz kept, 60 Hz and drift removed|rms 0.7071 (of 0.7071), 2.5e-5, 0.031|

## The results screen

On the summary (and the ended screen), the session record shows "Your results" (`#results-entry`):
above the points and the timeline: a progress bar (`#results-progress`) and a line with the
percent done, the time left estimated from the time so far, and the stage and round being
computed (the bar moves per round and per surrogate), then "See your results" (`#results-open`), or "The results could not be
computed." The screen (`#results`, over the page) has four steps in one path, with Back and Next
and numbered tabs, Escape or "Back to the summary" to leave:

1. **Points**: the points of each round as on the summary (`behavior/scores.json`, not
   recomputed).
2. **Rhythms**: the power spectrum of every round (the chosen one in blue) and the share of each
   band per round.
3. **Eyes closed and open**: the ratio per electrode on a log scale, the electrodes at the back of
   the head in blue, and the median there in one sentence.
4. **Networks**: the 3-D view (`scene3d/network-view.js`) with this session's links: each
   electrode drawn just inside the head under its place on the scalp, the links of the chosen band
   and round that stand above time-shifted copies, colors by group only where the groups stand
   above shuffled links (one neutral color otherwise, with the sentence that says so); "Play over
   time" steps through the 40 s windows (0.7 s each) with the round and time shown, applying the
   same two rules in each window; a slider picks a window.

Every figure has one sentence saying what was computed; a figure that could not be computed for
this session says why instead. `tests/brain_mapper/test_results_browser.py` runs a whole session
on the simulated headset (playing 60 s of synthetic 1/f noise with one lagged pair, looped, so every
round has links and windows), checks every figure against the files the analysis wrote, the failure
path, the zip's hash before and after, and the layout at 3840x1600, 1920x1080, 1512x982 and
1024x768.

The onlooker window's network view still shows sample data during a session (no live estimate
yet).

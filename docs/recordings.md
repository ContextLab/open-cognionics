# Recordings

`Headset.record(directory, participant)` creates the directory (and parents) and five files in it.

|File|Content|
|-|-|
|`raw.bin`|The bytes exactly as received from the headset, after the configuration reply.|
|`markers.jsonl`|One JSON object per marker.|
|`clock.jsonl`|Host time of sample positions, from the live clock model, about once per second.|
|`meta.json`|Session metadata and the headset configuration.|
|`recording.bdf`|BDF+ export of the EEG and accelerometer data and the markers.|

`raw.bin` together with `meta.json` is the source of truth. `recording.bdf` and the sample numbers in `markers.jsonl` and `clock.jsonl` are derived from the same bytes as they are saved. This holds for recordings made by `Headset.record` and by the local web server.

If the directory already holds any of these five files, `record` raises `FileExistsError` and changes nothing in it.

## Sample numbers

Every sample number saved with a recording counts from the first whole packet in `raw.bin`, which is sample 0. Lost samples count. `replay` and `read_recording` use the same numbers. This applies to `markers.jsonl`, `clock.jsonl`, the BDF annotations (markers and `lost <n>`) and the server's `stream_start` marker, which is at sample 0.

## When a recording begins

A recording begins with the first bytes read after recording was started. The recording is written on the reader thread from the raw bytes, so it does not depend on anyone iterating `stream()`. A slow consumer of `stream()` can miss chunks while the recording still holds every byte. A missed chunk shows only as a gap in `Chunk.index`; `Chunk.lost` counts only the samples the link lost.

`raw.bin`, `markers.jsonl` and `clock.jsonl` are written on every call, so a crash loses at most the call in progress. `meta.json` says `"finished": false` from the start and is rewritten when the recording is closed. If a write fails, the first error is kept, later writes are skipped, and `Recording.close()` raises it.

## raw.bin

The headset's stream bytes with nothing added. Decode them with the configuration in `meta.json`. `read_recording` and `replay` do this.

## markers.jsonl

One line per `Recording.mark(label)`, which returns the line's `sample`:

|Field|Meaning|
|-|-|
|`label`|The text given to `mark`.|
|`sample`|Samples recorded when `mark` was called, which is the index of the next recorded sample. Lost samples count.|
|`unix_time`|Host time, seconds since the Unix epoch, when `mark` was called.|

`sample` counts only the samples that had been read when `mark` was called. It is therefore early by the link latency plus up to one read of samples. In `tests/fixtures/read_timing_10s.csv`, recorded from the test headset on 2026-11-24, reads came about 50 ms apart (median 50.4 ms) with about 25 samples each. `Recorded.sample_at(marker.unix_time)` gives the sample position at the marker's time from the clock saved in `clock.jsonl` instead.

## clock.jsonl

One line `{"sample": n, "unix_time": t}` per clock point:

|Field|Meaning|
|-|-|
|`sample`|A sample number, counted as above.|
|`unix_time`|The live clock model's host time of that sample when the line was written, seconds since the Unix epoch.|

A line is written for sample 0 once the first sample has been read, then for every whole second of samples (`sample` a multiple of the rounded sample rate), and when the recording is closed for the sample count at the end. `sample` increases from line to line and `unix_time` never decreases. The clock model is described under [Live timestamps](#live-timestamps). A line holds the model's value when it was written; later lines are not used to correct earlier ones.

If the file does not end in a newline and its last line does not parse, that line was cut short by a crash or a full disk. `replay` and `read_recording` leave it out and read the whole lines before it; `Replay.clock_error` and `Recorded.clock_error` hold a string such as `line 3: cut short` (`None` otherwise). Any other bad line, including an unparsable last line that ends in a newline or lines out of order, is refused as before.

Recordings made before `clock.jsonl` existed do not have it. They still read: see below.

## meta.json

|Field|Meaning|
|-|-|
|`participant`|The `participant` argument of `record`.|
|`participant_uuid`|The `participant_uuid` argument, or `null`.|
|`start_unix`|Seconds since the Unix epoch when `record` was called.|
|`config`|The headset configuration, see below.|
|`port`|The serial port the headset was opened on.|
|`software_version`|The library version.|
|`eeg_volts_per_count`|Volts per count of an EEG channel: `5.0 / (gain * 2^24)`.|
|`finished`|`false` until the recording is closed, then `true`.|

`config` holds `layout` (`eeg`, `ext`, `acc` channel counts), `sample_rate`, `gain`, `labels` (EEG electrode labels, in wire order), `impedance_at_power_on` and `compressed`.

For the tested Quick-20: 20 EEG channels, 0 extension channels, 3 accelerometer channels, 500 Hz, gain 3.

## recording.bdf

A BDF+ file (24-bit EDF+), a derived export. It is written as samples arrive.

- One data record per second. The sample rate must be a whole number of Hz; `record` raises an error otherwise.
- The EEG channels, named by their labels, in microvolts.
- The accelerometer channels `ACC_X`, `ACC_Y`, `ACC_Z`, as raw counts.
- A `BDF Annotations` signal of 40 samples (120 bytes) per record. Markers are annotations, at `sample / sample_rate` seconds.
- Extension channels, trigger values and battery voltage are not written to the BDF. They are in `raw.bin`.
- The header start time is the whole second of `start_unix` in UTC. The fractional second is in `meta.json`.
- Lost samples repeat the previous sample (zeros if there is none) and are annotated `lost <n>` at the first lost sample.
- A trailing partial record is padded by repeating the last sample and annotated `padding`.
- Annotation text is limited to 80 bytes. The control bytes 0x00, 0x14 and 0x15 become spaces.
- After a disconnect, the BDF is annotated at `Recording.close()`.

### Precision

The BDF header stores each channel's physical range in 8-character fields, so values read from `recording.bdf` are not bit-exact. `tests/python/test_bdf_readback.py` reads a short test recording (made from a recorded stream with one packet removed) with MNE. For the first 100 samples of each of the 20 EEG channels it checks that every value is within 1e-6 V of the value computed from `raw.bin`, and that sample-to-sample differences are within 1e-9 V. It also checks the first sample of the first channel against the same 1e-6 V bound. It does not compare later samples.

`raw.bin` holds the exact counts. `meta.json` holds the exact volts per count.

## Reading a recording

`read_recording(path)` returns a `Recorded` object. `replay(path)` yields chunks.

- `path` is a session directory, or a bare `raw.bin`. A bare `raw.bin` needs the configuration: `read_recording("raw.bin", config=config)`. Without it a `ValueError` is raised.
- Lost samples appear as rows of NaN in `Recorded.data`.
- `Recorded.times`: with `clock.jsonl`, linear between its points, and extended before the first and after the last at the nominal rate. Without it, `start_unix + n / sample_rate`, with `start_unix` taken from `meta.json` (0 when there is none).
- `Recorded.sample_at(unix_time)` is the inverse of `Recorded.times`: a fractional sample position, from the same points, or `(unix_time - start_unix) * sample_rate` without `clock.jsonl`.
- `Recorded.clock` holds the `(sample, unix_time)` points of `clock.jsonl`, empty without the file. `Recorded.clock_error` is a string when the last line of `clock.jsonl` was cut short and left out, otherwise `None`.
- The chunks from `replay` are stamped `start_unix + n / sample_rate` whether or not `clock.jsonl` exists.
- Marker `sample` values are the same indices as in `replay`.
- `read_recording` holds the whole recording in memory as float64: 8 bytes per channel per sample, lost samples included. A lost sample takes a row, and the packet counter can report up to 127 lost samples per packet, so a `raw.bin` whose counters jump at every packet can take about 300 times its own size. `markers.jsonl`, `clock.jsonl` and `meta.json` are read whole.

## Live timestamps

`Chunk.time` of a live stream is host clock time fitted to the arrival times of the bytes. The headset sends no timestamps. A sample's timestamp is `n / sample_rate + offset`, where `offset` is the smallest `arrival - n / sample_rate` seen, followed as the two clocks drift. The timestamps are evenly spaced, with the jitter of the link removed. They mark when the sample reached the program over the fastest path seen, not when it was digitised: the constant part of the link latency is not known and is not removed. Timestamps from one stream never go backwards: after the host clock steps back, or after the offset moves down by more than the length of a chunk, consecutive chunks can share one `time` until the fitted times catch up, and in that stretch `time + k / sample_rate` of one chunk overlaps the next. During the first second or so the offset is still settling and spacing can be uneven. The full description is in the module documentation of `src/timing.rs`.

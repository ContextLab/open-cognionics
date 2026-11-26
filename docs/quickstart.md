# Quickstart

## Install

Install from a checkout of the repository that contains this documentation. Run `uv sync` from its top directory. It builds the Rust extension, so it needs a Rust toolchain and Python 3.10 or later.

```bash
uv sync
```

The package is not published to PyPI.

The Python snippets below run against a simulated headset in `tests/python/test_docs_snippets.py`. That test reads them from this file. In the test, `oc.connect()` is given the simulator's port. With a real headset the call needs no argument.

## Find the headset

`find_headsets` lists the serial ports that report themselves as a Cognionics headset. It opens nothing.

```python
import open_cognionics as oc

print(oc.__version__)
for port in oc.find_headsets():
    print(port.path, port.description)
```

## Connect and stream

`connect()` with no argument uses the only headset plugged in. It raises `NoHeadset` for none and `SeveralHeadsets` for more than one. Pass a path to choose: `oc.connect("/dev/cu.usbserial-XXXX")`.

```python
import open_cognionics as oc

with oc.connect() as headset:
    config = headset.config
    print(config.labels)
    print(config.sample_rate, config.gain, config.eeg_volts_per_count)
    for chunk in headset.stream():
        print(chunk.index, chunk.samples, chunk.data.shape, chunk.lost)
        if chunk.index >= 500:
            break
```

`chunk.data` is EEG in volts, shape `(samples, n_eeg)`, columns in the order of `config.labels`. `chunk.acc` holds accelerometer counts. `chunk.lost` is the number of samples lost just before the chunk.

A slow consumer of `stream()` can miss chunks. A missed chunk shows only as a gap in `Chunk.index`: `Chunk.lost` counts only the samples the link lost. A recording still holds every byte.

## Record

`record` writes a session directory. `mark` places a marker at the next recorded sample.

`record` raises `FileExistsError` for a directory that already holds a recording, and changes nothing in it. This block names the directory after the time, so it can be run again.

```python
import time

import open_cognionics as oc

directory = time.strftime("session-%Y%m%d-%H%M%S")
with oc.connect() as headset:
    with headset.record(directory, participant="P001") as recording:
        time.sleep(2)
        recording.mark("eyes closed")
        time.sleep(2)
print(directory)
```

A recording begins with the first bytes read after `record` was called. See [Recordings](recordings.md) for the files.

## Read a recording

This reads the newest directory written by the previous block.

```python
from pathlib import Path

import open_cognionics as oc

directory = sorted(Path().glob("session-*"))[-1]
recorded = oc.read_recording(directory)
print(recorded.data.shape, recorded.acc.shape)
print(recorded.meta["participant"], recorded.meta["finished"])
print([(marker.label, marker.sample) for marker in recorded.markers])
marker = recorded.markers[0]
print(marker.sample, round(recorded.sample_at(marker.unix_time)))
```

`recorded.times` holds the host time of each sample, from the clock points the recording saved in `clock.jsonl`. `marker.sample` counts the samples read when `mark` was called. `recorded.sample_at(marker.unix_time)` is the sample position at the marker's time, from the same clock points. See [Recordings](recordings.md).

## Replay a recording

`replay` yields the same kind of chunks as a live stream. With `realtime=True` they are released no faster than they were recorded.

This also reads the newest directory.

```python
from pathlib import Path

import open_cognionics as oc

directory = sorted(Path().glob("session-*"))[-1]
chunks = list(oc.replay(directory))
print(len(chunks), sum(chunk.samples + chunk.lost for chunk in chunks))
```

## Contact check

```python
import time

import open_cognionics as oc

with oc.connect() as headset:
    headset.impedance_check(True)
    time.sleep(1)
    headset.impedance_check(False)
```

This writes `0x11`, then `0x12`.

## Errors

All errors derive from `open_cognionics.Error`.

|Error|Raised when|
|-|-|
|`NoHeadset`|No headset dongle is plugged in.|
|`SeveralHeadsets`|More than one is plugged in. The message lists the ports.|
|`OpenError`|The port could not be opened or set up.|
|`NoData`|Nothing at all was received from the headset within 1.5 s of the first configuration request.|
|`NoConfiguration`|The headset streams but did not answer the configuration requests.|
|`ConfigError`|The configuration arrived but could not be understood.|
|`Disconnected`|The connection was lost.|

On 2026-11-24, of about eight opens of the test headset with data flowing, three got the stream but no configuration block within 5 s. The cause is not known. `connect` therefore repeats the request, up to 4 times, 1.5 s apart, while stream bytes arrive; a headset that streams but does not answer gets `NoConfiguration` after about 6.3 s. If no byte at all has arrived 1.5 s after the first request, `connect` sends no more requests and raises `NoData`, about 1.8 s after it was called. Whatever the device sends, `connect` returns or raises within 8 s of being called (see [The headset](device.md)). A call to `connect` cannot be interrupted, so a program that stops while one is in progress waits at most that long for it. Data that queued up before the port was opened is discarded: the first samples are ones the headset sent after it answered.

## Local live view

The repository holds a local web server that reads the headset through this library, shows the data live and records the session.

```bash
cargo run --features server -- [serial-port] [http-port]
```

It serves the live view at `http://127.0.0.1:8737/` and a channel order check page at `http://127.0.0.1:8737/order`. 8737 is the default `http-port`. Each connection writes a session directory under `captures/` (or `OPEN_COGNIONICS_OUT`). See the module documentation in `src/bin/serve.rs`.

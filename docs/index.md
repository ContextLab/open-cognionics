# open-cognionics

open-cognionics is a Rust library with Python bindings. It finds a Cognionics Quick-20 EEG headset on its USB serial dongle, streams its data, and records it to disk.

The Python package is imported as `open_cognionics`.

## What it does

- Finds the headset dongle (`find_headsets`) and opens it (`connect`).
- Asks the headset for its configuration and decodes the stream with it.
- Yields chunks of samples as NumPy arrays (`Headset.stream`). EEG is in volts.
- Records a session to a directory: the exact bytes received, markers, metadata and a BDF+ file (`Headset.record`).
- Reads a recording back whole (`read_recording`) or as chunks (`replay`).
- Writes three bytes to the headset: `0x11` (contact check on), `0x12` (off) and `0x14` (send configuration).

## Tested hardware

- One Cognionics Quick-20: 20 EEG channels and 3 accelerometer channels, 500 Hz, gain 3.
- Its FTDI USB serial dongle (USB ID `0403:6001`).
- macOS.

Linux and Windows are not tested.

## Install

From a checkout only, with `uv sync`. It builds the Rust extension, so it needs a Rust toolchain and Python 3.10 or later. The package is not published to PyPI. See [Quickstart](quickstart.md).

## Pages

|Page|Content|
|-|-|
|[Quickstart](quickstart.md)|Install, connect, stream, record, replay.|
|[API](api.md)|Every public class, function and error, from the docstrings.|
|[Recordings](recordings.md)|The files in a recording directory and their fields.|
|[Device](device.md)|The dongle, the port settings and the three bytes the library writes.|
|[Protocol](protocol.md)|Working notes on the wire protocol.|
|[Channel order check](channel-order-check.md)|The evidence for the channel order.|

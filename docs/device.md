# Device

What is known about the headset and its dongle, from one Quick-20 on macOS. See [Protocol](protocol.md) for the working notes.

## Hardware

|Item|Value|
|-|-|
|Headset|Cognionics Quick-20|
|Channels|20 EEG, 3 accelerometer|
|Sample rate|500 Hz|
|Gain|3|
|Dongle|FTDI, USB ID `0403:6001`|
|Dongle product string|`Cognionics Quick-20 20CH <serial>`|
|Operating system tested|macOS|

Linux and Windows are not tested.

`find_headsets()` lists the ports whose USB ID is `0403:6001` and whose product string contains "Cognionics" or "Quick".

## Serial settings

What the code does:

- It requests 3,000,000 baud with RTS/CTS flow control.
- 8 data bits, no parity and 1 stop bit are the defaults of the serial library. They are not set explicitly.
- On macOS the dongle's driver reads back flow control as `None` after RTS/CTS was requested without error. The library does not check it. (Observed with the test headset.)

What was observed with the test headset: a 30 s and a 60 s recording made through the library, each with 0 lost samples (these runs skipped the flow-control read-back), and an earlier run of the local server, which set the same options without reading back and streamed with zero loss.

A second set of observations with the same headset, off the head:

- Python: `oc.connect(port)` opened in about 0.5 s; a 20 s `record()` held 9,726 samples with 0 lost; `clock.jsonl` gives 500.17 samples per second by the computer's clock; `oc.read_recording` read it back with both markers.
- After a change of batteries, the battery reading was the same in every chunk of that recording and of eight further opens. The earlier flicker of the reading was not seen.
- Eight opens in a row, 0.2 s apart, each succeeded in about 0.5 s.
- The local server was started twice. Its pages `/` and `/order` showed a live link (497 to 500 Hz, 0 lost samples). On the first start, the first page load reported "nothing received from the headset"; the next load, about 20 s later, was live. On the second start the first load was live. The cause of the one failure is not known.

## Bytes written to the headset

The library writes only three bytes:

|Byte|Meaning|When|
|-|-|-|
|`0x14`|Send configuration|When opening. Up to 4 requests, 1.5 s apart, while stream bytes arrive. One request if nothing at all arrives.|
|`0x11`|Contact check on|`Headset.impedance_check(True)`|
|`0x12`|Contact check off|`Headset.impedance_check(False)`|

`brain-mapper` sends `0x12` each time the link comes up (the configuration of the test headset says `impedance_at_power_on`, and its stream carries the 125 Hz tone until the check is switched off), `0x11` only while its calibration screen measures the contacts, and `0x12` again when that ends or the program stops (see [brain-mapper](brain-mapper.md), "Calibration").

Of about eight opens of the test headset with data flowing, three got the stream but no configuration block within 5 s. The cause is not known. `connect` repeats the request. The reply gives the channel count, sample rate, gain and electrode labels.

When the reply does not come:

- If no byte at all has been received 1.5 s after the first request, `connect` raises `NoData` without sending another request, about 1.8 s after it was called (0.3 s of discarded input, then 1.5 s).
- If stream bytes arrive but no reply does, `connect` raises `NoConfiguration` after 4 requests, about 6.3 s after it was called.
- If a reply (or bytes that look like the start of one: four `0xFF`) starts but does not end, `connect` gives up 5 s after the request it followed and raises `NoConfiguration`, about 5.3 s after it was called when it followed the first request.
- Whatever the device sends, `connect` returns or raises within 8 s of being called (`CONNECT_LIMIT` in `src/transport.rs`). The clock starts when `connect` is called, so listing and opening the port count within the 8 s (an open call that the operating system itself never returns from cannot be bounded by the library). The limit itself ends the wait only when a reply that does not end starts after the fourth request; the `NoConfiguration` message then says "within the 8 s limit". A device that never takes the request (for instance while hardware flow control holds the computer's output) gets `NoConfiguration` after about 6.3 s: the request is handed to the operating system and `connect` does not wait for it to leave. When it gives up, `connect` discards what was not sent (this sends nothing) and closes the port.

Data that queued up before the port was opened (the headset streams all the time, so a port left unread holds a backlog) is read and discarded for the first 0.3 s, then the request is sent. Everything after the reply is what the headset sent after it answered: `connect` never hands the caller the backlog. Sample indices count from the reply, and a sample lost from then on is counted as lost. On 2026-11-25, with the simulated device (`tests/python/pty_device.py`) left streaming unread for 30 s, `connect` took about 0.3 s and the stream after the reply was at most 0.1 s behind the device's own clock, with no sample lost.

### Not verified on the headset

The following are not known; they were not checked with the real dongle and headset:

- Whether macOS and the FTDI driver send a queued byte (`0x14`, `0x11`, `0x12`) promptly now that the library no longer waits for it to leave, and whether they finish sending it when the port is closed.
- The size of the dongle's input buffer (and of the computer's buffer for the port): how much stream a port left unread holds before data is lost.
- Whether the earlier wait for each written byte to leave (`flush`) ever blocked on the real hardware.

## Units

EEG volts per count = `5.0 / (gain * 2^24)`. `Chunk.data` is in volts. Accelerometer values are raw counts.

## Channel order

`Chunk.data` columns are in the order of `Config.labels`. The evidence for this order is in [Channel order check](channel-order-check.md).

## Local live view

`cargo run --features server -- [serial-port] [http-port]` starts a local server. It serves the live view at `/` and the channel order check page at `/order`. See [Quickstart](quickstart.md).

## Headset facts the device screen relies on

The brain-mapper device screen relies on these facts about the headset:

- The headset has a power button, on the box on top of the headset.
- The headset has forehead pads.
- The headset has ear clips, which clip to the earlobes.
- The wireless link between the headset and the dongle has a limited range.

## Not covered on this page

- Pairing the headset with the dongle.
- Setup on Linux and Windows.
- Troubleshooting.

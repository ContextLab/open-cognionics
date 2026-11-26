# Cognionics / CGX wire protocol (working notes)

Status: derived from static reading of the vendor app. Section 0 lists what has been confirmed on
a real Quick-20; everything else is still unconfirmed against hardware.

Sources:

- `CGX Acquisition v66` (vendor Windows app, .NET, not obfuscated), downloaded 2026-11-24 from the
  link on <https://www.cgxsystems.com/software>; zip sha256
  `54fee96d244533f1774930e445eb5c4aeabdd36098d51b72e60305488fd58ab6`. Citations below are
  `Class::method IL_offset` in the main executable (namespace `Cognionics_Data_Acqusition`).
- `labstreaminglayer/App-Cognionics` `mainwindow.cpp` (2018), which independently shows the same
  legacy framing.
- Vendor "Device Pairing" PDF (pairing code, FTDI latency advice).

Tags: **[IL]** read in the vendor IL, **[IL✓]** read by a second pass as well, **[meta]** read from
static data in the executable, **[inferred]** not directly visible.

## 0. Confirmed on hardware (2026-11-24)

Read-only 10 s capture from one Quick-20 through its USB dongle (FTDI `0403:6001`, product string
`Cognionics Quick-20 20CH <serial>`), opened at 3000000 baud, 8N1, RTS/CTS, on macOS with
the built-in FTDI serial driver. No byte was written to the device.

- Legacy format: single `0xFF` sync, no triple-sync runs.
- Packet length 75 bytes = 3 × 23 + 6: 20 EEG channels, 0 EXT, then 3 channels that idle near
  32768 counts (the accelerometer midpoint in the vendor's ACC formula).
- Counter runs 0..127 and steps by 1; after the first 66 packets (stale buffered data with four
  jumps) there were 5216 consecutive packets with no loss, about 500 packets per second.
- Every channel byte is even, so `0xFF` occurs only as the sync byte (5284 of 5284), and decoded
  counts are multiples of 8 (21 effective bits).
- Trailer was `11 69 00 00` throughout: impedance check on, battery byte constant, trigger 0.
- With the headset idle the EEG channels swing rail to rail.

Not confirmed by this capture: channel order, trigger behavior, impedance values, commands other than `0x14`. For the channel order see [Channel order check](channel-order-check.md).

### Configuration dump on the same headset (2026-11-24)

One byte `0x14` was written; the headset replied with `FF FF FF FF`, 3553 big-endian words ending
in `1234567890`, and then resumed streaming 75-byte packets by itself. New layout (`W[7] = 1`),
`SC = 10`, `S = 100`, `CC = 3`, `CS = 512`.

| Word | Value | Meaning (per vendor code) |
|-|-|-|
| `W[105]` | 3 | gain index -> gain 3 |
| `W[106]`, `W[107]` | 1, 6 | high-resolution on, rate index 6 -> 500 Hz |
| `W[108]` | 1 | impedance mode |
| `W[200]` | 1 | data mode 1 = uncompressed |
| `W[201]`, `W[202]` | 1, 1 | wireless trigger enabled; impedance check on at power-up |
| `W[500..519]` | float32 0.09934107 | per-channel EEG scale, microvolts per count (= 5 / (3 x 2^24) V) |
| `W[570..579]` | float32 0.59604645 | not identified (vendor EXT constant is 0.5662 uV per count) |
| `W[580]` | float32 0.000108991 | accelerometer scale, matches the vendor constant |
| `W[600..605]` | 20, 24, 0, 128, 3, 8 | EEG channels, EEG buffer, EXT channels, EXT buffer, ACC channels, ACC buffer |
| `W[700..708]` | ASCII `COGDAQ000` | identifier string |
| `W[1000..]` | 14, 10, 7, 13, 0, 4, 1, 5, 2, 6, 3, 18, 17, 9, 12, 8, 16, 11, 15, 19, ... | channel order map (array 10 in the write protocol) |
| `W[1512..]` | 20 x 1, 4 x 2, 8 x 1, ... | channel mask |
| `W[2024..2043]` | 19, 4, 6, 27, 21, 23, 25, 43, 45, 71, 63, 67, 69, 341, 65, 84, 86, 47, 343, 348 | label codes |

Through the vendor label table (`ElecMapData::LabelName`, 349 entries, codes 0 to 348) the label codes read:
`F7, Fp1, Fp2, F8, F3, Fz, F4, C3, Cz, P8, P7, Pz, P4, T3, P3, O1, O2, C4, T4, A2`.
Whether stream channel k is label k, or the order map applies on the host: [Channel order check](channel-order-check.md) holds the observations. 7 of the 20 electrodes had a direct observation on the channel carrying their label (stream channel k = label k); the other 13 are not individually confirmed.

### On-head capture (2026-11-24)

With the impedance check on, every EEG channel carries a 125 Hz component (fs / 4), confirming the
carrier frequency inferred in section 8.

## 1. Transport

| Link | Details |
|-|-|
| Bluetooth (Quick series, Dev Kit, AIM) | Classic Bluetooth RFCOMM serial port; pairing code `1234` (pairing PDF). App connects with `BluetoothService::RFCommProtocol`, read timeout 5000 ms, up to 50 reconnect tries (`BTAcq::OpenDevice IL_4987`, `BTAcq::ReadByte IL_4a97`) [IL]. |
| FTDI dongle (Mobile-72/128 and "r"/"m" models) | 8N1, flow control `SetFlowControl(256,17,19)`, latency 2 ms (`FTAcq::OpenDevice IL_98ee-9945`) [IL]. Baud 1000000 if the device name contains `Dev`, `CGX Quick-20r`, `CGX Quick-20m`, `Freedom`, `CGX Quick-32r`, `CAMP`, `CGX Quick-8r` or `AIM Phys. Mon.`, else 3000000 (`buttonDevConnect_Click IL_a976-a9de`) [IL]. The pairing PDF recommends latency 4 ms and 512-byte buffers. |

## 2. Legacy stream format (`legacyFormat = 1`, the default)

One packet per sample, `3N + 6` bytes, no checksum, where `N = EEG + EXT + ACC` channels in that order.

| Bytes | Field | Decoding |
|-|-|-|
| 1 | sync | `0xFF` |
| 1 | packet counter | increments by 1, wraps modulo 128 (`mainDAQThread IL_797c-7985`) [IL✓] |
| 3 × N | samples | `raw = ((b0 << 24) \| (b1 << 17) \| (b2 << 10)) >> 8` as a signed 32-bit value with arithmetic shift (`mainDAQThread IL_7a2a-7a48`) [IL✓]. `b1`, `b2` carry 7 bits each [inferred]. |
| 1 | impedance status | `0x11` (17) = impedance check on (`IL_7a6a`) [IL] |
| 1 | battery | volts = byte × 0.0390625 (`IL_7a89`) [IL] |
| 2 | trigger | `(hi << 8) \| lo` (`IL_7ab1-7ad1`) [IL] |

Optional "encrypted" mode XORs `b0, b1, b2` with `173, 57, 191` before decoding (`IL_79bf-79f6`) [IL].

The formula alone would allow `b0 == 0xFF`; on hardware every channel byte has its low bit clear,
so data never collides with the sync byte (section 0).

### Scaling

- EEG volts = `raw × 5.0 / (gain × 16777216.0)` (`CogDAQ::SetADSGain IL_83c1-83d7`) [IL✓]. With the
  default gain 3 this is `9.934107462565105e-08` V per count.
- `gain = ADS_GAINS[index]`, `ADS_GAINS = {6, 1, 2, 3, 4, 8, 12}` [meta]; index comes from the device config.
- EXT volts = `raw × 5.662441253662109e-07` [IL].
- ACC = `(raw − 32768.0) × 0.00010899135044642858` (`IL_7b49`) [IL].

## 3. Delta ("high-speed") stream format (`legacyFormat = 0`)

Used when the device's data-mode word is 0.

- Sync: three consecutive `0xFF` (`mainDAQThread IL_772c-7763`).
- Counter: 16-bit big-endian, wraps modulo 32768 (`IL_7767`, `IL_779d`).
- Reset flag: 1 byte; value 1 clears decoder history (`DeltaDecoder::ReadDeltaData IL_968e`).
- Per channel, first byte `b`:
  - `(b & 1) == 0`: 1 byte, `delta = sext7(b >> 1) << 3` (`IL_96d7`)
  - else `((b >> 1) & 1) == 0`: 2 bytes, `t = (b & 252) | (b2 << 8)`, `delta = (t << 16) >> 15` (`IL_9716`)
  - else 3 bytes, absolute `((b3 << 24) | (b2 << 16) | ((b & 248) << 8)) >> 8` (`IL_9768`)
  - deltas are relative to the sample 4 packets back (ring of 4; `IL_96f4`, `IL_97ab`)
- EXT values are then shifted left 5, ACC left 3 (`mainDAQThread IL_781f`, `IL_7851`).
- Status byte (`IL_7802`): bit 0 impedance on; bit 1 trigger linked; bit 2 → a 4-byte big-endian
  trigger follows (`IL_7895`); bit 5 → a battery byte plus one discarded byte follow (`IL_78e1`).

All [IL], single pass.

## 4. Host → device command bytes

| Byte | Meaning | Citation |
|-|-|-|
| 0x11 (17) | impedance check on | `CogDAQ::ImpedanceCheckSet IL_8021` |
| 0x12 (18) | impedance check off | `CogDAQ::ImpedanceCheckSet IL_802f` |
| 0x14 (20) | dump configuration | `CogDeviceConfig::ReadConfigSequenceInitDAQ IL_2b01` |
| 0x13 (19) | **write configuration / channel remap (persistent)** | `WriteConfig IL_417d`, `WriteRemap IL_4735` |
| 0x30 (48) | query microSD card | `QuerySDCard IL_457d` |
| 0x31 / 0x32 (49 / 50) | start microSD recording (single / dual) | `StartSDCard IL_45e4` |
| 104, 105 | sent on keys `m`, `p`; meaning unknown | `IL_9a9a`, `IL_9a82` |

## 5. Configuration dump (command 20)

1. Host discards 65536 pending bytes (`preFlush`), writes the single command byte, then scans up to
   262144 bytes for `FF FF FF FF` (`enterConfigMode IL_2a96-2af0`).
2. Device sends 32-bit big-endian words (`readWord IL_44c0-44fd`) until the word `1234567890`
   (`downloadConfig IL_2c97-2cbd`).
3. No exit command exists; the device is assumed to resume streaming afterwards [inferred].

`W[n]` is the n-th word. `W[7] == 0` selects the legacy layout (`IL_2cbf-2cc7`).

| Field | Legacy layout | New layout (`S = W[2]`, `SC = W[1]`, `CS = W[4]`) |
|-|-|-|
| EEG channel count | `W[40]` | `W[6S]` |
| EXT channel count | `W[12]` | `W[6S+2]` |
| Accelerometer present | `W[13] > 0` | `W[6S+4] > 0` |
| Gain index | `W[15]` | `W[S+5]` |
| High-resolution flag | `W[16]` | `W[S+6]` |
| Sample-rate index | `W[17]` | `W[S+7]` |
| Impedance mode | `W[18]` | `W[S+8]` |
| Data mode (0 = delta) | `W[20]` | `W[2S]` |
| Channel mask start | `W[160]` | `W[SC·S + CS]` |
| Channel label codes start | `W[240]` | `W[SC·S + 2·CS]` |

- Sample rate = `{32000, 16000, 8000, 4000, 2000, 1000, 500}[index]` [meta], halved when the
  high-resolution flag is 0 (`IL_3578-35a4`).
- Label codes index the app's `ElecMapData::LabelName` table (10-5 names plus `T3/T4/T5/T6`,
  `M1/M2`, `A1/A2`); the full table is in `src/labels.rs`. The vendor initializer builds 349 names and then reassigns `LabelName` from a second, larger array (size constant 8196) whose construction has not been read; the 20 codes this headset reports index the 349-name table directly.
- Device name, serial and firmware version are not read by `downloadConfig`.

## 6. Configuration writes (command 19) — not to be issued by this library without explicit opt-in

Triples `(array, index, value)`, then word 0, then `1234567891` (constant named `DEVICE_DL_EEPROM`
[meta]); ack byte 126 (`WriteConfig IL_4244-4263`, `IL_448c-44ab`). `WriteRemap` rewrites channel
order and positions.

## 7. microSD

- Query: command 48, read 2 words; card present iff the first is non-zero (`QuerySDCard IL_4570-45b7`).
- Start: command 49 (or 50 for dual), filename one word per character + word 0, then year, month,
  day, hour, minute, second, id string, comments string, word `1234567890`; ack byte 87
  (`StartSDCard IL_45d4-4679`).

## 8. Impedance

With the check on, the headset injects a carrier at fs/4 [inferred]. Default path: I/Q demodulation
over one second, `Z = sqrt(I² + Q²) / ISTIM + ZOFF`, then a 1 Hz low-pass
(`mainDAQThread IL_7dc8-7e1f`); `ISTIM` defaults to `4.8e-08`, passive mode `2.4e-08`
(`IL_8347-83ad`). EEG output is notch-filtered at fs/4 and fs/2 while the check is on (`IL_7e70`).

## 9. Vendor app outputs (for comparison recordings)

- LSL: one sample per `push_sample` with timestamp 0.0, i.e. stamped by liblsl at push time; EEG
  values × 1e6 (µV); stream type `EEG`; a second stream of type `Impeadance` in kΩ
  (`CogDAQ::writeLSL IL_81d5-8230`, `StartLSL IL_8048`).
- TCP triggers: listener on a GUI-chosen port (default 50000), each received byte is one trigger
  value (`TCPTriggerRcv::cogTCPHandler`, `IL_ee76-eee5`).
- Files: BrainVision `.eeg` (float32), BDF, EDF (0.1 Hz high-pass, 5e-07 V per count), Persyst, text.

## Not yet known

- Which stream format and config layout this lab's Quick-20 uses, and its channel label list.
- Whether the config dump works over Bluetooth exactly as over FTDI.
- Internals of `CogFIR` / `CogLongFIR` and the `IMPGENTwo` impedance constants' origin.

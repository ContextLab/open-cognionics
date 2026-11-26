# Channel order check

Question: are the stream's channels in the order of the labels the headset
reports (F7, Fp1, Fp2, F8, F3, Fz, F4, C3, Cz, P8, P7, Pz, P4, T3, P3, O1, O2,
C4, T4, A2), or does the order map in the configuration have to be applied?

Status: of the 20 electrodes, 7 have a direct observation on the channel
carrying their label: Fp1, Fp2 and F7 (blinks), P7, T3 and C4 (the own channel
responded alone to a press) and F4 (the own channel had the second-largest
response to a press). The other 13 are not individually confirmed.

## Evidence

1. Guided recording: blink
   deflections were largest on the channels labeled Fp1, Fp2 and F7 and small
   on central and posterior channels with good contact; eyes-closed alpha rose
   most on posterior channels. Computed by `scripts/analyze_guided.py`.
2. Observation in the live view: jiggling an electrode
   changed the trace carrying that electrode's label, for the electrodes
   tried. By eye; not logged per electrode.
3. Prompted press test, contact
   check off, signals steady at rest. Movement
   during the press against the preceding rest, from
   `scripts/analyze_order_check.py`:

| Result | Electrodes | Detail |
|-|-|-|
| Own channel responded alone | P7, T3, C4 | P7 x312 (next x1.7); T3 x470 (next x17); C4 x22 (next x10, T4) |
| Own channel had the second-largest response | Fp2, F4 | Fp2 x464, behind O2 x910; F4 x5, behind Fp2 x15 |
| No detectable response | F7, Fp1, F8, Cz, P8, P4, P3, A2 | all channels within x0.6 to x2.2 |
| Every channel jumped | C3, Pz, O2 | A2 x2400 to x3900 with most others: the reference was disturbed |
| Weak response on another channel | F3, Fz, O1, T4 | F3 -> Cz x34; Fz -> Pz x6; O1 -> Cz x16; T4 -> Fz x6. |

P7, T3 and C4 are stream channels 10, 13 and 17. Together with the blinks on
channels 0, 1 and 2, these are six positions spread over the head where the
largest response was on the channel with the expected label. The order map
stored in the configuration (14, 10, 7, 13, ...) would not give that.

## What did not work

- First tap test (contact check on): unusable.
  Contact was poor on every channel at once (strong 60 Hz pickup
  everywhere) and tapping moved all channels.
- Gentle pressing often changed nothing, because a dry electrode stays in
  contact; firmer pressing disturbed the reference and every channel jumped.

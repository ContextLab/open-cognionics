# Test fixtures

Cut from recordings by `scripts/make_fixtures.py`. All were recorded with the Quick-20 dongle (23 channels, 75-byte packets).

- `config_reply.bin`: from `captures/config-01.bin`, 26141 bytes. 9 whole packets before the configuration block (the recording starts only 9 packets before it), the block itself (3553 big-endian words ending with 1234567890), then 150 whole packets after it. Condition: configuration dump requested while streaming.
- `onhead_10s.bin`: from a guided session recording (`captures/session-<unix>/raw.bin`), 5000 packets (375000 bytes) starting at the `eyes_closed_start` marker. Condition: headset on head, eyes closed.
- `impedance_on_2s.bin`: from `captures/onhead-01-impon.bin`, 1000 packets (75000 bytes) starting at packet 1000. Condition: headset on head, impedance measurement on.
- `read_timing_10s.csv`: from `captures/first-3M.chunks.csv`, 201 reads over 10 s. One row per `read()` on the serial port: host monotonic clock (seconds), byte offset in the stream, bytes returned. No signal data. Condition: first reads after opening the port (the first reads drain about 0.6 s of buffered stream), then steady reads about every 50 ms.

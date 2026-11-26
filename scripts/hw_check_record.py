"""Hardware pass 2: connect from Python, stream 20 s while recording, read it back.

Usage: python hw_pass2.py <serial port> <output folder>
"""
import collections
import sys
import time

import numpy as np
import open_cognionics as oc

port, out = sys.argv[1], sys.argv[2]
print("found:", [(p.path, p.description, p.serial_number) for p in oc.find_headsets()])
t0 = time.time()
with oc.connect(port) as hs:
    c = hs.config
    print("opened in %.1f s on %s" % (time.time() - t0, hs.port))
    print("config: rate", c.sample_rate, "gain", c.gain, "eeg", c.n_eeg, "ext", c.n_ext, "acc", c.n_acc,
          "impedance at power on", c.impedance_at_power_on, "compressed", c.compressed)
    print("labels:", c.labels)
    batt, lost, n, imp = [], 0, 0, collections.Counter()
    peak = None
    with hs.record(out, participant="hwpass2") as rec:
        rec.mark("pass2 start")
        start = time.time()
        for ch in hs.stream(timeout=5.0):
            n += ch.samples
            lost += ch.lost
            batt.append(ch.battery_volts)
            imp[ch.impedance_on] += 1
            d = ch.data
            pk = np.abs(d - d.mean(axis=-1, keepdims=True)).max()
            peak = pk if peak is None else max(peak, pk)
            if time.time() - start >= 20.0:
                break
        rec.mark("pass2 end")
        folder = rec.directory
    dur = time.time() - start
    b = np.array(batt)
    print("streamed %d samples in %.2f s (%.1f Hz), lost %d, chunk data shape %s" % (n, dur, n / dur, lost, d.shape))
    print("battery volts: min %.3f median %.3f max %.3f, distinct values %d: %s" % (
        b.min(), np.median(b), b.max(), len(np.unique(b)), np.unique(b.round(3))[:12]))
    print("impedance_on chunk counts:", dict(imp), "largest deviation from chunk mean (V): %.6f" % peak)
print("closed; recording at", folder)
r = oc.read_recording(folder)
print("read back:", r)
print("markers:", [(m.label, m.sample) for m in r.markers] if hasattr(r, "markers") else "n/a",
      "clock_error:", r.clock_error)

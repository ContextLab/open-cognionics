"""Open and close the headset several times in a row and report how each open went (reads only).

Usage: python hw_reopen.py <serial port> <times> <pause seconds between>
"""
import sys
import time

import open_cognionics as oc

port, times, pause = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
for k in range(times):
    t0 = time.time()
    try:
        with oc.connect(port) as hs:
            t1 = time.time() - t0
            n = 0
            for ch in hs.stream(timeout=5.0):
                n += ch.samples
                if n >= 500:
                    break
            print("open %d: ok in %.2f s, 500 samples after %.2f s, battery %.3f V" % (k + 1, t1, time.time() - t0, ch.battery_volts))
    except oc.Error as e:
        print("open %d: FAILED after %.2f s: %s: %s" % (k + 1, time.time() - t0, type(e).__name__, e))
    time.sleep(pause)

"""scripts/analyze_order_check.py on a recording made through the library from
the pty device, with a channel-order check that was stopped after one
electrode. No test here opens a real serial port."""

import json
import subprocess
import sys
import time
from pathlib import Path

import open_cognionics as oc

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "analyze_order_check.py"


def test_a_check_stopped_early_is_reported_not_a_key_error(device, tmp_path):
    out = tmp_path / "session"
    with oc.connect(device.path) as h:
        with h.record(out, participant="P020") as rec:
            rec.mark("order_check_start")
            time.sleep(0.5)
            rec.mark("order_F7_ready")
            time.sleep(1.0)
            rec.mark("order_F7_start")
            time.sleep(1.0)
            rec.mark("order_F7_end")
            time.sleep(1.5)
    # Labels come from meta.json: rename one to show which list is used.
    meta = json.loads((out / "meta.json").read_text())
    meta["config"]["labels"][1] = "XX"
    (out / "meta.json").write_text(json.dumps(meta))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(out)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith("F7   move: ") for line in lines), result.stdout
    assert "XX   not prompted" in lines, result.stdout
    assert "Fp1  not prompted" not in lines
    assert "Fp2  not prompted" in lines
    assert "stopped early" in result.stdout

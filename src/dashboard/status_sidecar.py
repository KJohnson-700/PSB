#!/usr/bin/env python3
"""2026-07-16 FIX #4 — status sidecar.

Computes the heavy /api/status payload in a SEPARATE process (its own GIL) and writes
data/runtime/status_snapshot.json atomically every ~90s. The dashboard reads that file
instead of assembling in-process, so the dashboard event loop never blocks. Previously the
~22s assembly ran via asyncio.to_thread inside the dashboard, but CPU-bound json parsing holds
the GIL, starving the event loop and freezing EVERY endpoint (inline /api/vitals measured
16-20s, the Exposure Manager card showed "NOT CONNECTED"). A separate process fixes that.

Long-running loop: pays the heavy module import ONCE, then only the ~22s assembly per cycle.
Run under systemd (psb-status-sidecar), Restart=always.
"""
import json
import os
import sys
import tempfile
import time

# repo root on path so `src.dashboard.server` imports
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.dashboard.server import _get_status_payload_sync, DATA_ROOT  # noqa: E402

OUT = DATA_ROOT / "runtime" / "status_snapshot.json"
INTERVAL = 120.0


def write_atomic(payload):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, default=str)
        os.replace(tmp, OUT)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main():
    sys.stderr.write("status_sidecar: started, writing %s every %.0fs\n" % (OUT, INTERVAL))
    sys.stderr.flush()
    while True:
        t0 = time.time()
        try:
            payload = _get_status_payload_sync(_force=True)
            write_atomic(payload)
        except Exception as e:  # never die on a bad cycle
            sys.stderr.write("status_sidecar cycle error: %r\n" % (e,))
            sys.stderr.flush()
        dt = time.time() - t0
        time.sleep(max(5.0, INTERVAL - dt))


if __name__ == "__main__":
    main()

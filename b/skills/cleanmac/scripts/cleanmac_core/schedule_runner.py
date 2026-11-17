"""LaunchAgent-only entrypoint: one saved local read-only scan, never cleanup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cleanmac_core.common import CleanMacError
from cleanmac_core.lifecycle_tools import run_schedule
from cleanmac_core.state import StateStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one explicitly saved read-only CleanMac schedule.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--schedule", required=True)
    args = parser.parse_args(argv)
    # Provider secrets and host hooks are not needed for local read-only jobs.
    kept = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}}
    os.environ.clear()
    os.environ.update(kept)
    cancelled = False
    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        report = run_schedule(StateStore(args.data_dir), args.schedule, cancel=lambda: cancelled)
        # Private record contains details; stdout does not disclose file paths.
        print(json.dumps({"id": report["id"], "status": report["status"], "reason": report.get("reason")}))
        return 0 if report["status"] in {"complete", "partial", "skipped", "cancelled"} else 1
    except CleanMacError as error:
        print(json.dumps({"status": "failed", "code": error.code}))
        return 1
    except Exception:
        # launchd stdout/stderr are not a channel for private state or tracebacks.
        print(json.dumps({"status": "failed", "code": "schedule_runner_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

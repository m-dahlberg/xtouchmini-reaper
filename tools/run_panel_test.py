#!/usr/bin/env python3
"""Run the live ReaImGui panel suite and wait for its verdict.

reascript_test.py reports success as soon as the script finishes *loading*,
which for a deferred panel is before a single frame has been drawn. So the
suite writes its verdict to a file and this polls for it.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "reaper/test/panel_in_reaper.lua"
RESULT = Path(os.environ.get("TMPDIR", "/tmp")) / "xtouchmini-panel-result.txt"
TIMEOUT = 30


def main():
    if subprocess.run(["pgrep", "-x", "reaper"], capture_output=True).returncode:
        print("no REAPER instance is running -- start REAPER first.")
        return 3

    RESULT.unlink(missing_ok=True)
    launch = subprocess.run(["reaper", "-nonewinst", str(SUITE)],
                            capture_output=True, text=True)
    if launch.returncode != 0:
        print(launch.stderr or launch.stdout)
        return launch.returncode

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if RESULT.exists():
            text = RESULT.read_text().strip()
            print(text)
            return 0 if text.startswith("RESULT OK") else 1
        time.sleep(0.2)

    print(f"timed out after {TIMEOUT}s with no result file at {RESULT}")
    return 2


if __name__ == "__main__":
    sys.exit(main())

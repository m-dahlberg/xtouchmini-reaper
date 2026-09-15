#!/usr/bin/env python3
"""Run every suite: Python unit tests, then the Lua suites inside REAPER.

The Lua suites are handed to an already-running REAPER with -nonewinst. The
harness refuses to start one, and so does this: -nonewinst silently BECOMES the
instance when none is running, leaving a stray GUI REAPER holding the audio
device and rewriting ~/.config/REAPER on exit.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path.home() / ".claude/skills/reascript-lua/assets/reascript_test.py"

LUA_SUITES = [
    "reaper/test/headless.lua",
    "reaper/test/ui_frame.lua",
    "reaper/test/verify_in_reaper.lua",
]


def run(label, argv):
    print(f"\n=== {label} ===", flush=True)
    result = subprocess.run(argv, cwd=ROOT)
    return result.returncode == 0


def reaper_running():
    return subprocess.run(["pgrep", "-x", "reaper"],
                          capture_output=True).returncode == 0


def main():
    ok = run("python", [sys.executable, "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."])

    if not HARNESS.exists():
        print(f"\nskipping Lua suites: harness not found at {HARNESS}")
        return 0 if ok else 1
    if not reaper_running():
        print("\nskipping Lua suites: no REAPER instance is running.")
        print("Start REAPER first, then re-run. (-nonewinst would otherwise")
        print("become the instance and rewrite ~/.config/REAPER on exit.)")
        return 0 if ok else 1

    for suite in LUA_SUITES:
        path = ROOT / suite
        if not path.exists():
            print(f"\nskipping {suite}: not written yet")
            continue
        ok = run(suite, [sys.executable, str(HARNESS), str(path)]) and ok

    # The live panel suite needs its own runner: it is deferred, so the shared
    # harness returns before the first frame is drawn.
    ok = run("reaper/test/panel_in_reaper.lua",
             [sys.executable, str(ROOT / "tools/run_panel_test.py")]) and ok

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

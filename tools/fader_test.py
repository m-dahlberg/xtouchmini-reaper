#!/usr/bin/env python3
"""Manual test rig for the fader.

The fader is the one control whose failure mode is silence. It declines to
move the track and there is nothing to say whether that was soft pickup
waiting to catch up, a relative pause, a walk-back, or a shadow value that
disagrees with REAPER about what "0.5" means. These three commands make each
of those visible.

    tools/fader_test.py scales     what REAPER, the panel and OSC each call
                                   the same level -- run this FIRST
    tools/fader_test.py scenario   drive the real Engine offline and trace it
    tools/fader_test.py live       follow the running daemon's fader log

`scales` and `live` need REAPER and the daemon running. `scenario` needs
neither, and is the one to reach for when reasoning about the state machine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xtouchmini.engine import Engine, Feel            # noqa: E402
from xtouchmini.protocol import FaderMove             # noqa: E402
from xtouchmini.state import parse                    # noqa: E402

RESET, BOLD, DIM, RED, GREEN, YELLOW = (
    "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m")


def _colour(text: str) -> str:
    if not sys.stdout.isatty():
        return text
    if "SENT" in text:
        return GREEN + text + RESET
    if "HELD" in text or "PAUSED" in text or "swallowed" in text:
        return YELLOW + text + RESET
    if text.startswith("--"):
        return BOLD + text + RESET
    if "shadow" in text:
        return RED + text + RESET
    return DIM + text + RESET


# --- offline: drive the real Engine -----------------------------------------

class Recorder:
    def __init__(self):
        self.msgs = []

    def send(self, *args):
        self.msgs.append(args)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _state(mode, volume=0.5, track=3, **fader):
    doc = {
        "version": 1, "seq": 1, "active": False,
        "track": {"index": track, "name": "Test", "volume": volume},
        "fx": {"index": -1, "ident": "", "name": "", "track": -1},
        "layers": {}, "reserved": [],
        "fader": dict({"mode": mode}, **fader),
    }
    return parse(json.dumps(doc))


# Each scenario is (description, mode, extra fader settings, steps).
# A step is an int (move the fader to that raw value) or a float (let that
# many seconds pass, and tick).
SCENARIOS = {
    "pickup-basic": (
        "Fader starts below the track's level and has to climb to it.",
        "pickup", {}, [10, 30, 50, 64, 70, 90],
    ),
    "pickup-from-above": (
        "Fader starts above the level and comes down onto it.",
        "pickup", {}, [120, 100, 80, 64, 50],
    ),
    "pickup-fast-sweep": (
        "A fast sweep steps straight over the target: it must still engage.",
        "pickup", {}, [10, 120],
    ),
    "relative-basic": (
        "Ordinary nudging, nowhere near either end.",
        "relative", {}, [60, 64, 68, 72, 68, 64],
    ),
    "relative-end-zone": (
        "Run into the top end zone, walk back, carry on.",
        "relative", {}, [100, 115, 126, 127, 100, 80, 1.5, 70, 60],
    ),
    "relative-reversal": (
        "Walk back and reverse without pausing: the reversal counts.",
        "relative", {}, [110, 126, 90, 70, 75, 80],
    ),
    "relative-overshoot": (
        "Walk back so far it lands in the other end zone.",
        "relative", {}, [110, 126, 60, 1, 0, 40, 30],
    ),
    "relative-pinned-bottom": (
        "Pull the track to silence, then bring it straight back up. The "
        "fader and the volume are both at the bottom, so they agree: no "
        "walk-back should be needed.",
        "relative", {}, [40, 20, 1, 6, 20, 40],
    ),
    "relative-pinned-top": (
        "The mirror image at the top.",
        "relative", {}, [90, 110, 126, 121, 110, 90],
    ),
    "relative-sensitivity": (
        "The same moves at half sensitivity.",
        "relative", {"sensitivity": 0.5}, [60, 70, 80, 90],
    ),
    "absolute": (
        "No pickup, no memory: every move goes straight out.",
        "absolute", {}, [10, 60, 120, 3],
    ),
}


def cmd_scenario(args) -> int:
    names = [args.name] if args.name else list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"known: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2

    for name in names:
        description, mode, settings, steps = SCENARIOS[name]
        print(f"\n{BOLD}=== {name} ==={RESET}")
        print(f"{DIM}{description}{RESET}")
        print(f"{DIM}mode={mode} volume starts at {args.volume} "
              f"{settings or ''}{RESET}\n")

        osc, clock = Recorder(), Clock()
        engine = Engine(osc, Recorder(), Feel(), clock)
        engine.set_fader_debug(lambda text: print("   " + _colour(text)))
        engine.set_state(_state(mode, volume=args.volume, **settings))

        for step in steps:
            if isinstance(step, float):
                clock.t += step
                print(f"   {DIM}... {step}s pass ...{RESET}")
                engine.tick()
                continue
            # A real fader move is many messages; one per step is enough to
            # exercise every branch and keeps the trace readable.
            clock.t += 0.05
            engine.handle(FaderMove(step / 127.0, step))

        sent = [f"{v:.4f}" for _, v in osc.msgs]
        print(f"\n   {BOLD}{len(osc.msgs)} sent:{RESET} "
              f"{', '.join(sent) if sent else '(nothing)'}")
    return 0


# --- live: what each side calls the same level ------------------------------

PROBE = r'''
local f = io.open("%s", "w")
local tr = reaper.GetSelectedTrack(0, 0)
if not tr then
    f:write("NOTRACK\n")
else
    local vol = reaper.GetMediaTrackInfo_Value(tr, "D_VOL")
    local db = (vol > 0) and (20 * math.log(vol, 10)) or -150
    local idx = math.floor(reaper.GetMediaTrackInfo_Value(tr, "IP_TRACKNUMBER"))
    local _, name = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
    f:write(string.format("%%d\t%%s\t%%.6f\t%%.2f\t%%.6f\n",
            idx, name, vol, db, reaper.DB2SLIDER(db) / 1000))
end
f:close()
'''


def _reascript(out_path: Path) -> str | None:
    """Run a probe inside the already-running REAPER. None if that failed."""
    runner = Path.home() / ".claude/skills/reascript-lua/assets/reascript_test.py"
    if not runner.exists():
        print("reascript runner not found; is the reascript-lua skill "
              "installed?", file=sys.stderr)
        return None
    script = Path(tempfile.mkdtemp()) / "xt_fader_probe.lua"
    script.write_text(PROBE % out_path)
    try:
        subprocess.run([sys.executable, str(runner), str(script)],
                       capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"could not reach REAPER: {exc}", file=sys.stderr)
        return None
    finally:
        shutil.rmtree(script.parent, ignore_errors=True)
    for _ in range(20):
        if out_path.exists():
            return out_path.read_text().strip()
        time.sleep(0.1)
    print("REAPER did not answer -- is it running, and is a modal dialog "
          "open? (see README, 'The panel starts but no window appears')",
          file=sys.stderr)
    return None


def _state_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") \
        / "xtouchmini" / "state.json"


def cmd_scales(args) -> int:
    """The three numbers that must agree, side by side.

    They are three different quantities that all get called "volume", and the
    engine's shadow is fed from more than one of them. If the columns below
    disagree, no amount of staring at the pickup arithmetic will help: it is
    comparing a fader position against something that is not one.
    """
    out = Path(tempfile.gettempdir()) / "xt-fader-probe.txt"
    out.unlink(missing_ok=True)
    answer = _reascript(out)
    if answer is None:
        return 1
    if answer == "NOTRACK":
        print("no track selected in REAPER -- select one and try again")
        return 1

    index, name, gain, db, slider = answer.split("\t")
    gain, db, slider = float(gain), float(db), float(slider)

    path = _state_path()
    try:
        published = parse(path.read_text()).track
    except OSError:
        print(f"cannot read {path} -- is the panel running?", file=sys.stderr)
        return 1

    print(f"\n{BOLD}selected track{RESET}  {index}  {name}   ({db:+.2f} dB)\n")
    rows = [
        ("REAPER D_VOL (gain)", gain, "not a fader position at all"),
        ("REAPER fader position", slider, "what /track/N/volume means"),
        ("state.json track.volume", published.volume,
         "what the daemon seeds its shadow from"),
    ]
    for label, value, note in rows:
        print(f"  {label:<26} {value:>8.4f}   {DIM}{note}{RESET}")

    agree = abs(published.volume - slider) <= 0.01
    print()
    if agree:
        print(f"  {GREEN}state.json agrees with REAPER's fader position.{RESET}")
    else:
        print(f"  {RED}{BOLD}MISMATCH{RESET}{RED}: the panel publishes "
              f"{published.volume:.4f} where REAPER's own fader scale — the "
              f"one\n  the daemon sends on and receives feedback in — calls "
              f"this level {slider:.4f}.{RESET}")
        print(f"\n  {DIM}The shadow is fed from both, so it flips between the "
              f"two scales.\n  Soft pickup then aims at a target that is not "
              f"where the track is\n  (jumps, and refusing to move), and "
              f"relative adds its nudges to a\n  base that changes under it "
              f"(jumps).{RESET}")
    print(f"\n  track index: REAPER {index}, state.json {published.index}")
    return 0 if agree else 1


# --- live: follow the daemon's own account ----------------------------------

def cmd_live(args) -> int:
    drop_in = Path.home() / ".config/systemd/user/xtouchmini.service.d/debug.conf"
    if not drop_in.exists() or "XTOUCHMINI_DEBUG_FADER" not in drop_in.read_text():
        print(f"{YELLOW}fader logging is not enabled in the daemon.{RESET}")
        print("\nTurn it on with:\n")
        print(f"  mkdir -p {drop_in.parent}")
        print(f"  printf '[Service]\\nEnvironment=XTOUCHMINI_DEBUG_FADER=1\\n'"
              f" >> {drop_in}")
        print("  systemctl --user daemon-reload && "
              "systemctl --user restart xtouchmini\n")
        return 1

    print(f"{BOLD}Following the daemon. Move the fader.{RESET} Ctrl-C to stop.\n")
    proc = subprocess.Popen(
        ["journalctl", "--user", "-u", "xtouchmini", "-f", "-n", "0",
         "-o", "cat"],
        stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            line = line.rstrip()
            # The OSC-feedback chatter drowns the fader lines; drop it unless
            # it is about volume, which is the half that feeds the shadow.
            if "osc: ignoring" in line:
                continue
            print(_colour(line.strip()))
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scenario", help="drive the Engine offline and trace it")
    p.add_argument("name", nargs="?", help=f"one of: {', '.join(SCENARIOS)}")
    p.add_argument("--volume", type=float, default=0.5,
                   help="the track's starting level (default 0.5). The "
                        "relative-pinned-* scenarios are about what happens "
                        "at the stops, so run those with --volume 0.05 and "
                        "--volume 0.95 respectively.")
    p.set_defaults(fn=cmd_scenario)

    p = sub.add_parser("scales", help="what each side calls the same level")
    p.set_defaults(fn=cmd_scales)

    p = sub.add_parser("live", help="follow the running daemon's fader log")
    p.set_defaults(fn=cmd_live)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

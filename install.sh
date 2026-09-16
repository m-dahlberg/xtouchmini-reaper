#!/usr/bin/env bash
# Install the X-Touch Mini bridge. Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/xtouchmini"
REAPER_RES="$HOME/.config/REAPER"
UNIT_DIR="$HOME/.config/systemd/user"

say() { printf '\n== %s\n' "$1"; }

say "Dependencies"
if ! python3 -c 'import rtmidi' 2>/dev/null; then
    echo "installing python3-rtmidi (needs sudo)"
    sudo apt-get install -y python3-rtmidi
else
    echo "python3-rtmidi present"
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    sys.exit("need Python 3.11+ for tomllib")
print(f"python {sys.version.split()[0]}")
PY

say "Launcher"
mkdir -p "$BIN_DIR"
# Symlink, not a generated copy: bin/xtouchmini works out the checkout from its
# own location, so nothing bakes in an absolute path. Moving or renaming the
# checkout only dangles this one symlink -- re-run install.sh to repoint it.
ln -sfn "$HERE/bin/xtouchmini" "$BIN_DIR/xtouchmini"
echo "$BIN_DIR/xtouchmini"

say "Config"
mkdir -p "$CFG_DIR"
if [ -f "$CFG_DIR/config.toml" ]; then
    echo "keeping existing $CFG_DIR/config.toml"
else
    cp "$HERE/config/config.toml" "$CFG_DIR/config.toml"
    echo "wrote $CFG_DIR/config.toml"
fi

say "REAPER scripts"
mkdir -p "$REAPER_RES/Scripts" "$REAPER_RES/OSC"
ln -sfn "$HERE/reaper" "$REAPER_RES/Scripts/XtouchMini"
echo "linked $REAPER_RES/Scripts/XtouchMini -> $HERE/reaper"
cp "$HERE/config/XTouchMini.ReaperOSC" "$REAPER_RES/OSC/XTouchMini.ReaperOSC"
echo "wrote $REAPER_RES/OSC/XTouchMini.ReaperOSC"
# Deliberately not touching Scripts/__startup.lua: the panel is launched as an
# action, and that file already belongs to the ShuttleXpress watcher.

say "Service"
mkdir -p "$UNIT_DIR"
sed "s|%h|$HOME|g" "$HERE/config/xtouchmini.service" > "$UNIT_DIR/xtouchmini.service"
systemctl --user daemon-reload
echo "installed $UNIT_DIR/xtouchmini.service (not started yet)"

cat <<'NEXT'

== Four manual steps remain, all inside REAPER

1. Free the device. REAPER claims the X-Touch exclusively via ALSA rawmidi.
   Preferences > Audio > MIDI Devices > X-TOUCH MINI
     -> disable both input and output.
   Without this the daemon cannot open the device at all.

2. Add the control surface.
   Preferences > Control/OSC/web > Add > OSC
     Mode          Configure device IP+local port
     Local port    8010
     Device IP     127.0.0.1
     Device port   9010
     Pattern       XTouchMini
   Leave "Local IP" alone -- it is the bind interface, not the destination.
   "Local port [receive only]" also works: the panel republishes state.json
   when a value changes, so the LEDs stay correct without the feedback
   channel. It only costs more frequent state.json writes.
   Leave the existing ShuttleXpress surface on port 8000 alone.

3. Register the actions and note their command IDs:
     reaper -nonewinst reaper/register_actions.lua
   (REAPER must already be running.)

4. Run the panel: Actions > xtouch_panel, then dock it.

Then start the daemon:
     systemctl --user enable --now xtouchmini
     journalctl --user -u xtouchmini -f
NEXT

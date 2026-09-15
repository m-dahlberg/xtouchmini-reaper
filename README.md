# X-Touch Mini → REAPER

A control surface for the Behringer X-Touch Mini that answers the question the
hardware cannot: **what does this knob do right now?**

The X-Touch already works as a MIDI surface, and REAPER can already map its
controls. The problem is not capability, it is memory. The controller has no
display, so there is nothing to tell you that encoder 5 is the attack time of
the compressor currently on screen. This project adds a panel you dock beside
your plugins that shows the live legend, and reduces mapping to two clicks:
click an encoder in the panel, touch the control in the plugin.

Mappings are stored **per plugin type, globally** — map ReaComp once and every
ReaComp in every project has it.

## How it fits together

```
X-Touch Mini ──ALSA (rtmidi)──┐
                              │
                ┌─────────────▼──────────────┐
                │  xtouchmini daemon         │
                │  decode · pickup · LEDs    │
                └──┬──────────────────▲──────┘
         OSC 8010  │                  │ OSC 9010 (feedback)
                ┌──▼──────────────────┴──────┐
                │   REAPER  csurf = OSC      │
                └──┬──────────────────▲──────┘
                   │ applies params   │
                ┌──▼──────────────────┴──────┐
                │  ReaImGui panel (Lua)      │
                └──┬─────────────────────────┘
                   │ writes on change
              ~/.config/xtouchmini/state.json ──► daemon
```

**Why OSC and not MIDI.** REAPER's Linux build enumerates ALSA *rawmidi*
devices and does not list sequencer ports at all, so a virtual MIDI port is
invisible to it no matter how often you rescan. The same finding drove the
sibling ShuttleXpress bridge.

**Why the daemon owns the device.** REAPER claims ALSA rawmidi *exclusively*.
Both processes cannot hold the X-Touch, so REAPER has to give it up — see
step 1 of the manual setup.

**Why the panel decides what is active.** The panel is the only component that
knows about plugins. It resolves the focused FX against the mapping store and
writes already-resolved track/FX/parameter indices into `state.json`; the
daemon is a translator that never parses a plugin name. One source of truth
means REAPER's idea of the focused FX and the panel's can never drift apart.

**Why a file and not ExtState.** REAPER rewrites `reaper-extstate.ini` on exit
and would clobber anything written from outside. ExtState is still right for
the transient flags the reserved-row action scripts set, because both ends of
that are inside REAPER.

## The surface

| Control | What it does |
|---|---|
| Encoders 1–8 | Per-plugin mappable. Push and turn for fine adjust; push and release to reset to the plugin's reported centre. |
| Buttons 1–8 | Per-plugin mappable, toggle or momentary. |
| Buttons 9–16 | Reserved. Each one configurable to a REAPER action or a built-in. Stays live with no plugin focused. |
| Fader | Selected-track volume, both layers. Three modes, set in the Fader tab — see below. |
| Layer A / B | A second page — 16 encoder slots and 16 button slots per plugin. The Surface tab shows both; the Display tab follows whichever the device is on. |

**Per-control feel.** Each slot has a taper (`linear`, `log`, `exp`) and a
sensitivity multiplier. A fixed step in the normalised domain is not a fixed
step in what a parameter means — measured on ReaEQ's frequency, one detent
moves 14% at 20 Hz but 4% at 1 kHz. `log` evens that out. See `docs/taper.md`.

**Custom labels.** Registered parameter names are often long and unhelpful
(`Gain-Low Shelf`). Any slot can be given a short label, which is what the
Display tab shows.

**Strict focus.** Encoders are live only while a plugin window is focused.
Click the arrange view and the panel blanks and the encoders go dead. The one
exception is the panel itself: clicking it does not count as losing focus, or
click-to-learn could never work.

**Fader modes.** The fader is not motorised, so after a track change it sits
wherever you left it, saying nothing true about the track it now controls.
There are three answers to that, chosen per taste in the Fader tab:

| Mode | What happens |
|---|---|
| **Absolute** | The track jumps to wherever the fader is. One to one, always — so the first touch after a track change moves the volume. |
| **Soft pickup** | Nothing happens until the fader crosses the track's current volume, then it takes over. Nothing jumps; you have to find the level first. |
| **Relative** | Every move nudges the volume by how far you moved. The two faders are never aligned — except at the stops — so nothing can jump, at the cost of running out of travel, which the end zones below give back. |

**Running out of road.** A relative fader eventually reaches an end stop with
the track's level nowhere near its own, and the way out has to cost nothing.
The last few steps at each end are an *end zone*: reach one and output pauses.
The next move is read as a **walk-back** — you drag the fader to somewhere
useful and it moves no volume doing it. The walk-back ends when you stop
pushing it, which shows up in exactly two ways:

- **you reverse direction** — that move is the first thing you meant since the
  walk-back began, so it counts and is sent; or
- **you hold still** for the timeout (1 s by default).

Walking back so far that you land in the *other* end zone is not an error: it
parks there and waits to be walked back the other way, because there is no road
that direction either.

**Except when the two are already agreed.** Pull a track all the way down and
the volume is at silence while the fader is at the bottom — pinned at the same
end, aligned by accident but exactly. There is nothing to recalibrate against,
so the fader comes straight back up with no walk-back; demanding one would
refuse the only thing anybody wants to do next. The same holds at the top.
Alignment is what is checked, not position: a volume that is not at the end,
or a move pushing further *into* it, is the ordinary case and still needs the
walk-back.

All three numbers — sensitivity, end-zone width, timeout — are sliders on the
Fader tab, because they are feel, and feel is argued about with the control in
your hand. They are stored in `mappings.json` and reach the daemon through
`state.json`, not through `config.toml`: the panel is the authority on what the
daemon should be doing, the same rule that puts resolved parameter indices
there. `pickup_epsilon` stays in `config.toml` — how close is close enough is
not something anyone adjusts twice.

## Install

```sh
./install.sh
```

Then four manual steps inside REAPER, which `install.sh` prints:

1. **Free the device** — Preferences → Audio → MIDI Devices → X-TOUCH MINI →
   disable input *and* output.
2. **Add the OSC surface** — Preferences → Control/OSC/web → Add → OSC.
   Mode **`Configure device IP+local port`** (that is the exact menu wording,
   and the only option that both listens and sends): local port `8010`,
   device IP `127.0.0.1`, device port `9010`, pattern `XTouchMini`. Leave
   "Local IP" alone — it is the bind interface, not the destination.

   **`Local port [receive only]` also works.** The panel republishes
   `state.json` whenever a mapped value changes, including changes made with
   the mouse, and the daemon repaints the LEDs from that — so the OSC feedback
   channel is an optimisation, not a requirement. Receive-only costs only more
   frequent `state.json` writes.
3. **Register the actions** — `reaper -nonewinst reaper/register_actions.lua`
4. **Run the panel** — Actions → `xtouch_panel`, then dock it.

```sh
systemctl --user enable --now xtouchmini
journalctl --user -u xtouchmini -f
```

### One-time hardware setup (Windows)

The encoders ship in **absolute** mode, which is unusable for endless knobs:
they pin at 0 and 127 and go dead, and the position cannot be resynced from the
host — the LED ring is display-only. This was measured, not assumed; see
`docs/protocol.md`.

The fix is the X-TOUCH Editor, which stores the setting *in the device*, so it
is needed once and never again:

- All 8 encoders, **both layers**: set the *minimum value* field to `relative1`.
- All 16 buttons, both layers: set to **momentary** (they ship latching).
- Write the preset back to the device.

## Commands

```sh
xtouchmini run          # the daemon (what the service runs)
xtouchmini monitor      # decode controller events, touch nothing
xtouchmini ports        # list MIDI ports, say which matches
xtouchmini leds         # light everything, to prove the output path
xtouchmini mode std|mc  # switch the device's operating mode
```

`tools/capture_midi.py session` arms a self-terminating capture and infers the
protocol from what it sees — that is how every number in `docs/protocol.md` was
established.

## Tests

```sh
python3 tools/run_tests.py
```

Python suites run anywhere. The Lua suites are handed to an **already-running**
REAPER with `-nonewinst`; the runner refuses to start one, because `-nonewinst`
silently *becomes* the instance when none is running, leaving a stray GUI
REAPER holding the audio device and rewriting `~/.config/REAPER` on exit.

| Suite | Covers |
|---|---|
| `tests/` | OSC wire format, protocol decode, engine behaviour, config, state parsing |
| `reaper/test/headless.lua` | JSON, mapping resolution, the moved-parameter case |
| `reaper/test/ui_frame.lua` | One panel frame against a stub ImGui; stack balance; the focus exception |
| `reaper/test/verify_in_reaper.lua` | Real track, real plugin, `GetLastTouchedFX`, `state.json`, the published volume scale |

The fader has a manual rig of its own, because soft pickup and relative are
both about what happens *between* moves and neither is judgeable without the
control in your hand:

```sh
tools/fader_test.py scales     # what each side calls the current level
tools/fader_test.py scenario   # drive the real engine offline, trace it
tools/fader_test.py live       # follow the running daemon, move the fader
```

`scales` is the one to run first when the fader misbehaves — see Troubleshooting.

## After changing the code

The checkout **is** the installation, so nothing is copied on save — but the
two halves pick up changes differently:

| Changed | To take effect |
|---|---|
| `xtouchmini/*.py` (the daemon) | `systemctl --user restart xtouchmini` |
| `reaper/**/*.lua` (the panel) | Terminate and re-run the `xtouch_panel` action |
| `config/XTouchMini.ReaperOSC` | Re-copy to `~/.config/REAPER/OSC/` and reopen REAPER's OSC dialog |

The daemon notices when its own source has changed under it and says so in the
journal, because a Python edit that appears to have had no effect is otherwise
indistinguishable from a bug:

```
NOTE: the daemon source has changed since this process started --
run `systemctl --user restart xtouchmini` for it to take effect
```

## Troubleshooting

### The panel "starts" but no window appears

A modal dialog in REAPER — **Preferences**, or the ReaScript task-control
warning — blocks the UI thread. Deferred callbacks stop firing, so a ReaImGui
panel never draws its first frame, while REAPER itself looks completely
healthy: the audio thread keeps running at normal CPU, and every *synchronous*
ReaScript still works. REAPER will also insist the script is already running,
because it is — it is just never getting a frame.

Close the dialog and the panel appears by itself. To confirm before hunting
elsewhere:

```sh
python3 ~/.claude/skills/reascript-lua/assets/reascript_test.py \
        reaper/test/diag_defer.lua && sleep 2 && cat /tmp/xt-defer.txt
```

`sync: script loaded` with no `defer frame` lines means defers are blocked.

### Checking the OSC surface without opening the dialog

REAPER writes the surface into `reaper.ini` **on exit**, so the line is only
current after REAPER has quit at least once since you configured it:

```
csurf_1=OSC "XTouchMini" 7 8010 "127.0.0.1" 9010 1024 10 "XTouchMini"
                         ^  ^    ^           ^                ^
                      mode  |    device IP   device port      pattern file
                            listen port
```

Observed mode values: **7** = `Configure device IP+local port` (send and
receive), **3** = `Local port [receive only]`. Worth reading back after setup —
a mistyped device port is invisible in the GUI and silently costs you the
feedback channel.

### The encoders do nothing

Check `~/.config/xtouchmini/state.json` exists and its mtime moves when you
focus a plugin. That file is the seam between the two halves: no file means the
panel is not running (see above), a stale file means the panel is not seeing
focus changes, and a fresh file with `"active": false` means strict focus has
decided no plugin is focused.

```sh
systemctl --user status xtouchmini
journalctl --user -u xtouchmini -f
```

### The device went dead after being unplugged

Every lamp is off and nothing the controller does reaches REAPER, while the
panel keeps working normally: it still tracks focus, `state.json` still moves.
The journal is the tell -- with `XTOUCHMINI_DEBUG=1` there are no decoded
events at all, only OSC feedback lines, and the last `connected:` line is from
before the unplug.

An ALSA unplug raises nothing. The sequencer port is torn down and the
subscription goes with it, but `get_message()` just returns nothing for ever
after, which from inside the read path is exactly what an untouched controller
looks like. Replugging does not help: the kernel hands the new device the same
port name and usually the same client number, and the daemon is still holding a
handle on the old one. `aconnect -l` shows it -- the X-Touch with no
`Connecting To:`, and the daemon's `RtMidiIn Client` with nothing connected
from:

```sh
aconnect -l | grep -A2 -i 'x-touch\|rtmidi'
cat /proc/asound/card2/midi0     # Rx bytes stuck at 0 == nobody is reading it
```

So the daemon asks ALSA once a second whether the port it opened is still in
the list (`Device.alive`), and drops back to the reconnect loop when it is not.
The unplugged window is as long as the cable is out, which is what makes a
poll sufficient where comparing names across a replug would see nothing.

### The LED rings show a different plugin's values

The rings are painted from `state.json`, but REAPER's OSC feedback can also
move them, and the two race whenever the focused plugin changes: REAPER sends
a burst of every parameter of the new FX before the panel has republished.
The daemon only adopts feedback while REAPER's own `/fx/name` and
`/fx/number/str` agree with what `state.json` says is focused, and says so
when it refuses a burst:

```
osc: ignoring parameter feedback -- REAPER says track None fx 3 ('TAL Reverb 4 ...'),
     state.json says track 1 fx 0 ('')
```

That line during a focus change is the mechanism working. The same line
persisting while a plugin sits focused is not: it means REAPER and the panel
disagree about what is focused, and the rings are running on `state.json`
alone. An FX in a track's input/monitoring chain is the likely cause, since
REAPER numbers those separately.

### The fader jumps, or sits there doing nothing

Both at once, alternating, is the signature of a **scale disagreement** rather
than anything wrong with the mode you are in. Run:

```sh
tools/fader_test.py scales
```

It prints what REAPER, the panel and OSC each call the current level and says
whether they agree. Three different quantities are called "volume" around here:

| | unity gain reads as |
|---|---|
| `D_VOL`, a linear gain factor | `1.0000` |
| `D_VOL / 4` | `0.2500` |
| **fader position** — what `/track/N/volume` means | **`0.7160`** |

The daemon's shadow of the track level is fed from *both* `state.json` and
REAPER's OSC feedback. If those are not in the same scale the shadow flips
between two numbers for one level, and every mode inherits it: soft pickup
waits for the fader to reach a target that is not where the track is (so it
does nothing, then takes over abruptly when it finally crosses), and relative
adds its nudges to a base that moves under it (so it jumps). `xt/volume.lua`
converts, and `DB2SLIDER` is the only honest way across — the taper is not
linear in either gain or dB.

To watch the daemon decide, in its own words:

```sh
tools/fader_test.py live
```

Every fader event, including every decision *not* to move the track — which is
the half that otherwise has no outward sign at all:

```
-- fader layer A raw=100 pos=0.7874 mode=relative shadow=0.7160 track=17
relative: raw 96->100 (+4) = +0.0315, volume 0.7160 -> 0.7475
SENT /track/17/volume 0.7475
```

A `shadow <- state.json` or `shadow <- osc feedback` line that moves the value
a long way is the scale problem above. To reason about the state machine
without touching the hardware, `tools/fader_test.py scenario` drives the real
engine through scripted sweeps and prints the same trace.

### The encoders die when I select a different track

The plugin stays focused, the panel still shows it, and nothing the encoders do
reaches REAPER. `/track/<N>/` in an OSC address is an index into the control
surface's track **bank**, not a REAPER track number, so the bank has to be wide
enough to contain the track you are addressing:

```
DEVICE_TRACK_COUNT 128      # in XTouchMini.ReaperOSC
```

With `DEVICE_TRACK_COUNT 1` the only reachable slot is `/track/1/`, and
`DEVICE_TRACK_FOLLOWS LAST_TOUCHED` aims it at the *selected* track. That is
correct for the fader, which follows the selection on purpose, and wrong for
the encoders, which drive the focused FX wherever it lives. It also fails
silently, and only once you select some other track -- on track 1 the absolute
number and the bank slot are both `1`, so everything works.

**REAPER only re-reads a pattern file when the control surface is recreated.**
Editing the file changes nothing until: Preferences -> Control/OSC/web -> select
the OSC surface -> Edit -> OK. Restarting REAPER also does it.

### The fader stays on the track I just left

Only if you changed track from something other than a mouse click in the
REAPER window — another control surface, say. `DEVICE_TRACK_FOLLOWS
LAST_TOUCHED` makes the bare `/track/volume` address follow "the last touched
track *in the REAPER window*" (REAPER's wording), and selecting a track
elsewhere does not touch it in that sense. The panel reads the real selection
every frame and shows the new track correctly, which is what makes this look
like a daemon bug rather than an addressing one.

Fixed by addressing the fader explicitly, `/track/<N>/volume`, with the number
from `state.json`. Needs `TRACK_VOLUME n/track/volume n/track/@/volume` in the
pattern file, and a REAPER surface reload to take effect.

### Watching what the daemon actually sends

Two debug switches, both via a systemd drop-in
(`~/.config/systemd/user/xtouchmini.service.d/debug.conf`):

| `XTOUCHMINI_DEBUG=1` | every decoded controller event |
| `XTOUCHMINI_DEBUG_FADER=1` | every fader decision, including the ones to do nothing. Quiet — a dozen lines for a whole sweep. `tools/fader_test.py live` turns it on and formats it |
| `XTOUCHMINI_DEBUG_OSC=1` | every inbound OSC message — several hundred per focus change, so turn it off again |

The MIDI going *to* the device can be watched without disturbing the daemon,
because rtmidi opens an ALSA sequencer port. Find it with `aconnect -l` (the
`RtMidiOut Client` whose pid is the daemon's) and dump it:

```sh
aconnect -l                 # -> e.g. client 131: 'RtMidiOut Client'
aseqdump -p 131:0           # CC 9-16 are the rings, notes 8-23 the lamps
```

## Traps worth knowing

Recorded in full in `docs/protocol.md`; these are the ones that cost time.

- **The layer B fader is CC 10, not CC 19.** The encoder banks sit far apart
  (1–8, 11–18), so extrapolating the pattern gets it wrong. Guessed wrong once;
  the failure is silent.
- **The manual is wrong about the button LED notes.** It says the upper row
  listens on notes 0–7; measured, notes 8–15 light the **top** row and 16–23
  the **bottom** — the same notes the buttons send. Notes 0–7 address nothing.
- **Closing the MIDI port clears every lamp**, so a diagnostic must hold the
  port open while you look at the device.
- **The hardware accelerates by itself** — 3 counts on a fast click, 1 on a
  slow one. The daemon scales linearly; a second curve makes it unusable.
- **`Mini` and `MINI` are different devices here.** A RØDE microphone and the
  X-Touch differ only in the case of their ALSA card id, and the card *number*
  moves. Match on the rtmidi port name.

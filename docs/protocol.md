# X-Touch Mini MIDI protocol

Measured on the actual unit (Behringer X-TOUCH MINI, USB 1397:00b3, ALSA card
id `MINI`), cross-checked against the RX MIDI DATA table in the Quick Start
Guide. Where the two disagree, the measurement wins and the disagreement is
noted.

Capture tool: `tools/capture_midi.py` (`session`, then `analyze`).
Raw logs: `docs/capture-raw.log`, `docs/capture-resync.log`.

## Global

- **Global channel: 11** (status nibble `0xA`, e.g. `BA` for CC). Every message
  in and out uses it in Standard mode.
- **Operation mode select: `CC 127`** on the global channel.
  `0` = Standard mode (MC LED off), `1` = MC mode (MC LED on), `2-127` ignored.
  Switchable over MIDI with no unplugging, and remembered across power-off.
  The manual's alternative is holding the MC button while connecting USB.
- **Preset layer change: Program Change** on the global channel.
  `0` = Layer A, `1` = Layer B. **Standard mode only** — MC mode has no layers.
  **This does not work on the unit here.** Sending it on the global channel
  (the same channel whose note messages drive the LEDs correctly) leaves the
  device on whatever layer it was on and the A/B lamp unchanged. Nothing in
  the bridge depends on it: the daemon learns the live layer from the CC and
  note numbers that arrive, which is the only thing that reflects what the
  hardware is actually doing.
- **Button LEDs are addressed the same way on both layers** — `Note 0`-`Note 7`
  for the upper row and `Note 8`-`Note 15` for the lower one, measured on
  layer A. Whether layer B uses the same range is still unverified.

## Standard mode, TX (device -> host)

Both layers measured (`docs/capture-raw.log`, `docs/capture-layerb.log`), after
the encoders were set to `relative1` and the buttons to momentary in the
X-TOUCH Editor.

| Control | Layer A | Layer B |
|---|---|---|
| Encoders 1-8 | `CC 1`-`CC 8` | `CC 11`-`CC 18` |
| Fader | `CC 9` | **`CC 10`** |
| Encoder push 1-8 | `Note 0`-`Note 7` | `Note 24`-`Note 31` |
| Buttons 1-16 | `Note 8`-`Note 23` | `Note 32`-`Note 47` |

The device switches layers internally and emits different numbers for each, so
the daemon recovers the live layer from the message that arrived. Nothing else
knows it: there is no "layer changed" message.

### Trap: the layer B fader is CC 10, not CC 19

The encoder banks sit far apart (1-8 and 11-18), so extrapolating the pattern
puts the layer B fader at CC 19. It is actually **CC 10**, adjacent to layer
A's CC 9. This was guessed wrong first time round, and the failure mode is
silent -- CC 19 is simply never sent, and CC 10 goes unrecognised, so the
layer B fader does nothing at all while everything else works.

### Encoders after the editor change

`relative1`, two's complement, **with the hardware acceleration retained**:

| Value | Meaning |
|---|---|
| `0x01` | +1 (slow turn) |
| `0x03` | +3 (fast turn) |
| `0x7F` | -1 |
| `0x7E` | -2 |

Because the hardware accelerates, the daemon scales the delta **linearly** and
adds no curve of its own; a second layer of acceleration makes the encoders
unusable.

### Buttons after the editor change

Momentary: note-on velocity 127 on press, note-off on release. As shipped they
latched, sending one message per press and nothing on release, which left the
software unable to distinguish a hold from a tap.

## Standard mode, RX (host -> device)

| Function | Message | Values |
|---|---|---|
| LED ring position, layer A | `CC 1`-`CC 8` | 0 all off; 1-127 spread evenly over the 13 LEDs |
| LED ring position, layer B | `CC 11`-`CC 18` | as above |
| LED ring behaviour | unknown | see below |
| Button LEDs | `Note 8`-`Note 23` | 0 off, 1 on, 2 blinking; 3-127 ignored |

Verified by sending them: all 16 button LEDs, and an eight-ring chase on each
CC range in turn with the port held open.

### The manual is wrong about the ring CCs, too

The Quick Start Guide's RX table says `CC 1-8` selects the ring *behaviour*
(Single/Pan/Fan/Spread/Trim) and `CC 9-16` sets the ring *position*. Both
halves are wrong for this unit, and neither fails visibly:

* **`CC 1-8` is the ring position for layer A.** Writing a behaviour value
  there does not configure anything — 0-4 simply moves the ring to "off" or
  LED 1-4. That is how this was caught: a test that set five different
  behaviours on five encoders lit them at five different positions instead.
* **`CC 9-16` addresses nothing useful.** `CC 9` and `CC 10` are the two
  faders, which have no rings at all, and `CC 11-16` are layer B's encoders
  1-6. Sending layer A's ring values there paints six of layer B's rings —
  invisible until you press the layer button — and drops the other two.

The rule is the same one the button lamps follow: **a ring listens on the same
CC its own encoder sends**, so unlike the button lamps the ring numbers *do*
change with the layer.

Where ring behaviour actually lives is unknown. It has not been measured, and
the manual's number for it is now known to be the position range, so writing
there would corrupt the rings rather than configure them. Nothing in the bridge
sends one.

### How this hid for so long

The daemon sent every ring message to `CC 9-16`, so the rings never showed a
parameter value. What they did show was the device redrawing its own ring from
its internal counter whenever an encoder was physically turned — which looks
close enough to working that the fault reads as "the rings are decoupled from
the plugin" rather than "the rings have never been driven at all".

Snooping the daemon's own output proves only that the right bytes left the
daemon; it cannot show that they landed anywhere. Confirming a lamp change
means looking at the device.

### The manual is wrong about the button LED notes

The Quick Start Guide's RX table says:

> Upper Row 1-8: Note 0 – Note 7 · Lower Row 9-16: Note 8 – Note 15

**That is not what this unit does.** Measured one range at a time, with the
port held open:

| Notes sent | What lights |
|---|---|
| `0` alone | nothing |
| `8`-`15` | the **top** row, all eight |
| `16`-`23` | the **bottom** row, all eight |

So a button's lamp listens on **exactly the note the button sends** — RX and TX
are symmetric, and notes 0-7 address nothing at all, being the encoder pushes,
which have no lamps.

Following the manual here produces a subtle wrong result rather than an
obvious one: the reserved row (buttons 9-16, notes 16-23) lights up on the
*top* row instead, because notes 8-15 were being used for it.

### The layer button transmits nothing

Pressing Layer A or B sends **no MIDI at all** — verified with a raw capture
open across four presses, which logged zero messages and moved the driver's
Rx byte counter not at all. There is no "layer changed" message to listen for.

The daemon therefore infers the live layer from the CC and note numbers that
arrive, which only happens once you touch a control on the new layer.

That matters for the lamps, because **the device keeps LED state per layer**:
switching to layer B shows layer B's lamps, which have never been written, so
everything goes dark. Nothing can detect this, so the daemon repaints the whole
surface unconditionally once a second (`[feel] led_refresh_interval`). At 24
messages a repaint that is a fraction of the traffic the un-deduplicated
version used to send.

### Trap: closing the port clears every lamp

The device drops all LED state when the MIDI port is closed, so a test that
sets a lamp and exits leaves nothing to look at. Any diagnostic has to hold the
port open while you look — and the daemon has to repaint on connect, which it
does.

### Trap: the device draws the ring you are turning

Turning an encoder makes the device move that encoder's ring itself, without
being asked. A host that de-duplicates its LED writes -- only sending a ring
message when the 13-step display would actually change -- therefore loses the
ring for most of a turn, because one detent is usually smaller than one step.
The visible result is a ring that jumps to the right place occasionally and
sits somewhere near LED 1 the rest of the time, which reads as the ring
"snapping back" rather than as a missing repaint.

The engine forces the repaint of a ring whose encoder was just turned
(`_led(..., force=True)`), and keeps de-duplicating everything else.

### Trap: ring position is 0-127, not 1-13

The manual's 1-13 is the *display*, not the wire. Measured with a static
staircase across the eight rings -- 0, 18, 36, 54, 73, 91, 109, 127 -- the
result is an even ramp from dark to full, so the value is a normal 0-127 CC
that the device quantises to 13 LEDs itself. That follows from the CC being
the encoder's own: it is the same domain an absolute encoder reports its
position in.

Sending 1-13 does not fail, which is the trap. Thirteen of 127 is a tenth of
the range, so every value in the parameter's span lands on the first LED or
two and the ring looks unresponsive rather than mis-scaled.

0 is genuinely all off -- a ring sent 0 is completely dark -- and 1 is the
lowest lit value. So an unassigned encoder (sent 0) stays distinguishable from
a parameter sitting at its minimum (sent 1), which is what ring_position
relies on.

Blinking, and the manual's "27 all on / 28 all blinking" codes, belong to the
1-13 scheme and mean nothing here; 127 is simply a full ring. Nothing in the
bridge sends a blink, and the parameter has been removed rather than left as
another untested constant.

## The encoder problem (resolved)

**As shipped**, the encoders are physically endless but report an **absolute**
0-127 position, with hard stops at both ends. This was fixed with the X-TOUCH
Editor -- the history is kept because the absolute path is still implemented as
a fallback, and because the resync finding below is what ruled out every
workaround that did not involve the editor.

Measured, turning right then left (`docs/capture-raw.log`):

```
1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 35,   <- right
34, 32, 29, 26, 23, 20, 17, 14, 11, 8, 5, 2        <- left
```

The step size varies with turn speed — 3 per click when turned fast, 1 per
click when turned slowly (`docs/capture-resync.log`), so **the hardware already
applies its own acceleration**. Software must not add a second layer of it.

### Resync is not possible

Tested directly: encoder 1's counter was left at ~2, its LED ring was driven to
position 13 (far right), and the encoder was then turned right. It reported
`1, 2, 3, 4, 5, 6, 7, 8` — continuing from its own counter, ignoring the ring.

**The LED ring is display-only. There is no RX command that sets an encoder's
internal position**: the ring CCs above move the lamps and nothing else, and
nothing in the RX set addresses the counter.

The consequence is that in stock Standard mode an encoder silently stops
responding once it pins at 0 or 127 — roughly 40 clicks of one-way turning.
Deriving deltas from successive absolute values gives a relative *feel* but
cannot avoid the dead zones.

### The fix: the X-TOUCH Editor

The editor (Windows) can switch any encoder to a relative mode, and the setting
is stored in the device -- so this is a one-time configuration, after which
Linux needs no Windows involvement. The relative options are exposed, oddly, in
the encoder's *minimum value* field: `relative1`, `relative2`, `relative3`.

This is the chosen route, because it is the only one that keeps relative
encoders *and* the A/B layers.

Also worth setting while in there: the 16 buttons ship as latching/toggle and
should be **momentary**, so that toggle behaviour is decided by the mapping
rather than baked into the hardware.

The three relative encodings are the usual Behringer set, and the daemon
supports all of them plus absolute, so whichever one gets selected will work:

| Mode | Encoding | Example |
|---|---|---|
| `relative1` | two's complement | `0x01`=+1, `0x7F`=-1 |
| `relative2` | binary offset | `0x41`=+1, `0x40`=0, `0x3F`=-1 |
| `relative3` | signed bit | `0x01`=+1, `0x41`=-1 |

## MC mode

Not characterised. It would give relative encoders at the cost of the A/B
layers, and is the fallback only if the editor route fails.

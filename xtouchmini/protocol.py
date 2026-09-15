"""X-Touch Mini wire protocol.

Every number here was measured on the unit and cross-checked against the RX
MIDI DATA table in the Quick Start Guide; see docs/protocol.md, which also
records the two traps encoded below.

Standard mode only. MC mode has no A/B layers and is not used.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

GLOBAL_CHANNEL = 11                    # 1-based, as the manual numbers it
_CH = GLOBAL_CHANNEL - 1

CC = 0xB0 | _CH
NOTE_ON = 0x90 | _CH
NOTE_OFF = 0x80 | _CH
PROGRAM_CHANGE = 0xC0 | _CH

ENCODERS = 8
BUTTONS = 16
RING_LEDS = 13                         # physical LEDs in one ring
# The wire domain, measured: 0 is all off, and 1-127 spreads evenly across the
# thirteen LEDs. NOT the manual's 1-13 -- see the RX notes below.
RING_MIN, RING_MAX = 1, 127

LAYER_A, LAYER_B = "A", "B"


@dataclass(frozen=True)
class LayerMap:
    """The TX numbers one preset layer emits.

    The device switches layers internally and sends different numbers for each,
    so the daemon works out which layer is live from the message that arrived
    rather than from anything the panel tells it.
    """
    name: str
    encoder_cc: range
    fader_cc: int
    push_note: range
    button_note: range
    measured: bool


# --- TX: what the device sends ---------------------------------------------
# Layer A is measured; see docs/capture-raw.log.
TX_LAYER_A = LayerMap(
    name=LAYER_A,
    encoder_cc=range(1, 9),            # CC 1-8
    fader_cc=9,
    push_note=range(0, 8),             # notes 0-7, momentary
    button_note=range(8, 24),          # notes 8-23
    measured=True,
)

# Layer B, measured; see docs/capture-layerb.log.
#
# Note the fader: it is CC 10, NOT the CC 19 that the encoder block's layout
# would suggest. The two faders sit adjacent at CC 9 and CC 10 while the
# encoder banks are far apart, so guessing from the pattern gets it wrong --
# and a wrong fader CC drives the wrong control silently.
TX_LAYER_B = LayerMap(
    name=LAYER_B,
    encoder_cc=range(11, 19),          # CC 11-18
    fader_cc=10,
    push_note=range(24, 32),           # notes 24-31
    button_note=range(32, 48),         # notes 32-47
    measured=True,
)

LAYERS = (TX_LAYER_A, TX_LAYER_B)

BY_LAYER = {layer.name: layer for layer in LAYERS}

TX_ENCODER_CC = TX_LAYER_A.encoder_cc
TX_FADER_CC = TX_LAYER_A.fader_cc
TX_PUSH_NOTE = TX_LAYER_A.push_note
TX_BUTTON_NOTE = TX_LAYER_A.button_note

# --- RX: what the device accepts -------------------------------------------
RX_BUTTON_LED_NOTE = range(8, 24)      # notes 8-23, the SAME as TX
RX_MODE_CC = 127                       # 0 = Standard, 1 = MC

# Measured: an encoder's LED ring listens on the SAME CC the encoder sends, so
# the ring numbers move with the layer -- layer A on CC 1-8, layer B on
# CC 11-18. ring_cc() is the only place that knows it.
#
# The Quick Start Guide's RX table says "LED ring behaviour CC 1-8, LED ring
# position CC 9-16". Both halves are wrong for this unit, and wrong in a way
# that fails silently rather than visibly:
#
#   * CC 1-8 is the ring POSITION for layer A, not a behaviour selector.
#     Writing a behaviour value there (0-4) does not configure anything, it
#     moves the ring to LED 1-4 -- which is how this was finally caught.
#   * CC 9-16 addresses nothing useful at all. CC 9 and CC 10 are the two
#     FADERS, which have no rings, and CC 11-16 are layer B's encoders 1-6.
#     So a daemon writing layer A's ring values to CC 9-16 silently paints six
#     of layer B's rings and drops the other two on the floor. Every ring
#     message was going there; the rings looked "decoupled" from the plugin
#     because the only thing still driving them was the device redrawing its
#     own ring from its internal counter when an encoder was physically
#     turned.
#
# Where the ring BEHAVIOUR lives on this unit is unknown -- untested, and there
# is now good reason to distrust the manual's number for it. Nothing here sends
# one, and nothing should start without measuring first: the manual's CC 1-8
# would corrupt the ring positions.

# Measured: a button's lamp listens on the SAME note the button sends.
# Notes 8-15 light the top row, 16-23 the bottom row -- verified one range at a
# time with the port held open, because closing it clears every lamp.
#
# The Quick Start Guide's RX table says "Upper Row 1-8: Note 0 - Note 7, Lower
# Row 9-16: Note 8 - Note 15". That is wrong for this unit: note 0 alone lights
# nothing, and notes 8-15 light the TOP row rather than the bottom. Notes 0-7
# are the encoder pushes, which have no lamps at all.
#
# The BUTTON lamp numbers do not change with the layer: they are physical, and
# always address whichever layer is visible. The rings are not like this -- see
# ring_cc -- which is exactly why the difference is worth writing down.
TX_BUTTON_NOTE_OFFSET = 8


class LedState(Enum):
    OFF = 0
    ON = 1
    BLINK = 2


class Encoding(Enum):
    """How an encoder reports motion. Set in the X-TOUCH Editor."""
    ABSOLUTE = "absolute"
    RELATIVE_1 = "relative1"           # two's complement: 0x01=+1, 0x7F=-1
    RELATIVE_2 = "relative2"           # binary offset:    0x41=+1, 0x3F=-1
    RELATIVE_3 = "relative3"           # signed bit:       0x01=+1, 0x41=-1


def decode_delta(value: int, encoding: Encoding) -> int:
    """Turn one encoder CC value into a signed step count.

    ABSOLUTE has no delta of its own -- the caller must diff against the
    previous position -- so it returns the raw value and is handled upstream.
    """
    if encoding is Encoding.RELATIVE_1:
        return value - 128 if value > 64 else value
    if encoding is Encoding.RELATIVE_2:
        return value - 64
    if encoding is Encoding.RELATIVE_3:
        return -(value - 64) if value >= 64 else value
    return value


# --- events -----------------------------------------------------------------

@dataclass(frozen=True)
class EncoderTurn:
    index: int                          # 0-based
    layer: str = LAYER_A
    delta: int = 0                      # relative encodings
    absolute: int | None = None         # absolute encoding


@dataclass(frozen=True)
class EncoderPush:
    index: int
    pressed: bool
    layer: str = LAYER_A


@dataclass(frozen=True)
class ButtonPress:
    index: int                          # 0-based, 0-15
    pressed: bool
    layer: str = LAYER_A


@dataclass(frozen=True)
class FaderMove:
    value: float                        # normalised 0.0-1.0
    raw: int
    layer: str = LAYER_A


def decode(msg: bytes | list[int], encoding: Encoding,
           layers: tuple = LAYERS) -> object | None:
    """Decode one MIDI message into an event, or None if we don't care.

    The layer is recovered from the number that arrived, because that is the
    only thing that actually knows: the hardware switches layers by itself.
    """
    if len(msg) < 3:
        return None
    status, d1, d2 = msg[0], msg[1], msg[2]

    if status == CC:
        for layer in layers:
            if d1 in layer.encoder_cc:
                index = d1 - layer.encoder_cc.start
                if encoding is Encoding.ABSOLUTE:
                    return EncoderTurn(index, layer.name, absolute=d2)
                return EncoderTurn(index, layer.name,
                                   delta=decode_delta(d2, encoding))
            if d1 == layer.fader_cc:
                return FaderMove(value=d2 / 127.0, raw=d2, layer=layer.name)
        return None

    if status in (NOTE_ON, NOTE_OFF):
        pressed = status == NOTE_ON and d2 > 0
        for layer in layers:
            if d1 in layer.push_note:
                return EncoderPush(d1 - layer.push_note.start, pressed,
                                   layer.name)
            if d1 in layer.button_note:
                return ButtonPress(d1 - layer.button_note.start, pressed,
                                   layer.name)
    return None


# --- outbound ---------------------------------------------------------------

def ring_cc(index: int, layer: str) -> int:
    """The CC that lights encoder `index`'s ring, on `layer`.

    `layer` is required rather than defaulted because getting it wrong is
    invisible: the message goes out, the device accepts it, and it lands on
    another layer's ring or on a fader that has none.
    """
    return BY_LAYER[layer].encoder_cc.start + index


def ring_position(index: int, value: float | None, layer: str) -> list[int]:
    """Light an encoder's ring for a normalised value.

    Measured: the value is 0-127, spread evenly over the thirteen LEDs, and
    it is the encoder's own CC -- the same domain an absolute encoder reports
    its position in. The manual's 1-13 is wrong, and wrong in a way that looks
    almost right: 13 of 127 is a tenth of the range, so the whole scale
    collapses into the first LED or two and the ring seems merely
    unresponsive rather than mis-scaled.

    0 is genuinely all off, so value=None (unassigned) stays distinguishable
    from a parameter sitting at its minimum, which is sent as 1.
    """
    cc = ring_cc(index, layer)
    if value is None:
        return [CC, cc, 0]
    span = RING_MAX - RING_MIN
    return [CC, cc, RING_MIN + round(max(0.0, min(1.0, value)) * span)]


def ring_all(index: int, on: bool, layer: str) -> list[int]:
    return [CC, ring_cc(index, layer), RING_MAX if on else 0]


def button_led(index: int, state: LedState) -> list[int]:
    """Light button `index` (0-15). Same note the button itself sends."""
    return [NOTE_ON, RX_BUTTON_LED_NOTE.start + index, state.value]


def select_layer(layer: str) -> list[int]:
    """Program Change to pick the layer -- DOES NOT WORK on this unit.

    The RX MIDI table documents it: "Preset Layer Change, GLOBAL CH, Program
    Change, 0 = Layer A, 1 = Layer B, Standard mode only". Sending exactly that
    on the global channel (verified working for LEDs) leaves the device on
    whatever layer it was on, with the A/B lamp unchanged.

    Kept because it is what the manual says and someone will try it again;
    nothing in the bridge depends on it. The daemon never needs to *set* the
    layer -- it learns which layer is live from the CC and note numbers that
    arrive, which is the only thing that reflects the hardware's real state.
    """
    return [PROGRAM_CHANGE, 0 if layer.upper() == "A" else 1]


def select_mode(mc: bool) -> list[int]:
    return [CC, RX_MODE_CC, 1 if mc else 0]
